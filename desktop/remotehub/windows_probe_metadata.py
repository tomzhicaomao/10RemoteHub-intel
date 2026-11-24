"""Read-only Windows device topology for the shared remote workbench."""
from __future__ import annotations

import ctypes as c
from dataclasses import dataclass
import re
from uuid import UUID

from .probe_device import UnlistedDevice


_U32 = c.c_uint32
_PTR = c.c_void_p
_DEVICE_PROPS = "a45c254e-df1c-4efd-8020-67d146a850e0"
_CONTAINER = "8c7ed206-3f8a-4827-b3ab-ae9e1faefc6c"
_INSTANCE = "78c34fc8-104a-4aca-9ea4-524d52996e57"
_SYSTEM_CONTAINER = UUID("00000000-0000-0000-ffff-ffffffffffff").bytes_le


class _Property(c.Structure):
    _fields_ = [("guid", c.c_ubyte * 16), ("id", _U32)]


def _property_key(guid, pid):
    return _Property((c.c_ubyte * 16).from_buffer_copy(UUID(guid).bytes_le), pid)


def _usb_pair(instance):
    match = re.match(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})([^\\]*)\\", instance, re.I)
    if match and "&MI_" not in match[3].upper():
        return int(match[1], 16), int(match[2], 16)
    return None


_HID_GATT_UUID = "00001812-0000-1000-8000-00805F9B34FB"


@dataclass(frozen=True)
class BlockedBLERemote:
    """A BLE HOGP (0x1812) remote connected to Windows Bluetooth whose HID driver failed."""

    name: str
    vendor: int
    product: int
    problem_code: int = 10
    transport: str = "蓝牙"
    # Bluetooth address from the device instance; used to find this remote when scanning
    # over a dongle, never shown in the UI.
    address: str = ""


@dataclass(frozen=True)
class WindowsRemoteIdentity:
    group: tuple
    name: str
    transport: str
    vendor: int
    product: int
    vendor_source: int | None = None
    serial: str = ""
    revision: int | None = None
    winusb_interfaces: tuple[int, ...] = ()


class WindowsProbeMetadata:
    def __init__(self):
        self.cfg = c.WinDLL("cfgmgr32", use_last_error=True)
        specs = {
            "CM_Get_Device_Interface_PropertyW": (c.c_wchar_p, _PTR, _PTR, _PTR, _PTR, _U32),
            "CM_Get_DevNode_PropertyW": (_U32, _PTR, _PTR, _PTR, _PTR, _U32),
            "CM_Locate_DevNodeW": (_PTR, c.c_wchar_p, _U32),
            "CM_Get_Device_IDW": (_U32, _PTR, _U32, _U32),
            "CM_Get_Parent": (_PTR, _U32, _U32),
            "CM_Get_Child": (_PTR, _U32, _U32),
            "CM_Get_Sibling": (_PTR, _U32, _U32),
            "CM_Get_Device_ID_List_SizeW": (_PTR, c.c_wchar_p, _U32),
            "CM_Get_Device_ID_ListW": (c.c_wchar_p, _PTR, _U32, _U32),
            "CM_Get_DevNode_Status": (_PTR, _PTR, _U32, _U32),
        }
        for name, arguments in specs.items():
            function = getattr(self.cfg, name)
            function.argtypes = arguments
            function.restype = _U32

    @staticmethod
    def _property(function, owner, guid, pid, expected_type):
        key = _property_key(guid, pid)
        kind = _U32()
        buffer = c.create_string_buffer(4096)
        size = _U32(len(buffer))
        result = function(owner, c.byref(key), c.byref(kind), buffer, c.byref(size), 0)
        return buffer.raw[:size.value] if result == 0 and kind.value == expected_type else b""

    def _node_property(self, node, guid, pid, kind):
        return self._property(self.cfg.CM_Get_DevNode_PropertyW, node, guid, pid, kind)

    def _text(self, node, guid, pid):
        return self._node_property(node, guid, pid, 0x12).decode("utf-16-le", errors="replace").rstrip("\0")

    def _node(self, instance):
        node = _U32()
        return node.value if instance and self.cfg.CM_Locate_DevNodeW(c.byref(node), instance, 0) == 0 else None

    def _instance(self, node):
        text = c.create_unicode_buffer(512)
        return text.value if self.cfg.CM_Get_Device_IDW(node, text, len(text), 0) == 0 else ""

    def _relate(self, function, node):
        related = _U32()
        return related.value if function(c.byref(related), node, 0) == 0 else None

    def _container(self, node):
        value = self._node_property(node, _CONTAINER, 2, 0x0d)
        return value.hex() if len(value) == 16 and any(value) and value != _SYSTEM_CONTAINER else ""

    def _name(self, node):
        return (self._text(node, _DEVICE_PROPS, 14) or self._text(node, "540b947e-8b40-45bc-a8a2-6a0b894cbda2", 4)
                or self._text(node, _DEVICE_PROPS, 2))

    def _usb(self, node, instance, pair):
        caps = int.from_bytes(self._node_property(node, _DEVICE_PROPS, 17, 0x07), "little")
        serial = instance.rsplit("\\", 1)[-1].strip() if caps & 0x10 else ""
        if serial and (not serial.strip("0") or "&" in serial):
            serial = ""
        hardware = self._node_property(node, _DEVICE_PROPS, 3, 0x2012).decode("utf-16-le", errors="replace")
        revision = re.search(r"REV_([0-9A-F]{4})\b", hardware, re.I)
        bound = []
        child = self._relate(self.cfg.CM_Get_Child, node)
        for _ in range(64):
            if child is None:
                break
            child_id = self._instance(child)
            if pair == (0x0416, 0x0602):
                interface = re.search(r"&MI_(01|04)\\", child_id, re.I)
                if interface and self._text(child, _DEVICE_PROPS, 6).casefold() == "winusb":
                    bound.append(int(interface[1], 16))
            else:
                interface = re.search(r"&MI_([0-9A-F]{2})\\", child_id, re.I)
                if interface and self._text(child, _DEVICE_PROPS, 6).casefold() == "winusb":
                    compat = self._node_property(child, _DEVICE_PROPS, 4, 0x2012).decode("utf-16-le", errors="replace").upper()
                    # Only treat HID class interfaces as key candidates; never claim mouse boot interfaces.
                    if "CLASS_03" in compat and "SUBCLASS_01&PROT_02" not in compat:
                        bound.append(int(interface[1], 16))
            child = self._relate(self.cfg.CM_Get_Sibling, child)
        group = ("usb", instance.casefold(), self._container(node))
        return WindowsRemoteIdentity(group, self._name(node) or "USB 遥控器", "USB", *pair,
                                     serial=serial, revision=int(revision[1], 16) if revision else None,
                                     winusb_interfaces=tuple(sorted(set(bound))))

    def for_interface(self, path, reported_pair):
        data = self._property(self.cfg.CM_Get_Device_Interface_PropertyW, path, _INSTANCE, 256, 0x12)
        interface = data.decode("utf-16-le", errors="replace").rstrip("\0")
        node = self._node(interface)
        chain = []
        for _ in range(24):
            if node is None:
                break
            instance = self._instance(node)
            chain.append((node, instance))
            node = self._relate(self.cfg.CM_Get_Parent, node)

        bluetooth = []
        for node, instance in chain:
            if instance.upper().startswith(("BTHLE\\", "BTHLEDEVICE\\", "BTHENUM\\")):
                bluetooth.append((node, instance))
            pair = _usb_pair(instance)
            if pair and not bluetooth:
                return self._usb(node, instance, pair)
        if not bluetooth:
            return None

        node, instance = next(((n, i) for n, i in bluetooth if "\\DEV_" in i.upper()), bluetooth[0])
        identifiers = " ".join([interface, path, *(i for _, i in bluetooth)])
        pnp = re.search(r"VID&000([12])([0-9A-F]{4})[_&]PID&([0-9A-F]{4})", identifiers, re.I)
        usb_style = re.search(r"VID_([0-9A-F]{4})[&_]PID_([0-9A-F]{4})", identifiers, re.I)
        source = None
        if pnp:
            source, vendor, product = int(pnp[1]), int(pnp[2], 16), int(pnp[3], 16)
        elif usb_style:
            vendor, product = int(usb_style[1], 16), int(usb_style[2], 16)
        else:
            vendor, product = reported_pair
        container = self._container(node)
        group = ("bluetooth", instance.casefold() if "\\DEV_" in instance.upper() else container or instance.casefold(),
                 vendor, product, source)
        return WindowsRemoteIdentity(group, self._name(node) or "蓝牙遥控器", "蓝牙", vendor, product, source)

    def winusb_receivers(self):
        flags = 0x101  # CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
        size = _U32()
        if self.cfg.CM_Get_Device_ID_List_SizeW(c.byref(size), "USB", flags) or not 0 < size.value <= 262144:
            return []
        names = c.create_unicode_buffer(size.value)
        if self.cfg.CM_Get_Device_ID_ListW("USB", names, size.value, flags):
            return []
        result = []
        for instance in names[:].split("\0"):
            pair = _usb_pair(instance)
            if pair is not None:
                node = self._node(instance)
                if node is not None:
                    identity = self._usb(node, instance, pair)
                    if identity.winusb_interfaces:
                        result.append(identity)
        return result

    def unlisted_bluetooth(self) -> list[UnlistedDevice]:
        """List connected Bluetooth devices without exposing MAC addresses or paths."""
        flags = 0x101  # CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
        seen: dict[tuple[int, int], UnlistedDevice] = {}
        for enumerator in ("BTHLE", "BTHLEDEVICE", "BTHENUM"):
            size = _U32()
            if self.cfg.CM_Get_Device_ID_List_SizeW(c.byref(size), enumerator, flags) or not 0 < size.value <= 262144:
                continue
            names = c.create_unicode_buffer(size.value)
            if self.cfg.CM_Get_Device_ID_ListW(enumerator, names, size.value, flags):
                continue
            for instance in names[:].split("\0"):
                if not instance:
                    continue
                pnp = re.search(r"VID&000([12])([0-9A-F]{4})[_&]PID&([0-9A-F]{4})", instance, re.I)
                usb_style = re.search(r"VID_([0-9A-F]{4})[&_]PID_([0-9A-F]{4})", instance, re.I)
                if pnp:
                    vendor, product = int(pnp[2], 16), int(pnp[3], 16)
                    key = (vendor, product)
                elif usb_style:
                    vendor, product = int(usb_style[1], 16), int(usb_style[2], 16)
                    key = (vendor, product)
                elif "\\DEV_" in instance.upper():
                    # GATT-only paired BLE device without a Windows HID/PnP VID:PID node.
                    vendor, product = 0, 0
                    node = self._node(instance)
                    name = (self._name(node) if node is not None else "") or "BLE 蓝牙设备"
                    key = ("name", name.casefold())
                    if key not in seen:
                        seen[key] = UnlistedDevice(name, "蓝牙", 0, 0, hid=False)
                    continue
                else:
                    continue
                if not (vendor or product) or key in seen:
                    continue
                node = self._node(instance)
                name = (self._name(node) if node is not None else "") or "蓝牙设备"
                seen[key] = UnlistedDevice(name, "蓝牙", vendor, product, hid=False)
        return sorted(seen.values(), key=lambda d: (d.vendor, d.product, d.name))

    def blocked_ble_remotes(self) -> list[BlockedBLERemote]:
        """Return present BLE HID (0x1812) remotes rejected by Windows' HID driver (e.g. Code 10).

        Strictly filters to BTHLEDEVICE nodes exposing GATT 0x1812 whose PnP
        problem code is non-zero, so Bluetooth audio/phones/working keyboards
        are never included.
        """
        flags = 0x101  # CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
        size = _U32()
        if self.cfg.CM_Get_Device_ID_List_SizeW(c.byref(size), "BTHLEDEVICE", flags) or not 0 < size.value <= 262144:
            return []
        names = c.create_unicode_buffer(size.value)
        if self.cfg.CM_Get_Device_ID_ListW("BTHLEDEVICE", names, size.value, flags):
            return []
        found: dict[tuple[int, int, str], BlockedBLERemote] = {}
        for instance in names[:].split("\0"):
            if not instance or _HID_GATT_UUID not in instance.upper():
                continue
            node = self._node(instance)
            if node is None:
                continue
            status, problem = _U32(), _U32()
            if self.cfg.CM_Get_DevNode_Status(c.byref(status), c.byref(problem), node, 0) != 0:
                continue
            if problem.value == 0:
                continue
            pnp = re.search(r"VID&0{0,3}([12])([0-9A-F]{4})[_&]PID&([0-9A-F]{4})", instance, re.I)
            usb_style = re.search(r"VID_([0-9A-F]{4})[&_]PID_([0-9A-F]{4})", instance, re.I)
            if pnp:
                vendor, product = int(pnp[2], 16), int(pnp[3], 16)
            elif usb_style:
                vendor, product = int(usb_style[1], 16), int(usb_style[2], 16)
            else:
                vendor, product = 0, 0
            parent = self._relate(self.cfg.CM_Get_Parent, node)
            raw_name = (self._name(parent) if parent is not None else "") or self._name(node) or "BLE 蓝牙遥控器"
            # Avoid exposing raw Bluetooth MAC addresses in display names.
            if re.fullmatch(r"[0-9A-Fa-f:_-]{12,}", raw_name.strip()):
                raw_name = "BLE 蓝牙遥控器"
            key = (vendor, product, raw_name.casefold())
            if key not in found:
                mac = re.search(r"_([0-9A-F]{12})\\", instance, re.I)
                found[key] = BlockedBLERemote(
                    name=raw_name.strip(),
                    vendor=vendor,
                    product=product,
                    problem_code=int(problem.value),
                    address=":".join(mac[1][i:i + 2] for i in range(0, 12, 2)).upper() if mac else "",
                )
        return sorted(found.values(), key=lambda d: (d.vendor, d.product, d.name))
