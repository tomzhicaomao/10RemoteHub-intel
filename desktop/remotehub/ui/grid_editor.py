"""Modal editor for configuring a 3x3 On-Screen Grid Launcher action."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QGridLayout, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from ..common_actions import available_actions
from ..model import Action, FORBIDDEN_GRID_CELL_COMMON, GRID_SIZE, GridCell
from ..presentation import action_text, chip_hint, grid_cell_texts
from ..theme import repolish, stylesheet
from .widgets import (
    ScrollSafeComboBox, button, keys_sequence, label, row, rule,
    sequence_keys, set_invalid,
)


def _clone_cells(action: Action) -> list[GridCell]:
    if action.kind == "grid" and len(action.cells) == GRID_SIZE:
        return [
            GridCell(title=cell.title, action=Action.from_dict(asdict(cell.action)))
            for cell in action.cells
        ]
    return [GridCell() for _ in range(GRID_SIZE)]


SLOT_KINDS = (
    ("none", "未设置（空槽位）"),
    ("text", "输入文本（AI 提示词 / 常用短语）"),
    ("shortcut", "按键 / 组合键"),
    ("mouse", "鼠标点击（左键 / 双击 / 右键 / 中键）"),
    ("macro", "多步动作链（如：粘贴并回车）"),
    ("ui_element", "界面控件（点击 / 聚焦）"),
    ("app_menu", "应用菜单命令"),
    ("toggle_app", "打开 / 收起 App"),
    ("file", "打开文件"),
    ("common", "常见系统操作"),
    ("minimize_window", "收起当前窗口"),
    ("close_window", "关闭当前窗口"),
    ("quit_app", "退出当前应用"),
    ("cycle_profile", "循环切换配置方案"),
)


def _available_slot_kinds(allow_custom_ax: bool = False):
    if sys.platform in ("darwin", "win32") or allow_custom_ax:
        return SLOT_KINDS
    return tuple(item for item in SLOT_KINDS if item[0] not in ("ui_element", "app_menu"))


class _SlotPreviewButton(QPushButton):
    def __init__(self, index: int, callback, parent=None):
        super().__init__(parent)
        self.index = index
        self.setProperty("role", "gridSlot")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(108, 66)
        self.clicked.connect(lambda checked=False: callback(self.index))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(3)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
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

    def refresh_cell(self, cell: GridCell, *, checked: bool):
        self.setChecked(checked)
        empty = cell.action.kind == "none"
        title, sub = grid_cell_texts(cell)
        t_metrics = QFontMetrics(self.title_label.font())
        s_metrics = QFontMetrics(self.sub_label.font())
        self.title_label.setText(t_metrics.elidedText(title, Qt.TextElideMode.ElideRight, 78))
        self.sub_label.setText(s_metrics.elidedText(sub, Qt.TextElideMode.ElideRight, 98))
        self.setAccessibleName(f"格位 {self.index + 1}：{title}")
        empty_str = "true" if empty else "false"
        if self.property("empty") != empty_str:
            self.setProperty("empty", empty_str)
            repolish(self)
        badge_sel = "true" if checked else "false"
        badge_empty = "true" if (empty and not checked) else "false"
        if self.badge.property("empty") != badge_empty or self.badge.property("selected") != badge_sel:
            self.badge.setProperty("empty", badge_empty)
            self.badge.setProperty("selected", badge_sel)
            repolish(self.badge)


class GridEditorDialog(QDialog):
    """Configure a 9-cell On-Screen Grid Launcher with live 3x3 preview."""

    def __init__(
        self,
        initial: Action | None = None,
        parent=None,
        *,
        button_name: str = "",
        initial_slot: int = 0,
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
        self.cells = _clone_cells(base)
        self.current_slot = min(max(0, initial_slot), GRID_SIZE - 1)
        self._loading = False
        self._legacy_app_kind = False
        self._cell_macro_action: Action | None = None
        self._cell_ui_action: Action | None = None
        self._cell_menu_action: Action | None = None
        self._saved_action: Action | None = None

        suffix = f" · {button_name}" if button_name else ""
        self.setWindowTitle(f"配置九宫格面板{suffix}")
        self.setModal(True)
        self.resize(760, 520)
        self.setMinimumSize(700, 480)
        self.setStyleSheet(stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        # Panel title strip
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_row.addWidget(label("面板名称（可选）", "section", wrap=False))
        self.panel_title = QLineEdit(base.target if base.kind == "grid" else "")
        self.panel_title.setMaxLength(60)
        self.panel_title.setPlaceholderText("例如：AI 常用指令 / 会议与工作区（留空显示「九宫格面板」）")
        self.panel_title.setAccessibleName("九宫格面板名称")
        title_row.addWidget(self.panel_title, 1)
        root.addLayout(title_row)
        root.addWidget(rule())

        # Split body: left 3x3 selector | right slot editor
        body = QHBoxLayout()
        body.setSpacing(18)

        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(label("点击选择格位（对应遥控器数字键 1–9）", "caption"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.slot_buttons: list[_SlotPreviewButton] = []
        for idx in range(GRID_SIZE):
            btn = _SlotPreviewButton(idx, self.select_slot, self)
            grid.addWidget(btn, idx // 3, idx % 3)
            self.slot_buttons.append(btn)
        left.addLayout(grid)
        self.clear_slot_button = button("清空此格", self.clear_current_slot, "danger")
        self.count_label = label("", "caption", wrap=False)
        left.addWidget(row(self.clear_slot_button, None, self.count_label))
        left.addStretch(1)
        body.addLayout(left, 0)

        right_card = QWidget()
        right_card.setObjectName("panel")
        right_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        right = QVBoxLayout(right_card)
        right.setContentsMargins(16, 14, 16, 14)
        right.setSpacing(8)

        self.slot_heading = label("", "heading", wrap=False)
        right.addWidget(self.slot_heading)
        right.addWidget(label("显示名称（可选，留空自动按动作生成）", "caption"))
        self.cell_title = QLineEdit()
        self.cell_title.setMaxLength(60)
        self.cell_title.setPlaceholderText("例如：代码审查 / 打开终端 / 粘贴周报模板")
        self.cell_title.setAccessibleName("格位显示名称")
        self.cell_title.textChanged.connect(self._on_field_changed)
        right.addWidget(self.cell_title)

        right.addWidget(label("动作类型", "caption"))
        self.kind_combo = ScrollSafeComboBox()
        self.kind_combo.setAccessibleName("格位动作类型")
        for key, text in _available_slot_kinds(bool(pick_ui_fn or pick_menu_fn)):
            self.kind_combo.addItem(text, key)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        right.addWidget(self.kind_combo)

        self.param_stack = QStackedWidget()
        self.param_stack.addWidget(self._build_hint_page())        # 0: none / window actions
        self.param_stack.addWidget(self._build_text_page())        # 1: text
        self.param_stack.addWidget(self._build_shortcut_page())    # 2: shortcut
        self.param_stack.addWidget(self._build_app_page())         # 3: toggle_app / app
        self.param_stack.addWidget(self._build_file_page())        # 4: file
        self.param_stack.addWidget(self._build_common_page())      # 5: common
        self.param_stack.addWidget(self._build_macro_page())       # 6: macro
        self.param_stack.addWidget(self._build_ui_element_page())  # 7: ui_element
        self.param_stack.addWidget(self._build_app_menu_page())    # 8: app_menu
        self.param_stack.addWidget(self._build_mouse_page())       # 9: mouse
        right.addWidget(self.param_stack, 1)

        self.error_label = label("", wrap=True)
        self.error_label.setObjectName("inlineError")
        self.error_label.hide()
        right.addWidget(self.error_label)

        body.addWidget(right_card, 1)
        root.addLayout(body, 1)
        root.addWidget(rule())

        self.cancel_button = button("取消", self.reject)
        self.save_button = button("保存九宫格", self.accept_grid, "primary")
        root.addWidget(row(label("提示：打开面板后焦点在中央第 5 格，方向键一步可达四周。", "caption"), None, self.cancel_button, self.save_button))

        self._load_slot(self.current_slot)
        self._refresh_previews()

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
        box.addWidget(label("按下此格时粘贴到当前光标位置的文本", "caption"))
        self.cell_text = QPlainTextEdit()
        self.cell_text.setPlaceholderText("支持多行文本与 AI 提示词，可勾选下方粘贴后自动回车发送")
        self.cell_text.setAccessibleName("格位输入文本")
        self.cell_text.setTabChangesFocus(True)
        self.cell_text.textChanged.connect(self._on_field_changed)
        box.addWidget(self.cell_text, 1)
        self.cell_text_enter = QCheckBox("粘贴后自动按回车发送")
        self.cell_text_enter.setAccessibleName("格位文本自动回车")
        self.cell_text_enter.toggled.connect(lambda *_: self._on_field_changed())
        box.addWidget(self.cell_text_enter)
        return page

    def _build_shortcut_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label("点击下方输入框后按下单键或组合键", "caption"))
        self.cell_sequence = QKeySequenceEdit()
        self.cell_sequence.setMaximumSequenceLength(1)
        self.cell_sequence.setAccessibleName("格位快捷键")
        self.cell_sequence.keySequenceChanged.connect(lambda *_: self._on_field_changed())
        box.addWidget(self.cell_sequence)
        box.addStretch(1)
        return page

    def _build_app_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label("未运行时启动；在后台时切到前台；已在前台时收起", "caption"))
        self.cell_app = QLineEdit()
        self.cell_app.setReadOnly(True)
        self.cell_app.setPlaceholderText("尚未选择应用程序")
        self.cell_app.setAccessibleName("格位应用程序")
        box.addWidget(row(self.cell_app, button("选择…", self.pick_cell_app), spacing=6))
        box.addStretch(1)
        return page

    def _build_file_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self.cell_file = QLineEdit()
        self.cell_file.setReadOnly(True)
        self.cell_file.setPlaceholderText("尚未选择文件")
        self.cell_file.setAccessibleName("格位文件路径")
        box.addWidget(row(self.cell_file, button("选择…", self.pick_cell_file), spacing=6))
        self._cell_file_app_path = ""
        self.cell_file_app_label = label("打开方式：系统默认", "caption")
        box.addWidget(self.cell_file_app_label)
        box.addWidget(row(
            button("指定打开方式…", self.pick_cell_file_app, "link"),
            button("系统默认", self.reset_cell_file_app, "link"),
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
        self.common_combo.setAccessibleName("格位常见系统操作")
        for op, title in available_actions().items():
            if op not in FORBIDDEN_GRID_CELL_COMMON:
                self.common_combo.addItem(title, op)
        self.common_combo.currentIndexChanged.connect(self._on_field_changed)
        box.addWidget(self.common_combo)
        box.addStretch(1)
        return page

    def _build_macro_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(8)
        box.addWidget(label("点击此格时按顺序执行多步动作链（不会嵌套弹出面板）", "caption"))
        self.cell_macro_summary = label("", "value")
        box.addWidget(self.cell_macro_summary)
        self.cell_macro_edit_button = button("编辑动作链步骤…", self.edit_cell_macro, "primary")
        self.cell_macro_edit_button.setAutoDefault(False)
        self.cell_macro_edit_button.setDefault(False)
        box.addWidget(row(self.cell_macro_edit_button, None))
        box.addStretch(1)
        return page

    def _build_ui_element_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(8)
        box.addWidget(label("直接点击没有快捷键的按钮，或聚焦到指定区域", "caption"))
        self.cell_ui_summary = label("尚未指定界面控件", "value", wrap=True)
        box.addWidget(self.cell_ui_summary)
        pick_btn = button("指定界面控件…", self.pick_cell_ui_element, "primary")
        pick_btn.setAutoDefault(False)
        pick_btn.setDefault(False)
        box.addWidget(row(pick_btn, None))
        box.addStretch(1)
        return page

    def _build_app_menu_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(8)
        box.addWidget(label("直接触发应用顶部菜单栏中的命令", "caption"))
        self.cell_menu_summary = label("尚未选择菜单命令", "value", wrap=True)
        box.addWidget(self.cell_menu_summary)
        pick_btn = button("选择菜单命令…", self.pick_cell_app_menu, "primary")
        pick_btn.setAutoDefault(False)
        pick_btn.setDefault(False)
        box.addWidget(row(pick_btn, None))
        box.addStretch(1)
        return page

    def _build_mouse_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label("选择此格位在当前鼠标位置执行的点击动作：", "caption"))
        self.mouse_combo = ScrollSafeComboBox()
        self.mouse_combo.setAccessibleName("格位鼠标动作")
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

    # -- Slot synchronization --------------------------------------------------

    def _page_for_kind(self, kind: str) -> int:
        return {
            "text": 1,
            "shortcut": 2,
            "toggle_app": 3,
            "app": 3,
            "file": 4,
            "common": 5,
            "macro": 6,
            "ui_element": 7,
            "app_menu": 8,
            "mouse": 9,
        }.get(kind, 0)

    def _clear_error(self):
        self.error_label.hide()
        set_invalid(self.cell_text, False)
        set_invalid(self.cell_sequence, False)
        set_invalid(self.panel_title, False)

    def _draft_cell_from_widgets(self) -> GridCell:
        title = self.cell_title.text().strip()
        kind = self.kind_combo.currentData() or "none"
        if kind == "none":
            return GridCell(title=title, action=Action("none"))
        if kind == "text":
            return GridCell(
                title=title,
                action=Action("text", text=self.cell_text.toPlainText(), enter=self.cell_text_enter.isChecked()),
            )
        if kind == "shortcut":
            return GridCell(title=title, action=Action("shortcut", keys=sequence_keys(self.cell_sequence.keySequence())))
        if kind == "macro":
            if self._cell_macro_action is not None and self._cell_macro_action.kind == "macro":
                return GridCell(title=title, action=Action.from_dict(asdict(self._cell_macro_action)))
            return GridCell(title=title, action=Action("macro", steps=[]))
        if kind == "ui_element":
            if self._cell_ui_action is not None and self._cell_ui_action.kind == "ui_element":
                return GridCell(title=title, action=Action.from_dict(asdict(self._cell_ui_action)))
            return GridCell(title=title, action=Action("ui_element"))
        if kind == "app_menu":
            if self._cell_menu_action is not None and self._cell_menu_action.kind == "app_menu":
                return GridCell(title=title, action=Action.from_dict(asdict(self._cell_menu_action)))
            return GridCell(title=title, action=Action("app_menu"))
        if kind in ("toggle_app", "app"):
            actual_kind = "app" if self._legacy_app_kind else "toggle_app"
            return GridCell(title=title, action=Action(actual_kind, target=self.cell_app.text().strip()))
        if kind == "file":
            return GridCell(
                title=title,
                action=Action("file", target=self.cell_file.text().strip(), application=self._cell_file_app_path),
            )
        if kind == "common":
            return GridCell(title=title, action=Action("common", target=self.common_combo.currentData() or ""))
        if kind == "mouse":
            return GridCell(title=title, action=Action("mouse", target=self.mouse_combo.currentData() or "left"))
        return GridCell(title=title, action=Action(kind))

    def _flush_current_slot(self):
        if self._loading:
            return
        self.cells[self.current_slot] = self._draft_cell_from_widgets()

    def _load_slot(self, index: int):
        self._loading = True
        self.current_slot = index
        cell = self.cells[index]
        act = cell.action
        self._legacy_app_kind = act.kind == "app"
        self.slot_heading.setText(f"格位 {index + 1}（对应遥控器数字键 {index + 1}）")
        self.cell_title.setText(cell.title)

        combo_kind = "toggle_app" if act.kind == "app" else act.kind
        idx = self.kind_combo.findData(combo_kind)
        if idx < 0 and combo_kind in ("ui_element", "app_menu"):
            self.kind_combo.addItem("界面控件（点击 / 聚焦）" if combo_kind == "ui_element" else "应用菜单命令", combo_kind)
            idx = self.kind_combo.findData(combo_kind)
        self.kind_combo.setCurrentIndex(max(0, idx))

        self.cell_text.setPlainText(act.text if act.kind == "text" else "")
        self.cell_text_enter.setChecked(bool(act.enter) if act.kind == "text" else False)
        self.cell_sequence.setKeySequence(keys_sequence(act.keys if act.kind == "shortcut" else []))
        self.cell_app.setText(act.target if act.kind in ("toggle_app", "app") else "")
        self.cell_file.setText(act.target if act.kind == "file" else "")
        self._cell_file_app_path = act.application if act.kind == "file" else ""
        self._update_file_app_label()
        self._cell_macro_action = (
            Action.from_dict(asdict(act))
            if act.kind == "macro" and act.steps
            else None
        )
        self._cell_ui_action = Action.from_dict(asdict(act)) if act.kind == "ui_element" else None
        self._cell_menu_action = Action.from_dict(asdict(act)) if act.kind == "app_menu" else None
        self._update_macro_summary()
        self._update_ax_labels()
        if act.kind == "common":
            c_idx = self.common_combo.findData(act.target)
            if c_idx >= 0:
                self.common_combo.setCurrentIndex(c_idx)
        if act.kind == "mouse":
            m_idx = self.mouse_combo.findData(act.target)
            if m_idx >= 0:
                self.mouse_combo.setCurrentIndex(m_idx)
        self._sync_param_page()
        self._clear_error()
        self._loading = False

    def _update_macro_summary(self):
        if self._cell_macro_action is not None and self._cell_macro_action.steps:
            steps = self._cell_macro_action.steps
            lines = [f"{i + 1}. {action_text(s.action)}" for i, s in enumerate(steps[:3])]
            if len(steps) > 3:
                lines.append(f"…共 {len(steps)} 步")
            self.cell_macro_summary.setText("\n".join(lines))
        else:
            self.cell_macro_summary.setText("尚未配置步骤（点击下方按钮编排 1–8 步动作）")

    def _update_ax_labels(self):
        if self._cell_ui_action is not None and self._cell_ui_action.kind == "ui_element" and self._cell_ui_action.spec:
            self.cell_ui_summary.setText(action_text(self._cell_ui_action))
        else:
            self.cell_ui_summary.setText("尚未指定界面控件")
        if self._cell_menu_action is not None and self._cell_menu_action.kind == "app_menu" and self._cell_menu_action.spec:
            self.cell_menu_summary.setText(action_text(self._cell_menu_action))
        else:
            self.cell_menu_summary.setText("尚未选择菜单命令")

    def pick_cell_ui_element(self):
        if self._pick_ui is not None:
            saved = self._pick_ui(self, self._cell_ui_action)
        else:
            from .ax_pickers import UiControlPickerDialog
            dialog = UiControlPickerDialog(
                self._cell_ui_action,
                self,
                default_app_id=self._default_app_id,
                default_app_name=self._default_app_name,
            )
            saved = dialog.result_action() if dialog.exec() == UiControlPickerDialog.DialogCode.Accepted else None
        if saved is not None:
            self._cell_ui_action = saved
            self._update_ax_labels()
            self._on_field_changed()

    def pick_cell_app_menu(self):
        if self._pick_menu is not None:
            saved = self._pick_menu(self, self._cell_menu_action)
        else:
            from .ax_pickers import AppMenuPickerDialog
            dialog = AppMenuPickerDialog(
                self._cell_menu_action,
                self,
                default_app_id=self._default_app_id,
                default_app_name=self._default_app_name,
            )
            saved = dialog.result_action() if dialog.exec() == AppMenuPickerDialog.DialogCode.Accepted else None
        if saved is not None:
            self._cell_menu_action = saved
            self._update_ax_labels()
            self._on_field_changed()

    def edit_cell_macro(self):
        from .macro_editor import MacroEditorDialog
        dialog = MacroEditorDialog(
            self._cell_macro_action,
            self,
            button_name=f"格位 {self.current_slot + 1}",
            default_app_id=self._default_app_id,
            default_app_name=self._default_app_name,
            choose_app_fn=self._choose_app,
            choose_file_fn=self._choose_file,
            pick_ui_fn=self._pick_ui,
            pick_menu_fn=self._pick_menu,
        )
        if dialog.exec() == MacroEditorDialog.DialogCode.Accepted:
            saved = dialog.result_action()
            if saved is not None:
                self._cell_macro_action = saved
                self._update_macro_summary()
                self._on_field_changed()

    def _sync_param_page(self):
        kind = self.kind_combo.currentData() or "none"
        self.param_stack.setCurrentIndex(self._page_for_kind(kind))
        self.simple_hint.setText(
            "此格位目前为空。选择上方的动作类型即可配置。"
            if kind == "none"
            else chip_hint(kind)
        )

    def _update_file_app_label(self):
        name = Path(self._cell_file_app_path).name if self._cell_file_app_path else "系统默认"
        self.cell_file_app_label.setText(f"打开方式：{name}")

    def _refresh_previews(self):
        active_count = sum(1 for c in self.cells if c.action.kind != "none")
        self.count_label.setText(f"已设置 {active_count} / {GRID_SIZE} 格")
        for idx, btn in enumerate(self.slot_buttons):
            btn.refresh_cell(self.cells[idx], checked=(idx == self.current_slot))
        cur = self.cells[self.current_slot]
        self.clear_slot_button.setEnabled(bool(cur.title or cur.action.kind != "none"))

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
        self._flush_current_slot()
        self._refresh_previews()

    # -- Public & button handlers ----------------------------------------------

    def select_slot(self, index: int):
        if not 0 <= index < GRID_SIZE:
            return
        self._flush_current_slot()
        self._load_slot(index)
        self._refresh_previews()

    def set_slot_action(self, index: int, action: Action, title: str | None = None):
        """Programmatic helper for setting a slot's action and optional title."""
        if not 0 <= index < GRID_SIZE:
            raise ValueError("格位编号超出范围")
        self._flush_current_slot()
        prev_title = self.cells[index].title if title is None else title
        self.cells[index] = GridCell(title=prev_title, action=action)
        if index == self.current_slot:
            self._load_slot(index)
        self._refresh_previews()

    def clear_current_slot(self):
        self.cells[self.current_slot] = GridCell()
        self._load_slot(self.current_slot)
        self._refresh_previews()

    def pick_cell_app(self):
        path = self._choose_app(self)
        if path:
            self.cell_app.setText(path)
            self._on_field_changed()

    def pick_cell_file(self):
        path = self._choose_file(self)
        if path:
            self.cell_file.setText(path)
            self._on_field_changed()

    def pick_cell_file_app(self):
        path = self._choose_app(self)
        if path:
            self._cell_file_app_path = path
            self._update_file_app_label()
            self._on_field_changed()

    def reset_cell_file_app(self):
        self._cell_file_app_path = ""
        self._update_file_app_label()
        self._on_field_changed()

    def build_action(self) -> Action:
        self._flush_current_slot()
        for idx, cell in enumerate(self.cells):
            try:
                cell.validate(platform=sys.platform)
            except ValueError as exc:
                self._load_slot(idx)
                self._refresh_previews()
                if cell.action.kind == "text":
                    set_invalid(self.cell_text, True)
                elif cell.action.kind == "shortcut":
                    set_invalid(self.cell_sequence, True)
                raise ValueError(f"格位 {idx + 1}：{exc}") from exc
        action = Action(
            kind="grid",
            target=self.panel_title.text().strip(),
            cells=[
                GridCell(title=c.title.strip(), action=Action.from_dict(asdict(c.action)))
                for c in self.cells
            ],
        )
        action.validate(platform=sys.platform)
        return action

    def accept_grid(self) -> bool:
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
