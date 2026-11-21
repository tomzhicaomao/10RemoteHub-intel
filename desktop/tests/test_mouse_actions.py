"""Tests for exclusive physical mouse buttons and instant Macro/Grid mouse clicks."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QScrollArea

from remotehub.actions import ActionRunner, KeyOutput, MouseOutput
from remotehub.app_layers import LayerApp
from remotehub.controller import AppController
from remotehub.gestures import KeyGesture
from remotehub.model import (
    Action,
    AppLayer,
    Button,
    GridCell,
    GRID_SIZE,
    MacroStep,
    Profile,
    effective_button,
)
from remotehub.presentation import action_text, grid_cell_texts, palette
from remotehub.store import ConfigStore
from remotehub.ui import MainWindow
from remotehub.ui.grid_editor import GridEditorDialog
from remotehub.ui.macro_editor import MacroEditorDialog


def _nine_cells(**overrides: GridCell) -> list[GridCell]:
    cells = [GridCell() for _ in range(GRID_SIZE)]
    for key, cell in overrides.items():
        cells[int(key[1:]) - 1] = cell
    return cells


def test_mouse_model_validation_and_effective_layer_isolation(tmp_path):
    for valid_act in (
        Action("mouse", target="left"),
        Action("mouse", target="right"),
        Action("mouse", target="middle"),
        Action("mouse", target="left_double"),
    ):
        valid_act.validate()

    for bad_act in (
        Action("mouse", target=""),
        Action("mouse", target="scroll_left"),
        Action("mouse", target="left", keys=["cmd"]),
        Action("mouse", target="left", text="hi"),
        Action("mouse", target="left", enter=True),
    ):
        with pytest.raises(ValueError):
            bad_act.validate()

    # Physical button cannot mix mouse with double/long or use left_double
    with pytest.raises(ValueError, match="左键、右键或中键"):
        Profile(buttons=[Button(action=Action("mouse", target="left_double"))]).validate()
    with pytest.raises(ValueError, match="同时设置双击或长按"):
        Profile(
            buttons=[
                Button(
                    action=Action("mouse", target="left"),
                    double_action=Action("shortcut", ["enter"]),
                )
            ]
        ).validate()
    with pytest.raises(ValueError, match="不支持鼠标按键"):
        Profile(buttons=[Button(double_action=Action("mouse", target="right"))]).validate()

    # AppLayer resolves effective_button cleanly without losing global gestures
    base_btn = Button(
        id="tv-ok",
        name="确定",
        trigger="hid:1:1",
        action=Action("shortcut", ["enter"]),
        double_action=Action("shortcut", ["space"]),
        long_action=Action("shortcut", ["esc"]),
    )
    mouse_layer = AppLayer(
        app_id="com.example.viewer",
        name="Viewer",
        overrides={"tv-ok": {"single": Action("mouse", target="left")}},
    )
    resolved = effective_button(base_btn, mouse_layer)
    assert resolved.action == Action("mouse", target="left")
    assert resolved.double_action.kind == "none"
    assert resolved.long_action.kind == "none"
    assert not KeyGesture.needed(resolved)
    assert base_btn.double_action == Action("shortcut", ["space"])

    # Serialization roundtrip across button, layer, macro, and grid
    store = ConfigStore(tmp_path)
    p = store.profiles[0]
    next(b for b in p.buttons if b.id == "tv-ok").action = Action("mouse", target="left")
    next(b for b in p.buttons if b.id == "tv-menu").action = Action(
        "macro",
        target="右击并复制",
        steps=[MacroStep(Action("mouse", target="left_double"), delay_ms=60)],
    )
    next(b for b in p.buttons if b.id == "tv-home").action = Action(
        "grid",
        cells=_nine_cells(c1=GridCell("中键", Action("mouse", target="middle"))),
    )
    p.app_layers.append(
        AppLayer(
            app_id="com.example.viewer",
            name="Viewer",
            overrides={"tv-channel-up": {"single": Action("mouse", target="right")}},
        )
    )
    store.save()
    reloaded = ConfigStore(tmp_path)
    reloaded.load()
    rp = reloaded.profiles[0]
    assert next(b for b in rp.buttons if b.id == "tv-ok").action == Action("mouse", target="left")
    assert next(b for b in rp.buttons if b.id == "tv-menu").action.steps[0].action == Action("mouse", target="left_double")
    assert next(b for b in rp.buttons if b.id == "tv-home").action.cells[0].action == Action("mouse", target="middle")
    assert rp.app_layers[0].overrides["tv-channel-up"]["single"] == Action("mouse", target="right")


def test_mouse_palette_and_presentation_texts():
    for plat in ("darwin", "win32"):
        ids = [ident for _, items in palette(plat) for ident, _ in items]
        assert ids.count("mouse:left") == 1
        assert ids.count("mouse:right") == 1
        assert ids.count("mouse:middle") == 1
        assert "mouse:left_double" not in ids

    assert action_text(Action("mouse", target="left")) == "鼠标左键"
    assert action_text(Action("mouse", target="right")) == "鼠标右键"
    assert action_text(Action("mouse", target="middle")) == "鼠标中键"
    assert action_text(Action("mouse", target="left_double")) == "鼠标左键双击"
    assert grid_cell_texts(GridCell("", Action("mouse", target="right"))) == ("鼠标右键", "鼠标按键")


def test_mouse_output_double_click_state_hold_and_macro_click(monkeypatch):
    now = [10.0]
    point = [(100.0, 200.0)]
    events = []

    keys = KeyOutput()
    mouse = MouseOutput(keys, clock=lambda: now[0])
    monkeypatch.setattr(mouse, "_cursor_point", lambda: point[0])
    monkeypatch.setattr(
        mouse,
        "_send_mouse",
        lambda btn, down, state, mods: events.append((btn, down, state, list(mods))),
    )

    # First quick tap -> state=1
    mouse.hold("k1", "left")
    mouse.release("k1")
    # Second tap within double-click window & distance -> state=2
    now[0] += 0.15
    mouse.hold("k1", "left")
    mouse.release("k1")
    # Third tap -> state=3
    now[0] += 0.15
    mouse.hold("k1", "left")
    mouse.release("k1")
    # Moved cursor resets back to state=1
    now[0] += 0.1
    point[0] = (150.0, 200.0)
    mouse.hold("k1", "left")
    mouse.release("k1")

    assert events == [
        ("left", True, 1, []),
        ("left", False, 1, []),
        ("left", True, 2, []),
        ("left", False, 2, []),
        ("left", True, 3, []),
        ("left", False, 3, []),
        ("left", True, 1, []),
        ("left", False, 1, []),
    ]

    # ActionRunner macro/grid instant clicks including left_double
    events.clear()
    runner = ActionRunner(keys, mouse=mouse)
    assert runner.run(Action("mouse", target="left_double"), True, owner="macro") == "已双击鼠标左键"
    assert events == [
        ("left", True, 1, []),
        ("left", False, 1, []),
        ("left", True, 2, []),
        ("left", False, 2, []),
    ]


def test_controller_mouse_takeover_undo_layer_and_safe_release(tmp_path, monkeypatch):
    ctl = AppController(tmp_path, auto_mapping=False)
    mouse_events = []
    monkeypatch.setattr(
        ctl.runner.mouse,
        "_send_mouse",
        lambda btn, down, state, mods: mouse_events.append((btn, down, state)),
    )
    monkeypatch.setattr(ctl.runner.mouse, "_cursor_point", lambda: (100.0, 100.0))

    ctl.select("tv-ok")
    trigger = ctl.selected.trigger
    assert trigger

    # Assign double & long actions in Global Layer first
    assert ctl.assign(Action("shortcut", ["enter"]), "single")
    assert ctl.assign(Action("shortcut", ["space"]), "double")
    assert ctl.assign(Action("shortcut", ["esc"]), "long")

    # Assigning mouse:left clears double & long atomically and enables 1-click Undo
    assert ctl.assign(Action("mouse", target="left"), "single")
    assert ctl.selected.action == Action("mouse", target="left")
    assert ctl.selected.double_action.kind == "none"
    assert ctl.selected.long_action.kind == "none"

    # Trying to assign double while mouse active is cleanly rejected
    assert not ctl.assign(Action("shortcut", ["a"]), "double")

    # Physical press & HID auto-repeat & release sends 1 Down + 1 Up immediately
    ctl.on_key(trigger, True)
    ctl.on_key(trigger, True)  # HID auto-repeat while held must not double-press or reset
    assert len(mouse_events) == 1 and mouse_events[0][:2] == ("left", True)
    ctl.on_key(trigger, False)
    assert len(mouse_events) == 2 and mouse_events[1][:2] == ("left", False)

    # Mid-hold shutdown / suspend force-releases MouseUp cleanly
    mouse_events.clear()
    ctl.on_key(trigger, True)
    assert len(mouse_events) == 1 and mouse_events[0][:2] == ("left", True)
    ctl._clear_gestures()
    assert len(mouse_events) == 2 and mouse_events[1][:2] == ("left", False)
    ctl.on_key(trigger, False)  # finish physical key release after interrupt

    # Undo restores single + double + long atomically!
    assert ctl.undo_action()
    assert ctl.selected.action == Action("shortcut", ["enter"])
    assert ctl.selected.double_action == Action("shortcut", ["space"])
    assert ctl.selected.long_action == Action("shortcut", ["esc"])

    # App Layer override to mouse:right keeps Global Layer double/long completely intact!
    layer = ctl.add_app_layer(LayerApp("com.example.viewer", "Viewer"))
    assert layer is not None
    ctl.select_editing_layer(layer.id)
    assert ctl.assign(Action("mouse", target="right"), "single")
    assert ctl.selected.double_action == Action("shortcut", ["space"])

    ctl.foreground_apps._provider = lambda: LayerApp("com.example.viewer", "Viewer")
    ctl.foreground_apps.changed.emit("com.example.viewer", "Viewer")
    mouse_events.clear()
    ctl.on_key(trigger, True)
    ctl.on_key(trigger, False)
    assert [e[:2] for e in mouse_events] == [("right", True), ("right", False)]

    ctl.shutdown()


def test_ui_inspector_macro_and_grid_mouse_integration(qtbot, tmp_path):
    w = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(w)
    w.show()
    c, ins = w.controller, w.inspector

    c.select("tv-number-1")
    c.assign(Action("shortcut", ["a"]), "double")
    ins.set_gesture("double")
    assert ins.gesture == "double"

    # Clicking mouse:left in palette takes over key, forces single tab, disables double/long
    ins.choose_kind("mouse:left")
    assert c.selected.action == Action("mouse", target="left")
    assert ins.gesture == "single"
    assert not ins.gesture_buttons["double"].isEnabled()
    assert not ins.gesture_buttons["long"].isEnabled()
    assert "专属模拟鼠标" in ins.gesture_hint.text()

    w.resize(1200, 720)
    QApplication.processEvents()
    for area in w.findChildren(QScrollArea):
        if area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
            assert area.horizontalScrollBar().maximum() == 0

    # MacroEditorDialog & GridEditorDialog mouse pickers
    macro_dlg = MacroEditorDialog()
    qtbot.addWidget(macro_dlg)
    macro_dlg.kind_combo.setCurrentIndex(macro_dlg.kind_combo.findData("mouse"))
    macro_dlg.mouse_combo.setCurrentIndex(macro_dlg.mouse_combo.findData("left_double"))
    built_macro = macro_dlg.build_action()
    assert built_macro.steps[0].action == Action("mouse", target="left_double")

    grid_dlg = GridEditorDialog()
    qtbot.addWidget(grid_dlg)
    grid_dlg.kind_combo.setCurrentIndex(grid_dlg.kind_combo.findData("mouse"))
    grid_dlg.mouse_combo.setCurrentIndex(grid_dlg.mouse_combo.findData("middle"))
    built_grid = grid_dlg.build_action()
    assert built_grid.cells[0].action == Action("mouse", target="middle")

    w.shutdown()
