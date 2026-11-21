"""Comprehensive tests for Phase-1 UI control, app menu, and voice-focus/auto-enter features."""
from __future__ import annotations

from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QScrollArea

from remotehub.actions import ActionRunner, KeyOutput
from remotehub.ax_actions import (
    MenuCommand,
    execute_app_menu,
    execute_ui_element,
    find_interactive_candidate,
    list_app_menus,
    resolve_ui_element,
)
from remotehub.chat_focus import App
from remotehub.chat_focus_locator import describe as chat_describe, signature as structural_signature
from remotehub.chat_focus_service import ChatFocusService
from remotehub.controller import AppController
from remotehub.macro_runner import MacroRunner
from remotehub.model import Action, AppLayer, GridCell, GRID_SIZE, MacroStep, Profile
from remotehub.presentation import action_text, grid_cell_texts, palette
from remotehub.store import ConfigStore
from remotehub.ui import MainWindow
from remotehub.ui.ax_pickers import AppMenuPickerDialog, UiControlPickerDialog
from remotehub.ui.grid_editor import GridEditorDialog
from remotehub.ui.macro_editor import MacroEditorDialog


class _AXNode:
    def __init__(
        self,
        role: str,
        *,
        subrole: str | None = None,
        identifier: str | None = None,
        title: str | None = None,
        description: str | None = None,
        enabled: bool = True,
        settable: bool = False,
        actions: set[str] | None = None,
        hidden: bool = False,
        modal: bool = False,
        focused: bool = False,
        children: list[_AXNode] | None = None,
    ):
        self.role = role
        self.subrole = subrole
        self.identifier = identifier
        self.title = title
        self.description = description
        self.enabled = enabled
        self.settable_flag = settable
        self.action_names = set(actions or ())
        self.hidden = hidden
        self.is_modal = modal
        self.is_focused = focused
        self.children_list: list[_AXNode] = []
        self.parent: _AXNode | None = None
        for child in children or []:
            self.add(child)

    def add(self, child: _AXNode) -> _AXNode:
        child.parent = self
        self.children_list.append(child)
        return child


class _FakeAXBackend:
    def __init__(self):
        self.app = App("com.example.reader", 4242, "Example Reader")
        self.front: App | None = self.app
        self.build = "a" * 64
        self.window_node = _AXNode("AXWindow", identifier="main-win")
        toolbar = self.window_node.add(_AXNode("AXGroup", identifier="toolbar"))
        self.next_btn = toolbar.add(
            _AXNode("AXButton", identifier="btn-next", title="下一页", actions={"AXPress"})
        )
        self.next_btn_text = self.next_btn.add(_AXNode("AXStaticText", title="下一页"))
        self.disabled_btn = toolbar.add(
            _AXNode("AXButton", identifier="btn-disabled", title="禁用按钮", enabled=False, actions={"AXPress"})
        )
        self.scroll = self.window_node.add(_AXNode("AXScrollArea", identifier="content-scroll"))
        self.table = self.scroll.add(_AXNode("AXTable", identifier="message-list", settable=True))
        self.cell_text = self.table.add(_AXNode("AXStaticText", title="一条消息"))
        self.chat_area = self.window_node.add(
            _AXNode("AXTextArea", identifier="prompt-textarea", settable=True)
        )

        self.menubar_node = _AXNode("AXMenuBar")
        apple = self.menubar_node.add(_AXNode("AXMenuBarItem", title="Apple"))
        apple_menu = apple.add(_AXNode("AXMenu"))
        apple_menu.add(_AXNode("AXMenuItem", title="关于本机"))

        view_item = self.menubar_node.add(_AXNode("AXMenuBarItem", title="显示"))
        view_menu = view_item.add(_AXNode("AXMenu"))
        self.sidebar_item = view_menu.add(_AXNode("AXMenuItem", title="隐藏侧边栏"))
        self.export_item = view_menu.add(_AXNode("AXMenuItem", title="导出为 PDF…"))
        self.grey_item = view_menu.add(_AXNode("AXMenuItem", title="不可用命令", enabled=False))

        self.focused_node: _AXNode | None = self.window_node
        self.cursor_elem: _AXNode | None = None
        self.cursor_point: tuple[float, float] = (200.0, 300.0)
        self.pressed_nodes: list[_AXNode] = []
        self.focused_requests: list[_AXNode] = []
        self.reject_focus = False
        self.reject_press = False
        self.stamp = (1, 0, 0, 0, 0)

    def applications(self, include_browsers: bool = True):
        return [self.app]

    def frontmost(self):
        return self.front

    def prepare(self, app, cancelled):
        return None

    def window(self, app):
        return self.window_node if app == self.app else None

    def focused(self, app):
        return self.focused_node if app == self.app else None

    def menubar(self, app):
        return self.menubar_node if app == self.app else None

    def children(self, element):
        return list(element.children_list) if element is not None else []

    def read(self, element, attr):
        if element is None:
            return None
        mapping = {
            "AXRole": element.role,
            "AXSubrole": element.subrole,
            "AXIdentifier": element.identifier,
            "AXTitle": element.title,
            "AXDescription": element.description,
            "AXEnabled": element.enabled,
            "AXHidden": element.hidden,
            "AXFocused": element.is_focused,
            "AXParent": element.parent,
            "AXModal": element.is_modal,
        }
        return mapping.get(attr)

    def settable(self, element):
        return bool(element and element.settable_flag)

    def modal(self, window):
        return bool(window and window.is_modal)

    def visible(self, element, window):
        return bool(element and not element.hidden)

    def actions(self, element):
        return set(element.action_names) if element is not None else set()

    def press(self, element) -> bool:
        if self.reject_press or element is None:
            return False
        self.pressed_nodes.append(element)
        return True

    def focus(self, element) -> bool:
        if self.reject_focus or element is None:
            return False
        if self.focused_node is not None:
            self.focused_node.is_focused = False
        element.is_focused = True
        self.focused_node = element
        self.focused_requests.append(element)
        return True

    def input_stamp(self):
        return self.stamp

    def discover(self, app, window, cancelled):
        return self.chat_area if window == self.window_node else None

    def describe(self, app, window, target):
        return chat_describe(self, window, target, self.build)

    def resolve(self, app, window, locator, cancelled):
        if locator and locator.get("signature") == structural_signature(self, self.chat_area, window):
            return self.chat_area
        return None

    def mouse_point(self):
        return self.cursor_point

    def element_at_cursor(self, app):
        return self.cursor_elem if app == self.app else None


def _nine_cells(**overrides: GridCell) -> list[GridCell]:
    cells = [GridCell() for _ in range(GRID_SIZE)]
    for key, cell in overrides.items():
        idx = int(key[1:]) - 1
        cells[idx] = cell
    return cells


def test_model_validation_and_roundtrip_for_new_actions(tmp_path):
    sig = "b" * 64
    ui_press = Action(
        kind="ui_element",
        target="press",
        application="com.example.reader",
        spec={"app_name": "Example Reader", "role": "AXButton", "label": "下一页", "signature": sig},
    )
    ui_focus = Action(
        kind="ui_element",
        target="focus",
        application="com.example.reader",
        spec={"app_name": "Example Reader", "role": "AXTable", "label": "消息列表", "signature": "c" * 64},
    )
    menu_act = Action(
        kind="app_menu",
        application="com.example.reader",
        spec={"app_name": "Example Reader", "path": ["显示", "隐藏侧边栏"]},
    )
    menu_any = Action(
        kind="app_menu",
        application="",
        spec={"app_name": "", "path": ["文件", "导出为 PDF…"]},
    )
    text_enter = Action("text", text="总结以上内容", enter=True)
    focus_then_page = Action(
        "macro",
        target="锁定列表并翻页",
        steps=[
            MacroStep(ui_focus, delay_ms=60),
            MacroStep(Action("shortcut", ["pagedown"]), delay_ms=0),
        ],
    )
    grid = Action("grid", target="常用操作", cells=_nine_cells(c1=GridCell("下一页", ui_press), c2=GridCell("", menu_act)))

    # Serialization round-trip across button, layer, macro, grid, and ConfigStore
    store = ConfigStore(tmp_path)
    p = store.profiles[0]
    next(b for b in p.buttons if b.id == "tv-ok").action = text_enter
    next(b for b in p.buttons if b.id == "tv-menu").action = grid
    p.app_layers.append(
        AppLayer(
            app_id="com.example.reader",
            name="Example Reader",
            overrides={
                "tv-channel-up": {"single": focus_then_page},
                "tv-channel-down": {"single": ui_press},
                "tv-home": {"single": menu_any},
            },
        )
    )
    store.save()
    reloaded = ConfigStore(tmp_path)
    reloaded.load()
    rp = reloaded.profiles[0]
    assert next(b for b in rp.buttons if b.id == "tv-ok").action == text_enter
    assert next(b for b in rp.buttons if b.id == "tv-menu").action == grid
    assert rp.app_layers[0].overrides["tv-channel-up"]["single"] == focus_then_page
    assert rp.app_layers[0].overrides["tv-channel-down"]["single"] == ui_press
    assert rp.app_layers[0].overrides["tv-home"]["single"] == menu_any

    # Validation rejections
    for bad in [
        Action("shortcut", ["a"], enter=True),
        Action("text", text="ok", enter="yes"),  # type: ignore[arg-type]
        Action("shortcut", ["a"], spec={"extra": 1}),
        Action("ui_element", target="invalid", application="com.example.app", spec={"role": "AXButton", "signature": sig}),
        Action("ui_element", target="press", application="", spec={"role": "AXButton", "signature": sig}),
        Action("ui_element", target="press", application="com.example.app", spec={"role": "AXDialog", "signature": sig}),
        Action("ui_element", target="press", application="com.example.app", spec={"role": "AXSecureTextField", "signature": sig}),
        Action("ui_element", target="press", application="com.example.app", spec={"role": "AXButton", "signature": "short"}),
        Action("app_menu", application="com.example.app", spec={"path": ["仅一级"]}),
        Action("app_menu", application="com.example.app", spec={"path": ["显示", ""]}),
        Action("app_menu", application="bad/path", spec={"path": ["显示", "隐藏"]}),
    ]:
        with pytest.raises(ValueError):
            bad.validate()


def test_presentation_texts_and_platform_palette():
    mac_ids = [ident for _, items in palette("darwin") for ident, _ in items]
    win_ids = [ident for _, items in palette("win32") for ident, _ in items]
    linux_ids = [ident for _, items in palette("linux") for ident, _ in items]
    assert "ui_element" in mac_ids and "app_menu" in mac_ids
    assert "ui_element" in win_ids and "app_menu" in win_ids
    assert "ui_element" not in linux_ids and "app_menu" not in linux_ids

    ui_act = Action(
        "ui_element",
        target="focus",
        application="com.example.reader",
        spec={"app_name": "微信", "role": "AXTable", "label": "消息列表", "signature": "a" * 64},
    )
    menu_act = Action(
        "app_menu",
        application="com.example.reader",
        spec={"app_name": "预览", "path": ["显示", "隐藏边栏"]},
    )
    assert action_text(ui_act) == "聚焦 · 微信 / 消息列表"
    assert action_text(menu_act) == "菜单 · 预览：显示 → 隐藏边栏"
    assert action_text(Action("text", text="你好", enter=True)) == "输入文本并回车 · 你好"
    assert grid_cell_texts(GridCell("", Action("text", text="你好", enter=True))) == ("你好", "输入文本并回车")
    assert grid_cell_texts(GridCell("", ui_act)) == ("消息列表", "聚焦 · 微信")
    assert grid_cell_texts(GridCell("", menu_act)) == ("隐藏边栏", "预览 · 显示 → 隐藏边栏")


def test_ax_actions_candidate_capture_execute_cache_and_menus():
    backend = _FakeAXBackend()

    # Clicking text child inside a button walks up to AXButton cleanly
    found_btn = find_interactive_candidate(backend, backend.next_btn_text, backend.window_node, "press")
    assert found_btn is not None
    btn_node, btn_role, btn_label, btn_sig = found_btn
    assert btn_node is backend.next_btn
    assert btn_role == "AXButton"
    assert btn_label == "下一页"

    # Pointing at scroll area resolves its focusable child AXTable for focus op
    found_table = find_interactive_candidate(backend, backend.scroll, backend.window_node, "focus")
    assert found_table is not None
    tbl_node, tbl_role, _tbl_label, tbl_sig = found_table
    assert tbl_node is backend.table and tbl_role == "AXTable"

    # Ambiguous sibling controls with identical signature refuse matching cleanly
    dup_btn = _AXNode("AXButton", identifier="btn-next", title="下一页", actions={"AXPress"})
    backend.next_btn.parent.add(dup_btn)
    assert resolve_ui_element(backend, backend.window_node, btn_sig, "AXButton", op="press") is None
    backend.next_btn.parent.children_list.remove(dup_btn)

    # Cache hit + detached cache invalidation + execution
    cache = {}
    press_act = Action(
        "ui_element",
        target="press",
        application=backend.app.bundle,
        spec={"app_name": backend.app.name, "role": "AXButton", "label": "下一页", "signature": btn_sig},
    )
    msg = execute_ui_element(backend, press_act, cache=cache)
    assert "下一页" in msg and backend.pressed_nodes == [backend.next_btn]
    assert len(cache) == 1

    # Wrong frontmost app aborts safely
    backend.front = App("com.other.app", 999, "Other")
    with pytest.raises(RuntimeError, match="前台应用不是"):
        execute_ui_element(backend, press_act, cache=cache)
    backend.front = backend.app

    # Modal window aborts safely
    backend.window_node.is_modal = True
    with pytest.raises(RuntimeError, match="弹窗"):
        execute_ui_element(backend, press_act, cache=cache)
    backend.window_node.is_modal = False

    # Focus op focuses target table cleanly
    focus_act = Action(
        "ui_element",
        target="focus",
        application=backend.app.bundle,
        spec={"app_name": backend.app.name, "role": "AXTable", "label": "消息列表", "signature": tbl_sig},
    )
    msg2 = execute_ui_element(backend, focus_act, cache=cache)
    assert "聚焦" in msg2 and backend.focused_requests == [backend.table]

    # Menu discovery skips Apple menu and reads nested commands
    app_info, cmds = list_app_menus(backend, backend.app.bundle)
    assert app_info == backend.app
    paths = [c.path for c in cmds]
    assert ("显示", "隐藏侧边栏") in paths
    assert ("显示", "导出为 PDF…") in paths
    assert ("Apple", "关于本机") not in paths

    # Menu execution triggers AXPress, supports "..." vs "…", and rejects disabled item
    assert "隐藏侧边栏" in execute_app_menu(
        backend, Action("app_menu", application=backend.app.bundle, spec={"app_name": backend.app.name, "path": ["显示", "隐藏侧边栏"]})
    )
    assert backend.pressed_nodes[-1] is backend.sidebar_item

    execute_app_menu(
        backend, Action("app_menu", application="", spec={"app_name": "", "path": ["显示", "导出为 PDF..."]})
    )
    assert backend.pressed_nodes[-1] is backend.export_item

    with pytest.raises(RuntimeError, match="禁用状态"):
        execute_app_menu(
            backend, Action("app_menu", application=backend.app.bundle, spec={"app_name": backend.app.name, "path": ["显示", "不可用命令"]})
        )


def test_swiftui_toolbar_buttons_large_tree_and_banner_stability(qtbot):
    backend = _FakeAXBackend()
    # Add 80 sibling rows (> 64) + a harmless menu item child + SwiftUI toolbar buttons without AXIdentifier
    sidebar = backend.window_node.add(_AXNode("AXGroup"))
    for idx in range(80):
        sidebar.add(_AXNode("AXStaticText", title=f"历史会话 {idx}"))
    backend.window_node.add(_AXNode("AXMenuItem", title="残留菜单项"))

    header = backend.window_node.add(_AXNode("AXToolbar"))
    sidebar_btn = header.add(_AXNode("AXButton", description="显示边栏", enabled=None, actions={"AXPress"}))
    new_chat_btn = header.add(_AXNode("AXButton", description="新建聊天", enabled=None, actions={"AXPress"}))
    new_chat_icon = new_chat_btn.add(_AXNode("AXImage"))
    blank_a = header.add(_AXNode("AXButton", actions={"AXPress"}))
    blank_b = header.add(_AXNode("AXButton", actions={"AXPress"}))

    # Buttons sharing parent without AXIdentifier still capture and execute uniquely
    found = find_interactive_candidate(backend, new_chat_icon, backend.window_node, "press")
    assert found is not None
    node, role, label_text, sig = found
    assert node is new_chat_btn and role == "AXButton" and label_text == "新建聊天"

    act = Action(
        "ui_element",
        target="press",
        application=backend.app.bundle,
        spec={"app_name": backend.app.name, "role": "AXButton", "label": "新建聊天", "signature": sig},
    )
    assert "新建聊天" in execute_ui_element(backend, act)
    assert backend.pressed_nodes[-1] is new_chat_btn
    assert find_interactive_candidate(backend, sidebar_btn, backend.window_node, "press")[0] is sidebar_btn
    assert find_interactive_candidate(backend, blank_b, backend.window_node, "press")[0] is blank_b
    assert find_interactive_candidate(backend, blank_a, backend.window_node, "press")[0] is blank_a

    # Menu bar item is clearly diagnosed, and _poll_tick preserves _capture_message without flashing
    backend.cursor_elem = backend.sidebar_item
    picker = UiControlPickerDialog(
        default_app_id=backend.app.bundle,
        default_app_name=backend.app.name,
        apps_provider=lambda: [backend.app],
        backend_factory=lambda: backend,
    )
    qtbot.addWidget(picker)
    picker.start_capture()
    qtbot.waitUntil(lambda: "顶部菜单栏" in picker.status_banner.text(), timeout=2000)
    assert picker.status_banner.minimumHeight() >= 48
    # Trigger another poll tick while probe finished: banner message stays on menu-bar hint rather than flipping back
    picker._poll_tick()
    assert "顶部菜单栏" in picker.status_banner.text()
    picker.stop_capture()


def test_text_auto_enter_standalone_and_in_macro(qtbot, monkeypatch):
    events = []
    monkeypatch.setattr("remotehub.actions.sys.platform", "darwin")
    monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: SimpleNamespace(setText=lambda v: events.append(("clip", v)))))

    scheduled_enters = []
    keys = KeyOutput()
    monkeypatch.setattr(keys, "shortcut", lambda chord: events.append(("keys", list(chord))))
    runner = ActionRunner(keys, text_enter=lambda owner: scheduled_enters.append(owner))

    # Standalone text(enter=False) does NOT invoke text_enter or Enter shortcut
    runner.run(Action("text", text="hello", enter=False), True)
    assert events == [("clip", "hello"), ("keys", ["cmd", "v"])]
    assert scheduled_enters == []

    # Standalone text(enter=True) triggers non-blocking deferred enter callback once
    runner.run(Action("text", text="world", enter=True), True, owner="btn1")
    assert events == [
        ("clip", "hello"), ("keys", ["cmd", "v"]),
        ("clip", "world"), ("keys", ["cmd", "v"]),
    ]
    assert scheduled_enters == ["btn1"]

    # Inside MacroRunner, text(enter=True) waits paste settle then sends enter, then next step
    events.clear()
    macro_runner = MacroRunner(
        lambda act, _idx: runner.run(act, True, owner="macro"),
        send_enter=lambda: events.append(("macro_enter",)),
    )
    finished = []
    macro_runner.finished.connect(finished.append)
    macro_runner.start(
        Action(
            "macro",
            target="粘贴发送并切走",
            steps=[
                MacroStep(Action("text", text="第一句", enter=True), delay_ms=40),
                MacroStep(Action("shortcut", ["esc"]), delay_ms=0),
            ],
        ),
        owner="trig1",
    )
    assert events == [("clip", "第一句"), ("keys", ["cmd", "v"])]
    qtbot.waitUntil(lambda: bool(finished), timeout=1500)
    assert events == [
        ("clip", "第一句"),
        ("keys", ["cmd", "v"]),
        ("macro_enter",),
        ("keys", ["esc"]),
    ]

    # Aborting macro mid-settle skips pending enter!
    events.clear()
    aborted = []
    macro_runner.aborted.connect(aborted.append)
    macro_runner.start(
        Action("macro", steps=[MacroStep(Action("text", text="别发送", enter=True), delay_ms=20)]),
        owner="trig2",
    )
    macro_runner.handle_key("tv-back", True, "tv-back")
    assert aborted == ["已中止动作链。"]
    qtbot.wait(180)
    assert ("macro_enter",) not in events


def _voice_focus_controller(tmp_path, monkeypatch, backend):
    ctl = AppController(tmp_path, auto_mapping=False)
    monkeypatch.setattr(ctl.chat_focus, "supported", True)
    monkeypatch.setattr(ctl.chat_focus, "live", True)
    ctl.chat_focus._backend = backend
    ctl.chat_focus.preferences.save({
        "enabled": True,
        "apps": {
            backend.app.bundle: {
                "name": backend.app.name,
                "mode": "auto",
                "enabled": True,
                "locator": backend.describe(backend.app, backend.window_node, backend.chat_area),
            }
        },
    })
    ctl.voice.switch_input = False  # never touch the real default input in tests
    chords = []
    monkeypatch.setattr(ctl.voice.keys, "press", lambda keys: chords.append(backend.focused_node is backend.chat_area))
    monkeypatch.setattr(ctl.voice.keys, "release", lambda: None)
    monkeypatch.setattr(ctl.voice.audio, "start", lambda *a: None)
    monkeypatch.setattr(ctl.voice.audio, "feed", lambda pcm: None)
    return ctl, chords


def test_usb_stream_opened_before_key_still_focuses_chat_before_recognition(qtbot, tmp_path, monkeypatch):
    """VT-03: the receiver opens its audio stream before the GUI sees the voice
    key, so the key handler is adopted and never runs. The pre-voice focus must
    therefore hold the recognition shortcut at session start."""
    backend = _FakeAXBackend()
    backend.focused_node = backend.window_node  # user clicked chat history
    ctl, chords = _voice_focus_controller(tmp_path, monkeypatch, backend)
    ctl.update_voice(voice_source="trigger", record_mode="hold", voice_keys="alt", voice_mode="hold")

    ctl.voice.start("usb")          # what UsbBridge._collected does on "start"
    ctl.voice.feed("usb", b"\0\0")  # speech arriving during the check is kept
    assert chords == []             # shortcut held until focus resolves
    qtbot.waitUntil(lambda: chords == [True], timeout=1500)
    assert backend.focused_requests == [backend.chat_area]
    ctl.shutdown()


def test_trigger_voice_focus_guard_cancel_race_and_toggle(qtbot, tmp_path, monkeypatch):
    backend = _FakeAXBackend()
    backend.focused_node = backend.window_node
    ctl, chords = _voice_focus_controller(tmp_path, monkeypatch, backend)
    ctl.update_voice(voice_source="trigger", record_mode="hold", voice_keys="alt", voice_mode="hold")

    ctl.voice_button(True)
    assert ctl.recording_requested is True
    qtbot.waitUntil(lambda: chords == [True], timeout=1500)
    ctl.voice_button(False)
    qtbot.waitUntil(lambda: ctl.voice.owner is None, timeout=1500)

    # Quick release before the focus result: no orphaned recognition chord.
    chords.clear()
    backend.focused_node = backend.window_node
    backend.chat_area.is_focused = False
    ctl.voice_button(True)
    ctl.voice_button(False)
    qtbot.wait(700)
    assert chords == [] and ctl.voice.owner is None

    # Turning the option off sends the shortcut immediately, without focusing.
    assert ctl.set_voice_auto_focus(False)
    backend.focused_node = backend.window_node
    ctl.voice_button(True)
    assert chords == [False]
    ctl.shutdown()


def test_ui_pickers_inspector_macro_and_grid_integration(qtbot, tmp_path, monkeypatch):
    backend = _FakeAXBackend()
    backend.cursor_elem = backend.next_btn_text

    # 1. UiControlPickerDialog: hover capture auto-locks after steady hover streak
    picker = UiControlPickerDialog(
        default_app_id=backend.app.bundle,
        default_app_name=backend.app.name,
        apps_provider=lambda: [backend.app],
        backend_factory=lambda: backend,
    )
    qtbot.addWidget(picker)
    picker.show()
    qtbot.waitUntil(lambda: picker.app_box.width() > 0, timeout=1500)
    assert picker.app_box.width() == picker.op_box.width()
    assert picker.app_box.height() == picker.op_box.height()
    assert picker.app_box.visibleRegion().boundingRect().height() == picker.app_box.height()
    assert picker.op_box.visibleRegion().boundingRect().height() == picker.op_box.height()
    assert picker.app_box.currentData() == backend.app.bundle
    picker.start_capture()
    qtbot.waitUntil(lambda: not picker._capturing and picker.save_button.isEnabled(), timeout=2500)
    assert picker.name_edit.text() == "下一页"
    picker._accept_action()
    ui_action = picker.result_action()
    assert ui_action is not None and ui_action.kind == "ui_element" and ui_action.target == "press"
    assert ui_action.spec["label"] == "下一页"

    # 2. AppMenuPickerDialog: filter, toggle scope, accept
    menu_picker = AppMenuPickerDialog(
        default_app_id=backend.app.bundle,
        default_app_name=backend.app.name,
        apps_provider=lambda: [backend.app],
        menu_loader=lambda app_id: (
            backend.app,
            [
                MenuCommand(("显示", "隐藏侧边栏"), enabled=True),
                MenuCommand(("显示", "不可用命令"), enabled=False),
            ],
        ),
    )
    qtbot.addWidget(menu_picker)
    menu_picker.show()
    qtbot.waitUntil(lambda: menu_picker.app_box.width() > 0, timeout=1500)
    assert menu_picker.app_box.parentWidget().height() >= menu_picker.app_box.height()
    assert menu_picker.app_box.visibleRegion().boundingRect().height() == menu_picker.app_box.height()
    assert menu_picker.menu_list.count() == 2
    menu_picker.search_edit.setText("隐藏")
    assert menu_picker.menu_list.count() == 1
    menu_picker._accept_action()
    menu_action = menu_picker.result_action()
    assert menu_action is not None and menu_action.kind == "app_menu"
    assert menu_action.spec["path"] == ["显示", "隐藏侧边栏"]

    # 3. MacroEditorDialog & GridEditorDialog compose new steps cleanly
    macro_dlg = MacroEditorDialog(
        None,
        default_app_id=backend.app.bundle,
        default_app_name=backend.app.name,
        pick_ui_fn=lambda *_: ui_action,
        pick_menu_fn=lambda *_: menu_action,
    )
    qtbot.addWidget(macro_dlg)
    macro_dlg.kind_combo.setCurrentIndex(macro_dlg.kind_combo.findData("ui_element"))
    macro_dlg.pick_step_ui_element()
    macro_dlg.add_step(MacroStep(Action("text", text="总结一下", enter=True), delay_ms=100))
    built_macro = macro_dlg.build_action()
    assert built_macro.steps[0].action == ui_action
    assert built_macro.steps[1].action == Action("text", text="总结一下", enter=True)

    grid_dlg = GridEditorDialog(
        None,
        default_app_id=backend.app.bundle,
        default_app_name=backend.app.name,
        pick_ui_fn=lambda *_: ui_action,
        pick_menu_fn=lambda *_: menu_action,
    )
    qtbot.addWidget(grid_dlg)
    grid_dlg.kind_combo.setCurrentIndex(grid_dlg.kind_combo.findData("app_menu"))
    grid_dlg.pick_cell_app_menu()
    grid_dlg.select_slot(1)
    grid_dlg.kind_combo.setCurrentIndex(grid_dlg.kind_combo.findData("text"))
    grid_dlg.cell_text.setPlainText("你好")
    grid_dlg.cell_text_enter.setChecked(True)
    built_grid = grid_dlg.build_action()
    assert built_grid.cells[0].action == menu_action
    assert built_grid.cells[1].action == Action("text", text="你好", enter=True)

    # 4. Inspector layout & text_enter persistence inside MainWindow (no horizontal overflow)
    import sys as _sys
    if _sys.platform not in ("darwin", "win32"):
        monkeypatch.setattr("remotehub.presentation.SUPPORTED_AX_PLATFORMS", ("darwin", "win32", _sys.platform))
    w = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(w)
    w.show()
    c, ins = w.controller, w.inspector
    QApplication.processEvents()
    assert not w.sidebar.new_profile_button.isHidden()
    assert w.sidebar.new_profile_button.text() == "新建…"
    assert w.sidebar.new_profile_button.x() < w.sidebar.rename_single_button.x()
    assert w.sidebar.new_profile_button.parentWidget().width() <= w.sidebar.width()
    c.create_profile("方案二")
    QApplication.processEvents()
    assert not w.sidebar.new_profile_button.isHidden()
    assert not w.sidebar.manage_profile.isHidden()
    assert w.sidebar.new_profile_button.x() < w.sidebar.manage_profile.x()
    assert w.sidebar.manage_profile.geometry().right() <= w.sidebar.new_profile_button.parentWidget().width()
    c.select("tv-number-1")
    ins.choose_kind("text")
    ins.text_input.setPlainText("每日总结")
    ins.text_enter.setChecked(True)
    assert ins.text_dirty and ins.text_save.isEnabled()
    assert ins.save_text()
    assert c.selected.action == Action("text", text="每日总结", enter=True)

    # Check frozen top header stays pinned even when the lower palette scroll is scrolled to the bottom
    ins.body_scroll.verticalScrollBar().setValue(ins.body_scroll.verticalScrollBar().maximum())
    QApplication.processEvents()
    ins.choose_kind("ui_element")
    QApplication.processEvents()
    assert ins.header_panel.y() == 0
    assert ins.ui_element_pick.isVisible()
    assert ins.ui_element_pick.mapTo(ins, ins.ui_element_pick.rect().topLeft()).y() >= 0
    assert ins.ui_element_pick.mapTo(ins, ins.ui_element_pick.rect().bottomLeft()).y() < ins.body_scroll.y()
    rendered = ins.grab().toImage()
    rule_y = ins.action_divider.mapTo(ins, ins.action_divider.rect().topLeft()).y()
    scroll_y = ins.body_scroll.mapTo(ins, ins.body_scroll.rect().bottomLeft()).y()
    assert rendered.pixelColor(8, rule_y - 4) == rendered.pixelColor(8, scroll_y - 8)

    # Check ui_element & app_menu inspector subpages preserve zero horizontal scrollbar overflow
    c.select("tv-number-2")
    for kind, act in (("ui_element", ui_action), ("app_menu", menu_action)):
        c.assign(act)
        w.resize(1200, 720)
        QApplication.processEvents()
        for area in w.findChildren(QScrollArea):
            if area.isVisible() and area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
                assert area.horizontalScrollBar().maximum() == 0
    w.shutdown()
