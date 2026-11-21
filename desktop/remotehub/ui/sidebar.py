"""Left rail: brand, page navigation, device card, import/export."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLineEdit,
    QVBoxLayout, QWidget,
)

from ..branding import APP_NAME
from ..inventory import receiver_name
from ..theme import SIDEBAR_WIDTH, stylesheet
from .widgets import Pill, ScrollSafeComboBox, button, label, row, rule


class Sidebar(QWidget):
    page_requested = Signal(int)
    retry_requested = Signal()
    settings_requested = Signal()

    def __init__(self, controller, pages, parent=None):
        super().__init__(parent)
        self.c = controller
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(SIDEBAR_WIDTH)
        side = QVBoxLayout(self)
        side.setContentsMargins(16, 22, 16, 16)
        side.setSpacing(6)
        brand = label(APP_NAME, wrap=False)
        brand.setObjectName("brand")
        sub = label("遥控器 · 按键 · 语音")
        sub.setObjectName("brandSub")
        side.addWidget(brand)
        side.addWidget(sub)
        side.addSpacing(18)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for index, (title, shortcut) in enumerate(pages):
            nav = button(title, lambda i=index: self.page_requested.emit(i), "nav")
            nav.setCheckable(True)
            nav.setToolTip(shortcut)
            self.nav.addButton(nav, index)
            side.addWidget(nav)
        side.addSpacing(18)

        side.addWidget(label("设备", "caption"))
        card = QFrame()
        card.setObjectName("deviceCard")
        box = QVBoxLayout(card)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(8)
        self.device_name = label("", "value", wrap=False)
        self.device_name.setAccessibleName("当前设备")
        box.addWidget(self.device_name)
        self.status = Pill("尚未检测")
        self.status.setAccessibleName("USB 连接状态")
        box.addWidget(self.status)
        self.detail = label("", "caption")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.detail)
        self.settings = button("前往通用设置", self.settings_requested.emit, "link")
        box.addWidget(self.settings)
        # Key input starts by itself; there is no enable switch. A failed
        # attempt waits for 重新检测 (or a replug) instead of retrying every scan.
        box.addWidget(button("重新检测", self.retry_requested.emit, "link"))
        side.addWidget(card)
        side.addStretch(1)

        scheme_header = QWidget()
        scheme_header_layout = QHBoxLayout(scheme_header)
        scheme_header_layout.setContentsMargins(0, 0, 2, 0)
        scheme_header_layout.setSpacing(6)
        self.scheme_caption = label("配置方案", "caption")
        self.new_profile_button = button("新建…", self.create_new_profile, "link")
        self.manage_profile = button("管理…", self.manage_profiles, "link")
        self.rename_single_button = button("重命名…", self.rename_current_profile, "link")
        scheme_header_layout.addWidget(self.scheme_caption, 1)
        scheme_header_layout.addWidget(self.new_profile_button, 0)
        scheme_header_layout.addWidget(self.manage_profile, 0)
        scheme_header_layout.addWidget(self.rename_single_button, 0)
        side.addWidget(scheme_header)

        self.profile_card = QFrame()
        self.profile_card.setObjectName("profileCard")
        pbox = QVBoxLayout(self.profile_card)
        pbox.setContentsMargins(10, 10, 10, 10)
        pbox.setSpacing(8)

        self.profile_box = ScrollSafeComboBox()
        self.profile_box.setAccessibleName("配置方案")
        self.profile_box.activated.connect(self.select_profile)
        pbox.addWidget(self.profile_box)

        self.single_profile_row = QWidget()
        single_layout = QHBoxLayout(self.single_profile_row)
        single_layout.setContentsMargins(2, 2, 2, 2)
        single_layout.setSpacing(6)
        self.current_profile_label = label("", "value", wrap=False)
        self.current_profile_label.setAccessibleName("当前配置方案")
        single_layout.addWidget(self.current_profile_label, 1)
        pbox.addWidget(self.single_profile_row)

        pbox.addWidget(rule())

        io_holder = QWidget()
        io_layout = QHBoxLayout(io_holder)
        io_layout.setContentsMargins(0, 0, 0, 0)
        io_layout.setSpacing(6)
        self.import_button = button("导入按键…", self.import_profile, "quiet")
        self.export_button = button("导出按键…", self.export_profile, "quiet")
        io_layout.addWidget(self.import_button, 1)
        io_layout.addWidget(self.export_button, 1)
        pbox.addWidget(io_holder)
        side.addWidget(self.profile_card)

        controller.profiles_changed.connect(self.refresh_profiles)
        controller.device_changed.connect(self.refresh_device)
        self.refresh_profiles()

    def set_page(self, index):
        self.nav.button(index).setChecked(True)

    def refresh_profiles(self):
        names = self.c.profile_names()
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        for idx, (name, prof) in enumerate(zip(names, self.c.profiles)):
            self.profile_box.addItem(name)
            hw = receiver_name(prof, self.c.monitor.devices)
            label_text = f"{name}（{hw}）"
            self.profile_box.setItemData(idx, label_text, Qt.ItemDataRole.ToolTipRole)
            self.profile_box.setItemData(idx, label_text, Qt.ItemDataRole.AccessibleTextRole)
        self.profile_box.setCurrentIndex(self.c.index)
        self.profile_box.blockSignals(False)
        # Single scheme shows plain name + rename; multiple schemes show the picker.
        multiple = len(names) > 1
        self.profile_box.setVisible(multiple)
        self.manage_profile.setVisible(multiple)
        self.single_profile_row.setVisible(not multiple)
        self.rename_single_button.setVisible(not multiple)
        hw_label = receiver_name(self.c.profile, self.c.monitor.devices) if names else ""
        current_name = names[self.c.index] if names else ""
        display_single = "默认方案" if current_name == hw_label and current_name else current_name
        metrics = QFontMetrics(self.current_profile_label.font())
        self.current_profile_label.setText(metrics.elidedText(display_single, Qt.TextElideMode.ElideRight, 162))
        self.current_profile_label.setToolTip(display_single)
        self.device_name.setVisible(True)
        self.device_name.setText(hw_label)
        self.refresh_device()

    def refresh_device(self):
        view = self.c.device_view()
        self.status.show_status(view)
        self.detail.setText(view.detail)
        self.detail.setToolTip(view.diagnostic)
        self.settings.setVisible(view.settings_link)

    def confirm_text_saved(self):
        inspector = getattr(self.window(), "inspector", None)
        return inspector is None or inspector.confirm_text_saved()

    def select_profile(self, index):
        if index == self.c.index:
            return
        if not self.confirm_text_saved():
            self.profile_box.setCurrentIndex(self.c.index)
            return
        self.c.select_profile(index)

    def import_profile(self):
        if not self.confirm_text_saved():
            return
        path = QFileDialog.getOpenFileName(self, "导入按键配置", "", "按键配置 (*.json)")[0]
        if not path:
            return
        from PySide6.QtWidgets import QMessageBox
        dialog = QMessageBox(self)
        dialog.setWindowTitle("导入按键配置")
        dialog.setText("替换当前配置，还是保留为一个新配置？语音设置不受影响。")
        replace = dialog.addButton("替换当前配置", QMessageBox.ButtonRole.AcceptRole)
        append = dialog.addButton("保留为新配置", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        if dialog.clickedButton() == replace:
            self.c.import_profile(path, replace=True)
        elif dialog.clickedButton() == append:
            self.c.import_profile(path)

    def create_new_profile(self):
        if not self.confirm_text_saved():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("新建配置方案")
        dialog.setModal(True)
        dialog.resize(380, 170)
        dialog.setStyleSheet(stylesheet())
        root = QVBoxLayout(dialog)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        root.addWidget(label("输入新配置方案名称：", "section"))
        name_edit = QLineEdit(self.c.next_new_profile_name())
        name_edit.setMaxLength(100)
        name_edit.selectAll()
        name_edit.setAccessibleName("新配置方案名称")
        root.addWidget(name_edit)
        copy_check = QCheckBox("复制当前方案的按键映射与应用层")
        root.addWidget(copy_check)
        cancel_btn = button("取消", dialog.reject)
        create_btn = button("新建方案", dialog.accept, "primary")
        create_btn.setDefault(True)
        name_edit.textChanged.connect(lambda text: create_btn.setEnabled(bool(text.strip())))
        root.addStretch(1)
        root.addWidget(row(None, cancel_btn, create_btn, spacing=8))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.c.create_profile(name_edit.text(), copy_current=copy_check.isChecked())

    def rename_current_profile(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "重命名配置方案", "方案名称", text=self.c.profile.name)
        if ok:
            self.c.rename_profile(name)

    def manage_profiles(self):
        from PySide6.QtWidgets import QMenu, QMessageBox
        menu = QMenu(self)
        rename = menu.addAction("重命名当前方案…")
        delete = menu.addAction("删除当前方案…")
        chosen = menu.exec(self.manage_profile.mapToGlobal(self.manage_profile.rect().bottomLeft()))
        if chosen == rename:
            self.rename_current_profile()
        elif chosen == delete:
            answer = QMessageBox.question(self, "删除配置方案", f"确定删除「{self.c.profile.name}」吗？此操作不能撤销。",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                          QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Yes:
                self.c.delete_profile()

    def export_profile(self):
        if not self.confirm_text_saved():
            return
        path = QFileDialog.getSaveFileName(self, "导出按键配置", self.c.profile.name.replace(":", "-").replace("/", "-").replace("\\", "-") + ".json", "按键配置 (*.json)")[0]
        if path:
            self.c.export_profile(path)
