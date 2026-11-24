import ctypes as c
from dataclasses import replace
from types import SimpleNamespace

import pytest

from remotehub import windows_input, windows_probe
from remotehub.model import Action
from remotehub.ui import MainWindow
from remotehub.windows_probe_metadata import WindowsProbeMetadata, WindowsRemoteIdentity


USB = WindowsRemoteIdentity(("usb", "receiver-a", "container"), "Receiver A", "USB", 0x0416, 0x0602,
                            serial="RECEIVER_A", revision=0x0100)
OTHER = replace(USB, group=("usb", "receiver-b", "container"), name="Receiver B", serial="RECEIVER_B")
BLE = WindowsRemoteIdentity(("bluetooth", "remote", 1, 2, 1), "Bluetooth remote", "蓝牙", 1, 2, 1)


class NativeInput:
    def __init__(self):
        self.entries = {10: (1, USB), 11: (2, USB), 20: (1, OTHER), 30: (1, BLE), 31: (0, BLE)}
        self.packet = b""
        self.receivers = []
        self.registrations = []

    def GetRawInputDeviceList(self, buffer, count, _size):
        count._obj.value = len(self.entries)
        if buffer is None:
            return 0
        for index, (handle, (kind, _identity)) in enumerate(self.entries.items()):
            buffer[index] = windows_probe._RawDevice(handle, kind)
        return len(self.entries)

    def GetRawInputDeviceInfoW(self, handle, operation, buffer, size):
        if operation == 0x2000000b:
            buffer._obj.page, buffer._obj.usage = 12, 1
            return size._obj.value
        text = str(handle)
        size._obj.value = len(text) + 1
        if buffer is not None:
            buffer.value = text
        return len(text)

    def RegisterRawInputDevices(self, entries, count, _size):
        self.registrations.append([entries[i].flags for i in range(count)])
        return True

    def GetRawInputData(self, _handle, _operation, buffer, size, _header_size):
        size._obj.value = len(self.packet)
        if buffer is None:
            return 0
        c.memmove(buffer, self.packet, len(self.packet))
        return len(self.packet)

    def winusb_receivers(self):
        return self.receivers

    def for_interface(self, path, _pair):
        return self.entries[int(path)][1]

    def key(self, adapter, handle, scan, flags):
        keyboard = bytes(windows_input.KEYBOARD(scan, flags, 0, 0x31, 0, 0))
        header = windows_input.HEADER(1, c.sizeof(windows_input.HEADER) + len(keyboard), handle, 0)
        self.packet = bytes(header) + keyboard
        msg = windows_input.w.MSG(message=0xff)
        adapter.nativeEventFilter(None, c.addressof(msg))

    def unplug(self, adapter, handle):
        self.entries.pop(handle)
        msg = windows_input.w.MSG(message=0xfe, wParam=2, lParam=handle)
        adapter.nativeEventFilter(None, c.addressof(msg))


@pytest.fixture
def probe(qapp, monkeypatch):
    native = NativeInput()
    names = ("GetRawInputDeviceList", "GetRawInputDeviceInfoW", "RegisterRawInputDevices", "GetRawInputData")
    user = SimpleNamespace(**{name: lambda *args, name=name: getattr(native, name)(*args) for name in names})
    monkeypatch.setattr(windows_input.c, "WinDLL", lambda *args, **kwargs: user, raising=False)
    monkeypatch.setattr(windows_probe, "WindowsProbeMetadata", lambda: native)
    adapter = windows_probe.WindowsHIDProbe()
    yield adapter, native
    adapter.stop()


def test_windows_probe_groups_interfaces_and_sessions_are_source_scoped(probe):
    adapter, native = probe
    entries = {item.name: item for item in adapter.enumerate()}
    selected = entries[USB.name]
    assert selected.connection == (10, 11)
    assert selected.unit_key and selected.bcd_device == 0x0100
    assert entries[OTHER.name].unit_key != selected.unit_key
    assert entries[BLE.name].transport == "蓝牙" and entries[BLE.name].connection == (30,)
    events, removals = [], []
    assert adapter.start(selected.id, lambda *args: events.append(args), lambda: removals.append(True),
                         expected=selected) == (2, 2)
    for handle in (20, 30, 10):
        native.key(adapter, handle, 2, 0)
        native.key(adapter, handle, 2, 0)
        native.key(adapter, handle, 2, 1)
    assert events == [((10, 0), "hid:7:30", True), ((10, 0), "hid:7:30", False)]

    native.key(adapter, 10, 2, 0)
    native.unplug(adapter, 10)
    native.key(adapter, 11, 2, 1)
    assert removals == [True]
    assert events[-1] == ((10, 0), "hid:7:30", True)
    with pytest.raises(RuntimeError, match="设备已更换"):
        adapter.start(selected.id, None, None, expected=selected)
    assert native.registrations[-1] == [1, 1]


def test_windows_bluetooth_can_learn_an_existing_or_unmapped_scan_code(probe):
    adapter, native = probe
    selected = next(device for device in adapter.enumerate() if device.transport == "蓝牙")
    events = []
    adapter.start(selected.id, lambda *args: events.append(args), lambda: None, expected=selected)
    native.key(adapter, 30, 0x48, 2)
    native.key(adapter, 30, 0x48, 3)
    native.key(adapter, 30, 0x1e, 0)
    native.key(adapter, 30, 0x1e, 1)
    assert [event[1:] for event in events] == [
        ("hid:7:82", True), ("hid:7:82", False), ("win:30:0", True), ("win:30:0", False),
    ]


def test_winusb_without_a_raw_keyboard_uses_the_selected_receiver(probe):
    adapter, native = probe
    first = replace(USB, winusb_interfaces=(4,))
    second = replace(OTHER, winusb_interfaces=(1, 4))
    native.entries = {30: (1, BLE)}
    native.receivers = [first, second]
    selected = next(device for device in adapter.enumerate() if device.name == second.name)
    opened, stopped = [], []
    def open_keys(serial, interfaces, *_):
        opened.append((serial, interfaces))
        return SimpleNamespace(interfaces=interfaces, close=lambda: stopped.append(True))

    adapter.winusb_keys = open_keys
    assert adapter.start(selected.id, None, None, expected=selected) == (2, 2)
    assert opened == [(second.serial, second.winusb_interfaces)] and not native.registrations
    adapter.stop()
    assert stopped == [True]

    one = SimpleNamespace(serial_number=first.serial)
    two = SimpleNamespace(serial_number=second.serial)
    assert windows_probe._device_with_serial([one, two], second.serial.lower()) is two
    assert windows_probe._device_with_serial([one, two, two], second.serial) is None
    assert windows_probe._device_with_serial([one, two], "") is None


def test_bluetooth_layer_is_selected_before_its_usb_adapter():
    metadata = WindowsProbeMetadata.__new__(WindowsProbeMetadata)
    metadata.cfg = SimpleNamespace(CM_Get_Parent=object(), CM_Get_Device_Interface_PropertyW=object())
    instances = {
        1: r"HID\VID_620A&PID_0407\collection", 2: r"BTHLEDEVICE\service_VID&0002620A_PID&0407\uuid",
        3: r"BTHLE\DEV_010203040506\address", 4: r"USB\VID_0001&PID_0002\adapter",
    }
    metadata._property = lambda *_: instances[1].encode("utf-16-le")
    metadata._node = lambda _: 1
    metadata._instance = instances.get
    metadata._relate = lambda _, node: node + 1 if node < 4 else None
    metadata._container = lambda _: "container"
    metadata._name = lambda _: "蓝牙键盘 620A:0407"
    info = metadata.for_interface("HID", (0, 0))
    assert (info.name, info.transport, info.vendor, info.product, info.vendor_source) == ("蓝牙键盘 620A:0407", "蓝牙", 0x620A, 0x0407, 2)
    assert info.group[1] == instances[3].casefold()


def test_usb_exact_identity_checks_interfaces_on_the_selected_node():
    metadata = WindowsProbeMetadata.__new__(WindowsProbeMetadata)
    metadata.cfg = SimpleNamespace(CM_Get_Child="child", CM_Get_Sibling="sibling")
    metadata._relate = lambda kind, node: {("child", 10): 11, ("sibling", 11): 12}.get((kind, node))
    metadata._instance = lambda node: rf"USB\VID_0416&PID_0602&MI_{1 if node == 11 else 4:02}\instance"
    metadata._text = lambda node, *_: "WinUSB" if node == 12 else "HidUsb"
    metadata._container = lambda _: "container"
    metadata._name = lambda _: "Receiver"
    metadata._node_property = lambda _node, _guid, prop, _kind: (
        (0x10).to_bytes(4, "little") if prop == 17 else "USB\\VID_0416&PID_0602&REV_0100".encode("utf-16-le")
    )
    info = metadata._usb(10, r"USB\VID_0416&PID_0602\FACTORY-A", (0x0416, 0x0602))
    assert info.winusb_interfaces == (4,)
    assert info.serial == "FACTORY-A" and info.revision == 0x0100
    other = metadata._usb(20, r"USB\VID_0416&PID_0602\5&PORT", (0x0416, 0x0602))
    assert not other.winusb_interfaces and not other.serial and info.group != other.group
