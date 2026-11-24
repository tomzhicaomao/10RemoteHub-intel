"""Tests for C1: canceling child photo learning refreshes parent without closing shared reader."""
from remotehub.device_probe import DeviceProbeDialog
from remotehub.probe_device import ProbeDevice


DEV_A = ProbeDevice(("usb", 1), "USB 遥控器 A", "USB", 0x1234, 0x5678, "a" * 64, (1,))
DEV_B = ProbeDevice(("hid", 2), "蓝牙遥控器 B", "蓝牙", 0x620A, 0x0407, "", (2,), vendor_id_source=1)


class SpyReader:
    def __init__(self, *devices):
        self.devices = list(devices)
        self.enumerate_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.closed = False
        self.on_key = None
        self.on_removed = None

    def enumerate(self):
        self.enumerate_calls += 1
        assert not self.closed
        return list(self.devices)

    def start(self, identity, on_key, on_removed, *, expected=None):
        assert not self.closed
        self.start_calls += 1
        self.on_key = on_key
        self.on_removed = on_removed
        return 1, 1

    def stop(self):
        self.stop_calls += 1
        self.on_key = None
        self.on_removed = None

    def close(self):
        self.close_calls += 1
        self.closed = True


def test_canceling_photo_learning_refreshes_parent_without_closing_shared_reader(qtbot):
    reader = SpyReader(DEV_A)
    dialog = DeviceProbeDialog(reader, install=lambda *_args: True)
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.picker.count() == 1
    dialog.start_button.click()
    reader.on_key((1, 0), "hid:7:82", True)
    reader.on_key((1, 0), "hid:7:82", False)
    qtbot.waitUntil(dialog.photo_link.isEnabled)

    dialog.photo_link.click()
    learning = dialog.learning_dialog
    assert learning is not None and learning.isVisible()

    # A new device appears while the child photo learning dialog is open; user cancels back.
    enum_before_cancel = reader.enumerate_calls
    reader.devices.append(DEV_B)
    learning.close_button.click()
    qtbot.waitUntil(lambda: dialog.learning_dialog is None)

    assert reader.close_calls == 0
    assert not reader.closed
    assert reader.enumerate_calls > enum_before_cancel
    assert dialog.picker.count() == 2
    assert dialog.devices == [DEV_A, DEV_B]

    # Parent still owns the live reader and can start capture again.
    dialog.picker.setCurrentIndex(1)
    dialog.start_button.click()
    assert dialog.active
    dialog.close_button.click()
    assert reader.close_calls == 1
