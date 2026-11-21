"""Chat-input focus controls within the existing General settings page."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFileDialog, QFrame, QScrollArea, QVBoxLayout, QWidget

from ..chat_focus import App, MESSAGES, app_from_path
from .voice import step_card
from .widgets import button, label, row

# The engine reports one result per activation (15 internal codes). The row
# only shows whether the user needs to act; the last attempt goes to a tooltip.
NEEDS_ACTION = {
    "rebind": "找不到已指定的输入框（应用可能已更新），请点「重新指定」",
    "needs_binding": "尚未指定输入框，请点「指定聊天输入框」",
    "manual_needed": "没能自动识别输入框，请点「指定聊天输入框」",
    "unavailable": "暂时读不到这个应用，请确认已授予辅助功能权限",
}
READY_TEXT = "已就绪：切到这个应用时自动把光标放回输入框"


def row_status(code, platform: str = sys.platform):
    """Return (row text, needs_action, tooltip) for an engine result code."""
    detail = MESSAGES.get(code, "")
    tip = "最近一次：" + detail if detail else ""
    if code == "unavailable" and platform == "win32":
        return "暂时读不到这个应用，请确认窗口未最小化且未以管理员身份运行", True, tip
    if code in NEEDS_ACTION:
        return NEEDS_ACTION[code], True, tip
    return READY_TEXT, False, tip


class ChatFocusCard(QWidget):
    def __init__(self, service, parent=None, *, controller=None):
        super().__init__(parent)
        self.service = service
        self.c = controller
        self.rows = {}
        self.checkboxes = {}
        self.containers = {}
        self.available = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card, box, _ = step_card(None, "聊天输入框")
        self._card = card
        outer.addWidget(card)
        self.enabled = QCheckBox("自动聚焦聊天输入框")
        box.addWidget(self.enabled)
        self.voice_focus = QCheckBox("按语音键前先确保光标在聊天输入框内")
        self.voice_focus.setVisible(controller is not None)
        if controller is not None:
            self.voice_focus.toggled.connect(self._on_voice_focus_toggled)
            controller.voice_changed.connect(self.refresh)
        box.addWidget(self.voice_focus)
        box.addWidget(label("选择要自动聚焦的应用。通过「切换应用」或遥控器映射键把它调到前台时，自动聚焦聊天输入框。", "caption"))
        box.addWidget(label("正在搜索、重命名或编辑其他内容时，保留你的操作。", "caption"))
        self.status = label("", "caption")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self.status)
        self.selection_count = label("", "caption")
        box.addWidget(row(label("选择应用（可多选）", "section"), None, self.selection_count))
        box.addWidget(label("逐行勾选需要自动聚焦的 App，可同时选择多个；取消勾选即可移出名单。", "caption"))
        self.refresh_apps_button = button("刷新应用", self.refresh_apps)
        self.catalog_button = None
        if sys.platform == "win32":
            self.catalog_button = button("从已安装软件添加…", self.pick_installed_app)
            self.browse_button = button("选择本地 EXE…", self.browse_app)
            box.addWidget(row(self.refresh_apps_button, self.catalog_button, self.browse_button, None))
        else:
            self.browse_button = button("浏览更多应用…", self.browse_app)
            box.addWidget(row(self.refresh_apps_button, self.browse_button, None))
        self.list_widget = QWidget()
        self.list_box = QVBoxLayout(self.list_widget)
        self.list_box.setContentsMargins(0, 0, 0, 0)
        self.list_box.setSpacing(12)
        self.list_box.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.list_scroll = QScrollArea()
        self.list_scroll.setWidgetResizable(True)
        self.list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_scroll.setAccessibleName("自动聚焦应用多选列表")
        self.list_scroll.setWidget(self.list_widget)
        box.addWidget(self.list_scroll)
        self.empty = label("还没有可显示的应用。打开目标软件后刷新，或点击上方按钮添加 EXE 程序。", "caption")
        box.addWidget(self.empty)
        self.binding_hint = label("", "caption")
        self.binding_hint.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self.binding_hint)
        self.confirm = button("确认指定", lambda: self.perform(service.confirm_binding), "primary")
        self.cancel = button("取消指定", lambda: self.perform(service.cancel_binding))
        self.binding_actions = row(self.confirm, self.cancel, None)
        box.addWidget(self.binding_actions)
        if sys.platform == "win32":
            scope_note = (
                "支持官网下载安装的任意 .exe 独立软件（如 Kimi / Kimi Code、Claude、ChatGPT / Codex、"
                "Cursor、微信、Windows 终端等）及商店应用；纯浏览器网页标签页暂不支持按整个浏览器启用。\n"
                "指定后保存输入框定位规则，重启时自动重新查找；无法唯一匹配时会提示重新指定。"
            )
        else:
            scope_note = (
                "当前支持 macOS 桌面应用。网页版暂不支持按整个浏览器启用。\n"
                "指定后保存输入框定位规则，重启时自动重新查找；无法唯一匹配时会提示重新指定。"
            )
        box.addWidget(label(scope_note, "caption"))
        self.enabled.toggled.connect(lambda value: self.perform(lambda: service.enable(value)))
        service.configuration_changed.connect(self.rebuild)
        service.updated.connect(self.refresh)
        self.refresh_apps()

    def setVisible(self, visible: bool):
        self._card.setVisible(bool(visible))
        super().setVisible(bool(visible))

    def perform(self, operation):
        try:
            operation()
        except Exception as exc:
            self.service.message = str(exc)
        self.refresh()

    def refresh_apps(self):
        try:
            self.available = {app.bundle: app for app in self.service.applications()}
        except Exception:
            self.service.message = "无法读取应用列表，请稍后重试。"
        self.rebuild()

    def select_app(self, bundle, checked):
        if checked:
            if bundle in self.service.data["apps"]:
                self.service.set_app_enabled(bundle, True)
            else:
                self.service.add(self.available[bundle])
        else:
            self.service.remove(bundle)

    def pick_installed_app(self):
        from .inspector import AppCatalogDialog, _installed_app_candidates
        items = _installed_app_candidates("win32")
        if not items:
            self.browse_app()
            return
        dlg = AppCatalogDialog(items, self, title="选择要自动聚焦的软件")
        if dlg.exec() == dlg.DialogCode.Accepted:
            chosen = dlg.selected_path
            if chosen:
                self.perform(lambda p=chosen: self.service.add(app_from_path(p)))
        elif dlg.browse_requested:
            self.browse_app()

    def browse_app(self):
        if sys.platform == "darwin":
            paths, _ = QFileDialog.getOpenFileNames(
                self, "选择自动聚焦的应用（可多选）", "/Applications", "应用程序 (*.app)"
            )
        else:
            start_dir = ""
            for candidate in (
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
                Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
                Path.home() / "Desktop",
                Path.home(),
            ):
                if candidate.is_dir():
                    start_dir = str(candidate)
                    break
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                "选择自动聚焦的 EXE 程序或快捷方式（可多选）",
                start_dir,
                "应用程序与快捷方式 (*.exe *.lnk);;所有文件 (*)",
            )
        for path in paths:
            self.perform(lambda path=path: self.service.add(app_from_path(path)))

    def rebuild(self):
        apps = dict(self.available)
        for bundle, entry in self.service.data["apps"].items():
            apps.setdefault(bundle, App(bundle, 0, entry["name"]))
        for bundle in set(self.rows) - set(apps):
            widget = self.containers.pop(bundle)
            self.list_box.removeWidget(widget)
            widget.deleteLater()
            self.rows.pop(bundle)
            self.checkboxes.pop(bundle)
        for index, app in enumerate(sorted(apps.values(), key=lambda app: (app.name.casefold(), app.bundle))):
            bundle = app.bundle
            if bundle in self.rows:
                continue  # Keep keyboard focus and checkbox identity while selecting.
            container = QWidget()
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
            active = QCheckBox(app.name)
            active.setText(self.fontMetrics().elidedText(app.name, Qt.TextElideMode.ElideRight, 280))
            active.setToolTip(app.name)
            active.setAccessibleName("对 " + app.name + " 自动聚焦")
            active.setMinimumHeight(28)
            bind = button("指定聊天输入框", lambda checked=False, key=bundle: self.perform(lambda: self.service.begin_binding(key)))
            bind.setAccessibleName("指定 " + app.name + " 的聊天输入框")
            layout.addWidget(row(active, None, bind))
            state = label("", "caption")
            state.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(state)
            active.toggled.connect(lambda value, key=bundle: self.perform(lambda: self.select_app(key, value)))
            self.list_box.insertWidget(index, container)
            self.rows[bundle] = (state, bind)
            self.checkboxes[bundle] = active
            self.containers[bundle] = container
        self.list_scroll.setFixedHeight(min(320, max(96, len(apps) * 64)))
        self.refresh()

    def _on_voice_focus_toggled(self, value: bool):
        if self.c is not None:
            self.c.set_voice_auto_focus(bool(value))

    def refresh(self):
        service = self.service
        self.enabled.blockSignals(True)
        self.enabled.setChecked(service.data["enabled"])
        self.enabled.blockSignals(False)
        if self.c is not None:
            self.voice_focus.blockSignals(True)
            self.voice_focus.setChecked(bool(self.c.settings.get("voice_auto_focus", True)))
            self.voice_focus.setEnabled(bool(service.supported and service.live and not service.preferences.error and service.data["enabled"]))
            self.voice_focus.blockSignals(False)
        usable = service.supported and service.live and not service.preferences.error
        for widget in (self.enabled, self.refresh_apps_button, self.browse_button, self.list_widget):
            widget.setEnabled(usable)
        if not service.supported:
            text = "此功能当前支持 macOS 与 Windows 桌面应用。"
        elif not service.live:
            text = "预览模式：不会读取或聚焦外部应用。"
        else:
            text = service.message or ("已开启，仅对白名单应用生效。" if service.data["enabled"] else "已暂停自动聚焦。")
        self.status.setText(text)
        self.empty.setVisible(not self.rows)
        self.list_scroll.setVisible(bool(self.rows))
        count = sum(entry["enabled"] for entry in service.data["apps"].values())
        self.selection_count.setText(f"已勾选 {count} 个应用")
        selecting = service.state["selecting"]
        ready = service.state["candidate_ready"]
        for bundle, (state, bind) in self.rows.items():
            entry = service.data["apps"].get(bundle)
            selected = bool(entry and entry["enabled"])
            checkbox = self.checkboxes[bundle]
            checkbox.blockSignals(True)
            checkbox.setChecked(selected)
            checkbox.blockSignals(False)
            state.setVisible(selected)
            if not selected:
                bind.hide()
                continue
            default = ("saved" if entry.get("locator") else "needs_binding") if entry["mode"] == "manual" else "waiting"
            code = service.state["states"].get(bundle, default)
            text, needs_action, tip = row_status(code)
            state.setText(text)
            state.setToolTip(tip)
            if bool(state.property("attention")) != needs_action:
                state.setProperty("attention", needs_action)
                state.style().unpolish(state)
                state.style().polish(state)
            has_target = bool(entry.get("locator")) or code in {"saved", "restored", "already"}
            bind.setText("重新指定" if has_target else "指定聊天输入框")
            bind.setVisible(True)
            bind.setEnabled(usable and not selecting)
        self.binding_hint.setVisible(bool(selecting))
        self.binding_actions.setVisible(bool(selecting))
        self.confirm.setEnabled(usable and ready)
        self.cancel.setEnabled(usable)
        remaining = service.state["seconds_left"]
        if ready:
            self.binding_hint.setText("已取得选中的输入框。确认它是聊天框后，点击「确认指定」。")
        elif remaining > 0:
            reason = {
                "window_unavailable": "暂时读不到应用窗口，请保持目标窗口打开。",
                "focus_unavailable": "应用尚未返回输入控件，请点击聊天输入框并停留两三秒。",
                "not_editor": "当前还没有识别到聊天输入框，请点击真正的输入区域。",
            }.get(service.state.get("capture_status"), "请切到目标应用并点击聊天输入框，再返回这里确认。")
            self.binding_hint.setText(f"{reason}剩余 {remaining} 秒。")
        else:
            self.binding_hint.setText("没有取得输入框，请取消后重新指定。")
