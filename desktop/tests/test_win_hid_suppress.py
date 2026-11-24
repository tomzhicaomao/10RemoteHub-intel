"""Tests for Windows per-device key suppression, expanded scan codes, WinUSB metadata, and voice boundaries."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from remotehub import windows_input, windows_probe
from remotehub.enrollment import ai_request
from remotehub.input_codes import (
    windows_keyboard,
    windows_scan_for_trigger,
    windows_vk_for_consumer_trigger,
)
from remotehub.model import Button, Profile
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import Connection, Remote, RemoteLibraryStore
from remotehub.win_hid_suppress import (
    LLKHF_EXTENDED,
    LLKHF_INJECTED,
    LLKHF_LOWER_IL_INJECTED,
    LLKHF_UP,
    CorrelationEngine,
    WatchSet,
)
from remotehub.windows_probe_metadata import WindowsProbeMetadata, WindowsRemoteIdentity


def test_windows_scan_and_consumer_vk_mappings():
    # Expanded remote-relevant keys round-trip cleanly to HID Page 7.
    assert windows_keyboard(0x0E, 0) == "hid:7:42"   # Backspace
    assert windows_keyboard(0x39, 0) == "hid:7:44"   # Space
    assert windows_keyboard(0x47, 2) == "hid:7:74"   # Home
    assert windows_keyboard(0x48, 2) == "hid:7:82"   # Up
    assert windows_keyboard(0x4F, 0) == "hid:7:89"   # Numpad 1
    assert windows_keyboard(0x1E, 0) is None         # Unmapped letter stays raw win:30:0

    assert windows_scan_for_trigger("hid:7:82") == (0x48, True)
    assert windows_scan_for_trigger("hid:7:30") == (0x02, False)
    assert windows_scan_for_trigger("hid:7:42") == (0x0E, False)
    assert windows_scan_for_trigger("win:30:0") == (30, False)
    assert windows_scan_for_trigger("win:72:2") == (72, True)
    assert windows_scan_for_trigger("hid:12:233") is None

    assert windows_vk_for_consumer_trigger("hid:12:233") == 0xAF  # VK_VOLUME_UP
    assert windows_vk_for_consumer_trigger("hid:12:234") == 0xAE  # VK_VOLUME_DOWN
    assert windows_vk_for_consumer_trigger("hid:12:226") == 0xAD  # VK_VOLUME_MUTE
    assert windows_vk_for_consumer_trigger("hid:12:548") == 0xA6  # VK_BROWSER_BACK
    assert windows_vk_for_consumer_trigger("hid:12:547") == 0xAC  # VK_BROWSER_HOME
    assert windows_vk_for_consumer_trigger("hid:7:82") is None


def test_correlation_engine_swallows_remote_keys_and_never_blocks_main_keyboard_or_injected():
    now = [100.0]
    engine = CorrelationEngine(
        allowed_handles={1001, 1002},
        triggers=("hid:7:30", "hid:7:82", "win:30:0", "hid:12:233", "hid:12:548"),
        clock=lambda: now[0],
    )
    assert engine.watched_count == 5

    # 1. Target remote presses "1" (scan=0x02, non-extended) -> SWALLOW down & up.
    assert engine.record_raw_keyboard(1001, 0x02, False, True) is True
    assert engine.decide_hook(0x31, 0x02, 0) is True
    # Key repeat while held by remote -> SWALLOW.
    now[0] += 0.080
    assert engine.decide_hook(0x31, 0x02, 0) is True
    # Key up from remote -> SWALLOW.
    now[0] += 0.010
    assert engine.record_raw_keyboard(1001, 0x02, False, False) is True
    assert engine.decide_hook(0x31, 0x02, LLKHF_UP) is True

    # 2. Main keyboard (handle=9999) presses the EXACT same key "1" -> PASS THROUGH!
    now[0] += 0.020
    assert engine.record_raw_keyboard(9999, 0x02, False, True) is False
    assert engine.decide_hook(0x31, 0x02, 0) is False
    assert engine.record_raw_keyboard(9999, 0x02, False, False) is False
    assert engine.decide_hook(0x31, 0x02, LLKHF_UP) is False

    # 3. Injected keys (SendInput mapped actions) ALWAYS pass through even if remote holds same key.
    assert engine.record_raw_keyboard(1001, 0x48, True, True) is True
    assert engine.decide_hook(0x26, 0x48, LLKHF_EXTENDED | LLKHF_INJECTED) is False
    assert engine.decide_hook(0x26, 0x48, LLKHF_EXTENDED | LLKHF_LOWER_IL_INJECTED) is False
    assert engine.decide_hook(0x26, 0x48, LLKHF_EXTENDED) is True  # real remote event swallowed
    engine.record_raw_keyboard(1001, 0x48, True, False)
    assert engine.decide_hook(0x26, 0x48, LLKHF_EXTENDED | LLKHF_UP) is True

    # 4. Unwatched key never triggers wait_and_drain and always passes through.
    waited = []
    assert engine.decide_hook(0x42, 0x30, 0, wait_and_drain=lambda: (waited.append(True), False)[1]) is False
    assert not waited

    # 5. Sub-millisecond WM_INPUT lag resolved by wait_and_drain; timeout strictly fails open.
    now[0] += 0.100
    def late_remote():
        engine.record_raw_keyboard(1001, 30, False, True)
        return True
    assert engine.decide_hook(0x41, 30, 0, wait_and_drain=late_remote) is True
    engine.record_raw_keyboard(1001, 30, False, False)
    assert engine.decide_hook(0x41, 30, LLKHF_UP) is True

    now[0] += 0.100
    assert engine.decide_hook(0x41, 30, 0, wait_and_drain=lambda: False) is False  # fail-open!

    # 6. Consumer Control keys (Volume+ / Back) swallowed only from remote, passed from laptop keyboard.
    now[0] += 0.100
    assert engine.record_raw_consumer(1002, "hid:12:233", True) is True
    assert engine.decide_hook(0xAF, 0, 0) is True
    assert engine.record_raw_consumer(1002, "hid:12:233", False) is True
    assert engine.decide_hook(0xAF, 0, LLKHF_UP) is True

    now[0] += 0.020
    assert engine.record_raw_consumer(8888, "hid:12:233", True) is False
    assert engine.decide_hook(0xAF, 0, 0) is False
    assert engine.record_raw_consumer(8888, "hid:12:233", False) is False
    assert engine.decide_hook(0xAF, 0, LLKHF_UP) is False


class FakeWinSuppressor:
    def __init__(self, *, allow=True, error="钩子启动被拒绝"):
        self.allow = allow
        self._error = error
        self.active = False
        self.error = ""
        self.started = []
        self.synced = []
        self.cleared = 0
        self.alive = True
        self._on_edge = None
        self._on_removed = None
        self._watch = WatchSet()

    @property
    def suppressed_count(self):
        return self._watch.count if self.active else 0

    def start(self, *, allowed_handles, triggers, on_edge, on_removed, keyboard_muted=False):
        self._watch = WatchSet(triggers)
        self.started.append((set(allowed_handles), self._watch.triggers, keyboard_muted))
        if not self.allow or self._watch.count == 0:
            self.active = False
            self.error = "" if self._watch.count == 0 else self._error
            return False
        self.active = True
        self.error = ""
        self._on_edge = on_edge
        self._on_removed = on_removed
        return True

    def sync(self, triggers):
        watch = WatchSet(triggers)
        self.synced.append(watch.triggers)
        if watch.triggers == self._watch.triggers:
            return False
        self._watch = watch
        return True

    def ensure(self):
        if not self.active:
            return False
        if not self.alive:
            self.active = False
            self.error = "原按键屏蔽线程已停止"
            return False
        return True

    def clear(self):
        if self.active:
            self.cleared += 1
        self.active = False
        self.error = ""
        self._on_edge = None
        self._on_removed = None


def _make_probe(monkeypatch, *, suppressor):
    ble = WindowsRemoteIdentity(("bluetooth", "remote", 0x620A, 0x0407, 2), "蓝牙遥控器", "蓝牙", 0x620A, 0x0407, 2)
    registrations = []

    class FakeNative:
        entries = {30: (1, ble), 31: (2, ble)}

        def GetRawInputDeviceList(self, buf, count, _size):
            count._obj.value = len(self.entries)
            if buf is not None:
                for i, (h, (kind, _)) in enumerate(self.entries.items()):
                    buf[i] = windows_probe._RawDevice(h, kind)
            return len(self.entries)

        def GetRawInputDeviceInfoW(self, handle, op, buf, size):
            if op == 0x2000000b:
                buf._obj.page, buf._obj.usage = 12, 1
                return size._obj.value
            text = str(handle)
            size._obj.value = len(text) + 1
            if buf is not None:
                buf.value = text
            return len(text)

        def RegisterRawInputDevices(self, entries, count, _size):
            registrations.append([entries[i].flags for i in range(count)])
            return True

        def GetRawInputData(self, *_):
            return 0

        def winusb_receivers(self):
            return []

        def for_interface(self, path, _pair):
            return self.entries[int(path)][1]

    native = FakeNative()
    names = ("GetRawInputDeviceList", "GetRawInputDeviceInfoW", "RegisterRawInputDevices", "GetRawInputData")
    user = SimpleNamespace(**{n: lambda *a, n=n: getattr(native, n)(*a) for n in names})
    monkeypatch.setattr(windows_input.c, "WinDLL", lambda *a, **kw: user, raising=False)
    monkeypatch.setattr(windows_probe, "WindowsProbeMetadata", lambda: native)
    adapter = windows_probe.WindowsHIDProbe(suppressor=suppressor)
    return adapter, registrations


def test_windows_probe_daily_use_activates_suppressor_and_syncs_triggers(qapp, monkeypatch):
    sup = FakeWinSuppressor(allow=True)
    adapter, registrations = _make_probe(monkeypatch, suppressor=sup)
    try:
        device = adapter.enumerate()[0]
        events = []
        adapter.take_over = True
        adapter.take_over_triggers = ("hid:7:82", "hid:12:233")
        assert adapter.start(device.id, lambda *a: events.append(a), lambda: None, expected=device) == (2, 2)
        # Suppressor owns Raw Input so Qt widget must NOT register a conflicting sink.
        assert not registrations
        assert adapter.seized == 2 and not adapter.seize_error
        assert sup.started == [({30, 31}, ("hid:7:82", "hid:12:233"), False)]

        # Forwarded remote edge reaches callback.
        sup._on_edge(30, "hid:7:82", True)
        sup._on_edge(30, "hid:7:82", False)
        assert events == [((30, 0), "hid:7:82", True), ((30, 0), "hid:7:82", False)]

        # Dynamic trigger sync updates seized count.
        assert adapter.sync_takeover(("hid:7:82", "hid:12:233", "hid:12:234")) is True
        assert adapter.seized == 3

        # Dead thread detection surfaces error cleanly.
        sup.alive = False
        assert adapter.ensure_takeover() is True
        assert adapter.seized == 0 and "已停止" in adapter.seize_error
    finally:
        adapter.stop()
    assert sup.cleared == 0 or not sup.active


def test_windows_probe_falls_back_to_shared_raw_input_on_hook_failure_and_stays_shared_in_add_dialog(qapp, monkeypatch):
    sup = FakeWinSuppressor(allow=False, error="安全软件拦截了低级钩子")
    adapter, registrations = _make_probe(monkeypatch, suppressor=sup)
    try:
        device = adapter.enumerate()[0]
        # 1. Daily use when hook is denied -> graceful fallback to shared Raw Input + explicit seize_error.
        adapter.take_over = True
        adapter.take_over_triggers = ("hid:7:82",)
        assert adapter.start(device.id, lambda *_: None, lambda: None, expected=device) == (2, 2)
        assert adapter.registered is True and len(registrations) == 1
        assert adapter.seized == 0 and adapter.seize_error == "安全软件拦截了低级钩子"
        adapter.stop()

        # 2. Add dialog (take_over=False) -> never starts suppressor.
        sup.started.clear()
        adapter.take_over = False
        adapter.take_over_triggers = ()
        assert adapter.start(device.id, lambda *_: None, lambda: None, expected=device) == (2, 2)
        assert not sup.started and adapter.registered is True and adapter.seized == 0 and not adapter.seize_error
    finally:
        adapter.stop()


def test_windows_metadata_detects_generic_hid_winusb_key_interface_and_skips_mouse():
    metadata = WindowsProbeMetadata.__new__(WindowsProbeMetadata)
    metadata.cfg = SimpleNamespace(CM_Get_Child="child", CM_Get_Sibling="sibling")
    metadata._relate = lambda kind, node: {("child", 10): 11, ("sibling", 11): 12}.get((kind, node))
    metadata._instance = lambda node: (
        r"USB\VID_1234&PID_5678&MI_00\inst" if node == 11 else r"USB\VID_1234&PID_5678&MI_01\inst"
    )
    metadata._text = lambda node, *_: "WinUSB"
    metadata._container = lambda _: "c1"
    metadata._name = lambda _: "通用 2.4G 接收器"

    def fake_prop(node, _guid, prop, _kind):
        if prop == 17:
            return (0x10).to_bytes(4, "little")
        if prop == 3:
            return "USB\\VID_1234&PID_5678&REV_0100".encode("utf-16-le")
        if prop == 4:
            # MI_00 is HID keyboard; MI_01 is HID boot mouse (must be skipped!)
            return (
                "USB\\Class_03&SubClass_01&Prot_01" if node == 11 else "USB\\Class_03&SubClass_01&Prot_02"
            ).encode("utf-16-le")
        return b""

    metadata._node_property = fake_prop
    info = metadata._usb(10, r"USB\VID_1234&PID_5678\SER01", (0x1234, 0x5678))
    assert info.winusb_interfaces == (0,)


def test_voice_private_boundary_in_enrollment_ui_and_ai_request(qtbot, tmp_path):
    from remotehub.controller import AppController
    from remotehub.ui.enrollment_voice import VoiceEnrollment
    from remotehub.ui.voice import VoicePage

    buttons = [
        Button(name="上", trigger="hid:7:82", role="up"),
        Button(name="语音", trigger="hid:7:62", role="voice"),
    ]
    remote = Remote(
        name="通用蓝牙遥控器",
        connections=[Connection("蓝牙", 0x620A, 0x0407)],
        profiles=[Profile(name="默认", vendor=0x620A, product=0x0407, buttons=buttons)],
    )
    store = RemoteLibraryStore(tmp_path)
    store.add(remote)
    ctrl = AppController(tmp_path, store=store, auto_mapping=False)
    try:
        dev = ProbeDevice(("hid", 1), "蓝牙遥控器", "蓝牙", 0x620A, 0x0407)
        guide = VoiceEnrollment(ctrl, lambda: ctrl.remote, lambda: dev, lambda: None)
        qtbot.addWidget(guide)
        guide.show()

        # 「仅触发识别」 (chosen on the voice page) lists the role="voice" key in the key picker.
        assert ctrl.update_voice(voice_source="trigger")
        labels = [guide.key_picker.itemText(i) for i in range(guide.key_picker.count())]
        assert any("语音（语音区）" in text for text in labels)

        # A marked voice key without a built-in protocol copies the private-voice AI help.
        guide.copy_ai.click()
        copied = QApplication.clipboard().text()
        assert "遥控器私有语音通道" in copied and "FIBER0D" in copied and "620A:0407" in copied

        # VoicePage says plainly that this remote's own microphone is not usable yet.
        page = VoicePage(ctrl)
        qtbot.addWidget(page)
        assert "遥控器麦克风目前用不了" in page.route.text() and "电脑麦克风" in page.route.text()

        # Direct ai_request with voice_private includes proven history and directions.
        req = ai_request(step="native_audio_not_available", case="voice_private", device=dev)
        assert "遥控器私有语音通道" in req and "可以尝试的方向" in req
    finally:
        ctrl.shutdown()
