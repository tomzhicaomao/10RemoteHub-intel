"""Development entry point for testing a remote before creating a mapping."""
from __future__ import annotations

import argparse
from contextlib import suppress
from itertools import count
import sys
import threading

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLayout, QMessageBox, QPlainTextEdit,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .branding import APP_NAME
from .enrollment import (
    BOUNDARIES, KEY_GUIDANCE, ai_request, ble_dongle_help_text, classify, diagnostic_text, driver_help_text,
    unlisted_access_text, windows_0416_needs_winusb_keys,
)
from .icons import configure_application
from .permissions import open_permission_settings, permission_status
from .protocols import (
    ACCESS_NOT_SEEN, ACCESS_ONE_CLICK, ACCESS_UNSUPPORTED, DONGLE_MISSING, access_heading, dongle_route_available,
)
from .theme import apply_theme
from .ui.widgets import ScrollSafeComboBox, button, label, row, rule, scroll, set_role


TAKEOVER_CONFIRM_SECONDS = 15
TAKEOVER_NOTE = (
    "接管后这只接收器只由 App 读取：学得到系统读不到的键，按键也不会再触发原来的功能。"
    "注意：App 没有运行时，这只遥控器不能用；随时可以点「恢复系统驱动」还原。"
)
NOTHING_TO_TAKE_OVER = "这个 USB 设备没有 App 能接管的按键或语音接口（比如只有鼠标、标准声卡或存储功能）。"
NOT_SEEN_TITLE = f"{access_heading(ACCESS_NOT_SEEN)}：还没看到你的遥控器"
NOT_SEEN_BODY = "检查 USB 接收器是否插好，或在电脑蓝牙设置里确认遥控器已连接，然后点「重新检测」。"
TAKEN_OVER_NOTE = "这只接收器已由 App 接管：App 没有运行时它不能用。点「恢复系统驱动」即可还原为普通设备。"


class PhotoPreviewReader:
    """Send example key edges through the same path as real HID reports."""

    def __init__(self):
        from .probe_device import ProbeDevice

        self.device = ProbeDevice(("preview",), "示例遥控器", "模拟", 0, 0)
        self.keys = count(30)
        self.on_key = None

    def enumerate(self):
        return [self.device]

    def start(self, _identity, on_key, _on_removed):
        self.on_key = on_key
        return 1, 1

    def press(self):
        if self.on_key is None:
            return
        emit = self.on_key
        self.on_key = None
        trigger = f"hid:7:{next(self.keys)}"
        emit((0, 0), trigger, True)
        emit((0, 0), trigger, False)

    def stop(self):
        self.on_key = None

    close = stop


class DeviceProbeDialog(QDialog):
    key_received = Signal(int, object, str, bool)
    device_removed = Signal(int)
    capture_started = Signal(int, int, int, str)
    ble_scan_finished = Signal(int, str)

    def __init__(self, reader, parent=None, *, device=None, preview=False, install=None, draft=None,
                 connection_users=None, usb_devices=None, owns_reader=True):
        super().__init__(parent)
        self.reader = reader
        self.owns_reader = owns_reader
        self.devices = []
        self.generation = 0
        self.active = False
        self.held = set()
        self.seen = set()
        self.photo = None
        self.install = install
        self.draft = draft
        self._initial_device = device
        self.connection_users = connection_users  # device -> names of remotes already accepting it
        self.usb_devices = usb_devices            # () -> USB inventory, to spot devices without HID
        self.enrollment = None
        self.simulate_button = None
        self.learning_dialog = None
        self.guide = None
        self.scroll_area = None
        self.footer = None
        self.pending_merge_trigger = ""
        self.merge_card = None
        self.merge_prompt = None
        self.merge_picker = None
        self.merge_confirm_button = None
        self.merge_cancel_button = None
        self._discard_confirmed = False
        self.input_step = "not_visible"
        self.interface_counts = (None, None)
        self.driver_help_dialog = None
        self.ble_dongle_help_dialog = None
        self.ble_dongle_help = None
        self.ble_dongle_card = None
        self.ble_dongle_note = None
        self.ble_dongle_picker = None
        self.ble_dongle_bind = None
        self.ble_dongle_scan = None
        self.ble_dongle_restore = None
        self.empty_card = None
        self.empty_title = None
        self.empty_body = None
        self.empty_refresh_button = None
        self.blocked_ble_card = None
        self.blocked_ble_title = None
        self.blocked_ble_body = None
        self.blocked_ble_bt_settings = None
        self.blocked_ble_help_link = None
        self._blocked_ble = []
        self._ble_scan_target = None
        self.picker_row = None
        self.header_hint = None
        self.diag_toggle = None
        self.diag_drawer = None
        self.back_to_list_button = None
        self.receiver_bind = None
        self.receiver_restore = None
        self.receiver_note = None
        self.driver_action_row = None
        self._confirm_box = None
        self._managed_dongles = []
        self._managed_receivers = []
        self._driver_pending = None  # (handle, result_path, action)
        self._ble_scanning = False
        self._ble_paused = False
        self._driver_poll_timer = QTimer(self)
        self._driver_poll_timer.setInterval(250)
        self._driver_poll_timer.timeout.connect(self._poll_win_driver_action)
        self.results_card = None
        apply_theme(self)
        outer = None
        self.picker = ScrollSafeComboBox(self)
        self.picker.setAccessibleName("选择要探测的遥控器")
        self.refresh_button = button("重新检测", self.refresh)
        if device is None and draft is None:
            self.setWindowTitle("添加遥控器")
            self.setMinimumSize(520, 540)
            self.resize(560, 620)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(24, 20, 24, 16)
            outer.setSpacing(12)
            outer.addWidget(label("添加遥控器", "title"))
            self.header_hint = label("插入 USB 接收器，或在系统设置中连接蓝牙遥控器。", "muted")
            self.header_hint.setTextFormat(Qt.TextFormat.PlainText)
            outer.addWidget(self.header_hint)
            # Keep the device picker pinned above the scroll area so it is never scrolled out of view.
            self.picker_row = row(self.picker, self.refresh_button)
            outer.addWidget(self.picker_row)
            body = QWidget()
            body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            layout = QVBoxLayout(body)
            layout.setContentsMargins(0, 0, 8, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            self.scroll_area = scroll(body)
            self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.scroll_area.setMinimumHeight(240)
            outer.addWidget(self.scroll_area, 1)
        else:
            from .ui.probe_photo import ProbePhotoPane

            self.setWindowTitle("照片学习")
            self.setMinimumSize(1040, 740)
            target_w, target_h = 1160, 800
            screen = QApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                target_w = min(target_w, max(1040, avail.width() - 48))
                target_h = min(target_h, max(740, avail.height() - 48))
            self.resize(target_w, target_h)
            workspace = QHBoxLayout(self)
            workspace.setContentsMargins(20, 16, 20, 16)
            workspace.setSpacing(16)
            self.photo = ProbePhotoPane(
                device, preview=preview, profile=draft.active_profile if draft else None, embed_controls=False
            )
            workspace.addWidget(self.photo, 1)
            panel = QWidget()
            panel.setMinimumWidth(420)
            panel.setMaximumWidth(470)
            workspace.addWidget(panel)
            panel_outer = QVBoxLayout(panel)
            panel_outer.setContentsMargins(0, 0, 0, 0)
            panel_outer.setSpacing(10)
            panel_outer.addWidget(label("学习实体按键", "title"))
            panel_body = QWidget()
            layout = QVBoxLayout(panel_body)
            layout.setContentsMargins(0, 0, 6, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            self.scroll_area = scroll(panel_body)
            self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            panel_outer.addWidget(self.scroll_area, 1)
            self._panel_outer = panel_outer
            self.photo.canvas.position_picked.connect(self.learn_position)
            self.photo.learn_requested.connect(self.learn_frame)
            self.photo.selection_changed.connect(self._sync_capture_prompt)
            if install is not None:
                self.photo.help_requested.connect(self.copy_ai_request)
                self.photo.help_button.show()
            self.photo.image_changed.connect(self.photo_changed)
            self.photo.mic_access = self.mic_access_text
        layout.setSpacing(10)

        if self.photo is not None and draft is not None:
            layout.addWidget(row(self.picker, self.refresh_button))
        elif self.photo is not None:
            self.picker.hide()
            description = (
                "设计预览 · 模拟按键" if preview
                else f"{device.name} · {device.transport} · {device.vendor:04X}:{device.product:04X}"
            )
            selected = label(description, "value")
            selected.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(row(selected, None, self.refresh_button))
            if preview:
                self.refresh_button.hide()
        if self.photo is not None:
            layout.addWidget(self.photo.mic_step_card)
        self.feedback = label("", "banner")
        self.feedback.setObjectName("banner")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setAccessibleName("探测状态")
        self.feedback.setMinimumHeight(52)
        layout.addWidget(self.feedback)
        if self.photo is not None:
            self.photo.failed.connect(self.feedback.setText)

        results = QFrame()
        results.setObjectName("deviceCard")
        results.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.results_card = results
        form = QFormLayout(results)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.source = label(wrap=False)
        self.edges = label(wrap=False)
        self.key_code = label(role="value", wrap=False)
        self.key_code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.original = label(wrap=False)
        for title, field in (
            ("输入来源", self.source), ("按下与松开", self.edges),
            ("键码", self.key_code), ("原按键行为", self.original),
        ):
            field.setTextFormat(Qt.TextFormat.PlainText)
            form.addRow(label(title, "muted", wrap=False), field)
        layout.addWidget(results)

        self.observe = button("原键也生效了", self.mark_original, "link")
        self.permissions = button("打开输入监控设置", self.open_permissions, "link")
        self.start_button = button("再测一个键", self.toggle_capture, "primary")
        self.close_button = button("关闭", self.reject)
        if preview:
            self.simulate_button = button("模拟按一下", self.simulate_press, "primary")
            self.simulate_button.setAutoDefault(False)
            self.simulate_button.setEnabled(False)
            set_role(self.start_button, None)
            layout.addWidget(row(self.close_button, None, self.start_button, self.simulate_button))
        elif self.photo is None:
            layout.addWidget(row(self.observe, self.permissions, None))
        else:
            layout.addWidget(row(self.observe, self.permissions, None, self.start_button))
        for control in (self.refresh_button, self.observe, self.permissions, self.start_button, self.close_button):
            control.setAutoDefault(False)
        self.start_button.setMinimumWidth(self.start_button.sizeHint().width())
        self.photo_link = None
        if self.photo is None:
            text = "导入照片，开始学习" if install is not None else "进入照片学习"
            self.photo_link = button(text, self.open_photo_learning)
            self.photo_link.setAutoDefault(False)
            if install is not None:
                # Protocol-first: Add Remote identifies the key/voice protocol, then the user
                # imports a photo of the remote in hand and learns its keys.
                self.protocol_note = label("", "muted")
                self.protocol_note.setTextFormat(Qt.TextFormat.PlainText)
                layout.addWidget(self.protocol_note)
        elif self.photo is not None:
            layout.addWidget(self.photo.key_bar)
            layout.addWidget(self.photo.tips)
            if draft is not None:
                self.merge_card = QFrame()
                self.merge_card.setObjectName("deviceCard")
                self.merge_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
                merge_layout = QVBoxLayout(self.merge_card)
                merge_layout.setContentsMargins(12, 10, 12, 10)
                merge_layout.setSpacing(8)
                self.merge_prompt = label("", "caption")
                self.merge_prompt.setTextFormat(Qt.TextFormat.PlainText)
                self.merge_picker = ScrollSafeComboBox(self)
                self.merge_picker.setAccessibleName("选择要归并的已学按键")
                self.merge_confirm_button = button("归到同一颗键", self.confirm_merge_trigger, "primary")
                self.merge_cancel_button = button("暂不归并", self.dismiss_merge_trigger)
                for control in (self.merge_confirm_button, self.merge_cancel_button):
                    control.setAutoDefault(False)
                merge_layout.addWidget(self.merge_prompt)
                merge_layout.addWidget(self.merge_picker)
                merge_layout.addWidget(row(None, self.merge_cancel_button, self.merge_confirm_button))
                self.merge_card.hide()
                layout.addWidget(self.merge_card)
                self.photo.selection_changed.connect(self._sync_merge_picker)
            if install is not None:
                from .ui.remote_enrollment import RemoteEnrollment

                self.enrollment = RemoteEnrollment(self, self.photo, install, draft)
                layout.addWidget(self.enrollment)

        if install is not None:
            self.guide = label("", "muted")
            self.guide.setTextFormat(Qt.TextFormat.PlainText)

            # Clean empty-state card (replaces blank dropdown + duplicate orange/gray text when no device is found).
            self.empty_card = QFrame()
            self.empty_card.setObjectName("deviceCard")
            self.empty_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            empty_layout = QVBoxLayout(self.empty_card)
            empty_layout.setContentsMargins(16, 14, 16, 14)
            empty_layout.setSpacing(8)
            self.empty_title = label(NOT_SEEN_TITLE, "value")
            self.empty_body = label(NOT_SEEN_BODY, "caption")
            self.empty_body.setTextFormat(Qt.TextFormat.PlainText)
            self.empty_refresh_button = button("重新检测", self.refresh, "primary")
            self.empty_refresh_button.setAutoDefault(False)
            self.back_to_list_button = button("返回已检测列表", self._return_to_device_list, "link")
            self.back_to_list_button.setAutoDefault(False)
            self.back_to_list_button.hide()
            empty_layout.addWidget(self.empty_title)
            empty_layout.addWidget(self.empty_body)
            empty_layout.addWidget(row(self.empty_refresh_button, self.back_to_list_button, None))
            self.empty_card.hide()
            layout.addWidget(self.empty_card)

            # Contextual card when a system-connected BLE remote is detected in the top list
            # but rejected by the OS built-in Bluetooth HID driver (e.g. Windows Code 10).
            self.blocked_ble_card = QFrame()
            self.blocked_ble_card.setObjectName("deviceCard")
            self.blocked_ble_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            blocked_layout = QVBoxLayout(self.blocked_ble_card)
            blocked_layout.setContentsMargins(16, 14, 16, 14)
            blocked_layout.setSpacing(8)
            self.blocked_ble_title = label("", "value")
            self.blocked_ble_body = label("", "caption")
            self.blocked_ble_body.setTextFormat(Qt.TextFormat.PlainText)
            self.blocked_ble_bt_settings = button("打开系统蓝牙设置", self._open_system_bluetooth_settings)
            self.blocked_ble_help_link = button("使用说明", self.open_ble_dongle_help, "link")
            for ctrl in (self.blocked_ble_bt_settings, self.blocked_ble_help_link):
                ctrl.setAutoDefault(False)
            blocked_layout.addWidget(self.blocked_ble_title)
            blocked_layout.addWidget(self.blocked_ble_body)
            blocked_layout.addWidget(row(self.blocked_ble_bt_settings, self.blocked_ble_help_link, None))
            self.blocked_ble_card.hide()
            layout.addWidget(self.blocked_ble_card)

            # External USB Bluetooth dongle card — shown ONLY when an external dongle is plugged in
            # and no ready remote is currently selected. Never mixes fake Step 1/Step 2 causality.
            self.ble_dongle_card = QFrame()
            self.ble_dongle_card.setObjectName("deviceCard")
            self.ble_dongle_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            dongle_layout = QVBoxLayout(self.ble_dongle_card)
            dongle_layout.setContentsMargins(16, 14, 16, 14)
            dongle_layout.setSpacing(8)
            self.ble_dongle_help = button("使用说明", self.open_ble_dongle_help, "link")
            self.ble_dongle_help.setAutoDefault(False)
            dongle_layout.addWidget(row(label("外接 USB 蓝牙棒", "value", wrap=False), None, self.ble_dongle_help))
            self.ble_dongle_picker = ScrollSafeComboBox(self)
            self.ble_dongle_picker.setAccessibleName("选择外接 USB 蓝牙棒")
            self.ble_dongle_picker.currentIndexChanged.connect(self._sync_ble_dongle_buttons)
            self.ble_dongle_note = label("", "caption")
            self.ble_dongle_note.setTextFormat(Qt.TextFormat.PlainText)
            self.ble_dongle_bind = button("切换为 BLE 直连模式", self.bind_ble_dongle, "primary")
            self.ble_dongle_scan = button("扫描 BLE 遥控器", self.scan_ble_dongle, "primary")
            self.ble_dongle_restore = button("恢复普通蓝牙", self.restore_ble_dongle)
            for ctrl in (self.ble_dongle_bind, self.ble_dongle_scan, self.ble_dongle_restore):
                ctrl.setAutoDefault(False)
                ctrl.hide()
            dongle_layout.addWidget(self.ble_dongle_picker)
            dongle_layout.addWidget(self.ble_dongle_note)
            dongle_layout.addWidget(row(self.ble_dongle_bind, self.ble_dongle_scan, self.ble_dongle_restore, None))
            self.ble_dongle_card.hide()
            layout.addWidget(self.ble_dongle_card)

            # Windows: let the app read the selected USB receiver itself (WinUSB takeover).
            self.receiver_bind = button("让 App 接管这个接收器", self.bind_receiver)
            self.receiver_restore = button("恢复系统驱动", self.restore_receiver)
            self.receiver_note = label(TAKEOVER_NOTE, "caption")
            self.receiver_note.setTextFormat(Qt.TextFormat.PlainText)
            self.receiver_note.hide()
            layout.addWidget(self.receiver_note)
            for ctrl in (self.receiver_bind, self.receiver_restore):
                ctrl.setAutoDefault(False)
                ctrl.hide()
            self.driver_action_row = row(self.receiver_bind, self.receiver_restore, None)
            self.driver_action_row.hide()
            layout.addWidget(self.driver_action_row)
            layout.addWidget(self.guide)

            # Subtle entry row in normal mode + collapsed troubleshooting drawer in empty mode.
            self.not_listed = button("没检测到我的接收器 / 蓝牙设备？", self.open_not_listed, "link")
            self.driver_help = button("配套接收器驱动说明", self.open_driver_help, "link")
            self.diag_toggle = button("▸ 排查无法识别的设备与诊断工具", self._toggle_diagnostics, "link")
            self.diag_toggle.setCheckable(True)
            self.diag_toggle.setChecked(False)
            self.copy_check = button("复制接入诊断", self.copy_diagnostics, "link")
            self.copy_ai = button("复制给 AI 的求助", self.copy_ai_request, "link")
            self.copy_ai.setToolTip("复制一段说明：卡在哪一步、这个项目以往的做法和可以尝试的方向，以及脱敏诊断。粘贴给任意 AI 助手。")
            for control in (self.not_listed, self.driver_help, self.diag_toggle, self.copy_check, self.copy_ai):
                control.setAutoDefault(False)
            layout.addWidget(row(self.not_listed, self.driver_help, self.diag_toggle, None))

            # Collapsed progressive-disclosure drawer for unlisted devices & diagnostics (zero scrollbar by default!).
            self.diag_drawer = QFrame()
            self.diag_drawer.setObjectName("deviceCard")
            self.diag_drawer.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            diag_layout = QVBoxLayout(self.diag_drawer)
            diag_layout.setContentsMargins(14, 12, 14, 12)
            diag_layout.setSpacing(8)
            self.unlisted_label = label("系统里发现了暂时读不了的设备，选出你的遥控器：", "muted")
            self.unlisted_box = ScrollSafeComboBox(self)
            self.unlisted_box.setAccessibleName("系统里有、但 App 读不了的设备")
            self.unlisted_box.currentIndexChanged.connect(self.unlisted_chosen)
            self.boundary_note = label("", "caption")
            self.boundary_note.setTextFormat(Qt.TextFormat.PlainText)
            self.system_connection = QCheckBox("", self)
            self.system_connection.hide()
            diag_layout.addWidget(self.unlisted_label)
            diag_layout.addWidget(self.unlisted_box)
            diag_layout.addWidget(self.boundary_note)
            diag_layout.addWidget(row(self.copy_check, self.copy_ai, None))
            self.diag_drawer.hide()
            layout.addWidget(self.diag_drawer)

        if self.photo is not None and not preview:
            # Fixed footer at the bottom right: 关闭 beside 保存遥控器.
            layout.addStretch(1)
            footer_host = getattr(self, "_panel_outer", layout)
            footer_host.addWidget(rule())
            save = (self.enrollment.save_button,) if self.enrollment is not None else ()
            self.footer = row(None, self.close_button, *save)
            footer_host.addWidget(self.footer)

        if outer is not None:
            layout.addStretch(1)
            outer.addWidget(rule())
            self.footer = row(self.close_button, None, self.start_button, self.photo_link)
            outer.addWidget(self.footer)

        self.capture_timer = QTimer(self)
        self.capture_timer.setSingleShot(True)
        self.capture_timer.setInterval(12000)
        self.capture_timer.timeout.connect(self.timeout)
        self.settle_timer = QTimer(self)
        self.settle_timer.setSingleShot(True)
        self.settle_timer.setInterval(150)
        self.settle_timer.timeout.connect(self.finish_sample)
        self.key_received.connect(self.on_key, Qt.ConnectionType.QueuedConnection)
        self.device_removed.connect(self.on_removed, Qt.ConnectionType.QueuedConnection)
        self.capture_started.connect(self._on_capture_started, Qt.ConnectionType.QueuedConnection)
        self.ble_scan_finished.connect(self._on_ble_scan_finished, Qt.ConnectionType.QueuedConnection)
        self.picker.currentIndexChanged.connect(self.reset_sample)
        self.finished.connect(self.release_reader)
        self.refresh()

    def selected_device(self):
        index = self.picker.currentIndex()
        return self.devices[index] if 0 <= index < len(self.devices) else None

    def selected_blocked_ble(self):
        index = self.picker.currentIndex()
        offset = index - len(self.devices)
        return self._blocked_ble[offset] if 0 <= offset < len(self._blocked_ble) else None

    def _open_system_bluetooth_settings(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if sys.platform == "win32":
            QDesktopServices.openUrl(QUrl("ms-settings:bluetooth"))
        elif sys.platform == "darwin":
            QDesktopServices.openUrl(QUrl("x-apple.systempreferences:com.apple.BluetoothSettings"))

    def set_input_step(self, step):
        self.input_step = step
        self.refresh_guide()

    def unlisted_entries(self):
        """Devices the system shows but the readers skipped, plus two catch-alls."""
        from .probe_device import UnlistedDevice

        entries = list(getattr(self.reader, "unlisted", []) or [])
        seen = {(d.vendor, d.product) for d in entries} | {(d.vendor, d.product) for d in self.devices}
        try:
            inventory = list(self.usb_devices()) if self.usb_devices else []
        except Exception:
            inventory = []
        for usb in inventory:
            pair = (usb.vendor, usb.product)
            if pair not in seen:
                seen.add(pair)
                entries.append(UnlistedDevice(getattr(usb, "name", "") or "USB 设备", "USB", *pair, hid=False))
        return entries

    def populate_unlisted(self):
        if self.guide is None:
            return
        self.unlisted_box.blockSignals(True)
        self.unlisted_box.clear()
        self.unlisted_box.addItem("选择…", None)
        for device in self.unlisted_entries():
            case = classify(device)
            kind = f"（{BOUNDARIES[case]['title']}）" if case else ""
            transport = device.transport or "HID"
            self.unlisted_box.addItem(f"{device.name} · {transport} · {device.vendor:04X}:{device.product:04X}{kind}",
                                      ("device", device))
        self.unlisted_box.addItem("列表里都没有：蓝牙显示已连接", ("case", "gatt_only"))
        self.unlisted_box.addItem("列表里都没有：插的是 USB 接收器", ("case", "private_usb"))
        self.unlisted_box.blockSignals(False)
        self.boundary_note.clear()

    def chosen_boundary(self):
        data = self.unlisted_box.currentData() if self.guide is not None else None
        if not data:
            return "", None
        if data[0] == "case":
            return data[1], None
        return classify(data[1]), data[1]

    def unlisted_chosen(self, *_):
        self.copy_ai.setText("复制给 AI 的求助")
        # The takeover row is worked out by refresh_guide; the access level depends on it.
        self.refresh_guide()
        case, device = self.chosen_boundary()
        if case or device is not None:
            takeover = any(not d.bound_winusb for d in self.receiver_interfaces())
            self.boundary_note.setText(unlisted_access_text(case, takeover=takeover))
        else:
            self.boundary_note.clear()

    def open_not_listed(self):
        if self.diag_toggle is not None:
            self.diag_toggle.setChecked(True)
        self.picker.setCurrentIndex(-1)

    def _return_to_device_list(self):
        if self.devices or self._blocked_ble:
            if self.diag_toggle is not None:
                self.diag_toggle.setChecked(False)
            self.picker.setCurrentIndex(0)

    def _toggle_diagnostics(self, *_):
        self.refresh_guide()

    def refresh_guide(self, *_):
        if self.results_card is not None and self.photo is None:
            self.results_card.setVisible(self.input_step != "not_visible")
        if self.guide is None:
            return
        device = self.selected_device()
        blocked = self.selected_blocked_ble() if self.photo is None else None
        unlisted = self.input_step == "not_visible" and self.photo is None
        has_any_item = bool(self.devices or self._blocked_ble)

        if self.picker_row is not None and self.photo is None:
            self.picker_row.setVisible(has_any_item)
        if self.header_hint is not None and self.photo is None:
            self.header_hint.setVisible(has_any_item)
        if self.photo is None:
            self.start_button.setVisible(not unlisted)
            if self.photo_link is not None:
                self.photo_link.setVisible(not unlisted)

        self.not_listed.setVisible(
            self.photo is None and (device is not None or blocked is not None)
            and self.input_step not in ("waiting", "down_only")
        )
        message = KEY_GUIDANCE[self.input_step]
        if self.input_step == "not_visible" and self.system_connection.isChecked():
            message = "系统连接已由你确认。App 尚未发现可学习的按键接口，可以复制接入诊断检查设备、驱动或协议。"
        elif self.input_step == "confirmed":
            if self.photo is not None:
                message = "已确认按下和松开。可以学习下一个位置，或保存遥控器。"
            else:
                message = "已确认按下和松开。可以导入照片学习按键。"
        self.guide.setText(message)
        # Hide duplicate guide paragraph when empty_card / blocked_ble_card already carries the prompt.
        self.guide.setVisible(not unlisted or self.system_connection.isChecked())

        self.driver_help.setVisible(
            device is not None and device.transport == "USB" and (device.vendor, device.product) == (0x0416, 0x0602)
            and sys.platform in ("darwin", "win32")
        )
        managed_dongles = []
        managed_receivers = []
        has_reader_managed = False
        if self.photo is None:
            if self.reader is not None:
                managed_fn = getattr(self.reader, "managed_dongles", None)
                if callable(managed_fn):
                    has_reader_managed = True
                    try:
                        managed_dongles = list(managed_fn() or ())
                    except Exception:
                        managed_dongles = []
            if sys.platform == "win32":
                try:
                    from . import win_usb_driver
                    win_dongles, win_receivers = win_usb_driver.list_managed_devices()
                    if not has_reader_managed:
                        managed_dongles = win_dongles
                    managed_receivers = win_receivers
                except Exception:
                    pass
        self._managed_dongles = managed_dongles
        self._managed_receivers = managed_receivers

        # Takeover row for whichever USB device the user picked, readable or not: the user
        # decides which device is the remote; the app only decides which of its interfaces to read.
        pair = self.takeover_pair()
        interfaces = self.receiver_interfaces()
        unbound = [d for d in interfaces if not d.bound_winusb]
        restorable = [d for d in interfaces if d.restorable]
        offer = self.photo is None and bool(unbound)
        show_restore = self.photo is None and not unbound and bool(restorable)
        # Only worth saying for a picked unreadable device; a readable one always has key data.
        nothing = (self.photo is None and pair is not None and sys.platform == "win32" and not interfaces
                   and self.selected_device() is None)
        busy = self._driver_pending is not None
        if self.receiver_bind is not None:
            self.receiver_bind.setVisible(offer)
            self.receiver_bind.setEnabled(not busy)
        if self.receiver_restore is not None:
            self.receiver_restore.setVisible(show_restore)
            self.receiver_restore.setEnabled(not busy)
        if self.receiver_note is not None:
            self.receiver_note.setText(TAKEN_OVER_NOTE if show_restore else NOTHING_TO_TAKE_OVER if nothing
                                       else TAKEOVER_NOTE)
            self.receiver_note.setVisible(offer or show_restore or nothing)
        if self.driver_action_row is not None:
            self.driver_action_row.setVisible(offer or show_restore)

        # External USB Bluetooth dongle card is shown ONLY when an external dongle is plugged in
        # and either a system-blocked BLE remote is selected OR no ready remote is selected.
        show_dongle_card = unlisted and bool(managed_dongles)

        if self.blocked_ble_card is not None:
            show_blocked = unlisted and blocked is not None
            self.blocked_ble_card.setVisible(show_blocked)
            if show_blocked and not dongle_route_available():
                self.blocked_ble_title.setText(f"{access_heading(ACCESS_UNSUPPORTED)}：「{blocked.name}」")
                self.blocked_ble_body.setText(
                    f"Windows 自带蓝牙读不了这只电视盒遥控器的按键（错误码 {blocked.problem_code}）。\n{DONGLE_MISSING}"
                )
            elif show_blocked:
                self.blocked_ble_title.setText(f"{access_heading(ACCESS_ONE_CLICK)}：「{blocked.name}」换外接蓝牙棒直连")
                if managed_dongles:
                    if any(d.bound_winusb or d.service.casefold() == "libusb" for d in managed_dongles):
                        self.blocked_ble_body.setText(
                            f"Windows 自带蓝牙读不了这只电视盒遥控器的按键（错误码 {blocked.problem_code}）。\n"
                            "请先点下方「打开系统蓝牙设置」移除这只遥控器（避免被自带蓝牙抢连），再把遥控器按进配对模式（灯快闪）后在下方卡片点「扫描 BLE 遥控器」。"
                        )
                    else:
                        self.blocked_ble_body.setText(
                            f"Windows 自带蓝牙读不了这只电视盒遥控器的按键（错误码 {blocked.problem_code}）。\n"
                            "已检测到你插着外接 USB 蓝牙棒，直接在下方一键切换为 BLE 直连模式即可绕开系统限制（不影响电脑自带蓝牙）。"
                        )
                else:
                    self.blocked_ble_body.setText(
                        f"Windows 自带蓝牙读不了这只电视盒遥控器的按键（错误码 {blocked.problem_code}）。\n"
                        "插一只普通外接 USB 蓝牙棒（十几元标准款即可），插好后点上方「重新检测」，即可在这里一键直连（不影响电脑自带蓝牙）。"
                    )
            if show_blocked:
                self.blocked_ble_bt_settings.setVisible(sys.platform in ("win32", "darwin"))
                self.blocked_ble_help_link.setVisible(not show_dongle_card and dongle_route_available())

        if self.empty_card is not None:
            show_empty = unlisted and blocked is None
            self.empty_card.setVisible(show_empty)
            if has_any_item:
                self.empty_title.setText(f"{access_heading(ACCESS_NOT_SEEN)}：列表里没有你的遥控器")
                self.empty_body.setText("确认 USB 接收器已插好、蓝牙已连接后点「重新检测」；"
                                        "仍然没有，就在下方展开排查工具，选出系统里有但读不了的设备。")
            else:
                self.empty_title.setText(NOT_SEEN_TITLE)
                self.empty_body.setText(NOT_SEEN_BODY)
            self.back_to_list_button.setVisible(has_any_item)
            # Single primary CTA rule: when dongle card has the primary CTA, keep empty refresh neutral.
            set_role(self.empty_refresh_button, None if show_dongle_card else "primary")
            self.empty_refresh_button.setEnabled(not busy)

        # Hide the redundant default "未发现可读取…" banner when empty_card or blocked_ble_card is shown.
        if self.photo is None:
            default_empty_msg = self.feedback.text().startswith("未发现可读取键盘或媒体按键")
            self.feedback.setVisible(not (unlisted and default_empty_msg))

        if self.ble_dongle_card is not None:
            self._populate_ble_dongle_picker()
            self.ble_dongle_card.setVisible(show_dongle_card)
        if self.ble_dongle_help is not None:
            self.ble_dongle_help.setVisible(show_dongle_card)

        if self.diag_toggle is not None and self.diag_drawer is not None:
            self.diag_toggle.setVisible(unlisted)
            expanded = unlisted and self.diag_toggle.isChecked()
            self.diag_toggle.setText("▾ 收起排查与诊断工具" if expanded else "▸ 排查无法识别的设备与诊断工具")
            self.diag_drawer.setVisible(expanded)
            for widget in (self.unlisted_label, self.unlisted_box, self.boundary_note):
                widget.setVisible(expanded)

    def _populate_ble_dongle_picker(self):
        if self.ble_dongle_picker is None:
            return
        previous = self.ble_dongle_picker.currentData()
        self.ble_dongle_picker.blockSignals(True)
        self.ble_dongle_picker.clear()
        for dev in self._managed_dongles:
            svc = dev.service.casefold()
            if svc == "libusb":
                mode = "BLE 直连就绪"
            elif dev.bound_winusb:
                mode = "BLE 直连模式"
            else:
                mode = "普通蓝牙"
            self.ble_dongle_picker.addItem(f"外接 USB 蓝牙棒（{dev.id_label} · {mode}）", dev.instance_id)
        if previous:
            idx = self.ble_dongle_picker.findData(previous)
            if idx >= 0:
                self.ble_dongle_picker.setCurrentIndex(idx)
        self.ble_dongle_picker.blockSignals(False)
        self.ble_dongle_picker.setVisible(bool(self._managed_dongles))
        self._sync_ble_dongle_buttons()

    def _selected_ble_dongle(self):
        if self.ble_dongle_picker is None or not self._managed_dongles:
            return None
        inst = self.ble_dongle_picker.currentData()
        for dev in self._managed_dongles:
            if dev.instance_id == inst:
                return dev
        return self._managed_dongles[0]

    def _sync_ble_dongle_buttons(self, *_):
        if self.ble_dongle_note is None:
            return
        busy = self._driver_pending is not None
        selected = self._selected_ble_dongle()
        if selected is None:
            for ctrl in (self.ble_dongle_bind, self.ble_dongle_scan, self.ble_dongle_restore):
                if ctrl is not None:
                    ctrl.hide()
            return
        if not dongle_route_available():
            # Never switch a dongle's driver for a route this build cannot use; undoing stays possible.
            self.ble_dongle_note.setText(DONGLE_MISSING)
            self.ble_dongle_bind.hide()
            self.ble_dongle_scan.hide()
            self.ble_dongle_restore.setVisible(selected.bound_winusb or selected.restorable)
            self.ble_dongle_restore.setEnabled(not busy)
        elif selected.service.casefold() == "libusb":
            # macOS / Linux direct libusb USB BT dongle — no driver switch needed.
            self.ble_dongle_note.setText(
                f"已就绪（{selected.id_label}）。把电视盒遥控器按进配对模式（灯快闪）后点下方「扫描 BLE 遥控器」即可。"
            )
            self.ble_dongle_bind.hide()
            self.ble_dongle_restore.hide()
            self.ble_dongle_scan.show()
            self.ble_dongle_scan.setEnabled(not busy)
        elif selected.bound_winusb:
            self.ble_dongle_note.setText(
                f"当前为 BLE 直连模式（不影响电脑自带蓝牙）。把电视盒遥控器按进配对模式（灯快闪）后点「扫描 BLE 遥控器」。"
            )
            self.ble_dongle_bind.hide()
            self.ble_dongle_scan.show()
            self.ble_dongle_scan.setEnabled(not busy)
            self.ble_dongle_restore.show()
            self.ble_dongle_restore.setEnabled(not busy)
        else:
            self.ble_dongle_note.setText(
                f"当前为普通蓝牙模式（不影响电脑自带蓝牙）。确认使用这只蓝牙棒时，点下方一键切换，随时可恢复。"
            )
            self.ble_dongle_bind.show()
            self.ble_dongle_bind.setEnabled(not busy)
            self.ble_dongle_scan.hide()
            self.ble_dongle_restore.setVisible(selected.restorable)
            self.ble_dongle_restore.setEnabled(not busy)

    def bind_ble_dongle(self):
        selected = self._selected_ble_dongle()
        if selected is None:
            self.feedback.setText("请先插上外接 USB 蓝牙棒并从列表中选中它。")
            return
        self._start_win_driver_action("bind", [selected.instance_id])

    def _cancel_ble_scan(self):
        if self.reader is None:
            return
        cancel_fn = getattr(self.reader, "cancel_ble_scan", None) or getattr(self.reader, "cancel_scan", None)
        if callable(cancel_fn):
            with suppress(Exception):
                cancel_fn()

    def scan_ble_dongle(self):
        if self.reader is None:
            return
        if self._ble_scanning:
            self._ble_scanning = False
            self.generation += 1
            self._cancel_ble_scan()
            self.refresh_button.setEnabled(True)
            if self.ble_dongle_scan is not None:
                self.ble_dongle_scan.setText("扫描 BLE 遥控器")
                self.ble_dongle_scan.setEnabled(True)
            if self.ble_dongle_restore is not None:
                self.ble_dongle_restore.setEnabled(True)
            self.feedback.setText("已停止扫描。")
            self.feedback.show()
            return
        scan_fn = getattr(self.reader, "scan_ble_dongles", None) or getattr(self.reader, "scan_dongles", None)
        if not callable(scan_fn):
            self.refresh()
            return
        blocked = self.selected_blocked_ble()
        self._ble_scan_target = blocked
        self._ble_scanning = True
        self.generation += 1
        generation = self.generation
        self.refresh_button.setEnabled(False)
        if self.ble_dongle_scan is not None:
            self.ble_dongle_scan.setEnabled(True)
            self.ble_dongle_scan.setText("停止扫描")
        if self.ble_dongle_restore is not None:
            self.ble_dongle_restore.setEnabled(False)
        if blocked is not None:
            self.feedback.setText(f"正在找「{blocked.name}」（最长 20 秒，找到自动连接）…请长按它的配对键让灯快闪，其他蓝牙设备不会被连接。")
        else:
            self.feedback.setText("正在扫描 BLE 遥控器（最长 20 秒，扫到自动连接）…请长按遥控器配对键让灯快闪。")
        self.feedback.show()

        def _worker():
            try:
                scan_fn(target=blocked) if blocked is not None else scan_fn()
            except Exception as exc:
                self.ble_scan_finished.emit(generation, str(exc))
                return
            self.ble_scan_finished.emit(generation, "")

        threading.Thread(target=_worker, name="remotehub-ble-scan", daemon=True).start()

    def _on_ble_scan_finished(self, generation: int, error_text: str):
        self._ble_scanning = False
        self._ble_paused = False
        if self.ble_dongle_scan is not None:
            self.ble_dongle_scan.setText("扫描 BLE 遥控器")
            self.ble_dongle_scan.setEnabled(True)
        if self.ble_dongle_restore is not None:
            self.ble_dongle_restore.setEnabled(True)
        if generation != self.generation:
            return
        self.refresh()
        if error_text:
            self.feedback.setText(f"扫描失败：{error_text}")
            self.feedback.show()
            return
        # Only a remote heard in this scan is in pairing mode; remembered peers (or a camera that
        # advertised like a remote last time) must not be picked just because they sort first.
        heard = self._scan_result("last_ble_scan_ids", "last_scan_ids")
        sure = self._scan_result("last_ble_scan_sure", "last_scan_sure")
        index = {dev.id: i for i, dev in enumerate(self.devices)}
        sure_idx = next((index[i] for i in sure if i in index), -1)
        maybe_idx = next((index[i] for i in heard if i in index), -1)
        if sure_idx >= 0:
            self.picker.setCurrentIndex(sure_idx)
            # Connect & pair immediately while the remote's pairing light is still blinking.
            self.start_capture()
        elif self._ble_scan_target is not None:
            self.feedback.setText(
                f"没找到「{self._ble_scan_target.name}」。请长按它的配对键让灯快闪，再点「扫描 BLE 遥控器」；"
                "扫描时其他蓝牙设备不会被连接。"
            )
            self.feedback.show()
        elif maybe_idx >= 0:
            # Heard something remote-like but not clearly a remote (e.g. a camera): let the user decide.
            self.picker.setCurrentIndex(maybe_idx)
            self.feedback.setText(
                f"扫到「{self.devices[maybe_idx].name}」，但它不像遥控器，没有自动连接。"
                "如果它就是你的遥控器，点「开始检测」；不是的话，把遥控器按进配对模式（灯快闪）后重新扫描。"
            )
            self.feedback.show()
        else:
            self.feedback.setText(
                "本次扫描未发现处于配对模式的 BLE 遥控器。请先在 Windows 蓝牙设置里删除旧配对，长按遥控器配对键让灯快闪，再点「扫描 BLE 遥控器」。"
            )
            self.feedback.show()

    def _scan_result(self, registry_name, reader_name):
        fn = getattr(self.reader, registry_name, None)
        return list(fn() or ()) if callable(fn) else list(getattr(self.reader, reader_name, None) or ())

    def restore_ble_dongle(self):
        selected = self._selected_ble_dongle()
        if selected is None or not selected.restorable:
            self.feedback.setText("当前选中的外接蓝牙棒已是普通蓝牙模式。")
            return
        self._start_win_driver_action("restore", [selected.instance_id])

    def takeover_pair(self):
        """VID:PID of the USB device the user picked: in the device list or among unreadable ones."""
        device = self.selected_device()
        if device is not None:
            if device.transport != "USB" or (isinstance(device.id, tuple) and device.id and device.id[0] == "ble-dongle"):
                return None
            return device.vendor, device.product
        if self.guide is None:
            return None
        _case, unlisted = self.chosen_boundary()
        if unlisted is not None and unlisted.transport == "USB" and unlisted.vendor:
            return unlisted.vendor, unlisted.product
        return None

    def receiver_interfaces(self):
        """Interfaces of the picked USB device that a takeover binds or restores."""
        pair = self.takeover_pair()
        if pair is None or not self._managed_receivers:
            return []
        from .win_usb_driver import receiver_interfaces

        return receiver_interfaces(self._managed_receivers, *pair)

    def bind_receiver(self):
        pair = self.takeover_pair()
        targets = [d.instance_id for d in self.receiver_interfaces() if not d.bound_winusb]
        if pair is None or not targets:
            self.feedback.setText("这个接收器已由 App 接管。")
            return
        # A receiver the app has not proven must be confirmed after the switch, or the elevated
        # helper restores it by itself (the picked device might be the user's keyboard and mouse).
        confirm = pair != (0x0416, 0x0602)
        self._start_win_driver_action("bind", targets, confirm=confirm)

    def restore_receiver(self):
        targets = [d.instance_id for d in self.receiver_interfaces() if d.restorable]
        if not targets:
            self.feedback.setText("这个接收器已是系统驱动。")
            return
        self._start_win_driver_action("restore", targets)

    def _start_win_driver_action(self, action: str, instance_ids: list[str], *, confirm: bool = False):
        if self._driver_pending is not None:
            return
        from . import win_usb_driver

        self.stop_capture()
        if self.reader is not None:
            with suppress(Exception):
                self.reader.stop()
        try:
            handle, result_path = win_usb_driver.launch_elevated_action(action, instance_ids, confirm=confirm)
        except win_usb_driver.DriverActionCancelled as exc:
            self.feedback.setText(str(exc))
            self.feedback.show()
            return
        except Exception as exc:
            self.feedback.setText(str(exc))
            self.feedback.show()
            return
        self._driver_pending = (handle, result_path, action)
        self.refresh_button.setEnabled(False)
        bluetooth = any(d.instance_id in instance_ids for d in self._managed_dongles)
        self.feedback.setText(
            ("正在切换为 BLE 直连模式" if bluetooth else "正在让 App 接管这个接收器") + "，请稍候（若弹出系统确认框请点「是」）…"
            if action == "bind"
            else "正在恢复普通系统驱动，请稍候（若弹出系统确认框请点「是」）…"
        )
        self.feedback.show()
        self.refresh_guide()
        self._driver_poll_timer.start()

    def _poll_win_driver_action(self):
        if self._driver_pending is None:
            self._driver_poll_timer.stop()
            return
        from . import win_usb_driver

        handle, result_path, _action = self._driver_pending
        done, exit_code = win_usb_driver.poll_elevated_process(handle)
        if not done:
            if self._confirm_box is None and win_usb_driver.read_action_stage(result_path).get("stage") == "awaiting_confirm":
                self._ask_keep_takeover(result_path)
            return
        self._driver_poll_timer.stop()
        self._driver_pending = None
        self._close_confirm_box()
        result = win_usb_driver.read_action_result(result_path, exit_code)
        self.refresh()
        if result.get("ok"):
            self.feedback.setText(result.get("message") or "操作已完成。")
        else:
            self.feedback.setText(result.get("error") or "切换驱动失败，请重试。")
        self.feedback.show()

    def _ask_keep_takeover(self, result_path):
        """Keep the takeover only on an explicit click; silence lets the helper restore it."""
        from . import win_usb_driver

        box = QMessageBox(self)
        box.setWindowTitle("确认接管")
        box.setIcon(QMessageBox.Icon.Question)
        keep = box.addButton("保留接管", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("恢复系统驱动", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        left = [TAKEOVER_CONFIRM_SECONDS]

        def tick():
            box.setText(
                "App 已接管这个接收器。键盘和鼠标都还正常吗？\n"
                f"点「保留接管」完成；{left[0]} 秒内不点会自动恢复系统驱动。"
            )
            left[0] -= 1
            if left[0] < 0:
                box.done(0)

        timer = QTimer(box)
        timer.timeout.connect(tick)
        tick()
        timer.start(1000)

        def answered(_result):
            timer.stop()
            clicked = box.clickedButton()
            if clicked is not None:
                win_usb_driver.answer_confirmation(result_path, keep=clicked is keep)
            self._confirm_box = None

        box.finished.connect(answered)
        self._confirm_box = box
        box.open()

    def _close_confirm_box(self):
        if self._confirm_box is not None:
            self._confirm_box.done(0)
            self._confirm_box = None

    def open_ble_dongle_help(self):
        platform = "Windows" if sys.platform == "win32" else "macOS"
        text = ble_dongle_help_text(platform)
        if self.ble_dongle_help_dialog is not None:
            self.ble_dongle_help_dialog.close()
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{APP_NAME} · BLE 专属蓝牙棒使用说明（{platform}）")
        dialog.setMinimumSize(540, 440)
        dialog.resize(620, 500)
        apply_theme(dialog)
        box = QVBoxLayout(dialog)
        box.setContentsMargins(20, 18, 20, 16)
        box.setSpacing(12)
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(text)
        box.addWidget(viewer, 1)
        copy_btn = button("复制说明", lambda: (QApplication.clipboard().setText(text), copy_btn.setText("已复制")))
        close_btn = button("关闭", dialog.accept, "primary")
        for ctrl in (copy_btn, close_btn):
            ctrl.setAutoDefault(False)
        box.addWidget(row(copy_btn, None, close_btn))
        self.ble_dongle_help_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda *_: setattr(self, "ble_dongle_help_dialog", None))
        dialog.open()

    def open_driver_help(self):
        platform = "Windows" if sys.platform == "win32" else "macOS"
        text = driver_help_text(platform)
        if self.driver_help_dialog is not None:
            self.driver_help_dialog.close()
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{APP_NAME} · 配套接收器设置说明（{platform}）")
        dialog.setMinimumSize(520, 420)
        dialog.resize(580, 460)
        apply_theme(dialog)
        box = QVBoxLayout(dialog)
        box.setContentsMargins(20, 18, 20, 16)
        box.setSpacing(12)
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(text)
        box.addWidget(viewer, 1)
        copy_btn = button("复制说明", lambda: (QApplication.clipboard().setText(text), copy_btn.setText("已复制")))
        close_btn = button("关闭", dialog.accept, "primary")
        copy_btn.setAutoDefault(False)
        close_btn.setAutoDefault(False)
        box.addWidget(row(copy_btn, None, close_btn))
        self.driver_help_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda *_: setattr(self, "driver_help_dialog", None))
        dialog.open()

    def copy_diagnostics(self):
        QApplication.clipboard().setText(self._diagnostics()[0])
        self.copy_check.setText("接入诊断已复制")

    def _diagnostics(self):
        opened, total = self.interface_counts
        if self.input_step == "not_visible":
            details = {"system_connection_confirmed_by_user": self.system_connection.isChecked()}
        elif self.input_step in ("no_signal", "down_only", "missing_release", "multiple_keys"):
            details = {"observed_signals": len(self.seen), "held_signals": len(self.held)}
        else:
            details = None
        case, unlisted = self.chosen_boundary() if self.input_step == "not_visible" else ("", None)
        if case:
            details = {**(details or {}), "boundary": case}
        trigger = self.key_code.text()
        if self.input_step in ("down_only", "missing_release") and len(self.seen) == 1:
            _, trigger = next(iter(self.seen))
        return diagnostic_text(step=self.input_step, device=self.selected_device(), trigger=trigger,
                               opened=opened, total=total, details=details), case, unlisted

    def copy_ai_request(self):
        text, case, unlisted = self._diagnostics()
        pending = self.photo.pending if self.photo is not None else None
        if pending is not None and pending.name and pending.name != "等待按键":
            text += f"\n卡住的按键：照片上的「{pending.name}」，按下后电脑没有收到任何按键信号。"
        QApplication.clipboard().setText(ai_request(
            step=self.input_step, case=case, device=unlisted or self.selected_device(), diagnostics=text,
        ))
        self.copy_ai.setText("已复制，可粘贴给 AI")
        if self.photo is not None:
            self.photo.help_button.setText("已复制，可粘贴给 AI")

    def show_protocol(self):
        """Say in plain words how the selected connection can be used (access level, not protocols)."""
        from .protocols import identify_protocol

        device = self.selected_device()
        note = ""
        if device is not None:
            proto = identify_protocol(device, self.reader)
            note = "\n".join(
                proto.access_lines()
                + ["按一个键确认能收到后，点「导入照片，开始学习」，按你手里遥控器的照片学习按键。"]
            )
        users = self.connection_users(device) if device is not None and self.connection_users else []
        if users:
            note += f"\n这个接收端也被「{'」「'.join(users)}」使用；同一时间按当前启用的遥控器映射。"
        if windows_0416_needs_winusb_keys(device, self.reader):
            note = (
                f"{access_heading(ACCESS_ONE_CLICK)}：Windows 自带驱动读不到这只接收器的「#」号键。"
                "点下方「让 App 接管这个接收器」，再重插接收器，全部按键就都能学。"
                + note[note.index("\n"):]
            )
        self.protocol_note.setText(note)
        self.protocol_note.setVisible(device is not None)
        self._update_primary_buttons()

    def mic_access_text(self):
        """Microphone level of the remote being learned, said after the user confirms it has one."""
        from .protocols import device_mic_level, identify_protocol, mic_text

        device = self.photo.device if self.photo is not None else None
        if device is None:
            return ""
        return mic_text(device_mic_level(device, identify_protocol(device, self.reader)), sys.platform)

    def _update_primary_buttons(self):
        if self.photo is not None:
            return
        confirmed = self.input_step == "confirmed" and self.photo_link is not None and self.photo_link.isEnabled()
        set_role(self.start_button, None if confirmed else "primary")
        if self.photo_link is not None:
            set_role(self.photo_link, "primary" if confirmed else None)

    def refresh(self):
        previous = self.selected_device() or self._initial_device
        self._initial_device = None
        self.stop_capture()
        self.picker.blockSignals(True)
        self.picker.clear()
        self.devices = []
        error = ""
        if self.reader is not None:
            try:
                self.devices = self.reader.enumerate()
                if self.photo is not None and self.draft is None:
                    current = self.photo.device
                    is_ble_dongle = (
                        isinstance(getattr(current, "id", None), tuple)
                        and bool(current.id)
                        and current.id[0] == "ble-dongle"
                    )
                    self.devices = [
                        device for device in self.devices
                        if device.id == current.id and (self.install is None or is_ble_dongle or device == current)
                    ]
                    if is_ble_dongle and self.devices:
                        self.photo.device = self.devices[0]
            except (RuntimeError, OSError) as exc:
                error = str(exc)
        self._blocked_ble = []
        if self.photo is None and self.reader is not None:
            blocked_fn = getattr(self.reader, "blocked_ble_remotes", None)
            if callable(blocked_fn):
                try:
                    self._blocked_ble = list(blocked_fn() or ())
                except Exception:
                    self._blocked_ble = []

        for index, device in enumerate(self.devices, start=1):
            self.picker.addItem(f"{index}. {device.name} · {device.transport} · {device.vendor:04X}:{device.product:04X}")
        for offset, blk in enumerate(self._blocked_ble, start=len(self.devices) + 1):
            self.picker.addItem(f"{offset}. {blk.name}（自带蓝牙无法读取，需外接蓝牙棒）")
        if previous is not None:
            chosen = next((index for index, device in enumerate(self.devices) if device.id == previous.id), -1)
            if chosen < 0 and self.draft is not None and self.devices:
                chosen = next((index for index, device in enumerate(self.devices) if self.draft.accepts(device)), 0)
            self.picker.setCurrentIndex(chosen)
        elif self.draft is not None and self.devices:
            self.picker.setCurrentIndex(
                next((index for index, device in enumerate(self.devices) if self.draft.accepts(device)), 0)
            )
        elif self.devices or self._blocked_ble:
            self.picker.setCurrentIndex(0)
        self.picker.blockSignals(False)
        if self.guide is not None:
            self.populate_unlisted()
        self.reset_sample()
        if error:
            self.feedback.setText(error)
            self.set_input_step("enumeration_failed")
        elif self.reader is None:
            self.feedback.setText("离屏检查模式。")
        self.refresh_button.setEnabled(self.reader is not None)
        return not error

    def _clear_merge_prompt(self):
        self.pending_merge_trigger = ""
        if self.merge_card is not None:
            self.merge_card.hide()

    def _populate_merge_picker(self):
        if self.merge_picker is None or self.photo is None:
            return
        self.merge_picker.blockSignals(True)
        self.merge_picker.clear()
        for btn in self.photo.profile.buttons:
            if not btn.configurable:
                continue
            title = f"{btn.name}（{btn.trigger}）" if btn.trigger else btn.name
            self.merge_picker.addItem(title, btn.id)
        if self.photo.selected:
            idx = self.merge_picker.findData(self.photo.selected)
            if idx >= 0:
                self.merge_picker.setCurrentIndex(idx)
        self.merge_picker.blockSignals(False)
        if self.merge_confirm_button is not None:
            self.merge_confirm_button.setEnabled(self.merge_picker.count() > 0)

    def _sync_merge_picker(self):
        if self.merge_card is not None and self.merge_card.isVisible():
            self._populate_merge_picker()

    def _offer_merge_trigger(self, trigger: str):
        if self.merge_card is None or self.photo is None:
            return
        self.pending_merge_trigger = trigger
        prompt = f"收到键码 {trigger}。这颗键在新接收端上发的码不同，是否归到同一颗键？"
        self.merge_prompt.setText(prompt)
        self.feedback.setText(prompt)
        self._populate_merge_picker()
        self.merge_card.show()

    def confirm_merge_trigger(self):
        trigger = self.pending_merge_trigger
        button_id = self.merge_picker.currentData() if self.merge_picker is not None else None
        if not trigger or not button_id or self.photo is None:
            return
        if self.photo.add_extra_trigger(button_id, trigger):
            target = self.photo.selected_button()
            name = target.name if target is not None else ""
            self._clear_merge_prompt()
            self.feedback.setText(f"已将键码 {trigger} 归并到「{name}」。点「保存遥控器」后即可同时生效。")

    def dismiss_merge_trigger(self):
        self._clear_merge_prompt()
        self.feedback.setText("已暂不归并。可以点击照片位置作为新按键学习，或继续试按其他键。")

    def reset_sample(self, *_):
        self._clear_merge_prompt()
        self.interface_counts = (None, None)
        if self.guide is not None:
            self.copy_check.setText("复制接入诊断")
            self.copy_ai.setText("复制给 AI 的求助")
            if self.selected_device() is not None:
                self.system_connection.setChecked(False)
        self.set_input_step("select_device" if self.selected_device() is not None else "not_visible")
        self.scope_detail = ""
        self.source.setText("等待检测")
        self.edges.setText("等待检测")
        self.key_code.setText("—")
        self.original.setText("待验证")
        self.observe.hide()
        self.permissions.hide()
        if self.photo is not None:
            self.photo.cancel()
            selected = self.selected_device()
            if self.draft is not None:
                self.photo.device = selected
            if self.enrollment is not None:
                self.enrollment.refresh()
            self.picker.setEnabled(self.draft is not None and bool(self.devices))
            if self.draft is not None and selected is not None:
                set_role(self.start_button, None)
                self.start_button.setText("试按验证键码")
                self.start_button.setEnabled(True)
            else:
                self.start_button.setText("停止学习")
                self.start_button.setEnabled(False)
            if selected is None:
                self.feedback.setText("未找到所选遥控器。重新连接后再检测。")
            elif self.draft is not None and not self.draft.accepts(selected):
                self.feedback.setText(
                    f"已选新接收端「{selected.name}」。点「试按验证键码」确认键码一致，或点照片位置补学按键后保存。"
                )
            else:
                self.feedback.setText(
                    "点击照片上的一个按键位置。" if self.photo.profile.image else "请先导入一张遥控器正面照片。"
                )
            return

        self.photo_link.setEnabled(False)
        if self.install is not None and self.photo is None:
            self.show_protocol()
        set_role(self.start_button, "primary")
        self.start_button.setText("开始检测")
        self.start_button.setEnabled(self.selected_device() is not None)
        self._update_primary_buttons()
        self.picker.setEnabled(bool(self.devices or self._blocked_ble))
        if self.devices:
            message = "选择遥控器后点「开始检测」。"
        else:
            message = "未发现可读取键盘或媒体按键的 USB、蓝牙设备。连接后点「重新检测」。"
        self.feedback.setText(message)

    def toggle_capture(self):
        if self.active:
            trial_only = self.photo is not None and self.photo.pending is None
            self.stop_capture()
            self.reset_sample()
            self.feedback.setText(
                "已停止试按。" if trial_only else ("已停止学习。" if self.photo is not None else "已停止检测。")
            )
            return
        if self.photo is None or self.draft is not None:
            self.start_capture()

    def open_photo_learning(self):
        device = self.selected_device()
        if self.active or device is None or not self.photo_link.isEnabled() or self.learning_dialog is not None:
            return
        if isinstance(device.id, tuple) and device.id and device.id[0] == "ble-dongle" and self.reader is not None:
            with suppress(Exception):
                latest = next((d for d in (self.reader.enumerate() or ()) if d.id == device.id), None)
                if latest is not None:
                    device = latest
        dialog = DeviceProbeDialog(self.reader, self, device=device, install=self.install, owns_reader=False)
        self.learning_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self.photo_closed)
        dialog.open()

    def photo_closed(self, result):
        self.learning_dialog = None
        if self.install is not None and result == QDialog.DialogCode.Accepted:
            QTimer.singleShot(0, self.accept)
        else:
            self.refresh()

    def photo_changed(self):
        self.stop_capture(keep_alive=True)
        self.reset_sample()
        if self.photo.profile.buttons:
            self.feedback.setText("照片已更换，请检查已学按键的位置。")

    def _sync_capture_prompt(self):
        if self.active and self.photo is not None and self.photo.pending is not None and not self.held and not self.seen:
            self.feedback.setText(self.photo.capture_prompt())

    def learn_position(self, rect):
        if self.photo is not None and getattr(self.photo, "marking_voice_key", False):
            from .protocols import identify_protocol

            self.stop_capture(keep_alive=True)
            marker = self.photo.place_voice_marker(rect)
            if self.enrollment is not None:
                self.enrollment.refresh()
            match = identify_protocol(self.photo.device, self.reader) if self.photo.device is not None else None
            if match is not None and match.voice_supported:
                self.feedback.setText(
                    "已标好「语音」键位置，不用按语音键。"
                    "接下来点击照片上的其他按键并按下实体键学习；保存后到「语音」页选「遥控器麦克风」。"
                )
            elif self.reader is not None and not marker.trigger:
                # No built-in voice protocol: the voice key can still trigger recognition with
                # the computer microphone once its key code is known.
                self.start_capture(marker.id)
            else:
                self.feedback.setText("已标好「语音」键位置。接下来点击照片上的其他按键并按下实体键学习。")
            return
        if self.photo is not None and getattr(self.photo, "mic_step_card", None) is not None:
            self.photo.mic_step_card.hide()
        self.stop_capture(keep_alive=True)
        self.start_capture(rect)

    def learn_frame(self, identity):
        """Learn an automatically placed frame; an empty id ends learning."""
        if not identity:
            self.stop_capture()
            self.reset_sample()
            return
        # Keep the open HID session alive when stepping between auto-detected frames:
        # closing and immediately reopening IOHIDDevice on macOS BLE/USB remotes can
        # drop reports or stall the next key.
        if self.active and self.photo is not None:
            self.capture_timer.stop()
            self.settle_timer.stop()
            self.held.clear()
            self.seen.clear()
            if not self.photo.begin(identity):
                self.stop_capture()
                self.reset_sample()
                return
            self.set_input_step("waiting")
            self.edges.setText("等待检测")
            self.key_code.setText("—")
            self.source.setText("等待设备信号" + self.scope_detail)
            self.photo.set_capturing(True)
            if self.enrollment is not None:
                self.enrollment.setEnabled(self.photo.learning_frame())
            self.start_button.setText("停止学习")
            self.start_button.setEnabled(True)
            self.feedback.setText(self.photo.capture_prompt())
            return
        self.stop_capture(keep_alive=True)
        self.start_capture(identity)

    def simulate_press(self):
        self.simulate_button.setEnabled(False)
        self.reader.press()

    def start_capture(self, rect=None):
        device = self.selected_device()
        if device is None:
            self.feedback.setText("请重新检测所选的遥控器。")
            return
        self.reset_sample()
        if self.photo is not None and rect is not None and not self.photo.begin(rect):
            return
        self.held.clear()
        self.seen.clear()
        self._ble_paused = False
        self.generation += 1
        generation = self.generation
        scope = {"expected": device} if self.install is not None else {}

        # BLE dongle connection & first-time pairing can take several seconds; run off the Qt UI thread
        # so the window never freezes ("未响应") and remains cancellable. When already connected (warm session),
        # fall through to the instant 0ms path with zero "connecting/pairing" flash!
        already_connected = callable(getattr(self.reader, "is_connected_to", None)) and self.reader.is_connected_to(device.id)
        if isinstance(device.id, tuple) and device.id and device.id[0] == "ble-dongle" and not already_connected:
            self.active = True
            self.picker.setEnabled(False)
            self.refresh_button.setEnabled(False)
            self.start_button.setEnabled(True)
            self.start_button.setText("取消连接")
            self.feedback.setText("正在连接并配对 BLE 遥控器，请保持遥控器配对灯闪烁…")
            self.feedback.show()

            def _connect_worker():
                try:
                    opened, total = self.reader.start(
                        device.id,
                        lambda channel, trigger, down: self.key_received.emit(generation, channel, trigger, down),
                        lambda: self.device_removed.emit(generation),
                        **scope,
                    )
                except (OSError, RuntimeError) as exc:
                    self.capture_started.emit(generation, 0, 0, str(exc))
                    return
                self.capture_started.emit(generation, opened, total, "")

            threading.Thread(target=_connect_worker, name="remotehub-probe-connect", daemon=True).start()
            return

        try:
            opened, total = self.reader.start(
                device.id,
                lambda channel, trigger, down: self.key_received.emit(generation, channel, trigger, down),
                lambda: self.device_removed.emit(generation),
                **scope,
            )
        except (OSError, RuntimeError) as exc:
            self._handle_start_error(str(exc))
            return
        self._finish_start_capture(opened, total)

    def _on_capture_started(self, generation: int, opened: int, total: int, error_text: str):
        if generation != self.generation or not self.active:
            return
        if error_text:
            self.active = False
            self.picker.setEnabled(bool(self.devices or self._blocked_ble))
            self.refresh_button.setEnabled(self.reader is not None)
            self.start_button.setText("开始检测")
            self.start_button.setEnabled(self.selected_device() is not None)
            if "不是遥控器" in error_text:
                self.refresh()                      # the reader just forgot it; drop it from the picker
            self._handle_start_error(error_text)
            return
        self._finish_start_capture(opened, total)

    def _handle_start_error(self, message: str):
        if self.photo is not None:
            self.photo.cancel()
        self.feedback.setText(message)
        self.feedback.show()
        self.set_input_step("open_failed")
        if sys.platform == "darwin" and permission_status().get("input") == "未授权":
            self.feedback.setText(message + " 请为当前 App 或启动终端开启输入监控后重试。")
            self.permissions.show()

    def _finish_start_capture(self, opened: int, total: int):
        self.interface_counts = (opened, total)
        self.set_input_step("waiting")
        self.active = True
        QApplication.instance().installEventFilter(self)
        self.picker.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.start_button.setEnabled(True)
        if self.photo is None:
            self.start_button.setText("停止检测")
            self.feedback.setText("已连接！请按一下遥控器任意键再松开。")
            if self.install is not None:
                self.show_protocol()
        else:
            self.photo.set_capturing(True)
            if self.enrollment is not None:
                # Keep saving available whenever at least one key has already been learned;
                # RemoteEnrollment.save stops capture cleanly before writing.
                self.enrollment.setEnabled(self.photo.learning_frame() or bool(self.photo.profile.buttons))
            if self.simulate_button is not None:
                self.simulate_button.setEnabled(True)
                self.feedback.setText("位置已选好。点「模拟按一下」看看绑定效果。")
            elif self.photo.pending is None:
                self.start_button.setText("停止试按")
                self.feedback.setText("按一下这只遥控器上的任意已学按键，核对键码是否一致。")
            else:
                self.start_button.setText("停止学习")
                self.feedback.setText(self.photo.capture_prompt())
        if opened < total:
            self.scope_detail = f" · 已打开 {opened}/{total} 个按键接口"
        if getattr(self.reader, "scope", ""):
            self.scope_detail += " · " + self.reader.scope
        self.source.setText("等待设备信号" + self.scope_detail)
        # Photo learning stays listening until the user presses the physical key, skips, or stops;
        # a 12s idle timeout would silently cut off learning while the user picks Zone -> Key.
        if self.photo is None or self.photo.pending is None:
            self.capture_timer.start(12000)
        else:
            self.capture_timer.stop()

    def eventFilter(self, target, event):
        if self.active and isinstance(target, QWidget) and target.window() == self and event.type() in (
            QEvent.Type.KeyPress, QEvent.Type.KeyRelease, QEvent.Type.ShortcutOverride,
        ):
            # Keys may still reach Qt during shared capture. Keep them from
            # choosing another device or activating a dialog button.
            event.accept()
            return True
        return super().eventFilter(target, event)

    def on_key(self, generation, channel, trigger, down):
        if generation != self.generation or not self.active:
            return
        if self.photo is not None and self.photo._queued is not None:
            return
        key = (channel, trigger)
        if down:
            self.settle_timer.stop()
            self.held.add(key)
            self.seen.add(key)
            if self.photo is not None and self.photo.pending is not None:
                # Guard only against a missed release edge; never cut off idle waiting.
                self.capture_timer.start(4000)
            source = "模拟输入" if self.simulate_button is not None else "所选设备"
            self.source.setText(source + self.scope_detail)
            self.edges.setText("已收到按下，等待松开")
            self.set_input_step("down_only")
        elif key in self.held:
            self.held.remove(key)
            if not self.held:
                if self.photo is not None and self.photo.pending is not None:
                    self.capture_timer.stop()
                self.settle_timer.start()

    def finish_sample(self):
        if not self.active or not self.seen or self.held:
            return
        keep_open = (
            self.photo is not None
            and self.photo.learning_frame()
            and len(self.seen) == 1
        )
        if not keep_open:
            self.stop_capture(keep_alive=True)
        if len(self.seen) == 1:
            _, trigger = next(iter(self.seen))
            self.edges.setText("已收到按下与松开")
            self.key_code.setText(trigger)
            self.set_input_step("confirmed")
            if self.photo is None:
                self.observe.show()
                self.photo_link.setEnabled(True)
                self.feedback.setText("已检测到这个键。")
                self._update_primary_buttons()
            elif self.photo.pending is None:
                existing = next((b for b in self.photo.profile.buttons if b.matches_trigger(trigger)), None)
                if existing is not None:
                    self._clear_merge_prompt()
                    self.photo.select(existing.id)
                    self.feedback.setText(
                        f"已收到「{existing.name}」（{trigger}），与现有键位一致。点「保存遥控器」即可同时支持此接收端。"
                    )
                elif self.merge_card is not None and any(b.configurable for b in self.photo.profile.buttons):
                    self._offer_merge_trigger(trigger)
                else:
                    self.feedback.setText(
                        f"收到键码 {trigger}，不在已学键位中。点击照片位置可学习该键，或确认是否选对了设备。"
                    )
            elif self.photo.bind(trigger):
                self.held.clear()
                self.seen.clear()
                self.settle_timer.stop()
                self.capture_timer.stop()
                if keep_open:
                    next_id = self.photo._queued
                    if next_id:
                        self.photo._queue.stop()
                        self.photo._queued = None
                        self.learn_frame(next_id)
                    else:
                        self.stop_capture()
                        self.feedback.setText("已绑定。点击照片上的另一个位置可继续学习。")
                else:
                    self.feedback.setText("已绑定。点击照片上的另一个位置可继续学习。")
            else:
                if keep_open:
                    self.stop_capture()
                self.feedback.setText("这个实体键已绑定，已在照片上选中它。")
                self.set_input_step("duplicate_key")
        else:
            if self.photo is not None:
                self.photo.cancel()
            self.edges.setText("收到多个信号")
            self.feedback.setText("这次没有得到唯一按键。请只按一个键，再试一次。")
            self.set_input_step("multiple_keys")
        if self.photo is None:
            self.start_button.setText("再测一个键")

    def timeout(self):
        if self.seen and not self.held:
            self.finish_sample()
            return
        if self.photo is not None and self.photo.pending is not None and self.active and self.held:
            # Recover from a missed release edge without tearing down photo learning.
            self.held.clear()
            self.seen.clear()
            self.settle_timer.stop()
            self.set_input_step("waiting")
            self.edges.setText("未收到松开，请再按一次")
            name = self.photo.pending.name
            label_part = f"「{name}」" if name and name != "等待按键" else "这颗实体键"
            self.feedback.setText(f"刚才没有收到松开信号，请再按一下{label_part}并松开。")
            return
        self.stop_capture()
        self.set_input_step("missing_release" if self.held else "no_signal")
        self.edges.setText("未收到松开" if self.held else "未收到可识别按键")
        if self.photo is None:
            retry = "请换一个键试试。"
            self.feedback.setText(retry + "如果这只设备已被其他 App 接管，退出该 App 后重新检测。")
            if sys.platform == "darwin" and permission_status().get("input") == "未授权":
                self.permissions.show()
            self.start_button.setText("再测一个键")
        else:
            was_trial = self.photo.pending is None
            self.photo.cancel()
            self.feedback.setText(
                "未收到按键。请点「试按验证键码」再按一下遥控器。" if was_trial
                else "请重新点照片上的位置，再按一下实体键并松开。"
            )

    def mark_original(self):
        self.original.setText("已观察到原键生效")
        self.observe.hide()

    def on_removed(self, generation):
        if generation == self.generation and self.active:
            if not self.refresh():
                return
            if self.selected_device() is not None:
                if self.photo is None:
                    self.start_button.setText("再测一个键")
                    self.feedback.setText("遥控器仍在，按键接口发生了变化。请再测一次。")
                else:
                    self.feedback.setText("遥控器仍在。请重新点照片上的位置。")
            else:
                self.feedback.setText("未找到所选遥控器。重新连接后再检测。")
            self.set_input_step("disconnected")

    def stop_capture(self, *, keep_alive: bool = False):
        self.generation += 1
        self.capture_timer.stop()
        self.settle_timer.stop()
        was_active = self.active
        if was_active:
            self.active = False
            QApplication.instance().removeEventFilter(self)
            device = self.selected_device()
            is_ble_dongle = isinstance(getattr(device, "id", None), tuple) and bool(device.id) and device.id[0] == "ble-dongle"
            pause_fn = getattr(self.reader, "pause", None) if (keep_alive and is_ble_dongle) else None
            if callable(pause_fn):
                pause_fn()
                self._ble_paused = True
            else:
                self._ble_paused = False
                self.reader.stop()
        elif self._ble_paused and not keep_alive:
            self._ble_paused = False
            if self.reader is not None:
                self.reader.stop()
        self.picker.setEnabled((self.photo is None or self.draft is not None) and bool(self.devices or self._blocked_ble))
        self.refresh_button.setEnabled(self.reader is not None)
        if self.photo is not None:
            can_trial = self.draft is not None and self.selected_device() is not None
            self.start_button.setText("试按验证键码" if can_trial else "停止学习")
            self.start_button.setEnabled(can_trial)
            self.photo.set_capturing(False)
            if self.enrollment is not None:
                self.enrollment.setEnabled(True)
        if self.simulate_button is not None:
            self.simulate_button.setEnabled(False)

    def _needs_close_confirmation(self):
        return (
            not self._discard_confirmed
            and self.photo is not None
            and self.enrollment is not None
            and self.photo.dirty
            and (bool(self.photo.profile.buttons) or self.draft is not None)
        )

    def _confirm_discard(self):
        answer = QMessageBox.question(
            self,
            "放弃未保存的改动",
            "已学按键的改动尚未保存，确定放弃并关闭吗？",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def accept(self):
        self._discard_confirmed = True
        super().accept()

    def reject(self):
        if self._needs_close_confirmation():
            if not self._confirm_discard():
                return
            self._discard_confirmed = True
        super().reject()

    def closeEvent(self, event):
        if self._needs_close_confirmation():
            if not self._confirm_discard():
                event.ignore()
                return
            self._discard_confirmed = True
        super().closeEvent(event)

    def release_reader(self, *_):
        if self._ble_scanning:
            self._ble_scanning = False
            self._cancel_ble_scan()
        self.stop_capture()
        if self.owns_reader and self.reader is not None:
            self.reader.close()

    def open_permissions(self):
        try:
            open_permission_settings("input")
        except (RuntimeError, ValueError) as exc:
            self.feedback.setText(str(exc))


def run(argv):
    parser = argparse.ArgumentParser(prog="remotehub --device-probe")
    parser.add_argument("--device-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--photo-preview", action="store_true", help="Open photo learning with simulated input")
    parser.add_argument("--photo-log", metavar="PATH", help="Write photo import diagnostics during photo preview")
    parser.add_argument("--smoke", action="store_true", help="Open without accessing any hardware")
    parser.add_argument("--screenshot", help="Save the smoke-mode window to this path")
    args = parser.parse_args(argv)
    if args.screenshot and not args.smoke:
        parser.error("--screenshot 需要 --smoke")
    if args.photo_log and not args.photo_preview:
        parser.error("--photo-log 需要 --photo-preview")
    if not (args.photo_preview or args.smoke) and sys.platform not in ("darwin", "win32"):
        parser.error("实体设备探测目前支持 macOS 和 Windows")
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("RemoteHub")
    configure_application(app, native=not args.smoke)
    reader = None
    if args.photo_preview:
        reader = PhotoPreviewReader()
    elif not args.smoke:
        from .readers import default_registry

        hid_entry = default_registry().get("hid")
        reader = hid_entry.factory(None) if hid_entry is not None else None
    device = reader.device if args.photo_preview else None
    dialog = DeviceProbeDialog(reader, device=device, preview=args.photo_preview)
    dialog.show()
    if args.smoke:
        def finish():
            if args.screenshot:
                dialog.grab().save(args.screenshot)
            dialog.close()
        QTimer.singleShot(150, finish)
    if args.photo_log:
        import logging
        from logging.handlers import RotatingFileHandler

        try:
            handler = RotatingFileHandler(args.photo_log, maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
        except OSError as exc:
            parser.error(f"无法打开照片导入日志：{exc.strerror}")
        handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(message)s"))
        logger = logging.getLogger("remotehub.ui.probe_photo")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            logger.info('{"event": "photo_log_started"}')
            return app.exec()
        finally:
            logger.removeHandler(handler)
            handler.close()
    return app.exec()
