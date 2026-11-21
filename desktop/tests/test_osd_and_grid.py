"""Tests for Cheat Sheet OSD and 3x3 On-Screen Grid Launcher."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QScrollArea
import pytest

from remotehub.app_layers import LayerApp
from remotehub.controller import AppController
from remotehub.model import Action, GRID_SIZE, GridCell, Profile
from remotehub.presentation import action_text, grid_cell_texts, palette
from remotehub.store import ConfigStore
from remotehub.ui import CheatSheetOverlay, GridEditorDialog, GridLauncherOverlay, MainWindow
from remotehub.ui.osd import cheat_callout_text


def _nine_cells(**overrides: GridCell) -> list[GridCell]:
    """Build a 9-cell list with 1-based cell overrides (e.g. c1=..., c5=...)."""
    cells = [GridCell() for _ in range(GRID_SIZE)]
    for key, cell in overrides.items():
        idx = int(key.removeprefix("c")) - 1
        cells[idx] = cell
    return cells


def _trigger_for(controller: AppController, button_id: str) -> str:
    return next(b.trigger for b in controller.profile.buttons if b.id == button_id)


# -- 1. Model, Validation & Persistence --------------------------------------


def test_grid_action_validation_and_roundtrip(tmp_path):
    cells = _nine_cells(
        c1=GridCell("代码审查", Action("text", text="请帮我审查当前改动")),
        c5=GridCell("命令面板", Action("shortcut", ["cmd", "shift", "p"])),
        c9=GridCell("", Action("common", target="copy")),
    )
    grid = Action("grid", target="AI 工作台", cells=cells)
    grid.validate("darwin")
    grid.validate("win32")

    # Reject empty 9-cell grid
    with pytest.raises(ValueError, match="至少为一个格位"):
        Action("grid", target="空面板", cells=[GridCell() for _ in range(GRID_SIZE)]).validate()

    # Reject wrong slot count
    with pytest.raises(ValueError, match="9 个格位"):
        Action("grid", cells=[GridCell(action=Action("common", target="copy"))]).validate()

    # Reject nested grid, hold, voice, cheat_sheet, app_switcher inside cells
    for bad_action in (
        Action("grid", cells=cells),
        Action("cheat_sheet"),
        Action("hold", keys=["cmd"], target="hold"),
        Action("voice"),
        Action("common", target="app_switcher"),
    ):
        with pytest.raises(ValueError, match="不支持"):
            Action("grid", cells=_nine_cells(c1=GridCell("坏格位", bad_action))).validate()

    # Reject non-grid action carrying cells
    with pytest.raises(ValueError, match="非九宫格"):
        Action("common", target="copy", cells=cells).validate()

    # Roundtrip through Profile JSON and ConfigStore (both Base Layer and AppLayer)
    store = ConfigStore(tmp_path)
    store.load()
    profile = store.profiles[0]
    menu_btn = next(b for b in profile.buttons if b.id == "tv-menu")
    menu_btn.action = grid
    menu_btn.long_action = Action("cheat_sheet")

    from remotehub.model import AppLayer
    app_grid = Action(
        "grid",
        target="浏览器面板",
        cells=_nine_cells(c2=GridCell("新标签", Action("shortcut", ["cmd", "t"]))),
    )
    profile.app_layers.append(
        AppLayer(
            app_id="com.google.Chrome",
            name="Google Chrome",
            overrides={"tv-menu": {"single": app_grid}},
        )
    )
    dumped = profile.dumps()
    restored_profile = Profile.loads(dumped)
    assert restored_profile.buttons == profile.buttons
    assert restored_profile.app_layers == profile.app_layers

    store.save()
    reloaded = ConfigStore(tmp_path)
    reloaded.load()
    reloaded_menu = next(b for b in reloaded.profiles[0].buttons if b.id == "tv-menu")
    assert reloaded_menu.action == grid
    assert reloaded_menu.long_action == Action("cheat_sheet")
    assert reloaded.profiles[0].app_layers[0].overrides["tv-menu"]["single"] == app_grid


def test_palette_exposes_grid_and_cheat_sheet_across_platforms():
    for plat in ("darwin", "win32"):
        flat = [ident for _, items in palette(plat) for ident, _ in items]
        assert flat.count("grid") == 1
        assert flat.count("cheat_sheet") == 1

    named_grid = Action(
        "grid",
        target="常用指令",
        cells=_nine_cells(c1=GridCell("解释代码", Action("text", text="请解释这段代码的边界条件"))),
    )
    unnamed_grid = Action(
        "grid",
        cells=_nine_cells(
            c1=GridCell("", Action("text", text="第一段提示词")),
            c2=GridCell("", Action("toggle_app", target=r"C:\Program Files\Microsoft VS Code\Code.exe")),
        ),
    )
    assert action_text(named_grid) == "九宫格 · 常用指令"
    assert action_text(unnamed_grid) == "九宫格面板（2 格）"

    assert grid_cell_texts(named_grid.cells[0]) == ("解释代码", "输入文本 · 请解释这段代码的边界条件")
    assert grid_cell_texts(unnamed_grid.cells[0]) == ("第一段提示词", "输入文本")
    assert grid_cell_texts(unnamed_grid.cells[1]) == ("Code.exe", "打开 / 收起 App")
    assert grid_cell_texts(unnamed_grid.cells[8]) == ("空槽位", "未设置")


# -- 2. Cheat Sheet Runtime & Gestures ---------------------------------------


def test_cheat_sheet_long_press_hold_to_peek_hides_on_release(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    shortcuts = []
    monkeypatch.setattr(c.keys, "shortcut", shortcuts.append)

    c.select("tv-menu")
    assert c.assign(Action("shortcut", ["cmd", "m"]), "single")
    assert c.assign(Action("cheat_sheet"), "long")

    menu_trig = _trigger_for(c, "tv-menu")
    c.on_key(menu_trig, True)
    assert not c.overlays.cheat_open

    # Fire 550ms long-press timer while key is still physically held
    c.gestures[menu_trig].long_timer.timeout.emit()
    assert c.overlays.cheat_open and not c.overlays.cheat_pinned

    # Releasing immediately after long-press popup MUST close ("松手即隐"), never pin or fire single
    c.on_key(menu_trig, False)
    assert not c.overlays.cheat_open
    assert shortcuts == []
    c.shutdown()


def test_cheat_sheet_single_hold_vs_quick_tap_and_passthrough(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    now = [100.0]
    c.overlays.clock = lambda: now[0]
    shortcuts = []
    monkeypatch.setattr(c.keys, "shortcut", shortcuts.append)

    c.select("tv-menu")
    assert c.assign(Action("cheat_sheet"))
    c.select("tv-home")
    assert c.assign(Action("shortcut", ["cmd", "c"]))

    menu_trig = _trigger_for(c, "tv-menu")
    home_trig = _trigger_for(c, "tv-home")
    back_trig = _trigger_for(c, "tv-back")

    # 1) Hold >= 220ms on single action -> hides immediately on release
    c.on_key(menu_trig, True)
    assert c.overlays.cheat_open and not c.overlays.cheat_pinned
    now[0] += 0.30
    c.on_key(menu_trig, False)
    assert not c.overlays.cheat_open

    # 2) Quick tap < 220ms -> pins open for comfortable reading
    c.on_key(menu_trig, True)
    now[0] += 0.05
    c.on_key(menu_trig, False)
    assert c.overlays.cheat_open and c.overlays.cheat_pinned

    # Pressing another mapped action key closes Cheat Sheet AND executes that key immediately
    c.on_key(home_trig, True)
    c.on_key(home_trig, False)
    assert not c.overlays.cheat_open
    assert shortcuts == [["cmd", "c"]]

    # 3) Re-pin and dismiss with tv-back without executing any action
    c.on_key(menu_trig, True)
    now[0] += 0.05
    c.on_key(menu_trig, False)
    assert c.overlays.cheat_open and c.overlays.cheat_pinned
    c.on_key(back_trig, True)
    c.on_key(back_trig, False)
    assert not c.overlays.cheat_open
    assert shortcuts == [["cmd", "c"]]
    c.shutdown()


def test_cheat_sheet_reflects_active_app_layer_and_live_switch(qapp, tmp_path):
    c = AppController(tmp_path, auto_mapping=False)
    now = [50.0]
    c.overlays.clock = lambda: now[0]
    front = [LayerApp("com.apple.finder", "Finder")]
    c.foreground_apps._provider = lambda: front[0]

    c.select("tv-home")
    assert c.assign(Action("common", target="copy"))
    c.select("tv-menu")
    assert c.assign(Action("cheat_sheet"))

    layer = c.add_app_layer(LayerApp("com.google.Chrome", "Chrome"))
    assert layer is not None
    c.select("tv-ok")
    assert c.assign(Action("shortcut", ["cmd", "enter"]))
    c.select_editing_layer("")

    menu_trig = _trigger_for(c, "tv-menu")
    c.on_key(menu_trig, True)
    now[0] += 0.05
    c.on_key(menu_trig, False)  # quick tap -> pinned
    assert c.overlays.cheat_open and c.overlays.cheat_layer is None
    assert c.overlays.cheat_app_name == "Finder"

    # Switching foreground app updates the pinned Cheat Sheet live
    front[0] = LayerApp("com.google.Chrome", "Chrome")
    c.foreground_apps.changed.emit("com.google.Chrome", "Chrome")
    assert c.overlays.cheat_layer == layer
    assert c.overlays.cheat_app_name == "Chrome"
    ok_btn = next(b for b in c.profile.buttons if b.id == "tv-ok")
    home_btn = next(b for b in c.profile.buttons if b.id == "tv-home")
    ok_text, ok_state = cheat_callout_text(c.profile, ok_btn, c.overlays.cheat_layer)
    home_text, home_state = cheat_callout_text(c.profile, home_btn, c.overlays.cheat_layer)
    assert ok_text.startswith("★ ") and ok_state == "override"
    assert not home_text.startswith("★ ") and home_state == "assigned"
    c.shutdown()


# -- 3. 3x3 Grid Launcher Runtime & App Layers -------------------------------


def test_grid_launcher_number_keys_and_dpad_navigation(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    events = []
    monkeypatch.setattr(c.keys, "input_text", lambda text: events.append(("text", text)))
    monkeypatch.setattr(c.keys, "shortcut", lambda keys: events.append(("shortcut", list(keys))))

    # Give tv-number-1 a base action to prove Grid consumes number-1's press & release cleanly
    c.select("tv-number-1")
    assert c.assign(Action("shortcut", ["cmd", "1"]))

    grid = Action(
        "grid",
        target="AI 面板",
        cells=_nine_cells(
            c1=GridCell("总结", Action("text", text="请总结要点")),
            c2=GridCell("搜索", Action("shortcut", ["cmd", "k"])),
            c5=GridCell("全选", Action("shortcut", ["cmd", "a"])),
        ),
    )
    c.select("tv-m")
    assert c.assign(grid)

    m_trig = _trigger_for(c, "tv-m")
    num1_trig = _trigger_for(c, "tv-number-1")
    num9_trig = _trigger_for(c, "tv-number-9")
    up_trig = _trigger_for(c, "tv-up")
    right_trig = _trigger_for(c, "tv-right")
    left_trig = _trigger_for(c, "tv-left")
    ok_trig = _trigger_for(c, "tv-ok")
    back_trig = _trigger_for(c, "tv-back")

    # Open Grid -> starts at center cell 5 (index 4)
    c.on_key(m_trig, True)
    c.on_key(m_trig, False)
    assert c.overlays.grid_open and c.overlays.grid_selected == 4

    # Direct number key 1 executes cell 1 immediately and absorbs release
    c.on_key(num1_trig, True)
    assert not c.overlays.grid_open
    assert events == [("text", "请总结要点")]
    c.on_key(num1_trig, False)
    assert events == [("text", "请总结要点")]

    # Reopen and navigate with D-pad: center(4) -> up(1) -> up(clamped 1) -> right(2) -> left(1) -> OK
    c.on_key(m_trig, True)
    c.on_key(m_trig, False)
    for trig in (up_trig, up_trig, right_trig, left_trig):
        c.on_key(trig, True)
        c.on_key(trig, False)
    assert c.overlays.grid_selected == 1
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert not c.overlays.grid_open
    assert events[-1] == ("shortcut", ["cmd", "k"])

    # Empty cell shows notice without closing; same trigger or tv-back closes cleanly
    c.on_key(m_trig, True)
    c.on_key(m_trig, False)
    c.on_key(num9_trig, True)
    c.on_key(num9_trig, False)
    assert c.overlays.grid_open and "尚未设置动作" in c.overlays.grid_notice
    c.on_key(back_trig, True)
    c.on_key(back_trig, False)
    assert not c.overlays.grid_open
    c.shutdown()


def test_grid_launcher_per_app_layer_and_voice_interrupt(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    texts = []
    monkeypatch.setattr(c.keys, "input_text", texts.append)
    front = [LayerApp("com.apple.finder", "Finder")]
    c.foreground_apps._provider = lambda: front[0]

    base_grid = Action(
        "grid",
        target="全局九宫格",
        cells=_nine_cells(c1=GridCell("全局一", Action("text", text="GLOBAL_1"))),
    )
    cursor_grid = Action(
        "grid",
        target="Cursor 专属九宫格",
        cells=_nine_cells(c1=GridCell("写单测", Action("text", text="CURSOR_TEST_PROMPT"))),
    )
    c.select("tv-m")
    assert c.assign(base_grid)

    layer = c.add_app_layer(LayerApp("com.cursor.Editor", "Cursor"))
    assert layer is not None
    c.select("tv-m")
    assert c.assign(cursor_grid)
    c.select_editing_layer("")

    m_trig = _trigger_for(c, "tv-m")
    num1_trig = _trigger_for(c, "tv-number-1")
    voice_trig = _trigger_for(c, "tv-voice")

    c.on_key(m_trig, True)
    c.on_key(m_trig, False)
    assert c.overlays.grid_action == base_grid and c.overlays.grid_layer_name == ""
    c.on_key(num1_trig, True)
    c.on_key(num1_trig, False)
    assert texts == ["GLOBAL_1"]

    front[0] = LayerApp("com.cursor.Editor", "Cursor")
    c.foreground_apps.changed.emit("com.cursor.Editor", "Cursor")
    c.on_key(m_trig, True)
    c.on_key(m_trig, False)
    assert c.overlays.grid_action == cursor_grid and c.overlays.grid_layer_name == "Cursor"

    # Dedicated voice key always interrupts and closes any active overlay
    c.on_key(voice_trig, True)
    assert not c.overlays.grid_open
    c.on_key(voice_trig, False)
    c.shutdown()


# -- 4. Offscreen Qt UI & Editor Tests ---------------------------------------


def test_osd_widgets_are_non_activating_and_mouse_clickable(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    texts = []
    monkeypatch.setattr(c.keys, "input_text", texts.append)

    for osd in (window.cheat_sheet_osd, window.grid_launcher_osd):
        assert isinstance(osd, (CheatSheetOverlay, GridLauncherOverlay))
        assert bool(osd.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        assert osd.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert osd.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert osd.windowOpacity() == 1.0

    # Show & hide CheatSheetOverlay, including 15s timeout auto-close
    c.overlays.run_cheat_sheet("trig", True, profile=c.profile, layer=None, app_name="Safari")
    assert window.cheat_sheet_osd.isVisible()
    assert "Safari" in window.cheat_sheet_osd.layer_pill.text()
    assert c.overlays.cheat_timer.isActive()
    assert c.overlays.cheat_timer.interval() == c.overlays.CHEAT_TIMEOUT_MS
    c.overlays.cheat_timer.timeout.emit()
    assert not c.overlays.cheat_open
    assert not window.cheat_sheet_osd.isVisible()

    # Show GridLauncherOverlay, verify 20s timeout auto-close, then reopen & click cell 1
    grid = Action(
        "grid",
        target="快捷指令面板",
        cells=_nine_cells(c1=GridCell("发送周报", Action("text", text="本周进展顺利"))),
    )
    c.overlays.open_grid(grid, "trig", layer_name="企业微信")
    assert window.grid_launcher_osd.isVisible()
    assert window.grid_launcher_osd.heading.text() == "快捷指令面板"
    assert "企业微信" in window.grid_launcher_osd.layer_pill.text()
    assert c.overlays.grid_timer.isActive()
    assert c.overlays.grid_timer.interval() == c.overlays.GRID_TIMEOUT_MS
    c.overlays.grid_timer.timeout.emit()
    assert not c.overlays.grid_open
    assert not window.grid_launcher_osd.isVisible()

    c.overlays.open_grid(grid, "trig", layer_name="企业微信")
    assert window.grid_launcher_osd.isVisible()

    qtbot.mouseClick(window.grid_launcher_osd.cells[0], Qt.MouseButton.LeftButton)
    assert not window.grid_launcher_osd.isVisible()
    assert texts == ["本周进展顺利"]
    window.shutdown()


def test_cocoa_osd_uses_nonactivating_panel_mask(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication
    if QApplication.platformName() != "cocoa":
        pytest.skip("Cocoa-only NSPanel verification")
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.overlays.run_cheat_sheet("trig", True, profile=c.profile, layer=None, app_name="TextEdit")
    ns_win = window.cheat_sheet_osd._native_ns_window()
    assert ns_win is not None
    assert bool(ns_win.styleMask() & (1 << 7))  # NSWindowStyleMaskNonactivatingPanel
    c.overlays.close_cheat_sheet()
    window.shutdown()


def test_osd_open_and_close_animations_and_reopen_mid_close(qtbot, tmp_path):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    osd = window.grid_launcher_osd
    osd.animated = True
    osd.OPEN_DURATION_MS = 60
    osd.CLOSE_DURATION_MS = 50

    grid = Action(
        "grid",
        target="动画九宫格",
        cells=_nine_cells(c5=GridCell("居中格", Action("shortcut", ["enter"]))),
    )
    c.overlays.open_grid(grid, "trig")
    assert osd.isVisible()
    assert osd._presenting and not osd._closing
    qtbot.waitUntil(lambda: osd.windowOpacity() >= 0.99, timeout=500)
    assert osd.pos() == osd._target_pos

    # Start close animation, then reopen mid-close -> cancels close and returns to full opacity
    c.overlays.close_grid()
    assert osd._closing and osd.isVisible()
    c.overlays.open_grid(grid, "trig")
    assert osd._presenting and not osd._closing
    qtbot.waitUntil(lambda: osd.windowOpacity() >= 0.99, timeout=500)

    # Full close animation hides the window cleanly at completion
    c.overlays.close_grid()
    assert osd._closing
    qtbot.waitUntil(lambda: not osd.isVisible(), timeout=500)
    assert not osd._closing and not osd._presenting
    window.shutdown()


def test_inspector_mini_grid_and_grid_editor_dialog_workflow(qtbot, tmp_path):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    window.resize(1200, 720)
    window.show()
    c = window.controller
    ins = window.inspector

    c.select("tv-menu")
    ins.choose_kind("grid")
    qtbot.wait(20)
    assert ins.grid_edit_button.isVisible()
    assert len(ins.grid_preview_buttons) == GRID_SIZE

    # Minimum 1200x720 window must still have zero horizontal clipping with mini-grid visible
    for area in window.findChildren(QScrollArea):
        if area.isVisible():
            assert area.horizontalScrollBar().maximum() == 0
            assert area.widget().width() <= area.viewport().width()

    dialog = GridEditorDialog(None, window, button_name="菜单")
    qtbot.addWidget(dialog)
    dialog.show()

    # All-empty grid fails validation inline without closing
    assert not dialog.accept_grid()
    assert dialog.error_label.isVisible() and "至少为一个格位" in dialog.error_label.text()

    # Configure slot 1 (text) and slot 5 (shortcut) via UI widgets
    dialog.panel_title.setText("研发常用面板")
    dialog.select_slot(0)
    dialog.cell_title.setText("解释报错")
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("text"))
    dialog.cell_text.setPlainText("请分析以下堆栈并给出最小修复方案：")

    dialog.select_slot(4)
    dialog.cell_title.setText("全局搜索")
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("shortcut"))
    dialog.cell_sequence.setKeySequence(QKeySequence("Ctrl+Shift+F"))

    assert dialog.accept_grid()
    saved = dialog.result_action()
    assert saved is not None and saved.kind == "grid" and saved.target == "研发常用面板"
    assert saved.cells[0].title == "解释报错" and saved.cells[0].action.kind == "text"
    assert saved.cells[4].title == "全局搜索" and saved.cells[4].action.kind == "shortcut"

    assert c.assign(saved)
    assert ins.kinds["grid"].isChecked()
    assert "2 / 9 格" in ins.grid_status.text()

    # Undo restores previous empty action cleanly
    assert c.undo_action()
    assert next(b for b in c.profile.buttons if b.id == "tv-menu").action == Action()
    window.shutdown()


def test_cycle_profile_smart_sync_conflict_prompt_and_runtime_toast(qtbot, tmp_path, monkeypatch):
    from remotehub.model import MacroStep

    Action("cycle_profile").validate("darwin")
    Action("cycle_profile").validate("win32")
    with pytest.raises(ValueError, match="多余字段"):
        Action("cycle_profile", target="extra").validate()
    with pytest.raises(ValueError, match="不支持"):
        Action("macro", steps=[MacroStep(Action("cycle_profile"))]).validate()

    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    ins = window.inspector
    assert "cycle_profile" in ins.kinds

    # Build 3 same-hardware schemes: 方案 A, 方案 B, 方案 C
    c.profiles[0].name = "方案 A"
    scheme_b = Profile.loads(c.profiles[0].dumps())
    scheme_b.id = "scheme-b"
    scheme_b.name = "方案 B"
    next(b for b in scheme_b.buttons if b.id == "tv-ok").action = Action("text", text="OK_FROM_B")

    scheme_c = Profile.loads(c.profiles[0].dumps())
    scheme_c.id = "scheme-c"
    scheme_c.name = "方案 C"
    c.profiles.extend([scheme_b, scheme_c])
    c.save()
    c.profiles_changed.emit()

    # 1. Both B and C have tv-number-* unassigned -> auto-syncs without prompting
    c.select("tv-number-*")
    ins.choose_kind("cycle_profile")
    for prof in c.profiles:
        assert next(b for b in prof.buttons if b.id == "tv-number-*").action == Action("cycle_profile")

    # Undo restores all 3 schemes back to none
    assert c.undo_action()
    for prof in c.profiles:
        assert next(b for b in prof.buttons if b.id == "tv-number-*").action == Action("none")

    # 2. Conflict on 方案 C (already mapped to copy) -> Inspector prompts user
    next(b for b in scheme_c.buttons if b.id == "tv-number-*").action = Action("common", target="copy")
    c.save()

    # 2a. User cancels prompt -> nothing changes
    monkeypatch.setattr(ins, "_confirm_cycle_profile_conflicts", lambda btn, conflicts: None)
    ins.choose_kind("cycle_profile")
    assert next(b for b in c.profiles[0].buttons if b.id == "tv-number-*").action == Action("none")
    assert next(b for b in scheme_b.buttons if b.id == "tv-number-*").action == Action("none")
    assert next(b for b in scheme_c.buttons if b.id == "tv-number-*").action == Action("common", target="copy")

    # 2b. User chooses "保留原动作" (False) -> A & B get cycle_profile, C keeps copy
    monkeypatch.setattr(ins, "_confirm_cycle_profile_conflicts", lambda btn, conflicts: False)
    ins.choose_kind("cycle_profile")
    assert next(b for b in c.profiles[0].buttons if b.id == "tv-number-*").action == Action("cycle_profile")
    assert next(b for b in scheme_b.buttons if b.id == "tv-number-*").action == Action("cycle_profile")
    assert next(b for b in scheme_c.buttons if b.id == "tv-number-*").action == Action("common", target="copy")
    assert c.undo_action()

    # 2c. User chooses "同意覆盖并同步" (True) -> A, B, and C all get cycle_profile; undo restores C's copy
    captured_conflicts = []

    def approve_overwrite(btn, conflicts):
        captured_conflicts.extend(conflicts)
        return True

    monkeypatch.setattr(ins, "_confirm_cycle_profile_conflicts", approve_overwrite)
    ins.choose_kind("cycle_profile")
    assert len(captured_conflicts) == 1 and captured_conflicts[0][3] == "方案 C"
    for prof in c.profiles:
        assert next(b for b in prof.buttons if b.id == "tv-number-*").action == Action("cycle_profile")
    assert c.undo_action()
    assert next(b for b in scheme_c.buttons if b.id == "tv-number-*").action == Action("common", target="copy")

    # Re-apply with overwrite so all 3 schemes cycle A -> B -> C -> A
    ins.choose_kind("cycle_profile")

    # 3. Runtime cycling + auto-repeat protection + click-through HUD Toast
    stops = []
    texts = []
    monkeypatch.setattr(c.device, "stop", lambda: stops.append(1))
    monkeypatch.setattr(c.keys, "input_text", texts.append)
    c.enabled = True
    c.keys_ready = True

    star_trig = _trigger_for(c, "tv-number-*")
    ok_trig = _trigger_for(c, "tv-ok")

    # Press star in A -> switches to B (2/3), shows toast; auto-repeat while held does NOT switch again
    c.on_key(star_trig, True)
    assert c.index == 1
    assert c.overlays.toast_open
    assert window.profile_toast_osd.isVisible()
    assert window.profile_toast_osd.badge.text() == "2/3"
    assert window.profile_toast_osd.title_label.text() == "方案 B"
    c.on_key(star_trig, True)  # simulated HID auto-repeat while still held
    assert c.index == 1
    c.on_key(star_trig, False)
    assert c.index == 1

    # While toast is visible, pressing OK immediately executes B's action without being intercepted
    assert c.overlays.toast_open and not c.overlays.active
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert texts == ["OK_FROM_B"]

    # Press star in B -> C (3/3), then in C -> wraps back to A (1/3)
    c.on_key(star_trig, True)
    c.on_key(star_trig, False)
    assert c.index == 2
    assert window.profile_toast_osd.badge.text() == "3/3"
    assert window.profile_toast_osd.title_label.text() == "方案 C"

    c.on_key(star_trig, True)
    c.on_key(star_trig, False)
    assert c.index == 0
    assert window.profile_toast_osd.badge.text() == "1/3"
    assert window.profile_toast_osd.title_label.text() == "方案 A"
    assert stops == [] and c.enabled and c.keys_ready

    # Toast auto-closes on timeout
    c.overlays.toast_timer.timeout.emit()
    assert not c.overlays.toast_open
    assert not window.profile_toast_osd.isVisible()
    window.shutdown()
