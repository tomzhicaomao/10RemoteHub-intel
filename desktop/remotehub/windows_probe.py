"""Selected-device keyboard/media input for the Windows remote workbench."""
from __future__ import annotations

import ctypes as c
from hashlib import sha256
import threading

from PySide6.QtWidgets import QWidget

from .input_codes import windows_keyboard
from .probe_device import ProbeDevice, UnlistedDevice
from .win_hid_suppress import WindowsHIDSuppressor
from .windows_input import WindowsInput
from .windows_probe_metadata import WindowsProbeMetadata


_U32 = c.c_uint32
_FAILED = 0xffffffff


class _RawDevice(c.Structure):
    _fields_ = [("handle", c.c_void_p), ("kind", _U32)]


class _DeviceInfo(c.Structure):
    _fields_ = [("size", _U32), ("kind", _U32), ("vendor", _U32), ("product", _U32),
                ("version", _U32), ("page", c.c_uint16), ("usage", c.c_uint16), ("rest", _U32 * 2)]


def _device_with_serial(devices, serial):
    if not serial:
        return None
    devices = list(devices or ())
    matches = []
    for device in devices:
        try:
            seen = device.serial_number
        except (OSError, ValueError):
            seen = None
        if not seen and len(devices) > 1:
            return None
        if seen and seen.casefold() == serial.casefold():
            matches.append(device)
    return matches[0] if len(matches) == 1 else None


class _WinUSBKeys:
    """Read an already configured keyboard/consumer HID interface on one proven USB unit."""

    def __init__(self, serial, interfaces, on_key, on_removed, *, vendor=0x0416, product=0x0602):
        from .windows_collector import WindowsCollector
        import libusb_package
        import usb.core
        import usb.util
        from usb.backend import libusb1

        backend = libusb1.get_backend(find_library=libusb_package.find_library)
        devices = list(usb.core.find(find_all=True, idVendor=vendor, idProduct=product, backend=backend) or ())
        device = _device_with_serial(devices, serial)
        if device is None and not serial and len(devices) == 1:
            device = devices[0]
        if device is None:
            raise RuntimeError("无法唯一确认已选择的 WinUSB 接收器")
        collector = WindowsCollector()
        try:
            readers = collector._claim_keys(device, interfaces=interfaces, park_mouse=False)
            if not readers:
                raise RuntimeError("无法读取所选接收器的 WinUSB 按键接口")
        except Exception:
            collector._release_device(device, usb.util, False)
            raise

        self.halt = threading.Event()
        self.interfaces = tuple(collector._claimed)

        def read(number, endpoint, parser):
            try:
                while not self.halt.is_set():
                    try:
                        report = bytes(endpoint.read(endpoint.wMaxPacketSize, timeout=100))
                    except usb.core.USBTimeoutError:
                        continue
                    try:
                        edges = parser.decode(report)
                    except ValueError:
                        continue
                    report_id = report[0] if parser.has_ids and report else 0
                    for trigger, down in edges:
                        if not self.halt.is_set():
                            on_key(("winusb", number, report_id), trigger, down)
            except (OSError, RuntimeError):
                if not self.halt.is_set():
                    self.halt.set()
                    on_removed()

        def supervise():
            workers = []
            try:
                for number, (endpoint, parser) in zip(self.interfaces, readers):
                    worker = threading.Thread(target=read, args=(number, endpoint, parser), daemon=True)
                    worker.start()
                    workers.append(worker)
                for worker in workers:
                    worker.join()
            except RuntimeError:
                if not self.halt.is_set():
                    self.halt.set()
                    on_removed()
            finally:
                for worker in workers:
                    worker.join()
                collector._release_device(device, usb.util, False)

        self.worker = threading.Thread(target=supervise, name="remotehub-winusb-keys", daemon=True)
        try:
            self.worker.start()
        except Exception:
            collector._release_device(device, usb.util, False)
            raise

    def close(self):
        self.halt.set()
        self.worker.join(0.3)


class WindowsHIDProbe(WindowsInput):
    """Backend with the same enumerate/start/stop/takeover contract as the Mac reader."""

    def __init__(self, *, suppressor=None):
        host = QWidget()
        super().__init__(None, lambda *_: None, lambda *_: None, host)
        self.metadata = WindowsProbeMetadata()
        self.winusb_keys = _WinUSBKeys
        self._suppressor = suppressor if suppressor is not None else WindowsHIDSuppressor()
        raw_list = self.user.GetRawInputDeviceList
        raw_list.argtypes = [c.POINTER(_RawDevice), c.POINTER(_U32), _U32]
        raw_list.restype = _U32
        self.devices = {}
        self.allowed = set()
        self.on_key = self.on_removed = None
        self.registered = False
        self.winusb = None
        self.scope = ""
        self.unlisted = []
        self.blocked_ble = []
        self.take_over = False
        self.take_over_triggers: tuple[str, ...] = ()
        self.seized = 0
        self.seize_error = ""
        self._using_suppressor = False
        self._active_take_over = False
        self._active_claimed: tuple[int, ...] = ()

    def _raw_devices(self):
        size = c.sizeof(_RawDevice)
        count = _U32()
        for _ in range(3):
            if self.user.GetRawInputDeviceList(None, c.byref(count), size) == _FAILED:
                break
            if not count.value:
                return []
            if count.value > 4096:
                break
            devices = (_RawDevice * count.value)()
            found = self.user.GetRawInputDeviceList(devices, c.byref(count), size)
            if found <= len(devices):
                return devices[:found]
        raise RuntimeError("无法读取 Windows 的按键设备列表")

    def _report_identity(self, raw, skipped=None):
        if raw.kind == 1:
            return 0, 0
        if raw.kind != 2:
            return None
        info = _DeviceInfo()
        info.size = c.sizeof(info)
        length = _U32(info.size)
        result = self.user.GetRawInputDeviceInfoW(raw.handle, 0x2000000b, c.byref(info), c.byref(length))
        if result == _FAILED:
            return None
        if (info.page, info.usage) != (12, 1):
            # Not a key interface we read; remember it to explain the boundary.
            pair = tuple(value if value <= 0xffff else 0 for value in (info.vendor, info.product))
            target = self._skipped if skipped is None else skipped
            target.setdefault(pair, set()).add((info.page, info.usage))
            return None
        return tuple(value if value <= 0xffff else 0 for value in (info.vendor, info.product))

    def _collect_catalog(self):
        from .enrollment import classify

        skipped = {}
        catalog = {info.group: (info, set()) for info in self.metadata.winusb_receivers()}
        for raw in self._raw_devices():
            reported = self._report_identity(raw, skipped=skipped)
            if reported is None or not raw.handle:
                continue
            path = self.device_path(raw.handle)
            info = self.metadata.for_interface(path, reported) if path else None
            if info is not None:
                catalog.setdefault(info.group, (info, set()))[1].add(raw.handle)

        devices = {}
        for group, (info, handles) in catalog.items():
            key = sha256(info.serial.casefold().encode()).hexdigest() if info.serial else ""
            # Use live handles to detect a stale selection even if Windows reuses
            # the same device instance path when the cable is moved.
            device = ProbeDevice(group, info.name, info.transport, info.vendor, info.product, key,
                                 tuple(sorted(handles)), info.vendor_source, info.revision)
            devices[group] = device, info
        listed = {(info.vendor, info.product) for _, info in devices.values()}
        unlisted = []
        seen_unlisted = set()
        for (vendor, product), collections in sorted(skipped.items()):
            if not (vendor or product):
                continue
            candidate = UnlistedDevice(
                "HID 设备", "", vendor, product, frozenset(collections),
                frozenset(page for page, _ in collections),
            )
            if (vendor, product) not in listed or classify(candidate):
                unlisted.append(candidate)
                seen_unlisted.add((vendor, product))
        if hasattr(self.metadata, "unlisted_bluetooth"):
            try:
                bt_entries = list(self.metadata.unlisted_bluetooth())
            except Exception:
                bt_entries = []
            for entry in bt_entries:
                pair = (entry.vendor, entry.product)
                if pair not in listed and pair not in seen_unlisted:
                    seen_unlisted.add(pair)
                    unlisted.append(entry)
        sorted_devices = sorted((pair[0] for pair in devices.values()), key=lambda d: (d.transport, d.name, repr(d.id)))
        return sorted_devices, devices, unlisted, skipped

    def enumerate(self):
        self.stop()
        sorted_devices, devices, unlisted, skipped = self._collect_catalog()
        self._skipped = skipped
        self.devices = devices
        self.unlisted = unlisted
        self.blocked_ble = []
        if hasattr(self.metadata, "blocked_ble_remotes"):
            try:
                self.blocked_ble = list(self.metadata.blocked_ble_remotes())
            except Exception:
                self.blocked_ble = []
        return sorted_devices

    def blocked_ble_remotes(self):
        return list(self.blocked_ble)

    def peek(self):
        """Read-only snapshot of Windows probes without stopping active readers."""
        sorted_devices, _, _, _ = self._collect_catalog()
        return sorted_devices

    def start(self, identity, on_key, on_removed, *, expected=None):
        taking_over = bool(self.take_over)
        triggers = tuple(self.take_over_triggers)
        self.enumerate()
        found = self.devices.get(identity)
        if found is None or (expected is not None and found[0] != expected):
            raise RuntimeError("设备已更换，请重新检测")
        device, info = found
        self.allowed = set(device.connection)
        self.on_key, self.on_removed = on_key, on_removed
        self.seized = 0
        self.seize_error = ""
        self._using_suppressor = False
        self._active_take_over = taking_over
        self._active_claimed = ()
        failure = ""
        try:
            if info.winusb_interfaces:
                try:
                    if not info.serial and (info.vendor, info.product) == (0x0416, 0x0602):
                        raise RuntimeError("无法通过序列号确认所选 WinUSB 接收器")
                    if self.winusb_keys is _WinUSBKeys:
                        self.winusb = self.winusb_keys(
                            info.serial, info.winusb_interfaces, on_key, on_removed,
                            vendor=info.vendor, product=info.product,
                        )
                    else:
                        self.winusb = self.winusb_keys(info.serial, info.winusb_interfaces, on_key, on_removed)
                except (ImportError, OSError, ValueError, RuntimeError) as exc:
                    failure = str(exc)
            claimed = tuple(self.winusb.interfaces) if self.winusb is not None else ()
            self._active_claimed = claimed
            self.keyboard_muted = (
                4 in claimed if (info.vendor, info.product) == (0x0416, 0x0602) else bool(claimed)
            )
            if self.allowed:
                if taking_over and triggers:
                    if self._suppressor.start(
                        allowed_handles=self.allowed,
                        triggers=triggers,
                        on_edge=self._edge,
                        on_removed=self._release,
                        keyboard_muted=self.keyboard_muted,
                    ):
                        self._using_suppressor = True
                        self.seized = self._suppressor.suppressed_count + len(claimed)
                        self.seize_error = ""
                    else:
                        self.seized = len(claimed)
                        self.seize_error = self._suppressor.error or "无法启用 Windows 按键接管"
                        self.registered = True
                        super().start()
                else:
                    self.registered = True
                    super().start()
                    if taking_over and claimed:
                        self.seized = len(claimed)
            elif taking_over and claimed:
                self.seized = max(len(triggers), len(claimed))
                self.seize_error = ""
            if not self.registered and not self._using_suppressor and self.winusb is None:
                raise RuntimeError(failure or "Windows 当前未提供这只遥控器的按键接口")
        except Exception:
            self.stop()
            raise
        opened = len(self.allowed) + len(claimed)
        if (info.vendor, info.product) == (0x0416, 0x0602) and info.transport == "USB":
            self.scope = "WinUSB 键盘接口已接入" if self.keyboard_muted else "Windows 基础按键通道，完整键位需 WinUSB"
        elif claimed:
            self.scope = "WinUSB 按键接口已接管"
        return opened, len(self.allowed) + len(info.winusb_interfaces)

    def sync_takeover(self, triggers) -> bool:
        """Keep the active Windows suppressor aligned with learned triggers."""
        if not self._active_take_over:
            return False
        items = tuple(triggers)
        if self._using_suppressor:
            changed = self._suppressor.sync(items)
            new_seized = self._suppressor.suppressed_count + len(self._active_claimed)
            if new_seized != self.seized:
                self.seized = new_seized
                changed = True
            return changed
        return False

    def ensure_takeover(self) -> bool:
        """Verify the Windows suppressor thread is still alive."""
        if not self._using_suppressor:
            return False
        before = (self.seized, self.seize_error)
        if not self._suppressor.ensure():
            self.seized = len(self._active_claimed)
            self.seize_error = self._suppressor.error or "原按键屏蔽已失效"
        return (self.seized, self.seize_error) != before

    def _matches(self, handle):
        return handle in self.allowed

    def _keyboard_trigger(self, scan, flags):
        return (windows_keyboard(scan, flags) or f"win:{scan}:{flags & 6}") if scan else None

    def _is_consumer_report(self):
        return True

    def _edge(self, handle, trigger, down):
        if self.on_key is not None and handle in self.allowed:
            self.on_key((handle, 0), trigger, down)

    def _release(self, handle):
        if handle in self.allowed and self.on_removed is not None:
            self.allowed.clear()
            self.on_removed()
        super()._release(handle)

    def stop(self):
        self.allowed.clear()
        self.on_key = self.on_removed = None
        if self._using_suppressor:
            self._using_suppressor = False
            self._suppressor.clear()
        if self.registered:
            self.registered = False
            super().stop()
        if self.winusb is not None:
            self.winusb.close()
            self.winusb = None
        self.keyboard_muted = False
        self.scope = ""
        self.seized = 0
        self.seize_error = ""
        self._active_take_over = False
        self._active_claimed = ()

    close = stop
