"""按键映射 page: remote photo canvas on the left, inspector on the right."""
from __future__ import annotations

import sys

from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox, QVBoxLayout, QWidget,
)

from ..canvas import RemoteCanvas
from ..theme import INSPECTOR_WIDTH
from .inspector import Inspector, choose_application, choose_installed_application
from .widgets import ScrollSafeComboBox, button, label, row


class MappingPage(QWidget):
    title = "按键映射"
    subtitle = "点图上的按键设置功能；可按前台应用创建专属覆盖层，未重定义的按键自动继承全局默认。"

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self._syncing = False
        self.setObjectName("page")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(16)

        left = QWidget()
        left.setObjectName("canvasPanel")
        box = QVBoxLayout(left)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        self.layer_box = ScrollSafeComboBox()
        self.layer_box.setMinimumHeight(34)
        self.layer_box.setMinimumWidth(136)
        self.layer_box.setMaximumWidth(220)
        self.layer_box.setAccessibleName("选择当前编辑的按键层")
        self.layer_box.activated.connect(self.select_layer_index)
        self.add_layer_button = button("＋ 应用层…", self.open_add_layer_menu, "quiet")
        self.add_layer_button.setToolTip("为指定应用创建专属按键覆盖层；未设置的按键继续继承全局默认")
        self.manage_layer_button = button("管理…", self.open_manage_layer_menu, "quiet")
        self.auto_layer_check = QCheckBox("自动切层")
        self.auto_layer_check.setToolTip("根据当前前台应用自动切换按键层；关闭后固定使用全局默认层")
        self.auto_layer_check.toggled.connect(self._on_auto_layer_toggled)
        self.count = label("", "muted", wrap=False)
        box.addWidget(row(
            label("编辑层", "caption", wrap=False),
            self.layer_box,
            self.add_layer_button,
            self.manage_layer_button,
            self.auto_layer_check,
            None,
            self.count,
            spacing=6,
        ))

        self.edit_banner = QLabel("框选模式：在照片上拖出新位置，松手即保存。")
        self.edit_banner.setObjectName("banner")
        self.edit_done = button("完成", lambda: self.inspector.draw.setChecked(False), "quiet")
        self.edit_strip = row(self.edit_banner, self.edit_done, None, spacing=8)
        self.edit_strip.hide()
        box.addWidget(self.edit_strip)
        self.canvas = RemoteCanvas()
        box.addWidget(self.canvas, 1)
        box.addWidget(label("单击选择 · 双击或 Enter 编辑动作 · 方向键切换按键 · ★ 表示当前应用层专属覆盖", "caption"))
        layout.addWidget(left, 1)

        self.inspector = Inspector(controller)
        layout.addWidget(self.inspector)

        self.canvas.selected.connect(self.select_key)
        self.canvas.activated.connect(self.activate)
        self.canvas.region.connect(controller.set_rect)
        self.inspector.edit_mode.connect(self.set_edit)
        controller.mapping_changed.connect(self.refresh)
        controller.profiles_changed.connect(self.refresh)
        controller.physical_key.connect(self._on_physical_key)
        controller.voice_key_active.connect(self._on_voice_key_active)
        self.refresh()

    def _on_physical_key(self, trigger: str, down: bool) -> None:
        btn = next((b for b in self.c.profile.buttons if b.matches_trigger(trigger)), None)
        if btn is not None:
            self.canvas.set_pressed(btn.id, down)

    def _on_voice_key_active(self, active: bool) -> None:
        for btn in self.c.profile.buttons:
            if getattr(btn, "role", "") == "voice" or btn.id == "tv-voice":
                self.canvas.set_pressed(btn.id, active)

    def refresh(self):
        self._syncing = True
        try:
            layer = self.c.editing_layer
            self.layer_box.blockSignals(True)
            self.layer_box.clear()
            self.layer_box.addItem("全局默认", "")
            current_index = 0
            for idx, item in enumerate(self.c.profile.app_layers, start=1):
                overridden_count, _ = self.c.layer_override_count(item)
                if not item.enabled:
                    suffix = "（已停用）"
                elif overridden_count:
                    suffix = f"（{overridden_count} 键）"
                else:
                    suffix = ""
                self.layer_box.addItem(item.name + suffix, item.id)
                if layer is not None and item.id == layer.id:
                    current_index = idx
            self.layer_box.setCurrentIndex(current_index)
            self.layer_box.blockSignals(False)

            has_layers = bool(self.c.profile.app_layers)
            self.manage_layer_button.setVisible(layer is not None)
            self.auto_layer_check.setVisible(has_layers)
            self.auto_layer_check.blockSignals(True)
            self.auto_layer_check.setChecked(self.c.app_layers_enabled)
            self.auto_layer_check.blockSignals(False)

            self.canvas.set_profile(self.c.profile, self.c.selected_id, layer=layer)
            if layer is None:
                done, total = self.c.assigned_count()
                self.count.setText(f"已分配 {done} / {total}")
                self.count.setToolTip("全局默认层已配置动作的按键数量")
            else:
                overridden, total = self.c.layer_override_count(layer)
                self.count.setText(f"已覆盖 {overridden} / {total}")
                self.count.setToolTip("其余未覆盖的按键与手势自动继承全局默认层")
        finally:
            self._syncing = False

    def select_layer_index(self, index: int):
        if self._syncing:
            return
        layer_id = self.layer_box.itemData(index) or ""
        if layer_id == self.c.editing_layer_id:
            return
        if not self.inspector.confirm_text_saved():
            self.refresh()
            return
        self.c.select_editing_layer(layer_id)

    def _on_auto_layer_toggled(self, checked: bool):
        if self._syncing:
            return
        self.c.set_app_layers_enabled(checked)

    def populate_add_layer_menu(self, menu: QMenu):
        apps = self.c.available_layer_apps()
        existing = {item.app_id.casefold() for item in self.c.profile.app_layers}
        if apps:
            header = menu.addAction("当前运行的应用")
            header.setEnabled(False)
            for app in apps:
                title = f"{app.name}（已创建）" if app.app_id.casefold() in existing else app.name
                act = menu.addAction(title)
                act.triggered.connect(lambda checked=False, item=app: self.c.add_app_layer(item))
            menu.addSeparator()
        if sys.platform != "darwin":
            catalog = menu.addAction("从已安装应用选择…")
            catalog.triggered.connect(self.pick_catalog_layer_app)
        browse = menu.addAction("浏览应用程序…")
        browse.triggered.connect(self.browse_layer_app)

    def open_add_layer_menu(self):
        if not self.inspector.confirm_text_saved():
            return
        menu = QMenu(self)
        self.populate_add_layer_menu(menu)
        menu.exec(self.add_layer_button.mapToGlobal(self.add_layer_button.rect().bottomLeft()))

    def pick_catalog_layer_app(self):
        if not self.inspector.confirm_text_saved():
            return
        path = choose_installed_application(self)
        if path:
            self.c.add_app_layer_from_path(path)

    def browse_layer_app(self):
        if not self.inspector.confirm_text_saved():
            return
        path = choose_application(self)
        if path:
            self.c.add_app_layer_from_path(path)

    def open_manage_layer_menu(self):
        layer = self.c.editing_layer
        if layer is None or not self.inspector.confirm_text_saved():
            return
        menu = QMenu(self)
        info = menu.addAction(f"{layer.name}（{layer.app_id}）")
        info.setEnabled(False)
        menu.addSeparator()
        toggle_action = menu.addAction("停用此应用层" if layer.enabled else "启用此应用层")
        rename_action = menu.addAction("重命名应用层…")
        clear_action = menu.addAction("清空此层全部覆盖…")
        clear_action.setEnabled(bool(layer.overrides))
        menu.addSeparator()
        delete_action = menu.addAction("删除此应用层…")
        chosen = menu.exec(self.manage_layer_button.mapToGlobal(self.manage_layer_button.rect().bottomLeft()))
        if chosen == toggle_action:
            self.c.set_app_layer_enabled(layer.id, not layer.enabled)
        elif chosen == rename_action:
            name, ok = QInputDialog.getText(self, "重命名应用层", "显示名称", text=layer.name)
            if ok:
                self.c.rename_app_layer(layer.id, name)
        elif chosen == clear_action:
            answer = QMessageBox.question(
                self,
                "清空专属覆盖",
                f"确定清空「{layer.name}」的全部专属按键覆盖吗？清空后所有按键恢复继承全局默认。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.c.clear_app_layer_overrides(layer.id)
        elif chosen == delete_action:
            answer = QMessageBox.question(
                self,
                "删除应用层",
                f"确定删除「{layer.name}」应用层吗？此操作不能撤销。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.c.delete_app_layer(layer.id)

    def select_key(self, ident):
        if self.inspector.nine_key_editor.pick(ident):
            return
        if ident == self.c.selected_id:
            return
        if not self.inspector.confirm_text_saved():
            self.refresh()
            return
        self.c.select(ident)

    def activate(self, ident):
        if self.inspector.nine_key_editor.active:
            return
        self.select_key(ident)
        if self.c.selected_id == ident:
            self.inspector.focus_editor()

    def set_edit(self, on):
        self.canvas.set_edit(on)
        self.edit_strip.setVisible(on)
