"""Read selected macOS HID reports through a shared IOKit connection."""
from __future__ import annotations

import ctypes as c
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .hid_reports import HIDReports
from .mac_hid_suppress import MacHIDSuppressor, usages_from_triggers
from .probe_device import ProbeDevice, UnlistedDevice

_REF = c.c_void_p
_INDEX = c.c_long
_U32 = c.c_uint32
_STATUS = c.c_int32
_BUFFER = c.POINTER(c.c_uint8)
_REPORT = c.CFUNCTYPE(None, _REF, _STATUS, _REF, c.c_int, _U32, _BUFFER, _INDEX)
_REMOVAL = c.CFUNCTYPE(None, _REF, _STATUS, _REF)
_UTF8 = 0x08000100
_SHARED = 0  # kIOHIDOptionsTypeNone
_MAX_REPORT = 16384
_KEY_PAGES = (7, 12)


class _API:
    def __init__(self):
        self.cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.hid = c.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        self._bind(self.cf, (
            ("CFRelease", None, _REF),
            ("CFSetGetCount", _INDEX, _REF),
            ("CFSetGetValues", None, _REF, c.POINTER(_REF)),
            ("CFGetTypeID", c.c_ulong, _REF),
            ("CFStringGetTypeID", c.c_ulong),
            ("CFNumberGetTypeID", c.c_ulong),
            ("CFBooleanGetTypeID", c.c_ulong),
            ("CFDataGetTypeID", c.c_ulong),
            ("CFStringCreateWithCString", _REF, _REF, c.c_char_p, _U32),
            ("CFStringGetCString", c.c_bool, _REF, c.c_char_p, _INDEX, _U32),
            ("CFNumberGetValue", c.c_bool, _REF, _INDEX, _REF),
            ("CFBooleanGetValue", c.c_bool, _REF),
            ("CFDataGetLength", _INDEX, _REF),
            ("CFDataGetBytePtr", _REF, _REF),
            ("CFRunLoopGetMain", _REF),
        ))
        self._bind(self.hid, (
            ("IOHIDManagerCreate", _REF, _REF, _U32),
            ("IOHIDManagerSetDeviceMatching", None, _REF, _REF),
            ("IOHIDManagerCopyDevices", _REF, _REF),
            ("IOHIDDeviceGetService", _U32, _REF),
            ("IORegistryEntryGetRegistryEntryID", _STATUS, _U32, c.POINTER(c.c_uint64)),
            ("IOHIDDeviceGetProperty", _REF, _REF, _REF),
            ("IOHIDDeviceOpen", _STATUS, _REF, _U32),
            ("IOHIDDeviceClose", _STATUS, _REF, _U32),
            ("IOHIDDeviceRegisterInputReportCallback", None, _REF, _BUFFER, _INDEX, _REPORT, _REF),
            ("IOHIDDeviceRegisterRemovalCallback", None, _REF, _REMOVAL, _REF),
            ("IOHIDDeviceScheduleWithRunLoop", None, _REF, _REF, _REF),
            ("IOHIDDeviceUnscheduleFromRunLoop", None, _REF, _REF, _REF),
        ))
        self.run_loop = self.cf.CFRunLoopGetMain()
        self.run_loop_mode = _REF.in_dll(self.cf, "kCFRunLoopCommonModes").value

    @staticmethod
    def _bind(library, specs):
        for name, result, *arguments in specs:
            function = getattr(library, name)
            function.restype = result
            function.argtypes = arguments

    def property(self, device, name):
        cf = self.cf
        key = cf.CFStringCreateWithCString(None, name.encode("ascii"), _UTF8)
        if not key:
            return None
        try:
            value = self.hid.IOHIDDeviceGetProperty(device, key)
            if not value:
                return None
            kind = cf.CFGetTypeID(value)
            if kind == cf.CFStringGetTypeID():
                buffer = c.create_string_buffer(512)
                if cf.CFStringGetCString(value, buffer, len(buffer), _UTF8):
                    return buffer.value.decode("utf-8", errors="replace").strip()
            elif kind == cf.CFNumberGetTypeID():
                number = c.c_int64()
                if cf.CFNumberGetValue(value, 4, c.byref(number)):  # kCFNumberSInt64Type
                    return number.value
            elif kind == cf.CFBooleanGetTypeID():
                return cf.CFBooleanGetValue(value)
            elif kind == cf.CFDataGetTypeID():
                size = cf.CFDataGetLength(value)
                if 0 < size <= _MAX_REPORT:
                    return c.string_at(cf.CFDataGetBytePtr(value), size)
            return None
        finally:
            cf.CFRelease(key)

    def registry_id(self, device):
        service = self.hid.IOHIDDeviceGetService(device)
        identifier = c.c_uint64()
        if service and not self.hid.IORegistryEntryGetRegistryEntryID(service, c.byref(identifier)):
            return identifier.value
        return None


@dataclass(frozen=True)
class _Interface:
    handle: int
    registry_id: int
    descriptor: bytes
    max_report: int


@dataclass
class _Listener:
    interface: _Interface
    buffer: c.Array
    report: _REPORT
    removed: _REMOVAL
    scheduled: bool = False
    options: int = _SHARED


def _report_packet(parser, report_id, data):
    """IOKit may pass a numbered report with or without the ID in the buffer."""
    if not parser.fields.get(report_id):
        return None
    if not parser.has_ids:
        return data
    size = (parser.offsets[report_id] + 7) // 8
    if len(data) > size and data[0] == report_id:
        return data
    if len(data) == size:
        return bytes([report_id]) + data
    return None


class MacHIDProbe:
    """Enumerate metadata, then listen for reports on the chosen interfaces."""

    def __init__(self, *, state_path: Path | None = None, suppressor: MacHIDSuppressor | None = None):
        self._api = _API()
        self._device_set = None
        self._catalog: dict[tuple, list[_Interface]] = {}
        self._devices: dict[tuple, ProbeDevice] = {}
        self._hid_meta: dict[tuple, tuple[str, int | None]] = {}
        self._listeners: list[_Listener] = []
        self._active_identity = None
        self._suppressor = suppressor if suppressor is not None else MacHIDSuppressor(state_path=state_path)
        self.unlisted: list[UnlistedDevice] = []
        # Daily use suppresses the remote's learned keys via per-device hidutil
        # UserKeyMapping while keeping raw HID reading shared (preserves air-mouse
        # and works without root). Set by the runtime right before start().
        self.take_over = False
        self.take_over_triggers: tuple[str, ...] = ()
        self.seized = 0           # number of suppressed keys (or seized interfaces)
        self.seize_error = ""

    def _copy_device_set(self):
        api = self._api
        manager = api.hid.IOHIDManagerCreate(None, _SHARED)
        if not manager:
            raise RuntimeError("无法读取 macOS HID 设备列表")
        try:
            api.hid.IOHIDManagerSetDeviceMatching(manager, None)
            return api.hid.IOHIDManagerCopyDevices(manager)
        finally:
            api.cf.CFRelease(manager)

    def _collect_devices(self, device_set, *, record_catalog: bool):
        from .enrollment import classify

        api = self._api
        handles = (_REF * api.cf.CFSetGetCount(device_set))()
        api.cf.CFSetGetValues(device_set, handles)
        devices = {}
        serials = {}
        vendor_sources = {}
        connection_ids = {}
        skipped = {}
        for handle in handles:
            transport = api.property(handle, "Transport")
            if not isinstance(transport, str) or api.property(handle, "Built-In"):
                continue
            bus = transport.casefold()
            if bus != "usb" and not bus.startswith("bluetooth"):
                continue
            registry_id = api.registry_id(handle)
            descriptor = api.property(handle, "ReportDescriptor")
            if not registry_id or not isinstance(descriptor, bytes):
                continue
            try:
                parser = HIDReports(descriptor)
            except ValueError:
                continue
            vendor, product = (
                value if type(value) is int and 0 <= value <= 0xffff else 0
                for value in (api.property(handle, "VendorID"), api.property(handle, "ProductID"))
            )
            if not any(field.page in _KEY_PAGES for fields in parser.fields.values() for field in fields):
                # Keep what it declares so the add dialog can explain the boundary.
                name = api.property(handle, "Product")
                key = ("USB" if bus == "usb" else "蓝牙", vendor, product)
                entry = skipped.setdefault(key, [name if isinstance(name, str) and name else "HID 设备", set(), set()])
                entry[1].update(parser.collections)
                entry[2].update(parser.input_pages)
                continue
            location = api.property(handle, "LocationID")
            # USB interfaces at the same physical port belong to one receiver.
            # BLE collections stay separate until a physical grouping is known.
            if bus == "usb" and type(location) is int and location > 0:
                identity = ("usb", location, vendor, product)
            else:
                identity = ("hid", registry_id)
            product_name = api.property(handle, "Product")
            name = product_name if isinstance(product_name, str) and product_name else "HID 遥控器"
            devices.setdefault(identity, (name, "USB" if bus == "usb" else "蓝牙", vendor, product))
            source = api.property(handle, "VendorIDSource")
            if type(source) is int and source in (1, 2):
                vendor_sources.setdefault(identity, set()).add(source)
            serial = api.property(handle, "SerialNumber")
            if isinstance(serial, str) and serial and serial.strip("0"):
                serials.setdefault(identity, set()).add(serial)
            connection_ids.setdefault(identity, []).append(registry_id)
            if record_catalog:
                loc = location if type(location) is int and location > 0 else None
                self._hid_meta.setdefault(identity, (transport, loc))
                declared_size = api.property(handle, "MaxInputReportSize")
                size = declared_size if type(declared_size) is int else 0
                required = max((bits + 7) // 8 for bits in parser.offsets.values()) + int(parser.has_ids)
                self._catalog.setdefault(identity, []).append(
                    _Interface(handle, registry_id, descriptor, min(_MAX_REPORT, max(size, required, 1)))
                )
        listed = {(transport, vendor, product) for _, transport, vendor, product in devices.values()}
        unlisted = []
        for key, (name, collections, pages) in sorted(skipped.items()):
            candidate = UnlistedDevice(name, *key, frozenset(collections), frozenset(pages))
            if key not in listed or classify(candidate):
                unlisted.append(candidate)
        collected = []
        for identity, properties in devices.items():
            observed = serials.get(identity, set())
            unit_key = sha256(next(iter(observed)).encode()).hexdigest() if len(observed) == 1 else ""
            connection = tuple(sorted(connection_ids[identity]))
            sources = vendor_sources.get(identity, set())
            vendor_source = next(iter(sources)) if len(sources) == 1 else None
            probe = ProbeDevice(identity, *properties, unit_key, connection, vendor_source)
            collected.append(probe)
            if record_catalog:
                self._devices[identity] = probe
        return sorted(collected, key=lambda device: (device.transport, device.name, device.id)), unlisted

    def enumerate(self):
        self.stop()
        self._device_set = self._copy_device_set()
        if not self._device_set:
            self.unlisted = []
            return []
        devices, unlisted = self._collect_devices(self._device_set, record_catalog=True)
        self.unlisted = unlisted
        return devices

    def peek(self):
        """Read-only snapshot of connected HID probes without disturbing active listeners."""
        device_set = self._copy_device_set()
        if not device_set:
            return []
        try:
            devices, _ = self._collect_devices(device_set, record_catalog=False)
            return devices
        finally:
            self._api.cf.CFRelease(device_set)

    def _apply_takeover(self, identity, triggers) -> bool:
        previous = (self.seized, self.seize_error)
        device = self._devices.get(identity)
        if device is None:
            self._suppressor.clear()
            self.seized, self.seize_error = 0, ""
            return (self.seized, self.seize_error) != previous
        usages = usages_from_triggers(triggers)
        if not usages:
            self._suppressor.clear()
            self.seized, self.seize_error = 0, ""
            return (self.seized, self.seize_error) != previous
        raw_transport, location_id = self._hid_meta.get(identity, ("", None))
        if not raw_transport and device.transport == "USB":
            raw_transport = "USB"
        if self._suppressor.apply(
            vendor=device.vendor,
            product=device.product,
            triggers=triggers,
            transport=raw_transport,
            location_id=location_id,
        ):
            self.seized = len(self._suppressor.active_usages)
            self.seize_error = ""
        else:
            self.seized = 0
            self.seize_error = self._suppressor.error
        return (self.seized, self.seize_error) != previous

    def sync_takeover(self, triggers) -> bool:
        """Update suppressed keys live if the active remote's learned key set changed."""
        if self._active_identity is None or not self._listeners:
            return False
        if usages_from_triggers(triggers) == self._suppressor.active_usages and (
            self._suppressor.active or not usages_from_triggers(triggers)
        ):
            return False
        return self._apply_takeover(self._active_identity, triggers)

    def ensure_takeover(self) -> bool:
        """Re-verify and self-heal suppression (e.g. after macOS sleep/wake)."""
        if self._active_identity is None or not self._listeners or not self._suppressor.active:
            return False
        previous = (self.seized, self.seize_error)
        if self._suppressor.ensure():
            self.seized = len(self._suppressor.active_usages)
            self.seize_error = ""
        else:
            self.seized = 0
            self.seize_error = self._suppressor.error
        return (self.seized, self.seize_error) != previous

    def start(self, identity, on_key, on_removed, *, expected=None):
        self.enumerate()
        interfaces = self._catalog.get(identity, ())
        if not interfaces or (expected is not None and self._devices.get(identity) != expected):
            self.stop()
            raise RuntimeError("设备已更换，请重新检测")
        api = self._api
        errors = []
        self.seized, self.seize_error = 0, ""
        try:
            for interface in interfaces:
                listener = self._listener(interface, on_key, on_removed)
                status = api.hid.IOHIDDeviceOpen(interface.handle, _SHARED)
                if status:
                    errors.append(f"0x{status & 0xffffffff:08x}")
                    continue
                self._listeners.append(listener)
                context = id(listener)
                api.hid.IOHIDDeviceRegisterInputReportCallback(
                    interface.handle, listener.buffer, len(listener.buffer), listener.report, context
                )
                api.hid.IOHIDDeviceRegisterRemovalCallback(interface.handle, listener.removed, context)
                api.hid.IOHIDDeviceScheduleWithRunLoop(interface.handle, api.run_loop, api.run_loop_mode)
                listener.scheduled = True
        except Exception:
            self.stop()
            raise
        if not self._listeners:
            self.stop()
            raise RuntimeError("macOS 无法共享读取所选设备（" + "、".join(dict.fromkeys(errors)) + "）。")
        self._active_identity = identity
        if self.take_over:
            self._apply_takeover(identity, self.take_over_triggers)
        return len(self._listeners), len(interfaces)

    @staticmethod
    def _listener(interface, on_key, on_removed):
        parser = HIDReports(interface.descriptor)

        @_REPORT
        def report(_context, status, _sender, _type, report_id, data, length):
            if status or not data or not 0 < length <= interface.max_report:
                return
            packet = _report_packet(parser, report_id, c.string_at(data, length))
            if packet is None:
                return
            try:
                events = parser.decode(packet)
            except ValueError:
                return
            for trigger, down in events:
                if trigger.startswith(("hid:7:", "hid:12:")):
                    on_key((interface.registry_id, report_id), trigger, down)

        @_REMOVAL
        def removed(_context, _status, _sender):
            on_removed()

        return _Listener(interface, (c.c_uint8 * interface.max_report)(), report, removed)

    def stop(self):
        self._suppressor.clear()
        self._active_identity = None
        self.seized, self.seize_error = 0, ""
        api = self._api
        for listener in self._listeners:
            handle = listener.interface.handle
            if listener.scheduled:
                api.hid.IOHIDDeviceUnscheduleFromRunLoop(handle, api.run_loop, api.run_loop_mode)
            context = id(listener)
            api.hid.IOHIDDeviceRegisterInputReportCallback(handle, listener.buffer, len(listener.buffer), _REPORT(), context)
            api.hid.IOHIDDeviceRegisterRemovalCallback(handle, _REMOVAL(), context)
            api.hid.IOHIDDeviceClose(handle, listener.options)
        self._catalog.clear()
        self._devices.clear()
        self._hid_meta.clear()
        if self._device_set:
            api.cf.CFRelease(self._device_set)
            self._device_set = None
        # IOKit keeps the report buffer and callback pointers on its device object.
        self._listeners.clear()

    close = stop
