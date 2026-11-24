"""Tests for A4 (scoped): remove unused confirmation logic from LearnedRemoteRuntime."""
import inspect

from PySide6.QtWidgets import QApplication

from remotehub.learned_remotes import LearnedRemoteRuntime
from remotehub.probe_device import ProbeDevice


class FakeReader:
    def __init__(self, device):
        self.device = device
        self.scope = "测试作用域"
        self.on_key = None
        self.on_removed = None
        self.stopped = 0

    def start(self, identity, on_key, on_removed, *, expected=None):
        assert identity == self.device.id
        assert expected == self.device
        self.on_key = on_key
        self.on_removed = on_removed
        return 1, 2

    def stop(self):
        self.stopped += 1
        self.on_key = None
        self.on_removed = None


def test_learned_remote_runtime_removes_unused_confirmation_logic(qtbot):
    device = ProbeDevice(("usb", 1), "通用遥控器", "USB", 0x1234, 0x5678, "a" * 64, (1,))
    reader = FakeReader(device)
    runtime = LearnedRemoteRuntime(reader)

    removed_attrs = (
        "confirm_key",
        "confirm_seen",
        "confirm_held",
        "confirm_timer",
        "settle",
        "verification_key",
        "_confirmed",
        "_confirmation_timeout",
    )
    for name in removed_attrs:
        assert not hasattr(LearnedRemoteRuntime, name)
        assert not hasattr(runtime, name)

    params = list(inspect.signature(LearnedRemoteRuntime.listen).parameters)
    assert params == ["self", "device"]

    keys, stops, feedback = [], [], []
    runtime.key.connect(lambda trigger, down: keys.append((trigger, down)))
    runtime.stopped.connect(lambda: stops.append(True))
    runtime.feedback.connect(feedback.append)

    assert runtime.listen(device) is True
    assert runtime.device == device
    assert "已打开 1/2 个按键接口" in runtime.scope

    # Keys pass straight through and dedup across channels without confirmation gating.
    reader.on_key((1, 0), "hid:7:82", True)
    reader.on_key((2, 0), "hid:7:82", True)
    qtbot.waitUntil(lambda: keys == [("hid:7:82", True)])
    reader.on_key((1, 0), "hid:7:82", False)
    QApplication.processEvents()
    assert keys == [("hid:7:82", True)]
    reader.on_key((2, 0), "hid:7:82", False)
    qtbot.waitUntil(lambda: keys == [("hid:7:82", True), ("hid:7:82", False)])

    # Removal stops runtime cleanly and notifies listeners.
    reader.on_removed()
    qtbot.waitUntil(lambda: stops == [True] and runtime.device is None)
    assert "遥控器已断开" in feedback[-1]
