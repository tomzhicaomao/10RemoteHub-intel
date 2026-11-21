"""Tests for Multi-Step Action Chains (Macros): model, non-blocking runner, layers, grid, and UI."""
from __future__ import annotations

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QScrollArea
import pytest

from remotehub.actions import ActionRunner
from remotehub.app_layers import LayerApp
from remotehub.controller import AppController
from remotehub.model import (
    Action, AppLayer, GRID_SIZE, GridCell, MAX_MACRO_DELAY_MS,
    MAX_MACRO_STEPS, MacroStep, Profile,
)
from remotehub.presentation import action_text, grid_cell_texts, palette
from remotehub.store import ConfigStore
from remotehub.ui import GridEditorDialog, MacroEditorDialog, MainWindow


def _trigger_for(controller: AppController, button_id: str) -> str:
    return next(b.trigger for b in controller.profile.buttons if b.id == button_id)


def _nine_cells(**overrides: GridCell) -> list[GridCell]:
    cells = [GridCell() for _ in range(GRID_SIZE)]
    for key, cell in overrides.items():
        idx = int(key.removeprefix("c")) - 1
        cells[idx] = cell
    return cells


# -- 1. Model, Validation & Persistence --------------------------------------


def test_macro_validation_bounds_and_roundtrip(tmp_path):
    chain = Action(
        "macro",
        target="粘贴提示词并发送",
        steps=[
            MacroStep(Action("text", text="请帮我总结当前改动"), delay_ms=150),
            MacroStep(Action("shortcut", ["enter"]), delay_ms=0),
        ],
    )
    chain.validate("darwin")
    chain.validate("win32")

    # Max 8 steps allowed; 0 and 9 rejected
    eight = Action(
        "macro",
        steps=[MacroStep(Action("shortcut", ["enter"]), delay_ms=50) for _ in range(MAX_MACRO_STEPS)],
    )
    eight.validate("darwin")

    with pytest.raises(ValueError, match="1–8 个步骤"):
        Action("macro", steps=[]).validate()
    with pytest.raises(ValueError, match="1–8 个步骤"):
        Action("macro", steps=eight.steps + [MacroStep(Action("shortcut", ["esc"]))]).validate()

    # Delay bounds and strict int check (bool must be rejected)
    MacroStep(Action("shortcut", ["enter"]), delay_ms=0).validate()
    MacroStep(Action("shortcut", ["enter"]), delay_ms=MAX_MACRO_DELAY_MS).validate()
    for bad_delay in (-1, MAX_MACRO_DELAY_MS + 1, True, False, 12.5, "100"):
        with pytest.raises(ValueError, match="步骤等待时间"):
            MacroStep(Action("shortcut", ["enter"]), delay_ms=bad_delay).validate()  # type: ignore[arg-type]

    # Reject nested macro, overlays, hold, voice, none, and app_switcher inside MacroStep
    for forbidden in (
        Action("none"),
        chain,
        Action("grid", cells=_nine_cells(c1=GridCell("复制", Action("common", target="copy")))),
        Action("cheat_sheet"),
        Action("hold", keys=["cmd"], target="hold"),
        Action("voice"),
        Action("common", target="app_switcher"),
    ):
        with pytest.raises(ValueError, match="不支持"):
            Action("macro", steps=[MacroStep(forbidden)]).validate()

    # Non-macro carrying steps or macro carrying extra fields must fail
    with pytest.raises(ValueError, match="非动作链"):
        Action("shortcut", ["enter"], steps=[MacroStep(Action("shortcut", ["esc"]))]).validate()
    with pytest.raises(ValueError, match="多余字段"):
        Action("macro", keys=["enter"], steps=[MacroStep(Action("shortcut", ["esc"]))]).validate()
    with pytest.raises(ValueError, match="0–60 字"):
        Action("macro", target="x" * 61, steps=[MacroStep(Action("shortcut", ["esc"]))]).validate()

    # GridCell CAN hold a Macro cleanly (bounded DAG: Grid -> Macro -> Atomic Steps)
    grid_with_macro = Action(
        "grid",
        target="AI 面板",
        cells=_nine_cells(c1=GridCell("一键总结发送", chain)),
    )
    grid_with_macro.validate("darwin")
    grid_with_macro.validate("win32")

    # Roundtrip through Profile JSON and ConfigStore (Base Layer + AppLayer + GridCell)
    store = ConfigStore(tmp_path)
    store.load()
    profile = store.profiles[0]
    ok_btn = next(b for b in profile.buttons if b.id == "tv-ok")
    menu_btn = next(b for b in profile.buttons if b.id == "tv-menu")
    ok_btn.action = chain
    menu_btn.action = grid_with_macro

    layer_chain = Action(
        "macro",
        target="保存并运行",
        steps=[
            MacroStep(Action("common", target="save"), delay_ms=100),
            MacroStep(Action("shortcut", ["cmd", "r"]), delay_ms=120),
        ],
    )
    profile.app_layers.append(
        AppLayer(
            app_id="com.microsoft.VSCode",
            name="VS Code",
            overrides={"tv-ok": {"single": layer_chain}},
        )
    )

    restored = Profile.loads(profile.dumps())
    assert restored.buttons == profile.buttons
    assert restored.app_layers == profile.app_layers

    store.save()
    reloaded = ConfigStore(tmp_path)
    reloaded.load()
    reloaded_ok = next(b for b in reloaded.profiles[0].buttons if b.id == "tv-ok")
    reloaded_menu = next(b for b in reloaded.profiles[0].buttons if b.id == "tv-menu")
    assert reloaded_ok.action == chain
    assert reloaded_menu.action == grid_with_macro
    assert reloaded.profiles[0].app_layers[0].overrides["tv-ok"]["single"] == layer_chain


def test_palette_and_presentation_texts_for_macro():
    for plat in ("darwin", "win32"):
        flat = [ident for _, items in palette(plat) for ident, _ in items]
        assert flat.count("macro") == 1

    named = Action(
        "macro",
        target="继续并回车",
        steps=[
            MacroStep(Action("text", text="继续"), delay_ms=120),
            MacroStep(Action("shortcut", ["enter"]), delay_ms=120),
        ],
    )
    unnamed_two = Action(
        "macro",
        steps=[
            MacroStep(Action("text", text="继续"), delay_ms=120),
            MacroStep(Action("shortcut", ["enter"]), delay_ms=120),
        ],
    )
    unnamed_one = Action(
        "macro",
        steps=[MacroStep(Action("shortcut", ["enter"]), delay_ms=120)],
    )
    assert action_text(named) == "动作链 · 继续并回车"
    assert action_text(unnamed_two) == "动作链（2 步）· 输入文本 · 继续 → …"
    assert action_text(unnamed_one, "darwin") == "动作链 · ↩"
    assert action_text(unnamed_one, "win32") == "动作链 · ↩"

    assert grid_cell_texts(GridCell("", named)) == ("继续并回车", "多步动作链 · 2 步")
    assert grid_cell_texts(GridCell("发送继续", unnamed_two)) == (
        "发送继续",
        "动作链（2 步）· 输入文本 · 继续 → …",
    )


# -- 2. Non-Blocking Runtime, Cancellation & Error Isolation -----------------


def test_macro_executes_with_qtimer_delay_without_blocking(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    events: list[tuple[str, object]] = []
    messages: list[tuple[str, bool]] = []
    c.message.connect(lambda text, err: messages.append((text, err)))

    monkeypatch.setattr(c.keys, "input_text", lambda txt: events.append(("text", txt)) or "已请求输入文本")
    monkeypatch.setattr(c.keys, "shortcut", lambda keys: events.append(("shortcut", list(keys))))

    chain = Action(
        "macro",
        target="粘贴发送",
        steps=[
            MacroStep(Action("text", text="继续执行下一步"), delay_ms=180),
            MacroStep(Action("shortcut", ["enter"]), delay_ms=120),
        ],
    )
    c.select("tv-ok")
    assert c.assign(chain, "single")

    ok_trig = _trigger_for(c, "tv-ok")
    c.on_key(ok_trig, True)

    # Step 1 ran synchronously; Step 2 is waiting on QTimer(180ms)
    assert events == [("text", "继续执行下一步")]
    assert c.macros.active
    assert c.macros.timer.isActive()
    assert c.macros.timer.interval() == 180
    assert messages[-1] == ("正在执行「粘贴发送」（1/2）…", False)

    # Auto-repeat while still held and physical release must NOT cancel or re-fire
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert c.macros.active
    assert events == [("text", "继续执行下一步")]

    # Fire QTimer -> Step 2 executes and macro finishes without trailing wait
    c.macros.timer.timeout.emit()
    assert not c.macros.active
    assert not c.macros.timer.isActive()
    assert events == [("text", "继续执行下一步"), ("shortcut", ["enter"])]
    assert messages[-1] == ("已执行「粘贴发送」（共 2 步）。", False)
    c.shutdown()


def test_zero_delay_macro_runs_all_steps_immediately(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    events: list[list[str]] = []
    monkeypatch.setattr(c.keys, "shortcut", lambda keys: events.append(list(keys)))

    chain = Action(
        "macro",
        target="全选复制",
        steps=[
            MacroStep(Action("shortcut", ["cmd", "a"]), delay_ms=0),
            MacroStep(Action("shortcut", ["cmd", "c"]), delay_ms=0),
        ],
    )
    c.select("tv-home")
    assert c.assign(chain, "single")

    trig = _trigger_for(c, "tv-home")
    c.on_key(trig, True)
    c.on_key(trig, False)
    assert not c.macros.active
    assert events == [["cmd", "a"], ["cmd", "c"]]

    # Standalone ActionRunner fallback also supports zero-delay macros cleanly
    standalone_events: list[list[str]] = []
    runner = ActionRunner()
    monkeypatch.setattr(runner.keys, "shortcut", lambda keys: standalone_events.append(list(keys)))
    runner.run(chain, True)
    assert standalone_events == [["cmd", "a"], ["cmd", "c"]]
    c.shutdown()


def test_zero_delay_text_macro_paces_clipboard_steps(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    pasted: list[str] = []
    clipboard_state = {"text": ""}

    class FakeClipboard:
        def setText(self, val):
            clipboard_state["text"] = val

    monkeypatch.setattr("PySide6.QtWidgets.QApplication.clipboard", staticmethod(lambda: FakeClipboard()))
    monkeypatch.setattr(c.keys, "shortcut", lambda keys: pasted.append(clipboard_state["text"]))

    chain = Action(
        "macro",
        target="零等待文本链",
        steps=[
            MacroStep(Action("text", text="TEST_MACRO_A"), delay_ms=0),
            MacroStep(Action("text", text="TEST_MACRO_B"), delay_ms=0),
            MacroStep(Action("text", text="TEST_MACRO_C"), delay_ms=0),
        ],
    )
    c.select("tv-home")
    assert c.assign(chain, "single")

    trig = _trigger_for(c, "tv-home")
    c.on_key(trig, True)
    c.on_key(trig, False)
    assert pasted == ["TEST_MACRO_A"]
    assert c.macros.active
    assert c.macros.timer.interval() == c.macros.TEXT_PASTE_SETTLE_MS

    c.macros.timer.timeout.emit()
    assert pasted == ["TEST_MACRO_A", "TEST_MACRO_B"]
    assert c.macros.active

    c.macros.timer.timeout.emit()
    assert pasted == ["TEST_MACRO_A", "TEST_MACRO_B", "TEST_MACRO_C"]
    assert not c.macros.active
    c.shutdown()


def test_macro_cancel_by_back_key_or_same_trigger_and_absorb_other_keys(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    events: list[str] = []
    messages: list[tuple[str, bool]] = []
    c.message.connect(lambda text, err: messages.append((text, err)))
    monkeypatch.setattr(c.keys, "shortcut", lambda keys: events.append("+".join(keys)))

    chain = Action(
        "macro",
        target="三步流程",
        steps=[
            MacroStep(Action("shortcut", ["a"]), delay_ms=300),
            MacroStep(Action("shortcut", ["b"]), delay_ms=300),
            MacroStep(Action("shortcut", ["c"]), delay_ms=0),
        ],
    )
    c.select("tv-ok")
    assert c.assign(chain, "single")
    c.select("tv-back")
    assert c.assign(Action("shortcut", ["esc"]), "single")
    c.select("tv-up")
    assert c.assign(Action("shortcut", ["up"]), "single")

    ok_trig = _trigger_for(c, "tv-ok")
    back_trig = _trigger_for(c, "tv-back")
    up_trig = _trigger_for(c, "tv-up")

    # Start macro and release trigger
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert events == ["a"] and c.macros.active

    # Pressing an unrelated key mid-macro warns and does NOT fire its mapped action
    c.on_key(up_trig, True)
    c.on_key(up_trig, False)
    assert events == ["a"] and c.macros.active
    assert messages[-1] == ("动作链执行中，按返回键可立即中止。", False)

    # Pressing tv-back cancels immediately and absorbs tv-back release (never sends Esc)
    c.on_key(back_trig, True)
    assert not c.macros.active
    assert not c.macros.timer.isActive()
    assert messages[-1] == ("已中止动作链。", False)
    c.on_key(back_trig, False)
    assert events == ["a"]

    # Restart macro, then press the SAME trigger (tv-ok) again to cancel
    events.clear()
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert events == ["a"] and c.macros.active

    c.on_key(ok_trig, True)
    assert not c.macros.active
    assert messages[-1] == ("已中止动作链。", False)
    c.on_key(ok_trig, False)
    assert events == ["a"]
    c.shutdown()


def test_macro_stops_remaining_steps_on_error(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    events: list[str] = []
    messages: list[tuple[str, bool]] = []
    c.message.connect(lambda text, err: messages.append((text, err)))

    def flaky_shortcut(keys):
        if keys == ["a"]:
            raise PermissionError("辅助功能权限未开启")
        events.append("+".join(keys))

    monkeypatch.setattr(c.keys, "shortcut", flaky_shortcut)
    chain = Action(
        "macro",
        target="权限检查链",
        steps=[
            MacroStep(Action("shortcut", ["a"]), delay_ms=100),
            MacroStep(Action("shortcut", ["b"]), delay_ms=0),
        ],
    )
    c.select("tv-ok")
    assert c.assign(chain, "single")

    ok_trig = _trigger_for(c, "tv-ok")
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert not c.macros.active
    assert not c.macros.timer.isActive()
    assert events == []
    assert messages[-1] == ("「权限检查链」第 1 步失败：辅助功能权限未开启", True)
    c.shutdown()


def test_macro_cancelled_on_voice_learning_reassign_and_shutdown(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    monkeypatch.setattr(c.keys, "shortcut", lambda keys: None)

    chain = Action(
        "macro",
        steps=[
            MacroStep(Action("shortcut", ["a"]), delay_ms=500),
            MacroStep(Action("shortcut", ["b"]), delay_ms=0),
        ],
    )
    c.select("tv-ok")
    assert c.assign(chain, "single")
    ok_trig = _trigger_for(c, "tv-ok")
    voice_trig = _trigger_for(c, "tv-voice")

    # 1. Voice key cancels active macro
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert c.macros.active
    c.on_key(voice_trig, True)
    c.on_key(voice_trig, False)
    assert not c.macros.active

    # 2. Learning mode cancels active macro
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert c.macros.active
    c.set_learning(True)
    assert not c.macros.active
    c.set_learning(False)

    # 3. Reassigning the same button cancels active macro
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert c.macros.active
    c.select("tv-ok")
    assert c.assign(Action("shortcut", ["enter"]), "single")
    assert not c.macros.active

    # 4. Shutdown cancels active macro
    assert c.assign(chain, "single")
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert c.macros.active
    c.shutdown()
    assert not c.macros.active


def test_macro_integrates_with_app_layers_and_grid_launcher(qapp, tmp_path, monkeypatch):
    c = AppController(tmp_path, auto_mapping=False)
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(c.keys, "input_text", lambda txt: events.append(("text", txt)) or "已请求输入文本")
    monkeypatch.setattr(c.keys, "shortcut", lambda keys: events.append(("shortcut", list(keys))))

    # Base layer tv-ok = Space; Cursor AppLayer tv-ok = Macro(text + enter)
    c.select("tv-ok")
    assert c.assign(Action("shortcut", ["space"]), "single")
    layer = c.add_app_layer(LayerApp("com.todesktop.230313mzl4w4u92", "Cursor"))
    assert layer is not None
    c.select("tv-ok")
    cursor_macro = Action(
        "macro",
        target="继续生成",
        steps=[
            MacroStep(Action("text", text="继续"), delay_ms=100),
            MacroStep(Action("shortcut", ["enter"]), delay_ms=0),
        ],
    )
    assert c.assign(cursor_macro, "single")
    c.select_editing_layer("")

    ok_trig = _trigger_for(c, "tv-ok")
    fg_app = [LayerApp("com.apple.QuickTimePlayerX", "QuickTime Player")]
    c.foreground_apps._provider = lambda: fg_app[0]
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert events == [("shortcut", ["space"])]

    events.clear()
    fg_app[0] = LayerApp("com.todesktop.230313mzl4w4u92", "Cursor")
    c.foreground_apps.changed.emit("com.todesktop.230313mzl4w4u92", "Cursor")
    c.on_key(ok_trig, True)
    c.on_key(ok_trig, False)
    assert events == [("text", "继续")]
    c.macros.timer.timeout.emit()
    assert events == [("text", "继续"), ("shortcut", ["enter"])]

    # Grid Launcher cell triggering a Macro
    events.clear()
    grid_macro = Action(
        "macro",
        target="审查并发送",
        steps=[
            MacroStep(Action("text", text="审查代码"), delay_ms=80),
            MacroStep(Action("shortcut", ["cmd", "enter"]), delay_ms=0),
        ],
    )
    c.select("tv-menu")
    assert c.assign(
        Action("grid", target="AI 九宫格", cells=_nine_cells(c2=GridCell("代码审查", grid_macro))),
        "single",
    )

    menu_trig = _trigger_for(c, "tv-menu")
    num2_trig = _trigger_for(c, "tv-number-2")
    c.on_key(menu_trig, True)
    c.on_key(menu_trig, False)
    assert c.overlays.grid_open

    c.on_key(num2_trig, True)
    c.on_key(num2_trig, False)
    assert not c.overlays.grid_open
    assert c.macros.active
    assert events == [("text", "审查代码")]

    c.macros.timer.timeout.emit()
    assert not c.macros.active
    assert events == [("text", "审查代码"), ("shortcut", ["cmd", "enter"])]
    c.shutdown()


# -- 3. Offscreen Qt UI: MacroEditorDialog, GridEditorDialog & Inspector -----


def test_macro_editor_dialog_templates_ordering_and_validation(qapp):
    dialog = MacroEditorDialog(button_name="确定")
    dialog.show()

    # Initial empty shortcut fails validation inline
    assert not dialog.accept_macro()
    assert dialog.error_label.isVisible()
    assert "第 1 步" in dialog.error_label.text()

    # Apply "paste_enter" template -> 2 steps: text + enter
    dialog.apply_template("paste_enter")
    assert len(dialog.steps) == 2
    assert not dialog.accept_macro()  # text still empty
    assert "第 1 步" in dialog.error_label.text()

    dialog.step_text.setPlainText("帮我写单元测试")
    dialog.delay_spin.setValue(200)
    dialog.chain_title.setText("一键单测")

    # Add a 3rd step after step 1 (index 1), set shortcut, move it up to 0 & back down to 1
    assert dialog.add_step()
    assert len(dialog.steps) == 3
    assert dialog.current_step == 1
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("shortcut"))
    dialog.step_sequence.setKeySequence(QKeySequence("Tab"))
    assert dialog.move_step(-1)
    assert dialog.current_step == 0
    assert dialog.move_step(1)
    assert dialog.current_step == 1
    assert dialog.remove_current_step()
    assert len(dialog.steps) == 2

    assert dialog.accept_macro()
    saved = dialog.result_action()
    assert saved is not None
    assert saved.kind == "macro"
    assert saved.target == "一键单测"
    assert saved.steps == [
        MacroStep(Action("text", text="帮我写单元测试"), delay_ms=200),
        MacroStep(Action("shortcut", ["enter"]), delay_ms=120),
    ]
    dialog.close()


def test_grid_editor_supports_macro_slot(qapp):
    dialog = GridEditorDialog(button_name="菜单")
    dialog.show()

    dialog.select_slot(0)
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("macro"))
    # Unconfigured macro slot shows validation error on save
    assert not dialog.accept_grid()
    assert "格位 1" in dialog.error_label.text()

    macro = Action(
        "macro",
        target="粘贴回车",
        steps=[
            MacroStep(Action("text", text="Hello"), delay_ms=100),
            MacroStep(Action("shortcut", ["enter"]), delay_ms=120),
        ],
    )
    dialog.set_slot_action(0, macro, title="打招呼")
    assert dialog.accept_grid()
    grid_action = dialog.result_action()
    assert grid_action is not None
    assert grid_action.cells[0].action == macro
    dialog.close()


def test_inspector_macro_draft_preview_and_minimum_window_fit(qtbot, tmp_path):
    win = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(win)
    win.resize(1200, 720)
    win.show()
    c = win.controller
    ins = win.inspector
    c.select("tv-ok")
    assert "macro" in ins.kinds

    # Clicking macro chip enters inline draft mode without popping a modal dialog
    ins.choose_kind("macro")
    assert ins.draft_kind == "macro"
    assert ins.pages.currentIndex() == 7
    assert c.selected.action.kind == "none"

    # Assign a multi-step macro with long text and verify zero horizontal clipping at 1200x720
    long_macro = Action(
        "macro",
        target="超长名称测试·粘贴一整段提示词随后自动回车发送给当前对话窗口",
        steps=[
            MacroStep(Action("toggle_app", target="/Applications/Notes.app"), delay_ms=450),
            MacroStep(Action("text", text="这是一段非常长的中文提示词内容，用来验证右侧检查器在 1200x720 最小窗口下不会发生水平溢出"), delay_ms=150),
            MacroStep(Action("shortcut", ["enter"]), delay_ms=120),
        ],
    )
    assert c.assign(long_macro, "single")
    qtbot.wait(20)

    assert ins.draft_kind is None
    assert ins.pages.currentIndex() == 7
    assert sum(1 for btn in ins.macro_preview_buttons if not btn.isHidden()) == 3

    for area in win.findChildren(QScrollArea):
        if area.isVisible():
            assert area.horizontalScrollBar().maximum() == 0
            assert area.widget().width() <= area.viewport().width()

    win.shutdown()
