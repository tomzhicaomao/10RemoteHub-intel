"""One-click Windows inbox WinUSB driver switch & restore (zero Zadig, zero Device Manager).

Binds external USB Bluetooth HCI dongles, or the interfaces of a USB remote
receiver that 10RemoteHub reads itself (keyboard, consumer and vendor HID data;
never pointer, audio or storage interfaces), to Microsoft's
built-in signed ``%SystemRoot%\\INF\\winusb.inf`` via SetupAPI /
``DiInstallDevice``, writes a deterministic ``DeviceInterfaceGUIDs`` entry so
``libusb`` can open the device immediately, and restarts the devnode.

Restore removes the managed registry values, uninstalls any legacy third-party
Zadig OEM INF belonging to the device, removes the devnode and triggers
``pnputil /scan-devices`` so Windows automatically restores the default
``BTHUSB`` / ``HidUsb`` driver without unplugging.

Built-in motherboard Bluetooth adapters are strictly excluded and refused.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable
import uuid

_INTEGRATED_BT_VIDS = frozenset({0x8087, 0x0489, 0x0E8D, 0x13D3, 0x04CA})
_GUID_NAMESPACE = uuid.UUID("7f3b5f4c-9b14-4d6f-9c2c-1052454d4f54")
_MANAGED_REG_VALUE = "10RemoteHubManaged"
_WINUSB_HARDWARE_ID = "usb\\ms_comp_winusb"
_RESTORABLE_SERVICES = frozenset({"winusb", "libusbk", "libusb0"})
_SAFE_OEM_DELETE_CLASSES = frozenset({"usbdevice", "libusbk devices", "libusb-win32 devices"})
_OEM_INF_RE = re.compile(r"^oem\d+\.inf$", re.IGNORECASE)
_MI_RE = re.compile(r"&MI_([0-9A-F]{2})", re.IGNORECASE)
_USB_VID_PID_RE = re.compile(r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&[^\\]*)?\\", re.IGNORECASE)

ERROR_CANCELLED = 1223
KEYBOARD_CLASS_GUID = "{4d36e96b-e325-11ce-bfc1-08002be10318}"
CONFIRM_SECONDS = 20          # the elevated helper restores by itself when nobody confirms
_USB_CLASS_RE = re.compile(r"USB\\CLASS_([0-9A-F]{2})(?:&SUBCLASS_([0-9A-F]{2}))?(?:&PROT_([0-9A-F]{2}))?", re.IGNORECASE)
_HID_USAGE_RE = re.compile(r"HID_DEVICE_UP:([0-9A-F]{4})_U:([0-9A-F]{4})", re.IGNORECASE)
_POINTER_USAGES = frozenset({"0001:0001", "0001:0002"})


@dataclass(frozen=True)
class ManagedUSBDevice:
    """One present USB devnode eligible for one-click WinUSB bind/restore."""

    instance_id: str
    vendor: int
    product: int
    kind: str  # "ble_dongle" | "receiver_0416" | "receiver"
    service: str
    name: str
    driver_inf: str = ""
    device_class: str = ""
    interface: int | None = None

    @property
    def bound_winusb(self) -> bool:
        return self.service.casefold() == "winusb"

    @property
    def restorable(self) -> bool:
        return self.service.casefold() in _RESTORABLE_SERVICES

    @property
    def id_label(self) -> str:
        return f"{self.vendor:04X}:{self.product:04X}"


class DriverActionCancelled(RuntimeError):
    """Raised when the user clicks Cancel on the Windows UAC elevation prompt."""


def device_interface_guid(instance_id: str) -> str:
    """Deterministic WinUSB DeviceInterfaceGUIDs value for a USB instance ID."""
    return "{" + str(uuid.uuid5(_GUID_NAMESPACE, instance_id.strip().upper())).upper() + "}"


def _parse_interface_number(instance_id: str) -> int | None:
    match = _MI_RE.search(instance_id or "")
    return int(match.group(1), 16) if match else None


def _parse_usb_vid_pid(instance_id: str) -> tuple[int, int] | None:
    """Parse ``(vendor, product)`` from any USB devnode, including composite ``&MI_XX`` interfaces."""
    match = _USB_VID_PID_RE.match(instance_id or "")
    return (int(match.group(1), 16), int(match.group(2), 16)) if match else None


def _list_present_usb_nodes_windows() -> list[dict]:
    """Read present Windows USB devnodes via cfgmgr32 (read-only, no elevation)."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes as c
        from .windows_probe_metadata import WindowsProbeMetadata

        meta = WindowsProbeMetadata()
        flags = 0x101  # CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
        size = c.c_uint32()
        if meta.cfg.CM_Get_Device_ID_List_SizeW(c.byref(size), "USB", flags) or not 0 < size.value <= 262144:
            return []
        buf = c.create_unicode_buffer(size.value)
        if meta.cfg.CM_Get_Device_ID_ListW("USB", buf, size.value, flags):
            return []
        rows: list[dict] = []
        for inst in buf[:].split("\0"):
            if not inst:
                continue
            pair = _parse_usb_vid_pid(inst)
            if pair is None:
                continue
            node = meta._node(inst)
            if node is None:
                continue
            service = meta._text(node, "a45c254e-df1c-4efd-8020-67d146a850e0", 6).strip()
            device_class = meta._text(node, "a45c254e-df1c-4efd-8020-67d146a850e0", 9).strip()
            driver_inf = meta._text(node, "a8b865dd-2e3d-4094-ad97-e593a70c75d6", 5).strip()
            compat = meta._node_property(node, "a45c254e-df1c-4efd-8020-67d146a850e0", 4, 0x2012).decode(
                "utf-16-le", errors="replace"
            ).upper()
            name = meta._name(node) or f"USB {pair[0]:04X}:{pair[1]:04X}"
            usages = []
            child = meta._relate(meta.cfg.CM_Get_Child, node)
            for _ in range(32):
                if child is None:
                    break
                child_hw = meta._node_property(child, "a45c254e-df1c-4efd-8020-67d146a850e0", 3, 0x2012).decode(
                    "utf-16-le", errors="replace"
                )
                child_compat = meta._node_property(child, "a45c254e-df1c-4efd-8020-67d146a850e0", 4, 0x2012).decode(
                    "utf-16-le", errors="replace"
                )
                usages.extend(hid_usages_from_ids(child_hw + "\0" + child_compat))
                child = meta._relate(meta.cfg.CM_Get_Sibling, child)
            rows.append(
                {
                    "instance_id": inst,
                    "vendor": pair[0],
                    "product": pair[1],
                    "service": service,
                    "device_class": device_class,
                    "driver_inf": driver_inf,
                    "compat": compat,
                    "name": name,
                    "hid_usages": sorted(set(usages)),
                }
            )
        return rows
    except Exception:
        return []


def hid_usages_from_ids(ids: str) -> list[str]:
    """``PAGE:USAGE`` of HID top-level collections named in Windows hardware/compatible IDs."""
    text = ids.upper()
    usages = [f"{page}:{usage}" for page, usage in _HID_USAGE_RE.findall(text)]
    if "HID_DEVICE_SYSTEM_MOUSE" in text:
        usages.append("0001:0002")
    if "HID_DEVICE_SYSTEM_KEYBOARD" in text:
        usages.append("0001:0006")
    return sorted(set(usages))


def takeover_interface(row: dict) -> bool:
    """True when 10RemoteHub should read this USB interface itself.

    Taken: HID interfaces carrying keyboard, consumer or vendor data, and vendor-class
    interfaces (private voice channels). Left with Windows: anything with a pointer
    collection (the cursor keeps moving without the app), boot mice, standard audio
    (a USB microphone stays usable by recognition software), storage and the rest.
    """
    compat = str(row.get("compat", "")).upper()
    match = _USB_CLASS_RE.search(compat)
    if match is None:
        return False
    usb_class, subclass, protocol = match.group(1), match.group(2) or "", match.group(3) or ""
    if usb_class == "FF":
        return True
    if usb_class != "03":
        return False
    if subclass == "01" and protocol == "02":
        return False
    usages = {str(item).upper() for item in row.get("hid_usages", ()) or ()}
    return not (usages & _POINTER_USAGES)


def classify_managed_devices(
    nodes: Iterable[dict],
    *,
    platform: str | None = None,
) -> tuple[list[ManagedUSBDevice], list[ManagedUSBDevice]]:
    """Return ``(ble_dongles, receiver_0416_interfaces)`` safe for one-click switch/restore.

    Safety invariants:
    - Integrated motherboard Bluetooth VIDs are NEVER returned.
    - When only 1 ``BTHUSB`` adapter exists, it is treated as built-in Bluetooth
      and is NEVER returned for switching.
    - For ``0416:0602``, ONLY ``MI_03`` (voice) and ``MI_04`` (keys) are returned.
    """
    plat = sys.platform if platform is None else platform
    if plat != "win32":
        return [], []
    all_nodes = list(nodes)
    bt_raw: list[dict] = []
    receivers: list[ManagedUSBDevice] = []
    for row in all_nodes:
        inst = str(row.get("instance_id", "")).strip()
        if not inst.upper().startswith("USB\\VID_"):
            continue
        vid = int(row.get("vendor", 0))
        pid = int(row.get("product", 0))
        svc = str(row.get("service", "")).strip()
        cls = str(row.get("device_class", "")).strip()
        inf = str(row.get("driver_inf", "")).strip()
        compat = str(row.get("compat", "")).upper()
        name = str(row.get("name", "")).strip() or f"USB {vid:04X}:{pid:04X}"
        mi = _parse_interface_number(inst)

        if (vid, pid) == (0x0416, 0x0602) and mi in (3, 4):
            receivers.append(
                ManagedUSBDevice(
                    instance_id=inst,
                    vendor=vid,
                    product=pid,
                    kind="receiver_0416",
                    service=svc,
                    name=name,
                    driver_inf=inf,
                    device_class=cls,
                    interface=mi,
                )
            )
            continue

        # External USB Bluetooth HCI dongles are single-function (or E0/01/01) USB BT nodes.
        is_bt = (
            svc.casefold() == "bthusb"
            or "CLASS_E0&SUBCLASS_01&PROT_01" in compat
            or ("BLUETOOTH" in name.upper() and svc.casefold() in _RESTORABLE_SERVICES)
        )
        if is_bt:
            if mi is None:
                bt_raw.append(row)
            continue
        if (vid, pid) == (0x0416, 0x0602):
            continue   # its proven interfaces are MI_03 / MI_04 only
        if svc.casefold() == "usbccgp" or vid in _INTEGRATED_BT_VIDS:
            continue   # composite parent: its interfaces are listed on their own
        if takeover_interface(row):   # USB class IDs survive the switch, so bound interfaces stay listed
            receivers.append(
                ManagedUSBDevice(
                    instance_id=inst,
                    vendor=vid,
                    product=pid,
                    kind="receiver",
                    service=svc,
                    name=name,
                    driver_inf=inf,
                    device_class=cls,
                    interface=mi,
                )
            )

    bthusb_count = sum(1 for row in bt_raw if str(row.get("service", "")).strip().casefold() == "bthusb")
    dongles: list[ManagedUSBDevice] = []
    for row in bt_raw:
        vid = int(row.get("vendor", 0))
        pid = int(row.get("product", 0))
        svc = str(row.get("service", "")).strip()
        if vid in _INTEGRATED_BT_VIDS:
            continue
        if svc.casefold() == "bthusb" and bthusb_count <= 1:
            continue
        dongles.append(
            ManagedUSBDevice(
                instance_id=str(row.get("instance_id", "")).strip(),
                vendor=vid,
                product=pid,
                kind="ble_dongle",
                service=svc,
                name=str(row.get("name", "")).strip() or f"USB {vid:04X}:{pid:04X}",
                driver_inf=str(row.get("driver_inf", "")).strip(),
                device_class=str(row.get("device_class", "")).strip(),
            )
        )
    receivers.sort(key=lambda item: (item.vendor, item.product, item.interface or 0, item.instance_id))
    dongles.sort(key=lambda item: (item.bound_winusb, item.vendor, item.product, item.instance_id))
    return dongles, receivers


def list_managed_devices(
    *,
    nodes_provider: Callable[[], Iterable[dict]] | None = None,
    platform: str | None = None,
) -> tuple[list[ManagedUSBDevice], list[ManagedUSBDevice]]:
    nodes = nodes_provider() if nodes_provider is not None else _list_present_usb_nodes_windows()
    return classify_managed_devices(nodes, platform=platform)


def receiver_interfaces(receivers: Iterable[ManagedUSBDevice], vendor: int, product: int) -> list[ManagedUSBDevice]:
    """The interfaces of one USB receiver type that a one-click takeover binds or restores."""
    return [item for item in receivers if (item.vendor, item.product) == (vendor, product)]


def _list_present_keyboards_windows() -> list[str]:
    """Instance IDs of present keyboard-class devnodes (read-only)."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes as c
        from .windows_probe_metadata import WindowsProbeMetadata

        meta = WindowsProbeMetadata()
        flags = 0x300  # CM_GETIDLIST_FILTER_CLASS | CM_GETIDLIST_FILTER_PRESENT
        size = c.c_uint32()
        if meta.cfg.CM_Get_Device_ID_List_SizeW(c.byref(size), KEYBOARD_CLASS_GUID, flags) or not 0 < size.value <= 262144:
            return []
        buf = c.create_unicode_buffer(size.value)
        if meta.cfg.CM_Get_Device_ID_ListW(KEYBOARD_CLASS_GUID, buf, size.value, flags):
            return []
        return [item for item in buf[:].split("\0") if item]
    except Exception:
        return []


def check_keyboard_remains(targets: Iterable[ManagedUSBDevice], keyboards: Iterable[str]) -> None:
    """Refuse a takeover that would leave the computer without any other keyboard."""
    pairs = {(d.vendor, d.product) for d in targets if d.kind not in ("ble_dongle", "receiver_0416")}
    if not pairs:
        return
    own = [f"VID_{v:04X}&PID_{p:04X}" for v, p in pairs]
    others = [k for k in keyboards if not any(tag in k.upper() for tag in own)]
    if not others:
        raise ValueError("这只接收器是电脑上唯一的键盘，接管后将没有键盘可用，已拒绝操作。请先接上其他键盘再试。")


def validate_targets(
    instance_ids: Iterable[str],
    action: str,
    *,
    nodes_provider: Callable[[], Iterable[dict]] | None = None,
    platform: str | None = None,
) -> list[ManagedUSBDevice]:
    """Validate requested instance IDs against live allowlisted managed devices."""
    if action not in ("bind", "restore"):
        raise ValueError(f"不支持的操作类型：{action}")
    requested = [str(item).strip() for item in instance_ids if str(item).strip()]
    if not requested:
        raise ValueError("未指定要操作的 USB 设备。")
    dongles, receivers = list_managed_devices(nodes_provider=nodes_provider, platform=platform)
    by_id = {item.instance_id.upper(): item for item in (*dongles, *receivers)}
    matched: list[ManagedUSBDevice] = []
    for inst in requested:
        device = by_id.get(inst.upper())
        if device is None:
            raise ValueError(
                f"拒绝操作不受支持或受保护的设备（{inst}）。为保护系统安全，本程序绝不修改电脑自带蓝牙或普通键鼠。"
            )
        if action == "restore" and not device.restorable:
            raise ValueError(f"设备「{device.name}」（{device.id_label}）当前已是普通系统驱动，无需恢复。")
        matched.append(device)
    if len({(d.vendor, d.product) for d in matched if d.kind != "ble_dongle"}) > 1:
        raise ValueError("一次只能接管一种 USB 接收器。")
    return matched


def _is_windows_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_pnputil(*args: str, timeout: float = 30.0) -> tuple[int, str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        ["pnputil", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creationflags,
    )
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode, output


def _bind_one_winusb_windows(device: ManagedUSBDevice) -> bool:
    """Bind one validated USB devnode to Microsoft inbox ``winusb.inf`` and return ``need_reboot``."""
    import ctypes as c
    from ctypes import wintypes as w

    pack = 8 if c.sizeof(c.c_void_p) == 8 else 1

    class GUID(c.Structure):
        _fields_ = [("Data1", c.c_uint32), ("Data2", c.c_uint16), ("Data3", c.c_uint16), ("Data4", c.c_ubyte * 8)]

    class SP_DEVINFO_DATA(c.Structure):
        _pack_ = pack
        _fields_ = [("cbSize", w.DWORD), ("ClassGuid", GUID), ("DevInst", w.DWORD), ("Reserved", c.c_size_t)]

    class SP_DEVINSTALL_PARAMS_W(c.Structure):
        _pack_ = pack
        _fields_ = [
            ("cbSize", w.DWORD),
            ("Flags", w.DWORD),
            ("FlagsEx", w.DWORD),
            ("hwndParent", w.HWND),
            ("InstallMsgHandler", c.c_void_p),
            ("InstallMsgHandlerContext", c.c_void_p),
            ("FileQueue", c.c_void_p),
            ("ClassInstallReserved", c.c_size_t),
            ("Reserved", w.DWORD),
            ("DriverPath", w.WCHAR * 260),
        ]

    class SP_DRVINFO_DATA_W(c.Structure):
        _pack_ = pack
        _fields_ = [
            ("cbSize", w.DWORD),
            ("DriverType", w.DWORD),
            ("Reserved", c.c_size_t),
            ("Description", w.WCHAR * 256),
            ("MfgName", w.WCHAR * 256),
            ("ProviderName", w.WCHAR * 256),
            ("DriverDate", w.FILETIME),
            ("DriverVersion", c.c_uint64),
        ]

    class SP_DRVINFO_DETAIL_DATA_W(c.Structure):
        _pack_ = pack
        _fields_ = [
            ("cbSize", w.DWORD),
            ("InfDate", w.FILETIME),
            ("CompatIDsOffset", w.DWORD),
            ("CompatIDsLength", w.DWORD),
            ("Reserved", c.c_size_t),
            ("SectionName", w.WCHAR * 256),
            ("InfFileName", w.WCHAR * 260),
            ("DrvDescription", w.WCHAR * 256),
            ("HardwareID", w.WCHAR * 1),
        ]

    setupapi = c.WinDLL("setupapi.dll", use_last_error=True)
    newdev = c.WinDLL("newdev.dll", use_last_error=True)
    advapi32 = c.WinDLL("advapi32.dll", use_last_error=True)

    setupapi.SetupDiCreateDeviceInfoList.argtypes = [c.POINTER(GUID), w.HWND]
    setupapi.SetupDiCreateDeviceInfoList.restype = c.c_void_p
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [c.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = w.BOOL
    setupapi.SetupDiOpenDeviceInfoW.argtypes = [c.c_void_p, w.LPCWSTR, w.HWND, w.DWORD, c.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiOpenDeviceInfoW.restype = w.BOOL
    setupapi.SetupDiGetDeviceInstallParamsW.argtypes = [c.c_void_p, c.POINTER(SP_DEVINFO_DATA), c.POINTER(SP_DEVINSTALL_PARAMS_W)]
    setupapi.SetupDiGetDeviceInstallParamsW.restype = w.BOOL
    setupapi.SetupDiSetDeviceInstallParamsW.argtypes = [c.c_void_p, c.POINTER(SP_DEVINFO_DATA), c.POINTER(SP_DEVINSTALL_PARAMS_W)]
    setupapi.SetupDiSetDeviceInstallParamsW.restype = w.BOOL
    setupapi.SetupDiBuildDriverInfoList.argtypes = [c.c_void_p, c.POINTER(SP_DEVINFO_DATA), w.DWORD]
    setupapi.SetupDiBuildDriverInfoList.restype = w.BOOL
    setupapi.SetupDiEnumDriverInfoW.argtypes = [c.c_void_p, c.POINTER(SP_DEVINFO_DATA), w.DWORD, w.DWORD, c.POINTER(SP_DRVINFO_DATA_W)]
    setupapi.SetupDiEnumDriverInfoW.restype = w.BOOL
    setupapi.SetupDiSetSelectedDriverW.argtypes = [c.c_void_p, c.POINTER(SP_DEVINFO_DATA), c.POINTER(SP_DRVINFO_DATA_W)]
    setupapi.SetupDiSetSelectedDriverW.restype = w.BOOL
    setupapi.SetupDiGetDriverInfoDetailW.argtypes = [
        c.c_void_p, c.POINTER(SP_DEVINFO_DATA), c.POINTER(SP_DRVINFO_DATA_W), c.c_void_p, w.DWORD, c.POINTER(w.DWORD)
    ]
    setupapi.SetupDiGetDriverInfoDetailW.restype = w.BOOL
    setupapi.SetupDiOpenDevRegKey.argtypes = [c.c_void_p, c.POINTER(SP_DEVINFO_DATA), w.DWORD, w.DWORD, w.DWORD, w.DWORD]
    setupapi.SetupDiOpenDevRegKey.restype = w.HKEY

    newdev.DiInstallDevice.argtypes = [
        w.HWND, c.c_void_p, c.POINTER(SP_DEVINFO_DATA), c.POINTER(SP_DRVINFO_DATA_W), w.DWORD, c.POINTER(w.BOOL)
    ]
    newdev.DiInstallDevice.restype = w.BOOL

    advapi32.RegSetValueExW.argtypes = [w.HKEY, w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD]
    advapi32.RegSetValueExW.restype = w.LONG
    advapi32.RegCloseKey.argtypes = [w.HKEY]
    advapi32.RegCloseKey.restype = w.LONG

    INVALID_HANDLE_VALUE = c.c_void_p(-1).value
    DIOD_INHERIT_CLASSDRVS = 0x00000002
    DI_ENUMSINGLEINF = 0x00010000
    DI_FLAGSEX_ALLOWEXCLUDEDDRVS = 0x00000800
    SPDIT_CLASSDRIVER = 1
    DICS_FLAG_GLOBAL = 1
    DIREG_DEV = 1
    KEY_SET_VALUE = 0x0002
    REG_DWORD = 4
    REG_MULTI_SZ = 7

    winusb_inf = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "INF", "winusb.inf")
    if not os.path.isfile(winusb_inf):
        raise RuntimeError(f"系统缺少内置 WinUSB 驱动文件：{winusb_inf}")

    h = setupapi.SetupDiCreateDeviceInfoList(None, None)
    if not h or h == INVALID_HANDLE_VALUE:
        raise RuntimeError(f"无法创建设备信息集（错误码 {c.get_last_error()}）")

    need_reboot = w.BOOL(0)
    try:
        # Build global class driver list from inbox winusb.inf FIRST so the opened device
        # inherits the USBDevice class list via DIOD_INHERIT_CLASSDRVS even when currently Bluetooth/HID.
        params = SP_DEVINSTALL_PARAMS_W()
        params.cbSize = c.sizeof(params)
        if not setupapi.SetupDiGetDeviceInstallParamsW(h, None, c.byref(params)):
            raise RuntimeError(f"无法读取驱动安装参数（错误码 {c.get_last_error()}）")
        params.Flags |= DI_ENUMSINGLEINF
        params.FlagsEx |= DI_FLAGSEX_ALLOWEXCLUDEDDRVS
        params.DriverPath = winusb_inf
        if not setupapi.SetupDiSetDeviceInstallParamsW(h, None, c.byref(params)):
            raise RuntimeError(f"无法设置 WinUSB 驱动路径（错误码 {c.get_last_error()}）")
        if not setupapi.SetupDiBuildDriverInfoList(h, None, SPDIT_CLASSDRIVER):
            raise RuntimeError(f"无法读取系统内置 WinUSB 驱动列表（错误码 {c.get_last_error()}）")

        dev = SP_DEVINFO_DATA()
        dev.cbSize = c.sizeof(dev)
        if not setupapi.SetupDiOpenDeviceInfoW(h, device.instance_id, None, DIOD_INHERIT_CLASSDRVS, c.byref(dev)):
            raise RuntimeError(f"无法打开设备节点（错误码 {c.get_last_error()}）")

        chosen: SP_DRVINFO_DATA_W | None = None
        idx = 0
        while True:
            drv = SP_DRVINFO_DATA_W()
            drv.cbSize = c.sizeof(drv)
            if not setupapi.SetupDiEnumDriverInfoW(h, c.byref(dev), SPDIT_CLASSDRIVER, idx, c.byref(drv)):
                break
            req = w.DWORD(0)
            setupapi.SetupDiGetDriverInfoDetailW(h, c.byref(dev), c.byref(drv), None, 0, c.byref(req))
            buf = c.create_string_buffer(max(req.value, c.sizeof(SP_DRVINFO_DETAIL_DATA_W)))
            detail = SP_DRVINFO_DETAIL_DATA_W.from_buffer(buf)
            detail.cbSize = c.sizeof(SP_DRVINFO_DETAIL_DATA_W)
            if setupapi.SetupDiGetDriverInfoDetailW(h, c.byref(dev), c.byref(drv), buf, len(buf), c.byref(req)):
                off = SP_DRVINFO_DETAIL_DATA_W.HardwareID.offset
                hwid = buf.raw[off : req.value].decode("utf-16-le", errors="replace").split("\0")[0].strip().lower()
                if hwid == _WINUSB_HARDWARE_ID or detail.SectionName.strip().upper() == "WINUSB":
                    chosen = drv
                    break
            idx += 1

        if chosen is None:
            raise RuntimeError("系统内置 winusb.inf 中未找到通用 WinUSB 驱动项。")

        if not setupapi.SetupDiSetSelectedDriverW(h, c.byref(dev), c.byref(chosen)):
            raise RuntimeError(f"选择系统内置 WinUSB 驱动失败（错误码 {c.get_last_error()}）")

        if not newdev.DiInstallDevice(None, h, c.byref(dev), c.byref(chosen), 0, c.byref(need_reboot)):
            err = c.get_last_error()
            raise RuntimeError(f"安装系统内置 WinUSB 驱动失败（错误码 {err}）")

        # Write DeviceInterfaceGUIDs so libusb can open the WinUSB device immediately.
        hkey = setupapi.SetupDiOpenDevRegKey(h, c.byref(dev), DICS_FLAG_GLOBAL, 0, DIREG_DEV, KEY_SET_VALUE)
        if not hkey or hkey == INVALID_HANDLE_VALUE:
            raise RuntimeError(f"驱动已切换，但无法写入设备接口标识（错误码 {c.get_last_error()}）")
        try:
            guid_bytes = (device_interface_guid(device.instance_id) + "\0\0").encode("utf-16-le")
            guid_buf = c.create_string_buffer(guid_bytes, len(guid_bytes))
            rc = advapi32.RegSetValueExW(hkey, "DeviceInterfaceGUIDs", 0, REG_MULTI_SZ, guid_buf, len(guid_bytes))
            if rc != 0:
                raise RuntimeError(f"写入 DeviceInterfaceGUIDs 失败（错误码 {rc}）")
            flag = w.DWORD(1)
            advapi32.RegSetValueExW(hkey, _MANAGED_REG_VALUE, 0, REG_DWORD, c.byref(flag), c.sizeof(flag))
        finally:
            advapi32.RegCloseKey(hkey)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(h)

    # Restart device node so winusb.sys publishes DeviceInterfaceGUIDs without unplugging.
    _run_pnputil("/restart-device", device.instance_id)
    return bool(need_reboot.value)


def _clear_managed_reg_values_windows(instance_id: str) -> None:
    """Remove DeviceInterfaceGUIDs & marker written by 10RemoteHub if present."""
    try:
        import ctypes as c
        from ctypes import wintypes as w

        pack = 8 if c.sizeof(c.c_void_p) == 8 else 1

        class GUID(c.Structure):
            _fields_ = [("Data1", c.c_uint32), ("Data2", c.c_uint16), ("Data3", c.c_uint16), ("Data4", c.c_ubyte * 8)]

        class SP_DEVINFO_DATA(c.Structure):
            _pack_ = pack
            _fields_ = [("cbSize", w.DWORD), ("ClassGuid", GUID), ("DevInst", w.DWORD), ("Reserved", c.c_size_t)]

        setupapi = c.WinDLL("setupapi.dll", use_last_error=True)
        advapi32 = c.WinDLL("advapi32.dll", use_last_error=True)
        setupapi.SetupDiCreateDeviceInfoList.argtypes = [c.POINTER(GUID), w.HWND]
        setupapi.SetupDiCreateDeviceInfoList.restype = c.c_void_p
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [c.c_void_p]
        setupapi.SetupDiDestroyDeviceInfoList.restype = w.BOOL
        setupapi.SetupDiOpenDeviceInfoW.argtypes = [c.c_void_p, w.LPCWSTR, w.HWND, w.DWORD, c.POINTER(SP_DEVINFO_DATA)]
        setupapi.SetupDiOpenDeviceInfoW.restype = w.BOOL
        setupapi.SetupDiOpenDevRegKey.argtypes = [c.c_void_p, c.POINTER(SP_DEVINFO_DATA), w.DWORD, w.DWORD, w.DWORD, w.DWORD]
        setupapi.SetupDiOpenDevRegKey.restype = w.HKEY
        advapi32.RegDeleteValueW.argtypes = [w.HKEY, w.LPCWSTR]
        advapi32.RegDeleteValueW.restype = w.LONG
        advapi32.RegCloseKey.argtypes = [w.HKEY]
        advapi32.RegCloseKey.restype = w.LONG

        INVALID_HANDLE_VALUE = c.c_void_p(-1).value
        h = setupapi.SetupDiCreateDeviceInfoList(None, None)
        if not h or h == INVALID_HANDLE_VALUE:
            return
        try:
            dev = SP_DEVINFO_DATA()
            dev.cbSize = c.sizeof(dev)
            if not setupapi.SetupDiOpenDeviceInfoW(h, instance_id, None, 0, c.byref(dev)):
                return
            hkey = setupapi.SetupDiOpenDevRegKey(h, c.byref(dev), 1, 0, 1, 0x0002)
            if not hkey or hkey == INVALID_HANDLE_VALUE:
                return
            try:
                advapi32.RegDeleteValueW(hkey, "DeviceInterfaceGUIDs")
                advapi32.RegDeleteValueW(hkey, _MANAGED_REG_VALUE)
            finally:
                advapi32.RegCloseKey(hkey)
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(h)
    except Exception:
        return


def _should_delete_legacy_oem_inf(device: ManagedUSBDevice) -> bool:
    """True only when a device is bound to a legacy Zadig/libwdi oemXX.inf (never vendor BT INF)."""
    inf_name = Path(device.driver_inf).name if device.driver_inf else ""
    return bool(
        _OEM_INF_RE.fullmatch(inf_name)
        and device.device_class.strip().casefold() in _SAFE_OEM_DELETE_CLASSES
        and device.service.casefold() in _RESTORABLE_SERVICES
    )


def execute_driver_action(
    action: str,
    instance_ids: Iterable[str],
    *,
    nodes_provider: Callable[[], Iterable[dict]] | None = None,
    bind_fn: Callable[[ManagedUSBDevice], bool] | None = None,
    pnputil_fn: Callable[..., tuple[int, str]] | None = None,
    clear_reg_fn: Callable[[str], None] | None = None,
    keyboards_provider: Callable[[], Iterable[str]] | None = None,
    confirm_file: Path | None = None,
    confirm_seconds: float = CONFIRM_SECONDS,
    on_stage: Callable[[dict], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    require_admin: bool = True,
    platform: str | None = None,
) -> dict:
    """Execute validated ``bind`` or ``restore`` (must run elevated on Windows).

    With ``confirm_file``, a receiver takeover waits for the app to write ``keep`` or
    ``revert`` there and restores the system driver by itself when nothing arrives in
    ``confirm_seconds``: if the receiver was the user's own keyboard and mouse, nobody can
    click a second elevation prompt.
    """
    plat = sys.platform if platform is None else platform
    if plat != "win32":
        return {"ok": False, "action": action, "error": "仅 Windows 支持一键切换 USB 驱动。"}
    if require_admin and not _is_windows_admin():
        return {"ok": False, "action": action, "error": "需要 Windows 管理员权限才能切换设备驱动。"}

    try:
        targets = validate_targets(instance_ids, action, nodes_provider=nodes_provider, platform=plat)
        if action == "bind":
            keyboards = keyboards_provider() if keyboards_provider is not None else _list_present_keyboards_windows()
            check_keyboard_remains(targets, keyboards)
    except ValueError as exc:
        return {"ok": False, "action": action, "error": str(exc)}

    do_bind = bind_fn or _bind_one_winusb_windows
    do_pnputil = pnputil_fn or _run_pnputil
    do_clear_reg = clear_reg_fn or _clear_managed_reg_values_windows
    live = bind_fn is None and pnputil_fn is None

    if action == "bind":
        need_reboot = False
        bound: list[ManagedUSBDevice] = []
        for dev in targets:
            try:
                if do_bind(dev):
                    need_reboot = True
                bound.append(dev)
            except Exception as exc:
                if bound:
                    _restore_devices(bound, do_pnputil, do_clear_reg, live=live)
                return {
                    "ok": False,
                    "action": action,
                    "error": f"切换「{dev.name}」（{dev.id_label}）失败：{exc}",
                }
        receiver = any(d.kind != "ble_dongle" for d in targets)
        if confirm_file is not None and receiver:
            if on_stage is not None:
                on_stage({"stage": "awaiting_confirm", "seconds": confirm_seconds,
                          "instances": [d.instance_id for d in targets]})
            answer = _wait_for_answer(Path(confirm_file), confirm_seconds, sleep_fn)
            if answer != "keep":
                _restore_devices(targets, do_pnputil, do_clear_reg, live=live)
                return {
                    "ok": False,
                    "action": action,
                    "rolled_back": True,
                    "error": ("已按你的选择恢复系统驱动。" if answer == "revert"
                              else "没有在规定时间内确认，已自动恢复系统驱动。"),
                }
        return {
            "ok": True,
            "action": action,
            "need_reboot": need_reboot,
            "instances": [d.instance_id for d in targets],
            "message": (
                "已启用专属接收器模式！请把电视盒遥控器按进配对模式（灯快闪）后点「重新检测」。"
                if not receiver
                else "已由 App 接管这只接收器。重新插拔一次接收器后点「重新检测」即可学习按键。"
            ),
        }

    # action == "restore"
    try:
        _restore_devices(targets, do_pnputil, do_clear_reg, live=live)
    except RuntimeError as exc:
        return {"ok": False, "action": action, "error": str(exc)}
    return {
        "ok": True,
        "action": action,
        "need_reboot": False,
        "instances": [d.instance_id for d in targets],
        "message": (
            "已恢复成普通蓝牙棒，无需重启电脑。"
            if any(d.kind == "ble_dongle" for d in targets)
            else "已恢复系统默认驱动。"
        ),
    }


def _wait_for_answer(path: Path, seconds: float, sleep_fn: Callable[[float], None]) -> str:
    """``keep`` / ``revert`` written by the app, or "" when the time runs out."""
    steps = max(1, int(seconds / 0.25))
    for _ in range(steps):
        try:
            answer = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        except OSError:
            answer = ""
        if answer in ("keep", "revert"):
            return answer
        sleep_fn(0.25)
    return ""


def _restore_devices(targets, do_pnputil, do_clear_reg, *, live: bool) -> None:
    oem_to_delete: set[str] = set()
    for dev in targets:
        do_clear_reg(dev.instance_id)
        if _should_delete_legacy_oem_inf(dev):
            oem_to_delete.add(Path(dev.driver_inf).name)
        rc, out = do_pnputil("/remove-device", dev.instance_id)
        if rc != 0:
            raise RuntimeError(f"卸载「{dev.name}」（{dev.id_label}）当前驱动失败：{out or rc}")
    for oem_inf in sorted(oem_to_delete):
        do_pnputil("/delete-driver", oem_inf, "/uninstall", "/force")
    do_pnputil("/scan-devices")
    if live:
        time.sleep(1.5)


def confirm_path_for(result_path: Path) -> Path:
    """Where the app writes ``keep`` / ``revert`` for a takeover awaiting confirmation."""
    return Path(str(result_path) + ".confirm")


def launch_elevated_action(action: str, instance_ids: Iterable[str], *, confirm: bool = False) -> tuple[int, Path]:
    """Spawn this app elevated via Windows UAC (``runas``) and return ``(hProcess, result_path)``."""
    if sys.platform != "win32":
        raise RuntimeError("仅 Windows 支持一键切换 USB 驱动。")
    ids = [str(item).strip() for item in instance_ids if str(item).strip()]
    if not ids:
        raise ValueError("未指定要操作的 USB 设备。")

    import ctypes as c
    from ctypes import wintypes as w

    fd, raw_path = tempfile.mkstemp(prefix="10remotehub-drv-", suffix=".json")
    os.close(fd)
    result_path = Path(raw_path)

    args = ["--win-usb-driver", action, "--result-file", str(result_path)]
    if confirm:
        args += ["--confirm-file", str(confirm_path_for(result_path))]
    args += ids
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = subprocess.list2cmdline(args)
    else:
        exe = sys.executable
        params = subprocess.list2cmdline(["-m", "remotehub.app", *args])

    class SHELLEXECUTEINFOW(c.Structure):
        _fields_ = [
            ("cbSize", w.DWORD),
            ("fMask", w.ULONG),
            ("hwnd", w.HWND),
            ("lpVerb", w.LPCWSTR),
            ("lpFile", w.LPCWSTR),
            ("lpParameters", w.LPCWSTR),
            ("lpDirectory", w.LPCWSTR),
            ("nShow", c.c_int),
            ("hInstApp", w.HINSTANCE),
            ("lpIDList", c.c_void_p),
            ("lpClass", w.LPCWSTR),
            ("hkeyClass", w.HKEY),
            ("dwHotKey", w.DWORD),
            ("hIconOrMonitor", w.HANDLE),
            ("hProcess", w.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0

    shell32 = c.WinDLL("shell32.dll", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [c.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = w.BOOL

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = c.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = SW_HIDE

    if not shell32.ShellExecuteExW(c.byref(sei)):
        err = c.get_last_error()
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass
        if err == ERROR_CANCELLED:
            raise DriverActionCancelled("已取消操作，设备未做任何改动。")
        raise RuntimeError(f"无法启动管理员权限切换驱动（错误码 {err}）。")
    return int(sei.hProcess or 0), result_path


def poll_elevated_process(handle: int) -> tuple[bool, int]:
    """Non-blocking poll of a Windows process handle: returns ``(done, exit_code)``."""
    if not handle or sys.platform != "win32":
        return True, 0
    import ctypes as c
    from ctypes import wintypes as w

    kernel32 = c.WinDLL("kernel32.dll", use_last_error=True)
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    res = kernel32.WaitForSingleObject(w.HANDLE(handle), 0)
    if res == WAIT_TIMEOUT:
        return False, 0
    code = w.DWORD(0)
    if res == WAIT_OBJECT_0:
        kernel32.GetExitCodeProcess(w.HANDLE(handle), c.byref(code))
    kernel32.CloseHandle(w.HANDLE(handle))
    return True, int(code.value)


def read_action_stage(result_path: Path) -> dict:
    """Peek at an interim stage the elevated helper wrote while it is still running."""
    try:
        data = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and "stage" in data else {}


def answer_confirmation(result_path: Path, keep: bool) -> None:
    """Tell the elevated helper to keep the takeover or restore the system driver now."""
    try:
        confirm_path_for(result_path).write_text("keep" if keep else "revert", encoding="utf-8")
    except OSError:
        pass


def read_action_result(result_path: Path, exit_code: int = 0) -> dict:
    """Read and clean up the JSON result written by the elevated helper."""
    try:
        confirm_path_for(result_path).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        raw = result_path.read_text(encoding="utf-8").strip() if result_path.is_file() else ""
    except OSError:
        raw = ""
    finally:
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    if exit_code == 0:
        return {"ok": True, "message": "操作已完成，请点「重新检测」确认设备状态。"}
    return {"ok": False, "error": f"驱动切换进程异常退出（退出码 {exit_code}）。"}


def run_cli(argv: list[str] | None = None) -> int:
    """Internal elevated entrypoint invoked via ``10RemoteHub.exe --win-usb-driver ...``."""
    import argparse

    parser = argparse.ArgumentParser(prog="10RemoteHub --win-usb-driver")
    parser.add_argument("action", choices=("bind", "restore"))
    parser.add_argument("--result-file", default="")
    parser.add_argument("--confirm-file", default="")
    parser.add_argument("instance_ids", nargs="+")
    args = parser.parse_args(argv)

    def stage(data):
        if args.result_file:
            try:
                Path(args.result_file).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    result = execute_driver_action(
        args.action, args.instance_ids,
        confirm_file=Path(args.confirm_file) if args.confirm_file else None, on_stage=stage,
    )
    payload = json.dumps(result, ensure_ascii=False)
    if args.result_file:
        try:
            Path(args.result_file).write_text(payload, encoding="utf-8")
        except OSError:
            pass
    else:
        print(payload)
    return 0 if result.get("ok") else 1
