"""「我的遥控器」page: bound remotes and which remotes can be connected."""
from __future__ import annotations

import base64
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QInputDialog, QMessageBox, QVBoxLayout, QWidget,
)

from .widgets import button, label, row


class RemoteCard(QFrame):
    def __init__(self, profile, title, details, action_text, action, *, compact=False, primary=False):
        super().__init__()
        self.setObjectName("deviceCard")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10 if compact else 12, 14, 10 if compact else 12)
        layout.setSpacing(14)
        self.photo = label()
        self.photo.setFixedSize(*((44, 60) if compact else (84, 116)))
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap()
        if profile.image and pixmap.loadFromData(base64.b64decode(profile.image)):
            self.photo.setPixmap(pixmap.scaled(
                self.photo.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            ))
        layout.addWidget(self.photo, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(3 if compact else 5)
        self.name = label(title, "value")
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.badge = label("", "caption")
        self.badge.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(row(self.name, self.badge, None, spacing=8))
        self.details = label(details, "muted")
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self.details)
        self.status = label("", "caption")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self.status)
        self.action = button(action_text, action, "primary" if primary else None)
        self.action.setAccessibleName(f"{action_text} · {title}")
        self.action.setAutoDefault(False)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.actions.addWidget(self.action)
        self.actions.addStretch(1)
        if compact:
            # One line: photo · name and details · the action on the right.
            self.status.hide()
            layout.addLayout(column, 1)
            layout.addWidget(self.action, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            column.addLayout(self.actions)
            layout.addLayout(column, 1)

    def add_link(self, text, action):
        link = button(text, action, "link")
        link.setAutoDefault(False)
        self.actions.insertWidget(self.actions.count() - 1, link)
        return link


def key_count(profile):
    return sum(key.configurable and bool(key.trigger) for key in profile.buttons)


class RemoteListPage(QWidget):
    title = "我的遥控器"
    subtitle = "添加、切换和管理遥控器。同一种接收器插哪一个都能用。"
    open_mapping = Signal()
    add_requested = Signal()
    learn_requested = Signal(str)    # remote id to learn more keys / add connection

    def __init__(self, controller):
        super().__init__()
        self.c = controller
        self.saved_cards = {}

        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(10)
        self.add = button("添加遥控器", self.add_requested.emit, "primary")
        self.add.setAutoDefault(False)
        page.addWidget(row(label("已添加", "section"), None, self.add))
        self.saved_column = QVBoxLayout()
        self.saved_column.setSpacing(10)
        page.addLayout(self.saved_column)
        self.empty = label("还没有添加遥控器。插入接收器或连接蓝牙遥控器，然后点「添加遥控器」。", "muted")
        self.saved_column.addWidget(self.empty)

        # What can be connected, in the same plain access levels as the add dialog.
        from ..protocols import (
            ACCESS_DIRECT, ACCESS_ONE_CLICK, ACCESS_UNSUPPORTED, MIC_DIRECT, MIC_DONGLE, MIC_LEVELS, MIC_NONE,
            access_heading, dongle_route_available, dongle_route_note,
        )

        self.catalog = QWidget()
        catalog = QVBoxLayout(self.catalog)
        catalog.setContentsMargins(0, 14, 0, 0)
        catalog.setSpacing(8)
        catalog.addWidget(label("能接入哪些遥控器", "section"))
        catalog.addWidget(
            label("不限遥控器外观和键数。点右上角「添加遥控器」，选中设备后会告诉你属于哪一档，再导入你自己的遥控器照片学习按键。", "muted")
        )
        self.protocol_card = QFrame()
        self.protocol_card.setObjectName("deviceCard")
        proto_layout = QVBoxLayout(self.protocol_card)
        proto_layout.setContentsMargins(14, 12, 14, 12)
        proto_layout.setSpacing(6)
        dongle = dongle_route_available()
        one_click = "系统读不到的 USB 接收器在 Windows 上让 App 接管" + (
            f"；电视盒蓝牙遥控器插外接蓝牙棒直连{dongle_route_note(sys.platform)}" if dongle else "")
        self.access_label = label(
            f"{access_heading(ACCESS_DIRECT)}：标准 USB 接收器和蓝牙遥控器的按键（含配套接收器 0416:0602）\n"
            f"{access_heading(ACCESS_ONE_CLICK)}：{one_click}\n"
            f"{access_heading(ACCESS_UNSUPPORTED)}：厂商私有接口、手柄式按键、电源键等；添加窗口里可以复制给 AI 求助",
            "caption",
        )
        self.access_label.setTextFormat(Qt.TextFormat.PlainText)
        tv_box = (f"FIBER0D、Android TV 等电视盒语音遥控器{MIC_LEVELS[MIC_DONGLE]}（目前版本通过电脑自带蓝牙收不到声音）；"
                  if dongle else "")
        self.mic_label = label(
            f"遥控器麦克风：配套接收器 0416:0602 {MIC_LEVELS[MIC_DIRECT]}；{tv_box}"
            f"其他遥控器{MIC_LEVELS[MIC_NONE]}，语音键可触发识别、用电脑麦克风说话",
            "caption",
        )
        self.mic_label.setTextFormat(Qt.TextFormat.PlainText)
        proto_layout.addWidget(self.access_label)
        proto_layout.addWidget(self.mic_label)
        catalog.addWidget(self.protocol_card)
        page.addWidget(self.catalog)
        page.addStretch(1)

        controller.remotes_changed.connect(self.populate)
        controller.device_changed.connect(self.update_status)
        self.populate()

    def populate(self):
        for card in self.saved_cards.values():
            self.saved_column.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.saved_cards.clear()
        remotes = self.c.remotes
        self.empty.setVisible(not remotes)
        for remote in remotes:
            profile = remote.active_profile
            connections = "、".join(c.label() for c in remote.connections)
            schemes = f" · {len(remote.profiles)} 个配置方案" if len(remote.profiles) > 1 else ""
            details = f"{connections} · {key_count(profile)} 个按键{schemes}"
            card = RemoteCard(profile, remote.name, details, "打开按键映射",
                              lambda ident=remote.id: self.open_remote(ident))
            if not remote.is_fixed_model:
                learn = card.add_link("学习更多按键", lambda ident=remote.id: self.learn_requested.emit(ident))
                learn.setToolTip("补充学习按键，或为这只遥控器添加备用 USB / 蓝牙接收端。")
            card.add_link("重命名", lambda ident=remote.id: self.rename_remote(ident))
            card.add_link("删除", lambda ident=remote.id: self.remove_remote(ident))
            self.saved_cards[remote.id] = card
            self.saved_column.addWidget(card)
        self.catalog.setVisible(True)
        self.update_status()

    def update_status(self):
        active = self.c.store.active
        paused = self.c.remote_input_paused
        view = self.c.remote_view()
        connected = view.tone == "ok"
        for identity, card in self.saved_cards.items():
            if identity == active:
                card.badge.setText("● 正在使用")
                card.status.setText("已连接" if connected else "未连接")
                card.status.setToolTip(" ".join(part for part in (view.title, view.detail) if part))
            else:
                card.badge.setText("")
                card.status.setText("")
                card.status.setToolTip("")
            card.action.setEnabled(not paused)
        self.add.setEnabled(not paused)

    def confirm_text_saved(self):
        inspector = getattr(self.window(), "inspector", None)
        return inspector is None or inspector.confirm_text_saved()

    def open_remote(self, identity):
        if not self.confirm_text_saved():
            return
        if self.c.select_remote(identity):
            self.open_mapping.emit()

    def rename_remote(self, identity):
        remote = next((r for r in self.c.remotes if r.id == identity), None)
        if remote is None:
            return
        name, ok = QInputDialog.getText(self, "重命名遥控器", "遥控器名称", text=remote.name)
        if ok:
            self.c.rename_remote(identity, name)

    def remove_remote(self, identity):
        remote = next((r for r in self.c.remotes if r.id == identity), None)
        if remote is None:
            return
        answer = QMessageBox.question(
            self, "删除遥控器",
            f"删除「{remote.name}」及它的 {len(remote.profiles)} 个配置方案？此操作不能撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.c.remove_remote(identity)
