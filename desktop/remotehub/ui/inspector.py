"""Right-hand inspector: edit the selected key's action in place.

Layout (top → bottom): single/double/long gesture · key name and current
action · value editor · 清除映射 · grouped action palette ·
(non-fixed models only) name, position and photo.

Commit rule: an action is saved the moment it is complete. Palette entries
without a value save on click; 按键 / 组合键, 打开 / 收起 App and 打开文件
save once their value is recorded or picked. Text is committed with 保存文本.
Unsaved text prompts before switching keys, actions or gestures.
"""
from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QGridLayout, QKeySequenceEdit, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from ..gestures import KeyGesture
from ..model import Action, GESTURE_FIELDS, GRID_SIZE, GridCell, MAX_MACRO_STEPS, effective_button
from ..nine_key import BINDING_ROLES, missing_bindings
from ..presentation import action_text, chip_hint, chip_id, grid_cell_texts, palette
from .nine_key_editor import NineKeyBindingEditor
from ..theme import INSPECTOR_WIDTH, repolish, stylesheet
from .widgets import button, keys_sequence, label, row, rule, scroll, sequence_keys, set_invalid, set_role
from .voice import VoiceKeyEditor, combo

PAGES = {
    "shortcut": 1,
    "app": 2,
    "toggle_app": 2,
    "file": 3,
    "text": 4,
    "hold": 5,
    "latch": 5,
    "grid": 6,
    "macro": 7,
    "ui_element": 8,
    "app_menu": 9,
    "nine_key": 10,
}


def _installed_app_candidates(platform: str | None = None) -> list[tuple[str, str]]:
    """Return [(display_name, launch_path)] discovered on Windows or Linux."""
    plat = platform or sys.platform
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    if plat == "win32":
        from ..app_layers import running_layer_apps
        from ..win_uia import discover_start_menu_apps
        for entry in discover_start_menu_apps():
            key = entry.name.casefold()
            if entry.path and key not in seen:
                seen.add(key)
                items.append((entry.name, entry.path))
        for layer_app in running_layer_apps("win32"):
            key = layer_app.name.casefold()
            if layer_app.executable and key not in seen:
                seen.add(key)
                items.append((layer_app.name, layer_app.executable))
    elif str(plat).startswith("linux"):
        from ..linux_desktop import scan_desktop_entries
        for entry in scan_desktop_entries():
            key = entry.name.casefold()
            if entry.desktop_path and key not in seen:
                seen.add(key)
                items.append((entry.name, entry.desktop_path))
    items.sort(key=lambda pair: pair[0].casefold())
    return items


class AppCatalogDialog(QDialog):
    """Searchable native app catalog picker for Windows Start Menu and Linux XDG apps."""

    def __init__(self, entries: list[tuple[str, str]], parent=None, *, title: str = "选择已安装的应用"):
        super().__init__(parent)
        self._entries = entries
        self._selected_path = ""
        self._browse_requested = False

        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(500, 440)
        self.setMinimumSize(420, 360)
        self.setStyleSheet(stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)
        root.addWidget(label("从本机已安装的应用中选择", "heading"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索应用名称…")
        self.search.setAccessibleName("搜索已安装应用")
        self.search.textChanged.connect(self._populate)
        root.addWidget(self.search)

        self.list_widget = QListWidget()
        self.list_widget.setAccessibleName("已安装应用列表")
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._accept_current())
        root.addWidget(self.list_widget, 1)

        browse_btn = button("浏览文件…", self._request_browse)
        cancel_btn = button("取消", self.reject)
        ok_btn = button("选择此应用", self._accept_current, "primary")
        ok_btn.setDefault(True)
        self._ok_btn = ok_btn
        self.list_widget.currentItemChanged.connect(lambda *_: self._ok_btn.setEnabled(self.list_widget.currentItem() is not None))
        root.addWidget(row(browse_btn, None, cancel_btn, ok_btn, spacing=8))
        self._populate()

    def _populate(self):
        needle = self.search.text().strip().casefold()
        self.list_widget.clear()
        for name, path in self._entries:
            if needle and needle not in name.casefold() and needle not in Path(path).name.casefold():
                continue
            item = QListWidgetItem(name)
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.list_widget.addItem(item)
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)
        self._ok_btn.setEnabled(self.list_widget.currentItem() is not None)

    def _request_browse(self):
        self._browse_requested = True
        self.reject()

    def _accept_current(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        self._selected_path = str(item.data(Qt.ItemDataRole.UserRole) or "")
        self.accept()

    @property
    def selected_path(self) -> str:
        return self._selected_path

    @property
    def browse_requested(self) -> bool:
        return self._browse_requested


def choose_application(parent):
    if sys.platform == "darwin":
        # Cocoa treats .app bundles as selectable files, not navigable folders.
        # Directory-only mode disables them in the native open panel.
        return QFileDialog.getOpenFileName(parent, "选择应用程序", "/Applications", "应用程序 (*.app)")[0]
    if sys.platform == "win32":
        return QFileDialog.getOpenFileName(
            parent,
            "选择应用程序",
            "",
            "应用程序与快捷方式 (*.exe *.lnk);;所有文件 (*)",
        )[0]
    if str(sys.platform).startswith("linux"):
        start_dir = "/usr/share/applications" if Path("/usr/share/applications").is_dir() else ""
        return QFileDialog.getOpenFileName(
            parent,
            "选择应用程序",
            start_dir,
            "应用程序 (*.desktop *);;所有文件 (*)",
        )[0]
    return QFileDialog.getOpenFileName(parent, "选择应用程序", "", "应用程序 (*.exe);;所有文件 (*)")[0]


def choose_installed_application(parent) -> str:
    """Open searchable Start Menu / XDG catalog on Windows & Linux, with browse fallback."""
    if sys.platform == "darwin":
        return choose_application(parent)
    entries = _installed_app_candidates(sys.platform)
    if not entries:
        return choose_application(parent)
    dialog = AppCatalogDialog(entries, parent)
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_path:
        return dialog.selected_path
    if dialog.browse_requested:
        return choose_application(parent)
    return ""


def choose_file(parent):
    return QFileDialog.getOpenFileName(parent, "选择要打开的文件")[0]


def action_for_chip(ident):
    if ident.startswith("common:"):
        return Action("common", target=ident.split(":", 1)[1])
    if ident.startswith("mouse:"):
        return Action("mouse", target=ident.split(":", 1)[1])
    return Action(ident)


class Inspector(QWidget):
    edit_mode = Signal(bool)
    voice_settings_requested = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.gesture = "single"
        self.draft_kind = None   # chosen but not yet complete
        self.draft_app = ""      # file "open with" picked before the file
        self.shown_id = None
        self.loading = False
        self.text_context = None
        self.nine_key_context = None
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(INSPECTOR_WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 6)
        outer.setSpacing(0)

        # Frozen top editor header: always stays visible above the scrollable action palette.
        self.header_panel = QWidget()
        self.header_panel.setObjectName("inspectorHeader")
        box = QVBoxLayout(self.header_panel)
        box.setContentsMargins(20, 16, 20, 10)
        box.setSpacing(8)

        self.gesture_buttons = {}
        gesture_controls = []
        for name, title in (("single", "单击"), ("double", "双击"), ("long", "长按")):
            control = button(title, lambda checked=False, g=name: self.set_gesture(g), "kind")
            control.setCheckable(True)
            self.gesture_buttons[name] = control
            gesture_controls.append(control)
        box.addWidget(row(*gesture_controls, spacing=4))
        self.gesture_hint = label("", "caption")
        box.addWidget(self.gesture_hint)

        self.title = label("选择一个按键", "heading")
        self.summary = label("在图上点一个键", "muted")
        box.addWidget(self.title)
        box.addWidget(self.summary)
        self.pages = QStackedWidget()
        self.pages.addWidget(QWidget())               # 0: no value needed
        self.pages.addWidget(self._shortcut_page())   # 1
        self.pages.addWidget(self._app_page())        # 2
        self.pages.addWidget(self._file_page())       # 3
        self.pages.addWidget(self._text_page())       # 4
        self.pages.addWidget(self._modifier_page())   # 5
        self.pages.addWidget(self._grid_page())       # 6
        self.pages.addWidget(self._macro_page())      # 7
        self.pages.addWidget(self._ui_element_page()) # 8
        self.pages.addWidget(self._app_menu_page())   # 9
        self.pages.addWidget(self._nine_key_page())   # 10
        box.addWidget(self.pages)
        self.hint = label("", "caption")
        box.addWidget(self.hint)
        self.clear_current = button("", lambda: self.c.clear_action(self.gesture), "danger")
        self.clear_current.setToolTip("只清除当前手势；另外两种手势的设置保留")
        self.mute_in_layer = button("在此应用无操作", lambda: self.c.mute_in_layer(self.gesture), "quiet")
        self.mute_in_layer.setToolTip("只在此应用中屏蔽当前手势，不影响全局默认和其他手势")
        self.test = button("测试", self.test_clicked)
        box.addWidget(row(self.clear_current, self.mute_in_layer, None, self.test, spacing=6))
        self.action_divider = rule()
        box.addWidget(self.action_divider)
        outer.addWidget(self.header_panel, 0)

        # Scrollable lower area: reserved voice setup or grouped action palette + details.
        self.body_widget = QWidget()
        self.body_widget.setObjectName("inspectorBody")
        self.body_widget.setMinimumWidth(0)
        body_box = QVBoxLayout(self.body_widget)
        body_box.setContentsMargins(18, 8, 12, 16)
        body_box.setSpacing(10)

        self.voice_editor = VoiceKeyEditor(controller)
        body_box.addWidget(self.voice_editor)
        self.voice_settings = button("更多语音设置", self.voice_settings_requested.emit, "link")
        body_box.addWidget(self.voice_settings)

        self.kinds = {}
        self.action_palette = QWidget()
        palette_box = QVBoxLayout(self.action_palette)
        palette_box.setContentsMargins(0, 0, 0, 0)
        palette_box.setSpacing(10)
        body_box.addWidget(self.action_palette)
        self.nine_key_editor = NineKeyBindingEditor()
        self.nine_key_editor.saved.connect(self.save_nine_key_bindings)
        self.nine_key_editor.canceled.connect(self.close_nine_key_editor)
        body_box.addWidget(self.nine_key_editor)
        palette_box.addWidget(label("选择当前手势要执行的动作", "caption"))
        for n, (group, items) in enumerate(palette()):
            if n:
                palette_box.addSpacing(2)
            palette_box.addWidget(label(group, "caption"))
            grid = QGridLayout()
            grid.setHorizontalSpacing(4)
            grid.setVerticalSpacing(2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            for i, (ident, title) in enumerate(items):
                control = button(title, lambda k=ident: self.choose_kind(k), "kind")
                control.setCheckable(True)
                control.setMinimumWidth(0)
                control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                control.setToolTip(chip_hint(ident))
                self.kinds[ident] = control
                grid.addWidget(control, i // 2, i % 2)
            palette_box.addLayout(grid)

        self.details_divider = rule()
        body_box.addWidget(self.details_divider)
        self.more = button("▸ 名称与位置", self.toggle_details, "disclosure")
        self.more.setCheckable(True)
        body_box.addWidget(self.more)
        self.details = self._details()
        self.details.hide()
        body_box.addWidget(self.details)
        body_box.addStretch(1)

        self.body_scroll = scroll(self.body_widget)
        outer.addWidget(self.body_scroll, 1)

        controller.mapping_changed.connect(self.refresh)
        controller.profiles_changed.connect(self.refresh)
        controller.test_pending.connect(self.show_test)
        self.refresh()

    # -- parameter pages -------------------------------------------------

    def _text_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label("要输入的文本", "caption"))
        self.text_input = QPlainTextEdit()
        self.text_input.setAccessibleName("要输入的文本")
        self.text_input.setPlaceholderText("例如 CODEX，支持中文和多行文本")
        self.text_input.setFixedHeight(86)
        self.text_input.setTabChangesFocus(True)
        self.text_input.textChanged.connect(self.text_edited)
        box.addWidget(self.text_input)
        self.text_enter = QCheckBox("输入后自动按回车发送")
        self.text_enter.setAccessibleName("输入后自动按回车发送")
        self.text_enter.toggled.connect(self.text_edited)
        box.addWidget(self.text_enter)
        self.text_error = label("", wrap=True)
        self.text_error.setObjectName("inlineError")
        self.text_error.hide()
        box.addWidget(self.text_error)
        self.text_save = button("保存文本", self.save_text)
        box.addWidget(row(self.text_save, None))
        return page

    @property
    def text_dirty(self):
        return bool(
            self.visible_kind == "text"
            and self.text_context is not None
            and (
                self.text_input.toPlainText() != self.text_context[2]
                or self.text_enter.isChecked() != self.text_context[4]
            )
        )

    def text_edited(self):
        if self.loading:
            return
        self.text_error.hide()
        set_invalid(self.text_input, False)
        self._fit_pages()
        self.text_save.setEnabled(self.text_dirty)
        if self.text_dirty:
            self.summary.setText("未保存 · 原动作保留：" + action_text(self.current_action))
        else:
            self.refresh()

    def save_text(self):
        if self.c.selected is None or self.c.selected_voice_key or self.visible_kind != "text":
            return False
        action = Action("text", text=self.text_input.toPlainText(), enter=self.text_enter.isChecked())
        try:
            action.validate()
        except ValueError as exc:
            self.text_error.setText(str(exc))
            self.text_error.show()
            set_invalid(self.text_input, True)
            self._fit_pages()
            return False
        if self.c.assign(action, self.gesture):
            self.draft_kind = None
            self.refresh()
            return True
        return False

    def confirm_text_saved(self):
        if not self.text_dirty:
            return True
        from PySide6.QtWidgets import QMessageBox
        answer = QMessageBox.question(
            self, "未保存的文本", "当前文本尚未保存。离开前要保存吗？",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save_text()
        return answer == QMessageBox.StandardButton.Discard

    def _shortcut_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self.sequence = QKeySequenceEdit()
        self.sequence.setMaximumSequenceLength(1)
        self.sequence.setAccessibleName("录制快捷键")
        self.sequence.setToolTip("点击后按下快捷键；松手自动保存")
        self.sequence.editingFinished.connect(self.sequence_done)
        self.sequence_error = label("", wrap=True)
        self.sequence_error.setObjectName("inlineError")
        self.sequence_error.hide()
        box.addWidget(self.sequence)
        box.addWidget(self.sequence_error)
        return page

    def _modifier_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        if sys.platform == "darwin":
            items = (("cmd", "Command"), ("ctrl", "Control"), ("alt", "Option / Alt"), ("shift", "Shift"))
        elif sys.platform == "win32":
            items = (("cmd", "Win（Windows 键）"), ("ctrl", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"))
        else:
            items = (("cmd", "Super（徽标键）"), ("ctrl", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"))
        self.modifier = combo(items, "选择要保持的键")
        self.modifier.activated.connect(self.modifier_chosen)
        box.addWidget(self.modifier)
        return page

    def modifier_chosen(self):
        if self.visible_kind in ("hold", "latch"):
            self._commit(Action("hold", target=self.visible_kind,
                                keys=[self.modifier.currentData()]))

    def _app_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self.app_target = QLineEdit()
        self.app_target.setReadOnly(True)
        self.app_target.setPlaceholderText("尚未选择应用")
        self.app_target.setAccessibleName("要打开的应用")
        box.addWidget(row(self.app_target, button("选择…", self.pick_app), spacing=6))
        if sys.platform != "darwin":
            box.addWidget(row(button("从已安装应用选择…", self.pick_installed_app, "link"), None))
        return page

    def _file_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self.file_target = QLineEdit()
        self.file_target.setReadOnly(True)
        self.file_target.setPlaceholderText("尚未选择文件")
        self.file_target.setAccessibleName("要打开的文件")
        box.addWidget(row(self.file_target, button("选择…", self.pick_file), spacing=6))
        self.file_app = label("", "caption")
        box.addWidget(self.file_app)
        box.addWidget(row(button("指定打开方式…", self.pick_file_app, "link"),
                          button("系统默认", self.default_file_app, "link"), None, spacing=14))
        return page

    def _grid_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self.grid_status = label("", "caption")
        box.addWidget(self.grid_status)
        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        self.grid_preview_buttons = []
        for idx in range(GRID_SIZE):
            btn = button("", lambda i=idx: self.open_grid_editor(i), "gridMini")
            btn.setMinimumWidth(0)
            grid.addWidget(btn, idx // 3, idx % 3)
            self.grid_preview_buttons.append(btn)
        box.addLayout(grid)
        self.grid_edit_button = button("配置九宫格…", lambda: self.open_grid_editor(0), "primary")
        box.addWidget(row(self.grid_edit_button, None))
        return page

    def _nine_key_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        self.nine_key_status = label("", "caption")
        self.nine_key_setup = button("设置九键键位…", self.open_nine_key_editor, "primary")
        box.addWidget(self.nine_key_status)
        box.addWidget(row(self.nine_key_setup, None))
        return page

    def _nine_key_context(self):
        layer = self.c.editing_layer
        return (self.c.profile.id, layer.id if layer else "", self.c.selected_id, self.gesture)

    def open_nine_key_editor(self):
        action = self.current_action
        if action.kind != "nine_key":
            return
        self.draw.setChecked(False)
        self.nine_key_context = self._nine_key_context()
        self.nine_key_editor.start(self.c.nine_key_buttons(), self.c.nine_key_bindings(action),
                                   self.c.nine_key_defaults())
        self.refresh()
        self.body_scroll.verticalScrollBar().setValue(0)

    def close_nine_key_editor(self):
        self.nine_key_context = None
        self.nine_key_editor.stop()
        self.refresh()

    def save_nine_key_bindings(self, bindings):
        if self.nine_key_context != self._nine_key_context():
            self.close_nine_key_editor()
            return
        action = Action("nine_key", spec={"bindings": bindings})
        resolved = self.c.nine_key_bindings(action)
        if missing_bindings(resolved, BINDING_ROLES):
            self.nine_key_editor.refresh("请根据提示登记全部 15 个键。")
            return
        self.c.sync_nine_key_roles(bindings)
        if self.c.assign(action, self.gesture):
            self.close_nine_key_editor()

    def _refresh_nine_key_preview(self, action: Action):
        available = self.c.nine_key_buttons()
        probe = action if action.kind == "nine_key" else Action("nine_key")
        bindings = self.c.nine_key_bindings(probe)
        missing = missing_bindings(bindings, BINDING_ROLES)
        if len(available) < len(BINDING_ROLES):
            self.nine_key_status.setText(
                f"这只遥控器当前只有 {len(available)} 个可用键，九键输入至少需要 {len(BINDING_ROLES)} 个键"
                "（0–9、*、# 和左/确定/右）。没有数字区可以跳过九键，或先「学习更多按键」。"
            )
            self.nine_key_setup.setText("按键数量不足")
            self.nine_key_setup.setEnabled(False)
            return
        self.nine_key_setup.setEnabled(True)
        if not missing:
            extra = "，含返回键" if "back" in bindings else ""
            self.nine_key_status.setText(f"九键键位已就绪（{len(BINDING_ROLES)} / {len(BINDING_ROLES)}{extra}），可直接保存或按需微调。")
            self.nine_key_setup.setText("调整九键键位…")
        else:
            done = len(BINDING_ROLES) - len(missing)
            if done:
                self.nine_key_status.setText(
                    f"已自动识别 {done} / {len(BINDING_ROLES)} 个键位，点下方按钮只需补选缺少的 {len(missing)} 个键。"
                )
            else:
                self.nine_key_status.setText("点下方按钮，在左侧遥控器图上依次点选 15 个键位（保存后全方案共用）。")
            self.nine_key_setup.setText("设置九键键位…")

    def _refresh_grid_preview(self, action: Action):
        cells = (
            action.cells
            if action.kind == "grid" and len(action.cells) == GRID_SIZE
            else [GridCell() for _ in range(GRID_SIZE)]
        )
        configured = sum(1 for c in cells if c.action.kind != "none")
        if action.kind == "grid":
            title_part = f"「{action.target.strip()}」· " if action.target.strip() else ""
            self.grid_status.setText(f"{title_part}已设置 {configured} / {GRID_SIZE} 格（点击格位可直接编辑）")
        else:
            self.grid_status.setText("尚未配置格位 · 点击下方按钮设置 1–9 格动作")
        for idx, btn in enumerate(self.grid_preview_buttons):
            cell = cells[idx]
            empty = cell.action.kind == "none"
            title, sub = grid_cell_texts(cell)
            short = f"{idx + 1} · —" if empty else f"{idx + 1} · {title}"
            metrics = QFontMetrics(btn.font())
            btn.setText(metrics.elidedText(short, Qt.TextElideMode.ElideRight, 66))
            btn.setToolTip(f"格位 {idx + 1}：{title}（{sub}）\n点击编辑此格")
            empty_str = "true" if empty else "false"
            if btn.property("empty") != empty_str:
                btn.setProperty("empty", empty_str)
                repolish(btn)

    def open_grid_editor(self, initial_slot: int = 0):
        b = self.c.selected
        if b is None or self.c.selected_voice_key:
            return
        from .grid_editor import GridEditorDialog
        current = self.current_action
        layer = self.c.editing_layer
        dialog = GridEditorDialog(
            current if current.kind == "grid" else None,
            self,
            button_name=b.name,
            initial_slot=initial_slot,
            default_app_id=layer.app_id if layer is not None else "",
            default_app_name=layer.name if layer is not None else "",
        )
        if dialog.exec() == GridEditorDialog.DialogCode.Accepted:
            saved = dialog.result_action()
            if saved is not None:
                self._commit(saved)

    def _macro_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(5)
        self.macro_status = label("", "caption")
        box.addWidget(self.macro_status)
        self.macro_preview_buttons = []
        for idx in range(MAX_MACRO_STEPS):
            btn = button("", lambda i=idx: self.open_macro_editor(i), "macroMini")
            btn.setMinimumWidth(0)
            btn.hide()
            box.addWidget(btn)
            self.macro_preview_buttons.append(btn)
        self.macro_edit_button = button("配置动作链…", lambda: self.open_macro_editor(0), "primary")
        box.addWidget(row(self.macro_edit_button, None))
        return page

    def _refresh_macro_preview(self, action: Action):
        steps = action.steps if action.kind == "macro" else []
        if action.kind == "macro" and steps:
            title_part = f"「{action.target.strip()}」· " if action.target.strip() else ""
            self.macro_status.setText(f"{title_part}共 {len(steps)} 步（点击步骤可直接编辑）")
        else:
            self.macro_status.setText("尚未配置步骤 · 点击下方按钮编排 1–8 步连续动作")
        for idx, btn in enumerate(self.macro_preview_buttons):
            if idx >= len(steps):
                btn.hide()
                continue
            step = steps[idx]
            desc = action_text(step.action)
            wait = "" if idx == len(steps) - 1 else (f" · 等 {step.delay_ms}ms" if step.delay_ms > 0 else " · 无等待")
            line = f"{idx + 1}. {desc}{wait}"
            metrics = QFontMetrics(btn.font())
            btn.setText(metrics.elidedText(line, Qt.TextElideMode.ElideRight, 224))
            btn.setToolTip(f"第 {idx + 1} 步：{desc}{wait}\n点击编辑此步")
            btn.show()

    def open_macro_editor(self, initial_step: int = 0):
        b = self.c.selected
        if b is None or self.c.selected_voice_key:
            return
        from .macro_editor import MacroEditorDialog
        current = self.current_action
        layer = self.c.editing_layer
        dialog = MacroEditorDialog(
            current if current.kind == "macro" else None,
            self,
            button_name=b.name,
            initial_step=initial_step,
            default_app_id=layer.app_id if layer is not None else "",
            default_app_name=layer.name if layer is not None else "",
        )
        if dialog.exec() == MacroEditorDialog.DialogCode.Accepted:
            saved = dialog.result_action()
            if saved is not None:
                self._commit(saved)

    def _ui_element_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self.ui_element_status = label("", "caption", wrap=True)
        self.ui_element_status.setMinimumWidth(0)
        box.addWidget(self.ui_element_status)
        self.ui_element_pick = button("指定界面控件…", self.open_ui_control_picker, "primary")
        box.addWidget(row(self.ui_element_pick, None))
        return page

    def _refresh_ui_element_preview(self, action: Action):
        if action.kind == "ui_element":
            op = "聚焦区域" if action.target == "focus" else "点击控件"
            spec = action.spec or {}
            app_name = (spec.get("app_name") or action.application).strip()
            elem_name = (spec.get("label") or "").strip() or "控件"
            self.ui_element_status.setText(f"已绑定：{app_name} · {op} · {elem_name}")
            self.ui_element_pick.setText("重新指定控件…")
        else:
            self.ui_element_status.setText("尚未指定控件 · 点击下方按钮并悬停到目标按钮或区域即可锁定")
            self.ui_element_pick.setText("指定界面控件…")

    def open_ui_control_picker(self):
        b = self.c.selected
        if b is None or self.c.selected_voice_key:
            return
        from .ax_pickers import UiControlPickerDialog
        current = self.current_action
        layer = self.c.editing_layer
        dialog = UiControlPickerDialog(
            current if current.kind == "ui_element" else None,
            self,
            default_app_id=layer.app_id if layer is not None else "",
            default_app_name=layer.name if layer is not None else "",
        )
        if dialog.exec() == UiControlPickerDialog.DialogCode.Accepted:
            saved = dialog.result_action()
            if saved is not None:
                self._commit(saved)

    def _app_menu_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self.app_menu_status = label("", "caption", wrap=True)
        self.app_menu_status.setMinimumWidth(0)
        box.addWidget(self.app_menu_status)
        self.app_menu_pick = button("选择菜单命令…", self.open_app_menu_picker, "primary")
        box.addWidget(row(self.app_menu_pick, None))
        return page

    def _refresh_app_menu_preview(self, action: Action):
        if action.kind == "app_menu":
            spec = action.spec or {}
            path = " → ".join(spec.get("path", []))
            scope = (spec.get("app_name") or action.application or "任意前台应用").strip()
            self.app_menu_status.setText(f"已选菜单（{scope}）：{path}")
            self.app_menu_pick.setText("更换菜单命令…")
        else:
            self.app_menu_status.setText("尚未选择菜单 · 从目标应用顶部菜单栏挑一条命令")
            self.app_menu_pick.setText("选择菜单命令…")

    def open_app_menu_picker(self):
        b = self.c.selected
        if b is None or self.c.selected_voice_key:
            return
        from .ax_pickers import AppMenuPickerDialog
        current = self.current_action
        layer = self.c.editing_layer
        dialog = AppMenuPickerDialog(
            current if current.kind == "app_menu" else None,
            self,
            default_app_id=layer.app_id if layer is not None else "",
            default_app_name=layer.name if layer is not None else "",
        )
        if dialog.exec() == AppMenuPickerDialog.DialogCode.Accepted:
            saved = dialog.result_action()
            if saved is not None:
                self._commit(saved)

    def _details(self):
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 2, 0, 0)
        box.setSpacing(8)
        box.addWidget(label("名称", "caption"))
        self.name = QLineEdit()
        self.name.setAccessibleName("按键名称")
        self.name.editingFinished.connect(lambda: self.c.rename(self.name.text()))
        box.addWidget(self.name)
        box.addWidget(label("实体按键", "caption"))
        self.trigger = label("", "muted")
        self.trigger.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.trigger)
        box.addWidget(label("在图片上的位置（%）", "caption"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self.rect_spins = []
        for i, title in enumerate(("X", "Y", "宽", "高")):
            spin = QDoubleSpinBox()
            spin.setRange(0, 100)
            spin.setDecimals(1)
            spin.setPrefix(title + "  ")
            spin.setAccessibleName({"X": "横向位置", "Y": "纵向位置", "宽": "宽度", "高": "高度"}[title] + " %")
            spin.valueChanged.connect(self.rect_edited)
            grid.addWidget(spin, i // 2, i % 2)
            self.rect_spins.append(spin)
        box.addLayout(grid)
        self.draw = QCheckBox("在图上框选位置")
        self.draw.toggled.connect(self.edit_mode)
        box.addWidget(self.draw)
        box.addWidget(label("照片", "caption"))
        box.addWidget(row(button("更换照片…", self.pick_photo), None, spacing=6))
        self.remove_button_control = button("移除这个键", self.c.remove_button, "danger")
        self.custom = row(self.remove_button_control, None, spacing=6)
        box.addWidget(self.custom)
        return panel

    # -- state -> widgets -------------------------------------------------

    @property
    def current_action(self):
        button = self.c.selected
        if button is None:
            return Action()
        if self.c.editing_layer is not None:
            override = self.c.layer_override_action(button.id, self.gesture)
            return override if override is not None else Action()
        field = {"single": "action", "double": "double_action", "long": "long_action"}[self.gesture]
        return getattr(button, field)

    def set_gesture(self, gesture):
        if gesture == self.gesture:
            return
        if self.c.selected_voice_key or not self.confirm_text_saved():
            self.refresh()
            return
        self.gesture = gesture
        self.draft_kind, self.draft_app = None, ""
        self.text_context = None
        self.refresh()

    @property
    def visible_kind(self):
        """Chip id shown as current: the draft if any, else the saved action."""
        if self.c.selected is None:
            return None
        return self.draft_kind or chip_id(self.current_action)

    def _fit_pages(self):
        height = self.pages.currentWidget().sizeHint().height() if not self.pages.isHidden() else 0
        self.pages.setFixedHeight(max(0, height))

    def refresh(self):
        self.loading = True
        b = self.c.selected
        layer = self.c.editing_layer
        eff_btn = effective_button(b, layer) if b is not None else None
        mouse_exclusive = bool(eff_btn is not None and eff_btn.action.kind == "mouse")
        if (self.c.selected_voice_key or mouse_exclusive) and self.gesture != "single":
            self.gesture = "single"
            self.draft_kind, self.draft_app = None, ""
            self.text_context = None
        shown_key = (layer.id if layer else "", b.id) if b else None
        if shown_key != self.shown_id:
            self.draft_kind, self.draft_app = None, ""
            self.shown_id = shown_key
            set_invalid(self.sequence, False)
            self.sequence_error.hide()
        elif self.draft_kind and b is not None and chip_id(self.current_action) == self.draft_kind:
            self.draft_kind = None
        kind = self.visible_kind
        if self.nine_key_editor.active and (kind != "nine_key" or self.nine_key_context != self._nine_key_context()):
            self.nine_key_context = None
            self.nine_key_editor.stop()
        editing_nine_key = self.nine_key_editor.active
        fixed = self.c.fixed_model
        in_layer = layer is not None
        reserved_voice = self.c.selected_voice_key
        self.voice_editor.setVisible(reserved_voice)
        self.voice_settings.setVisible(reserved_voice)
        self.action_palette.setVisible(not reserved_voice and not editing_nine_key)
        self.action_divider.setVisible(not reserved_voice)
        self.clear_current.setVisible(not reserved_voice)
        self.mute_in_layer.setVisible(in_layer and not reserved_voice)
        self.title.setText(b.name if b else "选择一个按键")
        gesture_name = {"single": "单击", "double": "双击", "long": "长按"}[self.gesture]
        self.gesture_hint.setText(
            "此键已专属模拟鼠标按键（短按单点 · 连按双击 · 按住不放即鼠标长按），不支持同时设置双击或长按。"
            if mouse_exclusive
            else {
                "single": "当前编辑：单击动作。",
                "double": f"当前编辑：双击动作。快速按两次执行，单击需等待 {KeyGesture.DOUBLE_INTERVAL} ms 区分。",
                "long": f"当前编辑：长按动作。按住 {KeyGesture.LONG_PRESS} ms 执行，短按仍执行单击。",
            }[self.gesture]
        )
        overridden = bool(b and in_layer and self.c.is_gesture_overridden(b.id, self.gesture, layer))
        base_act = self.c.base_action(b.id, self.gesture) if b else Action()
        base_desc = action_text(base_act) if base_act.kind != "none" else "未设置"
        if in_layer:
            self.clear_current.setText("恢复继承全局")
            self.clear_current.setToolTip(f"移除此应用的{gesture_name}覆盖，恢复全局默认（{base_desc}）")
        else:
            self.clear_current.setText(f"清除{gesture_name}映射")
            self.clear_current.setToolTip("只清除当前手势；另外两种手势的设置保留")
        if b is None:
            self.summary.setText("在图上点一个键，或用 Tab / 方向键选择。")
        elif reserved_voice:
            self.summary.setText("专用语音键 · 开始 / 结束")
        elif self.text_dirty:
            self.summary.setText(f"{gesture_name}动作 · 未保存，原动作保留：" + action_text(self.current_action))
        elif self.draft_kind:
            retained = (
                f"继承全局（{base_desc}）"
                if (in_layer and not overridden)
                else ("无操作" if (in_layer and self.current_action.kind == "none") else action_text(self.current_action))
            )
            self.summary.setText(f"{gesture_name}动作 · 未完成，原动作保留：{retained}")
        elif in_layer:
            if not overridden:
                self.summary.setText(f"{gesture_name}（{layer.name}）：继承全局（{base_desc}）")
            elif self.current_action.kind == "none":
                self.summary.setText(f"{gesture_name}（{layer.name}）：在此应用无操作（全局：{base_desc}）")
            else:
                self.summary.setText(f"{gesture_name}（{layer.name} 专属）：{action_text(self.current_action)}")
        else:
            self.summary.setText(gesture_name + "动作：" +
                                 (action_text(self.current_action) if self.current_action.kind != "none" else "未设置"))
        for name, control in self.gesture_buttons.items():
            control.setChecked(name == self.gesture)
            control.setEnabled(b is not None and (name == "single" or (not reserved_voice and not mouse_exclusive)))
        for ident, control in self.kinds.items():
            control.setEnabled(b is not None and not reserved_voice)
            control.setChecked(ident == kind)
            if ident == "voice":
                control.setVisible(not fixed)
            if ident == "cycle_profile":
                control.setVisible(getattr(self.c, "supports_profile_cycling", True))
        page = PAGES.get(kind or "", 0)
        for i in range(self.pages.count()):
            self.pages.widget(i).setVisible(i == page)
        self.pages.setCurrentIndex(page)
        self.pages.setVisible(page != 0 and not reserved_voice and not editing_nine_key)
        action = self.current_action if b else Action()
        context = (
            b.id if b else None,
            kind,
            action.text if action.kind == "text" else "",
            layer.id if layer else "",
            bool(action.enter) if action.kind == "text" else False,
        )
        if context != self.text_context:
            self.text_context = context
            self.text_input.setPlainText(context[2])
            self.text_enter.setChecked(context[4])
            self.text_error.hide()
            set_invalid(self.text_input, False)
        self.text_save.setEnabled(self.text_dirty)
        if not self.sequence.hasFocus():
            self.sequence.setKeySequence(keys_sequence(action.keys if action.kind == "shortcut" else []))
        self.app_target.setText(action.target if action.kind in ("app", "toggle_app") else "")
        self.file_target.setText(action.target if action.kind == "file" else "")
        file_app = action.application if action.kind == "file" else self.draft_app
        self.file_app.setText("打开方式：" + (Path(file_app).name if file_app else "系统默认"))
        modifier = action.keys[0] if action.kind == "hold" and action.keys else "cmd"
        self.modifier.setCurrentIndex(max(0, self.modifier.findData(modifier)))
        self._refresh_grid_preview(action)
        self._refresh_macro_preview(action)
        self._refresh_ui_element_preview(action)
        self._refresh_app_menu_preview(action)
        self._refresh_nine_key_preview(action)
        self.hint.setText(chip_hint(kind or "none") if b else "")
        self.hint.setVisible(not reserved_voice and not editing_nine_key)
        if in_layer:
            self.clear_current.setEnabled(bool(b and overridden))
            self.mute_in_layer.setEnabled(bool(b and not (overridden and self.current_action.kind == "none")))
        else:
            self.clear_current.setEnabled(bool(b and self.current_action.kind != "none"))
        # The fixed remote is a finished product: no delayed test, key codes,
        # names, geometry or photo controls. Those stay for other models.
        self.test.setVisible(not fixed)
        show_details = not fixed and not in_layer and not editing_nine_key
        for widget in (self.details_divider, self.more):
            widget.setVisible(show_details)
        if not show_details and (self.more.isChecked() or self.draw.isChecked()):
            self.more.setChecked(False)
            self.toggle_details()
        self.show_test(self.c.test_left if self.c.test_timer.isActive() else 0)
        if not self.name.hasFocus():
            self.name.setText(b.name if b else "")
        self.name.setEnabled(b is not None)
        self.trigger.setText((" / ".join(b.all_triggers) or "尚未学习") if b else "")
        self.remove_button_control.setEnabled(b is not None)
        for i, spin in enumerate(self.rect_spins):
            spin.setEnabled(b is not None)
            spin.setValue(b.rect[i] * 100 if b else 0)
        self.draw.setEnabled(b is not None)
        self._fit_pages()
        self.loading = False

    def show_test(self, seconds):
        b = self.c.selected
        if seconds:
            self.test.setText(f"取消（{seconds}）")
            set_role(self.test, "pending")
            self.test.setEnabled(True)
            return
        self.test.setText("测试")
        set_role(self.test, None)
        eff_action = getattr(effective_button(b, self.c.editing_layer), GESTURE_FIELDS[self.gesture]) if b else Action()
        self.test.setEnabled(bool(b and eff_action.kind not in ("none", "voice") and not self.draft_kind))
        self.test.setToolTip("语音动作请在「语音设置」页验证" if b and eff_action.kind == "voice"
                             else "3 秒后在当前前台应用执行一次这个动作")

    # -- intents -----------------------------------------------------------

    def choose_kind(self, ident):
        b = self.c.selected
        if b is None or self.c.selected_voice_key:
            return
        if ident == self.visible_kind:
            return
        if not self.confirm_text_saved():
            self.refresh()
            return
        current = self.current_action
        if ident == chip_id(current):
            self.draft_kind = None
        elif ident == "cycle_profile" and self.c.editing_layer is None:
            sync_conflicts = False
            _empty, conflicts = self.c.find_cycle_profile_targets(b.id, self.gesture)
            if conflicts:
                choice = self._confirm_cycle_profile_conflicts(b, conflicts)
                if choice is None:
                    self.refresh()
                    return
                sync_conflicts = choice
            self.draft_kind = None
            self.c.assign(action_for_chip(ident), self.gesture, sync_conflicts=sync_conflicts)
        elif ident.startswith("mouse:"):
            self.draft_kind = None
            self.gesture = "single"
            self.c.assign(action_for_chip(ident), "single")
        elif ident not in PAGES or ident == "nine_key":
            self.draft_kind = None
            self.c.assign(action_for_chip(ident), self.gesture)
        elif {ident, current.kind} == {"app", "toggle_app"} and current.target:
            self.draft_kind = None
            self.c.assign(Action(ident, target=current.target), self.gesture)
        else:
            self.draft_kind = ident
        self.refresh()
        if self.draft_kind == "shortcut":
            self.sequence.setFocus()
        elif self.visible_kind == "text":
            self.text_input.setFocus()

    def _confirm_cycle_profile_conflicts(self, button_obj, conflicts) -> bool | None:
        from PySide6.QtWidgets import QMessageBox
        gesture_label = {"single": "单击", "double": "双击", "long": "长按"}[self.gesture]
        lines = [f"• {scheme}：{action_text(old_act)}" for _prof, _btn, old_act, scheme in conflicts]
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle("同步「循环切换方案」")
        dialog.setText(
            f"为保证按「{button_obj.name}」（{gesture_label}）能在各方案之间顺畅循环，"
            f"未设置该手势的其他方案会自动同步。\n\n"
            f"以下方案的「{button_obj.name}」（{gesture_label}）已配置了其他动作：\n"
            + "\n".join(lines)
            + "\n\n是否同意一并覆盖为「循环切换方案」？"
        )
        overwrite = dialog.addButton("同意覆盖并同步", QMessageBox.ButtonRole.AcceptRole)
        keep = dialog.addButton("保留原动作", QMessageBox.ButtonRole.ActionRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(overwrite)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked == overwrite:
            return True
        if clicked == keep:
            return False
        if clicked == cancel:
            return None
        return None

    def _commit(self, action):
        self.draft_kind, self.draft_app = None, ""
        if not self.c.assign(action, self.gesture):
            self.refresh()

    def sequence_done(self):
        if self.loading:
            return
        keys = sequence_keys(self.sequence.keySequence())
        b = self.c.selected
        if not keys or b is None:
            return
        try:
            Action("shortcut", keys).validate(sys.platform)
        except ValueError as exc:
            self.sequence_error.setText(str(exc))
            self.sequence_error.show()
            set_invalid(self.sequence, True)
            self._fit_pages()
            return
        self.sequence_error.hide()
        set_invalid(self.sequence, False)
        if self.current_action.kind == "shortcut" and self.current_action.keys == keys:
            self.draft_kind = None
            self.refresh()
            return
        self._commit(Action("shortcut", keys))

    def pick_app(self):
        path = choose_application(self)
        if path:
            kind = self.visible_kind if self.visible_kind in ("app", "toggle_app") else "toggle_app"
            self._commit(Action(kind, target=path))

    def pick_installed_app(self):
        path = choose_installed_application(self)
        if path:
            kind = self.visible_kind if self.visible_kind in ("app", "toggle_app") else "toggle_app"
            self._commit(Action(kind, target=path))

    def pick_file(self):
        path = choose_file(self)
        if not path:
            return
        b = self.c.selected
        app = self.current_action.application if b and self.current_action.kind == "file" else self.draft_app
        self._commit(Action("file", target=path, application=app))

    def pick_file_app(self):
        path = choose_application(self)
        if path:
            self._set_file_app(path)

    def default_file_app(self):
        self._set_file_app("")

    def _set_file_app(self, path):
        b = self.c.selected
        if b and self.current_action.kind == "file" and not self.draft_kind:
            self._commit(Action("file", target=self.current_action.target, application=path))
        else:
            self.draft_app = path
            self.refresh()

    def rect_edited(self):
        if not self.loading:
            self.c.set_rect([s.value() / 100 for s in self.rect_spins])

    def pick_photo(self):
        path = QFileDialog.getOpenFileName(self, "选择遥控器实物照片", "", "图片 (*.png *.jpg *.jpeg *.webp)")[0]
        if path:
            self.c.replace_photo(path)

    def test_clicked(self):
        if self.c.test_timer.isActive():
            self.c.cancel_test()
        else:
            self.c.schedule_test(self.gesture)

    def toggle_details(self):
        open_ = self.more.isChecked()
        self.more.setText(("▾ " if open_ else "▸ ") + "名称与位置")
        self.details.setVisible(open_)
        if not open_ and self.draw.isChecked():
            self.draw.setChecked(False)

    def focus_editor(self):
        """Enter / double-click on the canvas: jump to the most useful control."""
        if self.c.selected_voice_key:
            self.voice_editor.shortcut.setFocus()
            return
        kind = self.visible_kind
        if kind == "shortcut":
            self.sequence.setFocus()
        elif kind == "text":
            self.text_input.setFocus()
        elif kind == "grid":
            self.grid_edit_button.setFocus()
        elif kind == "nine_key":
            self.nine_key_setup.setFocus()
        elif kind == "macro":
            self.macro_edit_button.setFocus()
        elif kind == "ui_element":
            self.ui_element_pick.setFocus()
        elif kind == "app_menu":
            self.app_menu_pick.setFocus()
        elif kind in self.kinds:
            self.kinds[kind].setFocus()
        else:
            self.kinds["shortcut"].setFocus()
