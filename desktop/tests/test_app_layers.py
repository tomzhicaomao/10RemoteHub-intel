"""Tests for context-aware per-application keymap layers and F11 profile hot-swap."""
from __future__ import annotations

import json
import plistlib

import pytest
from PySide6.QtWidgets import QMenu

from remotehub.app_layers import ForegroundAppTracker, LayerApp, layer_app_from_path, match_app_layer
from remotehub.canvas import RemoteCanvas, callout_text
from remotehub.controller import AppController
from remotehub.gestures import KeyGesture
from remotehub.model import Action, AppLayer, Button, Profile, effective_button, normalize_app_layer_id, validate_app_layer_id
from remotehub.photo_template import apply_fixed_keys
from remotehub.store import ConfigStore
from remotehub.ui import MainWindow


def test_app_layer_id_validation_and_privacy_normalization(tmp_path):
    assert normalize_app_layer_id("  com.google.Chrome ") == "com.google.Chrome"
    assert normalize_app_layer_id("EXE:VLC.EXE") == "exe:vlc.exe"
    assert validate_app_layer_id("com.apple.Safari") == "com.apple.Safari"
    assert validate_app_layer_id("exe:mpv.exe") == "exe:mpv.exe"

    for invalid in (
        "",
        "/Applications/Safari.app",
        "exe:C:\\Program Files\\VideoLAN\\VLC\\vlc.exe",
        "exe:../vlc.exe",
        "org.remotehub.desktop",
        "Org.RemoteHub.Desktop",
        "exe:10RemoteHub.exe",
        "exe:remotehub.exe",
    ):
        with pytest.raises(ValueError):
            validate_app_layer_id(invalid)

    # macOS bundle path -> Bundle ID + display name, never persisting filesystem path
    bundle = tmp_path / "Firefox.app"
    (bundle / "Contents").mkdir(parents=True)
    with (bundle / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump(
            {"CFBundleIdentifier": "org.mozilla.firefox", "CFBundleDisplayName": "Firefox"},
            fh,
        )
    mac_app = layer_app_from_path(bundle, platform="darwin")
    assert mac_app == LayerApp("org.mozilla.firefox", "Firefox")

    # Windows executable path -> exe:<lowercase_basename>, stripping parent directories
    win_exe = tmp_path / "Program Files" / "VideoLAN" / "VLC.EXE"
    win_exe.parent.mkdir(parents=True)
    win_exe.write_bytes(b"MZ")
    win_app = layer_app_from_path(win_exe, platform="win32")
    assert win_app == LayerApp("exe:vlc.exe", "VLC")


def test_profile_app_layers_roundtrip_legacy_load_and_validation(tmp_path):
    store = ConfigStore(tmp_path)
    profile = store.profiles[0]
    ok_btn = next(b for b in profile.buttons if b.id == "tv-ok")
    ok_btn.action = Action("shortcut", ["enter"])
    ok_btn.long_action = Action("shortcut", ["esc"])

    layer = AppLayer(
        app_id="com.google.Chrome",
        name="Google Chrome",
        overrides={
            "tv-ok": {
                "single": Action("shortcut", ["space"]),
                "double": Action("none"),
            }
        },
    )
    profile.app_layers.append(layer)
    dumped = profile.dumps()
    loaded = Profile.loads(dumped)
    assert loaded == profile

    store.settings["app_layers_enabled"] = False
    store.save()
    raw = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
    assert set(raw) == {"version", "profiles", "settings"}
    assert raw["settings"]["app_layers_enabled"] is False

    reloaded = ConfigStore(tmp_path)
    reloaded.load()
    assert not reloaded.load_error
    assert reloaded.profiles[0] == profile
    assert reloaded.settings["app_layers_enabled"] is False

    # Legacy profile JSON without app_layers loads cleanly
    legacy_dict = json.loads(dumped)
    legacy_dict.pop("app_layers")
    legacy_loaded = Profile.loads(json.dumps(legacy_dict))
    assert legacy_loaded.app_layers == []

    # Duplicate app_id (case-insensitive) is rejected
    dup = Profile.loads(dumped)
    dup.app_layers.append(AppLayer(app_id="com.google.chrome", name="Chrome Copy"))
    with pytest.raises(ValueError, match="重复"):
        dup.validate()

    # Dedicated voice key and blocked keys cannot be overridden in AppLayer
    bad_voice = Profile.loads(dumped)
    bad_voice.app_layers[0].overrides["tv-voice"] = {"single": Action("shortcut", ["space"])}
    with pytest.raises(ValueError, match="固定按键"):
        bad_voice.validate()
    assert apply_fixed_keys(bad_voice) is True
    assert "tv-voice" not in bad_voice.app_layers[0].overrides
    bad_voice.validate()


def test_per_gesture_inheritance_explicit_mute_restore_and_undo(qapp, tmp_path):
    ctl = AppController(tmp_path, auto_mapping=False)
    try:
        ctl.select("tv-ok")
        assert ctl.assign(Action("shortcut", ["enter"]), "single")
        assert ctl.assign(Action("shortcut", ["tab"]), "double")
        assert ctl.assign(Action("shortcut", ["esc"]), "long")

        chrome = ctl.add_app_layer(LayerApp("com.google.Chrome", "Google Chrome"))
        assert chrome is not None
        assert ctl.editing_layer == chrome

        # Override only single; double and long inherit from Base Layer
        assert ctl.assign(Action("shortcut", ["space"]), "single")
        base_ok = next(b for b in ctl.profile.buttons if b.id == "tv-ok")
        eff = effective_button(base_ok, chrome)
        assert eff.action == Action("shortcut", ["space"])
        assert eff.double_action == Action("shortcut", ["tab"])
        assert eff.long_action == Action("shortcut", ["esc"])

        # Explicitly mute long press in Chrome ("在此应用无操作") without affecting Base Layer
        assert ctl.mute_in_layer("long")
        assert ctl.is_gesture_overridden("tv-ok", "long", chrome)
        assert ctl.layer_override_action("tv-ok", "long", chrome) == Action("none")
        eff = effective_button(base_ok, chrome)
        assert eff.long_action == Action("none")
        assert base_ok.long_action == Action("shortcut", ["esc"])

        # Clear in AppLayer restores Base Layer inheritance ("恢复继承全局")
        assert ctl.clear_action("long")
        assert not ctl.is_gesture_overridden("tv-ok", "long", chrome)
        assert effective_button(base_ok, chrome).long_action == Action("shortcut", ["esc"])

        # AL-03 regression: holding past LONG_PRESS when long_action is muted ("none")
        # and double_action is inherited must NOT fall back to single_action on release.
        executed_al03: list[list[str]] = []
        ctl.keys.shortcut = lambda keys: executed_al03.append(list(keys))
        ctl.select_editing_layer(chrome.id)
        ctl.select("tv-ok")
        assert ctl.mute_in_layer("long")
        ctl.select_editing_layer("")
        ctl.foreground_apps._provider = lambda: LayerApp("com.google.Chrome", "Google Chrome")
        ok_trig = next(b.trigger for b in ctl.profile.buttons if b.id == "tv-ok")
        ctl.on_key(ok_trig, True)
        ctl.gestures[ok_trig].long_timer.timeout.emit()
        ctl.on_key(ok_trig, False)
        assert executed_al03 == []
        assert not ctl.gestures[ok_trig].awaiting_second

        # Undo restores the explicit mute on Chrome layer
        ctl.select_editing_layer(chrome.id)
        ctl.select("tv-ok")
        assert ctl.clear_action("long")
        assert ctl.undo_action()
        assert ctl.editing_layer == chrome
        assert ctl.layer_override_action("tv-ok", "long", chrome) == Action("none")

        # Voice key cannot be overridden even when editing an AppLayer
        ctl.select("tv-voice")
        assert not ctl.assign(Action("shortcut", ["space"]), "single")
        assert "tv-voice" not in chrome.overrides
    finally:
        ctl.shutdown()


def test_app_layer_mutations_roll_back_on_save_failure(qapp, tmp_path, monkeypatch):
    ctl = AppController(tmp_path, auto_mapping=False)
    try:
        ctl.select("tv-ok")
        assert ctl.assign(Action("shortcut", ["enter"]), "single")
        vlc = ctl.add_app_layer(LayerApp("exe:vlc.exe", "VLC"))
        assert vlc is not None
        assert ctl.assign(Action("shortcut", ["space"]), "single")

        def fail_save():
            raise ValueError("模拟磁盘写入失败")

        monkeypatch.setattr(ctl.store, "save", fail_save)

        assert ctl.add_app_layer(LayerApp("com.apple.Safari", "Safari")) is None
        assert [layer.app_id for layer in ctl.profile.app_layers] == ["exe:vlc.exe"]

        assert not ctl.assign(Action("shortcut", ["f"]), "single")
        assert vlc.overrides["tv-ok"]["single"] == Action("shortcut", ["space"])

        assert not ctl.clear_action("single")
        assert vlc.overrides["tv-ok"]["single"] == Action("shortcut", ["space"])

        assert not ctl.rename_app_layer(vlc.id, "New VLC")
        assert vlc.name == "VLC"

        assert not ctl.set_app_layer_enabled(vlc.id, False)
        assert vlc.enabled is True

        assert not ctl.clear_app_layer_overrides(vlc.id)
        assert "tv-ok" in vlc.overrides

        assert not ctl.delete_app_layer(vlc.id)
        assert len(ctl.profile.app_layers) == 1

        assert not ctl.set_app_layers_enabled(False)
        assert ctl.app_layers_enabled is True
    finally:
        ctl.shutdown()


def test_runtime_foreground_switching_zero_disconnect_and_mid_press_safety(qapp, tmp_path, monkeypatch, qtbot):
    current_front = {"app": None}
    ctl = AppController(tmp_path, auto_mapping=False)
    try:
        ctl.foreground_apps = ForegroundAppTracker(ctl, live=False, provider=lambda: current_front["app"])
        ctl.foreground_apps.changed.connect(ctl._on_foreground_app_changed)

        executed: list[tuple[str, object]] = []
        held_events: list[tuple[str, str]] = []
        monkeypatch.setattr(ctl.keys, "shortcut", lambda keys: executed.append(("shortcut", list(keys))))
        monkeypatch.setattr(ctl.keys, "_send", lambda key, down, flags: held_events.append((key, "down" if down else "up")))

        stops = []
        monkeypatch.setattr(ctl.device, "stop", lambda: stops.append(1))
        ctl.enabled = True
        ctl.keys_ready = True

        # Base Layer: OK -> Enter, Left -> Hold Ctrl, Right -> single A / double B
        ctl.select("tv-ok")
        assert ctl.assign(Action("shortcut", ["enter"]), "single")
        ctl.select("tv-left")
        assert ctl.assign(Action("hold", ["ctrl"], target="hold"), "single")
        ctl.select("tv-right")
        assert ctl.assign(Action("shortcut", ["a"]), "single")
        assert ctl.assign(Action("shortcut", ["b"]), "double")

        # Chrome layer: OK -> Space, Right -> immediate C (no double gesture!)
        chrome = ctl.add_app_layer(LayerApp("com.google.Chrome", "Google Chrome"))
        assert chrome is not None
        ctl.select("tv-ok")
        assert ctl.assign(Action("shortcut", ["space"]), "single")
        ctl.select("tv-right")
        # Override double_action to mute ("none") and single to "c", so KeyGesture is NOT needed in Chrome!
        assert ctl.assign(Action("shortcut", ["c"]), "single")
        assert ctl.mute_in_layer("double")

        # VLC layer: OK -> muted ("none")
        vlc = ctl.add_app_layer(LayerApp("exe:vlc.exe", "VLC"))
        assert vlc is not None
        ctl.select("tv-ok")
        assert ctl.mute_in_layer("single")

        # Leave UI editing layer on Global while testing runtime foreground changes
        ctl.select_editing_layer("")

        ok_trigger = next(b.trigger for b in ctl.profile.buttons if b.id == "tv-ok")
        left_trigger = next(b.trigger for b in ctl.profile.buttons if b.id == "tv-left")
        right_trigger = next(b.trigger for b in ctl.profile.buttons if b.id == "tv-right")

        # 1. Global foreground -> Enter
        ctl.on_key(ok_trigger, True)
        ctl.on_key(ok_trigger, False)
        assert executed[-1] == ("shortcut", ["enter"])

        # 2. Switch to Chrome -> Space (and Right fires immediate "c" without 280ms double-click wait)
        current_front["app"] = LayerApp("com.google.Chrome", "Google Chrome")
        ctl.foreground_apps.changed.emit("com.google.Chrome", "Google Chrome")
        ctl.on_key(ok_trigger, True)
        ctl.on_key(ok_trigger, False)
        assert executed[-1] == ("shortcut", ["space"])

        ctl.on_key(right_trigger, True)
        ctl.on_key(right_trigger, False)
        assert executed[-1] == ("shortcut", ["c"])

        # 3. Switch to VLC -> OK is muted in VLC, so nothing new executes
        before_count = len(executed)
        current_front["app"] = LayerApp("exe:VLC.EXE", "VLC")
        ctl.foreground_apps.changed.emit("exe:vlc.exe", "VLC")
        ctl.on_key(ok_trigger, True)
        ctl.on_key(ok_trigger, False)
        assert len(executed) == before_count

        # 4. Global toggle off -> falls back to Base Layer (Enter) even while VLC is frontmost
        assert ctl.set_app_layers_enabled(False)
        ctl.on_key(ok_trigger, True)
        ctl.on_key(ok_trigger, False)
        assert executed[-1] == ("shortcut", ["enter"])
        assert ctl.set_app_layers_enabled(True)

        # 5. Mid-hold app switch safety: press Hold Ctrl in Global, switch to Chrome where tv-left is overridden, then release!
        ctl.select_editing_layer(chrome.id)
        ctl.select("tv-left")
        assert ctl.assign(Action("shortcut", ["j"]), "single")
        ctl.select_editing_layer("")

        current_front["app"] = None
        ctl.on_key(left_trigger, True)
        assert ctl.holds.active
        current_front["app"] = LayerApp("com.google.Chrome", "Google Chrome")
        ctl.foreground_apps.changed.emit("com.google.Chrome", "Google Chrome")
        ctl.on_key(left_trigger, False)
        assert not ctl.holds.active
        assert ("ctrl", "down") in held_events and ("ctrl", "up") in held_events

        # 6. Double-click back in Global still works after Chrome used immediate single
        current_front["app"] = None
        ctl.foreground_apps.changed.emit("", "")
        ctl.on_key(right_trigger, True)
        ctl.on_key(right_trigger, False)
        ctl.on_key(right_trigger, True)
        ctl.on_key(right_trigger, False)
        assert executed[-1] == ("shortcut", ["b"])

        # Zero USB disconnects happened across all switches
        assert stops == []
        assert ctl.enabled and ctl.keys_ready
    finally:
        ctl.shutdown()


def test_f11_same_hardware_profile_switch_keeps_capture_while_cross_device_or_import_drops(qapp, tmp_path, monkeypatch):
    ctl = AppController(tmp_path, auto_mapping=False)
    try:
        second = Profile.loads(ctl.profile.dumps())
        second.id = "profile-two"
        second.name = "演示模式"
        ctl.profiles.append(second)
        ctl.save()

        stops = []
        monkeypatch.setattr(ctl.device, "stop", lambda: stops.append(1))
        monkeypatch.setattr(ctl.keys, "_send", lambda key, down, flags: None)

        class DummyWinCollector:
            def __init__(self, profile):
                self.profile = profile

        ctl.device.windows = DummyWinCollector(ctl.profile)
        ctl.enabled = True
        ctl.keys_ready = True

        # Start a modifier hold before switching same-hardware profile
        ctl.select("tv-left")
        assert ctl.assign(Action("hold", ["shift"], target="hold"), "single")
        left_trigger = next(b.trigger for b in ctl.profile.buttons if b.id == "tv-left")
        ctl.on_key(left_trigger, True)
        assert ctl.holds.active

        # Same (vendor, product) switch -> keeps USB capture alive, clears holds, updates windows collector profile
        ctl.select_profile(1)
        assert ctl.index == 1
        assert ctl.enabled and ctl.keys_ready
        assert stops == []
        assert not ctl.holds.active
        assert ctl.device.windows.profile is second

        # AL-09: selected profile persists across controller restarts
        reopened = AppController(tmp_path, auto_mapping=False)
        try:
            assert reopened.index == 1
            assert reopened.profile.id == second.id
        finally:
            reopened.shutdown()

        # Different (vendor, product) switch -> drops USB capture cleanly
        third = Profile(id="custom-hid", name="Other HID", vendor=0x1234, product=0x5678, buttons=[Button(id="b1", name="K1")])
        ctl.profiles.append(third)
        ctl.select_profile(2)
        assert ctl.index == 2
        assert not ctl.enabled
        assert len(stops) == 1

        # Importing an external profile always drops live capture per security contract
        ctl.select_profile(0)
        ctl.enabled = True
        ctl.keys_ready = True
        exported = tmp_path / "exported.json"
        ctl.export_profile(exported)
        assert ctl.import_profile(exported)
        assert not ctl.enabled
        assert len(stops) == 2
    finally:
        ctl.shutdown()


def test_mapping_page_canvas_and_inspector_layer_ui(qapp, tmp_path):
    win = MainWindow(tmp_path, safe=True)
    try:
        win.resize(1200, 720)
        win.show()
        qapp.processEvents()

        c = win.controller
        mapping = win.mapping
        inspector = win.inspector
        canvas: RemoteCanvas = win.canvas

        c.select("tv-ok")
        assert c.assign(Action("shortcut", ["enter"]), "single")
        assert mapping.layer_box.count() == 1
        assert mapping.manage_layer_button.isHidden()
        assert mapping.auto_layer_check.isHidden()

        # Add app layer and verify UI transitions to editing that layer
        safari = c.add_app_layer(LayerApp("com.apple.Safari", "Safari"))
        assert safari is not None
        qapp.processEvents()

        assert mapping.layer_box.count() == 2
        assert mapping.layer_box.currentIndex() == 1
        assert not mapping.manage_layer_button.isHidden()
        assert not mapping.auto_layer_check.isHidden()
        assert mapping.count.text().startswith("已覆盖 0 / ")

        # Inherited key shows global action without ★ and state="empty", Inspector shows inheritance
        ok_btn = next(b for b in c.profile.buttons if b.id == "tv-ok")
        text, state = callout_text(ok_btn, layer=safari)
        assert not text.startswith("★ ")
        assert state == "empty"
        assert "继承全局" in inspector.summary.text()
        assert inspector.clear_current.text() == "恢复继承全局"
        assert not inspector.clear_current.isEnabled()
        assert not inspector.mute_in_layer.isHidden()
        assert inspector.mute_in_layer.isEnabled()

        # Click mute_in_layer ("在此应用无操作")
        inspector.mute_in_layer.click()
        qapp.processEvents()
        text, state = callout_text(ok_btn, layer=safari)
        assert text == "★ 确定  无操作"
        assert state == "assigned"
        assert "在此应用无操作" in inspector.summary.text()
        assert inspector.clear_current.isEnabled()
        assert not inspector.mute_in_layer.isEnabled()
        assert mapping.count.text().startswith("已覆盖 1 / ")

        # Click clear_current ("恢复继承全局")
        inspector.clear_current.click()
        qapp.processEvents()
        assert not c.is_gesture_overridden("tv-ok", "single", safari)
        assert "继承全局" in inspector.summary.text()

        # Populate add-layer menu cleanly in safe mode
        menu = QMenu()
        mapping.populate_add_layer_menu(menu)
        assert any("浏览应用程序" in act.text() for act in menu.actions())

        # Check no horizontal scrollbar overflow in inspector at minimum window size 1200x720
        assert canvas.width() >= 576
    finally:
        win.shutdown()
        win.close()


def test_foreground_tracker_windows_cache_and_custom_button_cleanup(qapp, tmp_path, monkeypatch):
    tracker = ForegroundAppTracker(live=True)
    calls = {"info": 0}
    hwnd_state = {"hwnd": 101, "pid": 4242, "exe": "C:\\Program Files\\VideoLAN\\VLC\\VLC.EXE"}

    class FakeDWORD:
        def __init__(self):
            self.value = 0

    class FakeWinHelper:
        class w:
            DWORD = FakeDWORD

        class c:
            @staticmethod
            def byref(obj):
                return obj

        class user:
            @staticmethod
            def GetForegroundWindow():
                return hwnd_state["hwnd"]

            @staticmethod
            def GetWindowThreadProcessId(hwnd, pid_ref):
                pid_ref.value = hwnd_state["pid"]

        @staticmethod
        def _info(hwnd):
            calls["info"] += 1

            class Info:
                executable = hwnd_state["exe"]

            return Info()

    tracker._win_helper = FakeWinHelper()
    monkeypatch.setattr("remotehub.app_layers.sys.platform", "win32")

    first = tracker.current_app()
    second = tracker.current_app()
    assert first == LayerApp("exe:vlc.exe", "VLC")
    assert second == first
    assert calls["info"] == 1  # second lookup hit (hwnd, pid) cache without querying process image again

    # Windows system host process is ignored
    hwnd_state.update(hwnd=202, pid=5000, exe="C:\\Windows\\System32\\ApplicationFrameHost.exe")
    assert tracker.current_app() is None
    tracker.close()

    # Removing a custom button also removes stale overrides from all AppLayers
    ctl = AppController(tmp_path, auto_mapping=False)
    try:
        ctl.profiles[0] = Profile(
            id="custom",
            name="Custom Remote",
            vendor=0x1111,
            product=0x2222,
            buttons=[Button(id="b1", name="键1"), Button(id="b2", name="键2")],
        )
        ctl.selected_id = "b1"
        layer = ctl.add_app_layer(LayerApp("com.apple.Safari", "Safari"))
        assert layer is not None
        assert ctl.assign(Action("shortcut", ["space"]), "single")
        assert "b1" in layer.overrides
        ctl.remove_button()
        assert "b1" not in layer.overrides
        ctl.profile.validate()
    finally:
        ctl.shutdown()


def test_window_physical_key_feedback_reflects_active_app_layer(qapp, tmp_path):
    win = MainWindow(tmp_path, safe=True)
    try:
        c = win.controller
        front = {"app": LayerApp("com.google.Chrome", "Google Chrome")}
        c.foreground_apps = ForegroundAppTracker(c, live=False, provider=lambda: front["app"])
        chrome = c.add_app_layer(front["app"])
        assert chrome is not None
        c.select("tv-ok")
        assert c.mute_in_layer("single")
        ok_trigger = next(b.trigger for b in c.profile.buttons if b.id == "tv-ok")
        win.show_physical_key(ok_trigger, True)
        assert "Google Chrome 层" in win.feedback.text()
        assert "无操作" in win.feedback.text()
    finally:
        win.shutdown()
        win.close()
