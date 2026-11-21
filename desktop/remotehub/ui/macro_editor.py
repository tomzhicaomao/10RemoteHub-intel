"""Modal editor for configuring a multi-step Action Chain (Macro)."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from ..common_actions import available_actions
from ..model import (
    Action, DEFAULT_MACRO_DELAY_MS, FORBIDDEN_MACRO_STEP_COMMON,
    MAX_MACRO_DELAY_MS, MAX_MACRO_STEPS, MacroStep,
)
from ..presentation import action_text, chip_hint, key_label
from ..theme import repolish, stylesheet
from .widgets import (
    ScrollSafeComboBox, button, keys_sequence, label, row, rule,
    scroll, sequence_keys, set_invalid,
)


STEP_KINDS = (
    ("shortcut", "按键 / 组合键"),
    ("text", "输入文本（粘贴短语 / AI 提示词）"),
    ("mouse", "鼠标点击（左键 / 双击 / 右键 / 中键）"),
    ("toggle_app", "打开 / 收起 App"),
    ("file", "打开文件"),
    ("common", "常见系统操作"),
    ("ui_element", "界面控件（点击 / 聚焦）"),
    ("app_menu", "应用菜单命令"),
    ("minimize_window", "收起当前窗口"),
    ("close_window", "关闭当前窗口"),
    ("quit_app", "退出当前应用"),
)


def _available_step_kinds(allow_custom_ax: bool = False):
    if sys.platform in ("darwin", "win32") or allow_custom_ax:
        return STEP_KINDS
    return tuple(item for item in STEP_KINDS if item[0] not in ("ui_element", "app_menu"))


def _clone_steps(action: Action) -> list[MacroStep]:
    if action.kind == "macro" and action.steps:
        return [
            MacroStep(
                action=Action.from_dict(asdict(step.action)),
                delay_ms=step.delay_ms,
            )
            for step in action.steps[:MAX_MACRO_STEPS]
        ]
    return [MacroStep(action=Action("shortcut", keys=[]), delay_ms=DEFAULT_MACRO_DELAY_MS)]


def _step_summary_text(step: MacroStep) -> tuple[str, bool]:
    """Return (display_summary, is_complete) for a draft MacroStep."""
    try:
        step.validate(platform=sys.platform)
        return action_text(step.action), True
    except ValueError:
        kind = step.action.kind
        placeholders = {
            "shortcut": "待补全 · 请录制或选择按键",
            "text": "待补全 · 请输入要粘贴的文本",
            "toggle_app": "待补全 · 请选择应用程序",
            "app": "待补全 · 请选择应用程序",
            "file": "待补全 · 请选择要打开的文件",
            "common": "待补全 · 请选择系统操作",
            "ui_element": "待补全 · 请指定界面控件",
            "app_menu": "待补全 · 请选择应用菜单命令",
        }
        return placeholders.get(kind, "待补全步骤"), False


class _ScrollSafeSpinBox(QSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


class _StepPreviewButton(QPushButton):
    def __init__(self, index: int, callback, parent=None):
        super().__init__(parent)
        self.index = index
        self.setProperty("role", "macroStep")
        self.setCheckable(True)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(52)
        self.clicked.connect(lambda checked=False: callback(self.index))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 6, 9, 6)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(7)
        self.badge = QLabel(str(index + 1))
        self.badge.setObjectName("gridNumber")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label = label("", "value", wrap=False)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(top)
        self.sub_label = label("", "caption", wrap=False)
        self.sub_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.sub_label)

    def refresh_step(self, step: MacroStep, *, total: int, checked: bool):
        self.badge.setText(str(self.index + 1))
        self.setChecked(checked)
        summary, complete = _step_summary_text(step)
        if self.index >= total - 1:
            delay_text = "最后一步 · 执行完结束"
        elif step.delay_ms > 0:
            delay_text = f"等待 {step.delay_ms} ms → 下一步"
        else:
            delay_text = "无等待 → 紧接下一步"
        t_metrics = QFontMetrics(self.title_label.font())
        s_metrics = QFontMetrics(self.sub_label.font())
        self.title_label.setText(t_metrics.elidedText(summary, Qt.TextElideMode.ElideRight, 210))
        self.sub_label.setText(s_metrics.elidedText(delay_text, Qt.TextElideMode.ElideRight, 236))
        self.setAccessibleName(f"第 {self.index + 1} 步：{summary}")
        inc_str = "false" if complete else "true"
        if self.property("incomplete") != inc_str:
            self.setProperty("incomplete", inc_str)
            repolish(self)
        badge_sel = "true" if checked else "false"
        badge_empty = "true" if (not complete and not checked) else "false"
        if self.badge.property("empty") != badge_empty or self.badge.property("selected") != badge_sel:
            self.badge.setProperty("empty", badge_empty)
            self.badge.setProperty("selected", badge_sel)
            repolish(self.badge)


class MacroEditorDialog(QDialog):
    """Configure an ordered 1..8 step Action Chain with per-step QTimer delays."""

    def __init__(
        self,
        initial: Action | None = None,
        parent=None,
        *,
        button_name: str = "",
        initial_step: int = 0,
        default_app_id: str = "",
        default_app_name: str = "",
        choose_app_fn=None,
        choose_file_fn=None,
        pick_ui_fn=None,
        pick_menu_fn=None,
    ):
        super().__init__(parent)
        from .inspector import choose_application, choose_file

        self._default_app_id = default_app_id
        self._default_app_name = default_app_name
        self._choose_app = choose_app_fn or choose_application
        self._choose_file = choose_file_fn or choose_file
        self._pick_ui = pick_ui_fn
        self._pick_menu = pick_menu_fn
        base = initial if initial is not None else Action()
        self.steps: list[MacroStep] = _clone_steps(base)
        self.current_step = min(max(0, initial_step), len(self.steps) - 1)
        self._loading = False
        self._legacy_app_kind = False
        self._saved_action: Action | None = None

        suffix = f" · {button_name}" if button_name else ""
        self.setWindowTitle(f"配置多步动作链{suffix}")
        self.setModal(True)
        self.resize(780, 540)
        self.setMinimumSize(720, 490)
        self.setStyleSheet(stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        # Header: optional title + 1-click workflow templates
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_row.addWidget(label("动作链名称（可选）", "section", wrap=False))
        self.chain_title = QLineEdit(base.target if base.kind == "macro" else "")
        self.chain_title.setMaxLength(60)
        self.chain_title.setPlaceholderText("例如：粘贴提示词并发送 / 打开终端执行（留空自动按步骤生成）")
        self.chain_title.setAccessibleName("动作链名称")
        title_row.addWidget(self.chain_title, 1)
        root.addLayout(title_row)

        self.tpl_paste_enter = self._btn("模板：粘贴文本并回车", lambda: self.apply_template("paste_enter"), "quiet")
        self.tpl_app_paste = self._btn("模板：打开应用并粘贴", lambda: self.apply_template("app_paste"), "quiet")
        root.addWidget(row(label("常用组合：", "caption", wrap=False), self.tpl_paste_enter, self.tpl_app_paste, None, spacing=6))
        root.addWidget(rule())

        # Split body: left ordered step list | right step editor
        body = QHBoxLayout()
        body.setSpacing(18)

        left_widget = QWidget()
        left_widget.setFixedWidth(286)
        left = QVBoxLayout(left_widget)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)
        self.count_label = label("", "caption", wrap=False)
        left.addWidget(row(label("执行顺序（由上到下）", "caption", wrap=False), None, self.count_label))

        list_container = QWidget()
        self.step_list_layout = QVBoxLayout(list_container)
        self.step_list_layout.setContentsMargins(0, 0, 4, 0)
        self.step_list_layout.setSpacing(6)
        self.step_buttons: list[_StepPreviewButton] = []
        for idx in range(MAX_MACRO_STEPS):
            btn = _StepPreviewButton(idx, self.select_step, list_container)
            self.step_list_layout.addWidget(btn)
            self.step_buttons.append(btn)
        self.step_list_layout.addStretch(1)
        left.addWidget(scroll(list_container, horizontal=False), 1)

        self.add_step_button = self._btn("+ 添加步骤", lambda: self.add_step())
        self.move_up_button = self._btn("↑ 上移", lambda: self.move_step(-1), "quiet")
        self.move_down_button = self._btn("↓ 下移", lambda: self.move_step(1), "quiet")
        self.remove_step_button = self._btn("删除", self.remove_current_step, "danger")
        left.addWidget(row(self.add_step_button, self.move_up_button, self.move_down_button, None, self.remove_step_button, spacing=4))
        body.addWidget(left_widget, 0)

        right_card = QWidget()
        right_card.setObjectName("panel")
        right_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        right = QVBoxLayout(right_card)
        right.setContentsMargins(16, 14, 16, 14)
        right.setSpacing(8)

        self.step_heading = label("", "heading", wrap=False)
        right.addWidget(self.step_heading)

        right.addWidget(label("本步动作类型", "caption"))
        self.kind_combo = ScrollSafeComboBox()
        self.kind_combo.setAccessibleName("步骤动作类型")
        for key, text in _available_step_kinds(bool(pick_ui_fn or pick_menu_fn)):
            self.kind_combo.addItem(text, key)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        right.addWidget(self.kind_combo)

        self.param_stack = QStackedWidget()
        self.param_stack.addWidget(self._build_hint_page())        # 0: window actions
        self.param_stack.addWidget(self._build_text_page())        # 1: text
        self.param_stack.addWidget(self._build_shortcut_page())    # 2: shortcut
        self.param_stack.addWidget(self._build_app_page())         # 3: toggle_app / app
        self.param_stack.addWidget(self._build_file_page())        # 4: file
        self.param_stack.addWidget(self._build_common_page())      # 5: common
        self.param_stack.addWidget(self._build_ui_element_page())  # 6: ui_element
        self.param_stack.addWidget(self._build_app_menu_page())    # 7: app_menu
        self.param_stack.addWidget(self._build_mouse_page())       # 8: mouse
        right.addWidget(self.param_stack, 1)

        right.addWidget(rule())
        self.delay_spin = _ScrollSafeSpinBox()
        self.delay_spin.setRange(0, MAX_MACRO_DELAY_MS)
        self.delay_spin.setSingleStep(50)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setAccessibleName("步骤等待时间")
        self.delay_spin.valueChanged.connect(lambda *_: self._on_field_changed())
        right.addWidget(row(
            label("执行完本步后等待", "caption", wrap=False),
            self.delay_spin,
            self._btn("120 ms", lambda: self.delay_spin.setValue(120), "link"),
            self._btn("500 ms", lambda: self.delay_spin.setValue(500), "link"),
            None,
            spacing=8,
        ))
        self.delay_hint = label("", "caption")
        right.addWidget(self.delay_hint)

        self.error_label = label("", wrap=True)
        self.error_label.setObjectName("inlineError")
        self.error_label.hide()
        right.addWidget(self.error_label)

        body.addWidget(right_card, 1)
        root.addLayout(body, 1)
        root.addWidget(rule())

        self.cancel_button = self._btn("取消", self.reject)
        self.save_button = self._btn("保存动作链", self.accept_macro, "primary")
        root.addWidget(row(
            label("安全提示：任一步骤出错会自动停止后续步骤；等待期间按遥控器返回键可立即中止。", "caption"),
            None,
            self.cancel_button,
            self.save_button,
        ))

        self._load_step(self.current_step)
        self._refresh_previews()

    def _btn(self, text: str, callback, role=None) -> QPushButton:
        w = button(text, callback, role)
        w.setAutoDefault(False)
        w.setDefault(False)
        return w

    # -- Parameter subpages ----------------------------------------------------

    def _build_hint_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        self.simple_hint = label("", "caption")
        box.addWidget(self.simple_hint)
        box.addStretch(1)
        return page

    def _build_text_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label("本步粘贴到当前光标位置的文本；可勾选粘贴后直接回车，或后接其他按键步骤", "caption"))
        self.step_text = QPlainTextEdit()
        self.step_text.setPlaceholderText("支持中文、多行文本与常用 AI 提示词")
        self.step_text.setAccessibleName("步骤输入文本")
        self.step_text.setTabChangesFocus(True)
        self.step_text.textChanged.connect(self._on_field_changed)
        box.addWidget(self.step_text, 1)
        self.step_text_enter = QCheckBox("粘贴后自动按回车发送")
        self.step_text_enter.setAccessibleName("步骤文本自动回车")
        self.step_text_enter.toggled.connect(lambda *_: self._on_field_changed())
        box.addWidget(self.step_text_enter)
        return page

    def _build_shortcut_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label("点击录制框按下组合键，或直接点下方常用键：", "caption"))
        self.step_sequence = QKeySequenceEdit()
        self.step_sequence.setMaximumSequenceLength(1)
        self.step_sequence.setAccessibleName("步骤快捷键")
        self.step_sequence.keySequenceChanged.connect(lambda *_: self._on_field_changed())
        box.addWidget(self.step_sequence)

        mod_enter = ["cmd" if sys.platform == "darwin" else "ctrl", "enter"]
        enter_title = "回车 ↩" if sys.platform == "darwin" else "回车 Enter"
        box.addWidget(row(
            self._btn(enter_title, lambda: self._set_shortcut_keys(["enter"]), "quiet"),
            self._btn(f"{key_label(mod_enter)}", lambda: self._set_shortcut_keys(mod_enter), "quiet"),
            self._btn("Tab", lambda: self._set_shortcut_keys(["tab"]), "quiet"),
            self._btn("Esc", lambda: self._set_shortcut_keys(["esc"]), "quiet"),
            None,
            spacing=6,
        ))
        box.addStretch(1)
        return page

    def _set_shortcut_keys(self, keys: list[str]):
        self.step_sequence.setKeySequence(keys_sequence(keys))
        self._on_field_changed()

    def _build_app_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label("未运行时启动；在后台时切到前台（建议等待时间设为 400–800 ms）", "caption"))
        self.step_app = QLineEdit()
        self.step_app.setReadOnly(True)
        self.step_app.setPlaceholderText("尚未选择应用程序")
        self.step_app.setAccessibleName("步骤应用程序")
        box.addWidget(row(self.step_app, self._btn("选择…", self.pick_step_app), spacing=6))
        box.addStretch(1)
        return page

    def _build_file_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self.step_file = QLineEdit()
        self.step_file.setReadOnly(True)
        self.step_file.setPlaceholderText("尚未选择文件")
        self.step_file.setAccessibleName("步骤文件路径")
        box.addWidget(row(self.step_file, self._btn("选择…", self.pick_step_file), spacing=6))
        self._step_file_app_path = ""
        self.step_file_app_label = label("打开方式：系统默认", "caption")
        box.addWidget(self.step_file_app_label)
        box.addWidget(row(
            self._btn("指定打开方式…", self.pick_step_file_app, "link"),
            self._btn("系统默认", self.reset_step_file_app, "link"),
            None,
            spacing=14,
        ))
        box.addStretch(1)
        return page

    def _build_common_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label("选择常见系统操作", "caption"))
        self.common_combo = ScrollSafeComboBox()
        self.common_combo.setAccessibleName("步骤常见系统操作")
        for op, title in available_actions().items():
            if op not in FORBIDDEN_MACRO_STEP_COMMON:
                self.common_combo.addItem(title, op)
        self.common_combo.currentIndexChanged.connect(self._on_field_changed)
        box.addWidget(self.common_combo)
        box.addStretch(1)
        return page

    def _build_ui_element_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self._step_ui_action: Action | None = None
        box.addWidget(label("先聚焦某个阅读区/列表框再翻页，或点击没有快捷键的按钮", "caption"))
        self.step_ui_summary = label("尚未指定界面控件", "value", wrap=True)
        box.addWidget(self.step_ui_summary)
        box.addWidget(row(self._btn("指定界面控件…", self.pick_step_ui_element, "primary"), None))
        box.addStretch(1)
        return page

    def _build_app_menu_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self._step_menu_action: Action | None = None
        box.addWidget(label("调用目标应用顶部菜单栏中的命令（如 显示 → 隐藏侧边栏）", "caption"))
        self.step_menu_summary = label("尚未选择菜单命令", "value", wrap=True)
        box.addWidget(self.step_menu_summary)
        box.addWidget(row(self._btn("选择菜单命令…", self.pick_step_app_menu, "primary"), None))
        box.addStretch(1)
        return page

    def _build_mouse_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label("执行到本步时，在当前鼠标指针位置点击：", "caption"))
        self.mouse_combo = ScrollSafeComboBox()
        self.mouse_combo.setAccessibleName("步骤鼠标动作")
        for target, title in (
            ("left", "鼠标左键（单击）"),
            ("left_double", "鼠标左键（双击）"),
            ("right", "鼠标右键（单击）"),
            ("middle", "鼠标中键（单击）"),
        ):
            self.mouse_combo.addItem(title, target)
        self.mouse_combo.currentIndexChanged.connect(self._on_field_changed)
        box.addWidget(self.mouse_combo)
        box.addStretch(1)
        return page

    # -- Step synchronization --------------------------------------------------

    def _page_for_kind(self, kind: str) -> int:
        return {
            "text": 1,
            "shortcut": 2,
            "toggle_app": 3,
            "app": 3,
            "file": 4,
            "common": 5,
            "ui_element": 6,
            "app_menu": 7,
            "mouse": 8,
        }.get(kind, 0)

    def _clear_error(self):
        self.error_label.hide()
        set_invalid(self.step_text, False)
        set_invalid(self.step_sequence, False)
        set_invalid(self.chain_title, False)

    def _draft_step_from_widgets(self) -> MacroStep:
        kind = self.kind_combo.currentData() or "shortcut"
        delay = int(self.delay_spin.value())
        if kind == "text":
            act = Action("text", text=self.step_text.toPlainText(), enter=self.step_text_enter.isChecked())
        elif kind == "shortcut":
            act = Action("shortcut", keys=sequence_keys(self.step_sequence.keySequence()))
        elif kind == "toggle_app":
            actual_kind = "app" if self._legacy_app_kind else "toggle_app"
            act = Action(actual_kind, target=self.step_app.text().strip())
        elif kind == "file":
            act = Action("file", target=self.step_file.text().strip(), application=self._step_file_app_path)
        elif kind == "common":
            act = Action("common", target=self.common_combo.currentData() or "")
        elif kind == "mouse":
            act = Action("mouse", target=self.mouse_combo.currentData() or "left")
        elif kind == "ui_element":
            act = (
                Action.from_dict(asdict(self._step_ui_action))
                if self._step_ui_action is not None and self._step_ui_action.kind == "ui_element"
                else Action("ui_element")
            )
        elif kind == "app_menu":
            act = (
                Action.from_dict(asdict(self._step_menu_action))
                if self._step_menu_action is not None and self._step_menu_action.kind == "app_menu"
                else Action("app_menu")
            )
        else:
            act = Action(kind)
        return MacroStep(action=act, delay_ms=delay)

    def _flush_current_step(self):
        if self._loading or not (0 <= self.current_step < len(self.steps)):
            return
        self.steps[self.current_step] = self._draft_step_from_widgets()

    def _load_step(self, index: int):
        self._loading = True
        self.current_step = index
        step = self.steps[index]
        act = step.action
        self._legacy_app_kind = act.kind == "app"

        combo_kind = "toggle_app" if act.kind == "app" else act.kind
        idx = self.kind_combo.findData(combo_kind)
        if idx < 0 and combo_kind in ("ui_element", "app_menu"):
            self.kind_combo.addItem("界面控件（点击 / 聚焦）" if combo_kind == "ui_element" else "应用菜单命令", combo_kind)
            idx = self.kind_combo.findData(combo_kind)
        self.kind_combo.setCurrentIndex(max(0, idx))

        self.step_text.setPlainText(act.text if act.kind == "text" else "")
        self.step_text_enter.setChecked(bool(act.enter) if act.kind == "text" else False)
        self.step_sequence.setKeySequence(keys_sequence(act.keys if act.kind == "shortcut" else []))
        self.step_app.setText(act.target if act.kind in ("toggle_app", "app") else "")
        self.step_file.setText(act.target if act.kind == "file" else "")
        self._step_file_app_path = act.application if act.kind == "file" else ""
        self._update_file_app_label()
        self._step_ui_action = Action.from_dict(asdict(act)) if act.kind == "ui_element" else None
        self._step_menu_action = Action.from_dict(asdict(act)) if act.kind == "app_menu" else None
        self._update_ax_labels()
        if act.kind == "common":
            c_idx = self.common_combo.findData(act.target)
            if c_idx >= 0:
                self.common_combo.setCurrentIndex(c_idx)
        if act.kind == "mouse":
            m_idx = self.mouse_combo.findData(act.target)
            if m_idx >= 0:
                self.mouse_combo.setCurrentIndex(m_idx)
        self.delay_spin.setValue(step.delay_ms)
        self._sync_param_page()
        self._clear_error()
        self._loading = False

    def _sync_param_page(self):
        kind = self.kind_combo.currentData() or "shortcut"
        self.param_stack.setCurrentIndex(self._page_for_kind(kind))
        self.simple_hint.setText(chip_hint(kind))
        is_last = self.current_step >= len(self.steps) - 1
        self.delay_spin.setEnabled(not is_last)
        self.delay_hint.setText(
            "当前为最后一步，执行完直接结束，无需等待。"
            if is_last
            else "给目标应用留出响应时间（粘贴后按回车建议 120 ms；启动应用建议 450 ms）。"
        )

    def _update_file_app_label(self):
        name = Path(self._step_file_app_path).name if self._step_file_app_path else "系统默认"
        self.step_file_app_label.setText(f"打开方式：{name}")

    def _refresh_previews(self):
        total = len(self.steps)
        self.count_label.setText(f"共 {total} / {MAX_MACRO_STEPS} 步")
        self.step_heading.setText(f"第 {self.current_step + 1} 步（共 {total} 步）")
        for idx, btn in enumerate(self.step_buttons):
            visible = idx < total
            btn.setVisible(visible)
            if visible:
                btn.refresh_step(self.steps[idx], total=total, checked=(idx == self.current_step))
        self.add_step_button.setEnabled(total < MAX_MACRO_STEPS)
        self.move_up_button.setEnabled(self.current_step > 0)
        self.move_down_button.setEnabled(self.current_step < total - 1)
        self.remove_step_button.setEnabled(total > 1)
        self._sync_param_page()

    def _on_kind_changed(self):
        if self._loading:
            return
        self._legacy_app_kind = False
        self._sync_param_page()
        self._on_field_changed()

    def _on_field_changed(self):
        if self._loading:
            return
        self._clear_error()
        self._flush_current_step()
        self._refresh_previews()

    # -- Public & button handlers ----------------------------------------------

    def apply_template(self, name: str):
        if name == "paste_enter":
            existing_text = ""
            for s in self.steps:
                if s.action.kind == "text" and s.action.text:
                    existing_text = s.action.text
                    break
            self.steps = [
                MacroStep(action=Action("text", text=existing_text), delay_ms=DEFAULT_MACRO_DELAY_MS),
                MacroStep(action=Action("shortcut", keys=["enter"]), delay_ms=DEFAULT_MACRO_DELAY_MS),
            ]
        elif name == "app_paste":
            existing_app = ""
            existing_text = ""
            for s in self.steps:
                if s.action.kind in ("toggle_app", "app") and s.action.target and not existing_app:
                    existing_app = s.action.target
                elif s.action.kind == "text" and s.action.text and not existing_text:
                    existing_text = s.action.text
            self.steps = [
                MacroStep(action=Action("toggle_app", target=existing_app), delay_ms=450),
                MacroStep(action=Action("text", text=existing_text), delay_ms=DEFAULT_MACRO_DELAY_MS),
            ]
        else:
            raise ValueError(f"未知动作链模板：{name}")
        self._load_step(0)
        self._refresh_previews()

    def select_step(self, index: int):
        if not 0 <= index < len(self.steps):
            return
        self._flush_current_step()
        self._load_step(index)
        self._refresh_previews()

    def add_step(self, step: MacroStep | None = None) -> bool:
        self._flush_current_step()
        if len(self.steps) >= MAX_MACRO_STEPS:
            return False
        new_step = step if step is not None else MacroStep(
            action=Action("shortcut", keys=[]),
            delay_ms=DEFAULT_MACRO_DELAY_MS,
        )
        insert_at = self.current_step + 1
        self.steps.insert(insert_at, new_step)
        self._load_step(insert_at)
        self._refresh_previews()
        return True

    def move_step(self, delta: int) -> bool:
        self._flush_current_step()
        target = self.current_step + delta
        if not 0 <= target < len(self.steps):
            return False
        self.steps[self.current_step], self.steps[target] = self.steps[target], self.steps[self.current_step]
        self._load_step(target)
        self._refresh_previews()
        return True

    def remove_current_step(self) -> bool:
        if len(self.steps) <= 1:
            return False
        self.steps.pop(self.current_step)
        next_idx = min(self.current_step, len(self.steps) - 1)
        self._load_step(next_idx)
        self._refresh_previews()
        return True

    def set_step(self, index: int, action: Action, delay_ms: int = DEFAULT_MACRO_DELAY_MS):
        """Programmatic helper for setting a step's action and delay."""
        if not 0 <= index < len(self.steps):
            raise ValueError("步骤编号超出范围")
        self._flush_current_step()
        self.steps[index] = MacroStep(action=action, delay_ms=delay_ms)
        if index == self.current_step:
            self._load_step(index)
        self._refresh_previews()

    def pick_step_app(self):
        path = self._choose_app(self)
        if path:
            self.step_app.setText(path)
            self._on_field_changed()

    def pick_step_file(self):
        path = self._choose_file(self)
        if path:
            self.step_file.setText(path)
            self._on_field_changed()

    def pick_step_file_app(self):
        path = self._choose_app(self)
        if path:
            self._step_file_app_path = path
            self._update_file_app_label()
            self._on_field_changed()

    def reset_step_file_app(self):
        self._step_file_app_path = ""
        self._update_file_app_label()
        self._on_field_changed()

    def _update_ax_labels(self):
        if self._step_ui_action is not None and self._step_ui_action.kind == "ui_element" and self._step_ui_action.spec:
            self.step_ui_summary.setText(action_text(self._step_ui_action))
        else:
            self.step_ui_summary.setText("尚未指定界面控件")
        if self._step_menu_action is not None and self._step_menu_action.kind == "app_menu" and self._step_menu_action.spec:
            self.step_menu_summary.setText(action_text(self._step_menu_action))
        else:
            self.step_menu_summary.setText("尚未选择菜单命令")

    def pick_step_ui_element(self):
        if self._pick_ui is not None:
            saved = self._pick_ui(self, self._step_ui_action)
        else:
            from .ax_pickers import UiControlPickerDialog
            dialog = UiControlPickerDialog(
                self._step_ui_action,
                self,
                default_app_id=self._default_app_id,
                default_app_name=self._default_app_name,
            )
            saved = dialog.result_action() if dialog.exec() == UiControlPickerDialog.DialogCode.Accepted else None
        if saved is not None:
            self._step_ui_action = saved
            self._update_ax_labels()
            self._on_field_changed()

    def pick_step_app_menu(self):
        if self._pick_menu is not None:
            saved = self._pick_menu(self, self._step_menu_action)
        else:
            from .ax_pickers import AppMenuPickerDialog
            dialog = AppMenuPickerDialog(
                self._step_menu_action,
                self,
                default_app_id=self._default_app_id,
                default_app_name=self._default_app_name,
            )
            saved = dialog.result_action() if dialog.exec() == AppMenuPickerDialog.DialogCode.Accepted else None
        if saved is not None:
            self._step_menu_action = saved
            self._update_ax_labels()
            self._on_field_changed()

    def build_action(self) -> Action:
        self._flush_current_step()
        title = self.chain_title.text().strip()
        if len(title) > 60 or "\x00" in title:
            set_invalid(self.chain_title, True)
            raise ValueError("动作链名称应为 0–60 字")
        for idx, step in enumerate(self.steps):
            try:
                step.validate(platform=sys.platform)
            except ValueError as exc:
                self._load_step(idx)
                self._refresh_previews()
                if step.action.kind == "text":
                    set_invalid(self.step_text, True)
                elif step.action.kind == "shortcut":
                    set_invalid(self.step_sequence, True)
                raise ValueError(f"第 {idx + 1} 步：{exc}") from exc
        action = Action(
            kind="macro",
            target=title,
            steps=[
                MacroStep(
                    action=Action.from_dict(asdict(s.action)),
                    delay_ms=s.delay_ms,
                )
                for s in self.steps
            ],
        )
        action.validate(platform=sys.platform)
        return action

    def accept_macro(self) -> bool:
        try:
            self._saved_action = self.build_action()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return False
        self.accept()
        return True

    def result_action(self) -> Action | None:
        return self._saved_action
