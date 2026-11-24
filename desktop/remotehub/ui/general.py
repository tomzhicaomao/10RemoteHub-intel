"""通用设置 page: login startup, permissions and the unified device collector.

Nothing here changes the system until the user clicks: permission rows only
read status and open System Settings; login startup writes only on toggle.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QVBoxLayout, QWidget

from .. import permissions
from ..branding import APP_NAME
from ..controller import StatusView
from .input_overlays import InputOverlaySettings
from .voice import column_page, step_card
from .widgets import Pill, button, hbox, label, row

PERMISSIONS = (
    ("accessibility", "辅助功能", "执行按键和窗口操作"),
    ("input", "输入监控", "读取通用 USB／蓝牙遥控器与添加试按"),
)


class GeneralPage(QWidget):
    title = "通用设置"
    subtitle = "管理遥控器读取组件、系统授权与登录启动。"

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.permission_overlay = None
        self.setObjectName("page")
        steps = column_page(self)

        from .chat_focus import ChatFocusCard
        self.chat_focus_card = ChatFocusCard(controller.chat_focus, controller=controller)
        self.chat_focus_card.setVisible(controller.chat_focus.supported)
        steps.addWidget(self.chat_focus_card)
        input_card, input_box, _ = step_card(None, "输入浮层")
        self.input_overlay_settings = InputOverlaySettings(controller)
        input_box.addWidget(self.input_overlay_settings)
        steps.addWidget(input_card)

        # -- device component and connection -------------------------------
        card, box, _ = step_card(None, "遥控器与读取组件" if sys.platform == "darwin" else "设备连接")
        self.helper_pill = None
        if sys.platform == "darwin":
            self.helper_pill = Pill("未检测")
            self.helper_pill.setWordWrap(False)
            box.addWidget(self.helper_pill, 0, Qt.AlignmentFlag.AlignLeft)
            box.addWidget(label("读取组件仅用于 USB 0416:0602 配套接收器的独占按键和声音，安装时需要一次管理员密码；其他 USB／蓝牙遥控器无需安装。", "caption"))
            self.helper_install = button("安装 / 更新读取组件", self.install_helper, "primary")
            helper_actions = hbox(self.helper_install, button("重新检查权限与连接", self.retry))
            helper_actions.addStretch(1)
            box.addLayout(helper_actions)
            box.addWidget(button("卸载读取组件…", lambda: self.install_helper(True), "link"))
        elif sys.platform == "win32":
            from PySide6.QtCore import QTimer
            self._win_driver_pending = None
            self._win_driver_paused = False
            self._win_confirm_box = None
            self._win_driver_timer = QTimer(self)
            self._win_driver_timer.setInterval(250)
            self._win_driver_timer.timeout.connect(self._poll_win_binding)
            # Receivers the app reads itself (WinUSB takeover): current remote's USB receiver
            # can be taken over right here or in 「添加遥控器」, and any bound one restored here.
            box.addWidget(label("由 App 接管的接收器", "value"))
            box.addWidget(label(
                "接管后，这只接收器只由 App 读取（可完整读取 # 号键与遥控器麦克风；App 没有运行时它不能用）。"
                "可以直接在这里或「添加遥控器」点「让 App 接管这个接收器」，随时点「恢复系统驱动」还原。", "caption"))
            self.takeover_rows = QWidget()
            self.takeover_layout = QVBoxLayout(self.takeover_rows)
            self.takeover_layout.setContentsMargins(0, 0, 0, 0)
            self.takeover_layout.setSpacing(6)
            box.addWidget(self.takeover_rows)
            self.takeover_empty = label("还没有接管任何接收器。", "caption")
            box.addWidget(self.takeover_empty)
            self.takeover_buttons = []
            box.addWidget(row(button("重新检查权限与连接", self.retry), None))
        else:
            self.linux_udev_pill = Pill("未检测")
            self.linux_usb_pill = Pill("未检测")
            self.linux_uinput_pill = Pill("未检测")
            self.linux_udev_detail = label("", "caption")
            self.linux_copy_cmd = button("复制 udev 规则安装命令", self.copy_linux_udev_command)
            box.addWidget(row(label("udev 设备规则（免 root 读取）", wrap=False), None, self.linux_udev_pill, spacing=8))
            box.addWidget(row(label("USB 接收器读写（0416:0602）", "caption", wrap=False), None, self.linux_usb_pill, spacing=8))
            box.addWidget(row(label("虚拟输入设备（/dev/uinput）", "caption", wrap=False), None, self.linux_uinput_pill, spacing=8))
            box.addWidget(self.linux_udev_detail)
            box.addWidget(row(self.linux_copy_cmd, button("重新检查权限与连接", self.retry), None))

        self.connection_status = Pill()
        self.connection_status.setWordWrap(False)
        box.addWidget(self.connection_status, 0, Qt.AlignmentFlag.AlignLeft)
        self.connection_detail = label("", "caption")
        box.addWidget(self.connection_detail)
        self.connection_error = label("", "caption")
        self.connection_error.setTextFormat(Qt.TextFormat.PlainText)
        self.connection_error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.connection_error.setAccessibleName("设备接入原始错误")
        box.addWidget(self.connection_error)
        steps.addWidget(card)

        # -- permissions ---------------------------------------------------
        card, box, _ = step_card(None, "系统授权")
        self.permission_rows = {}
        if sys.platform == "darwin":
            box.addWidget(label(f"点击「前往授权」，在系统设置里为 {APP_NAME} 打开开关。", "caption"))
            for key, title, purpose in PERMISSIONS:
                pill = Pill("尚未检测")
                self.permission_rows[key] = pill
                box.addWidget(row(label(f"{title} · {purpose}", wrap=False), None, pill,
                                  button("前往授权", lambda k=key: self.c.open_permission(k)), spacing=8))
            bundle = permissions.running_app_bundle()
            if bundle:
                box.addWidget(label("系统设置底部会出现拖拽引导，把应用图标拖入列表后再打开开关。", "caption"))
                box.addWidget(label("列表里已有本应用时，直接确认开关已打开；改完点「重新检查权限与连接」。", "caption"))
            else:
                box.addWidget(label("列表里没有本应用时，用「＋」添加，再打开开关。", "caption"))
        elif sys.platform == "win32":
            box.addWidget(label(
                "Windows 普通按键映射、窗口控制与 UI 自动化无需额外系统授权开关。\n"
                "注意：若前台窗口以「管理员身份」运行，Windows 安全策略（UIPI）会拦截普通权限程序的模拟按键和窗口控制；"
                "此时底部状态栏会给出明确提示。",
                "caption",
            ))
        else:
            from ..linux_desktop import is_wayland_session
            session_name = "Wayland" if is_wayland_session() else "X11"
            box.addWidget(label(
                f"当前桌面会话协议：{session_name}。\n"
                "• X11 会话完整支持按键/鼠标注入、终端智能粘贴（Ctrl+Shift+V）、按前台应用自动切层与窗口显示/收起。\n"
                "• Wayland 会话通过 /dev/uinput 注入全局按键、滚轮与鼠标点击；受 Wayland 安全隔离限制，跨应用前台切层与窗口收起暂不可用。",
                "caption",
            ))
        steps.addWidget(card)

        # System shortcuts are cached at launch; this button is the only refresh.
        self.hotkey_labels = {}
        if sys.platform == "darwin":
            from ..system_hotkeys import GROUPS, OPERATIONS
            card, box, _ = step_card(None, "系统快捷键")
            box.addWidget(label("每次启动时自动读取。修改 macOS 键盘快捷键后，点击「重新读取」更新。", "caption"))
            self.hotkeys_reload = button("重新读取", self.reload_hotkeys)
            box.addWidget(self.hotkeys_reload, 0, Qt.AlignmentFlag.AlignLeft)
            for group, operations in GROUPS:
                box.addWidget(label(group, "caption"))
                for operation in operations:
                    title = OPERATIONS[operation][1]
                    value = label("", "caption")
                    value.setAccessibleName(title + "当前快捷键")
                    self.hotkey_labels[operation] = value
                    box.addWidget(row(label(title, wrap=False), None, value))
            steps.addWidget(card)
            self.show_hotkeys()

        # -- startup -------------------------------------------------------
        card, box, _ = step_card(None, "启动与后台运行")
        self.start_at_login = QCheckBox("登录后自动启动")
        self.start_at_login.toggled.connect(self.set_autostart)
        box.addWidget(self.start_at_login)
        self.startup_status = label("", "caption")
        box.addWidget(self.startup_status)
        tray_hint = (
            "关闭窗口后仍在后台运行；从菜单栏图标选择「退出」即可停止。"
            if sys.platform == "darwin"
            else "关闭窗口后仍在后台驻留；从任务栏右下角系统托盘图标选择「退出」即可停止。"
            if sys.platform == "win32"
            else "关闭窗口后仍在后台运行（桌面支持系统托盘时）；从系统托盘图标选择「退出」即可停止。"
        )
        box.addWidget(label(tray_hint, "caption"))
        steps.addWidget(card)

        controller.device_changed.connect(self.refresh)
        controller.remotes_changed.connect(self.refresh_takeovers)
        controller.permission_opened.connect(self.show_permission_overlay)
        self.refresh()
        self.refresh_autostart()

    def show_hotkeys(self):
        cache = self.c.runner.system_hotkeys
        if cache is not None:
            for operation, value in self.hotkey_labels.items():
                value.setText(cache.status(operation))

    def reload_hotkeys(self):
        self.c.runner.system_hotkeys.refresh()
        self.show_hotkeys()
        self.c.tell("已重新读取系统快捷键；未修改 macOS 设置。")

    def on_show(self):
        """Called when the page becomes visible; status reads are cheap and read-only."""
        self.refresh_permissions()
        self.refresh_autostart()
        self.refresh_helper()
        self.refresh_takeovers()
        self.refresh_linux_udev()

    def refresh(self):
        c = self.c
        connection = c.device_view() if c.uses_dedicated_reader() else c.remote_view()
        self.connection_status.show_status(connection)
        self.connection_detail.setText(connection.detail)
        self.connection_error.setText("具体错误：" + connection.diagnostic if connection.diagnostic else "")
        self.connection_error.setVisible(bool(connection.diagnostic))
        self.refresh_takeovers()
        self.refresh_linux_udev()

    def refresh_permissions(self):
        states = self.c.permission_status()
        for key, pill in self.permission_rows.items():
            text = states.get(key, "无法检测")
            tone = "ok" if text == "已授权" else "error" if text == "未授权" else "neutral"
            pill.show_status(StatusView(text, "", tone))

    def refresh_autostart(self):
        try:
            enabled = self.c.autostart_enabled()
            self.start_at_login.blockSignals(True)
            self.start_at_login.setChecked(enabled)
            self.start_at_login.blockSignals(False)
            self.startup_status.setText("已设置登录启动" if enabled else "未设置登录启动")
        except Exception as exc:
            self.startup_status.setText("无法读取登录启动设置：" + str(exc))

    def set_autostart(self, enabled):
        try:
            self.c.set_autostart(enabled)
            self.refresh_autostart()
        except Exception as exc:
            self.start_at_login.blockSignals(True)
            self.start_at_login.setChecked(not enabled)
            self.start_at_login.blockSignals(False)
            self.startup_status.setText("登录启动设置失败：" + str(exc))

    def refresh_helper(self):
        if self.helper_pill is not None:
            from ..mac_device_helper import installation_status
            code, message = installation_status()
            self.helper_pill.show_status(StatusView(message, "", "ok" if code == 0 else "neutral"))

    def copy_linux_udev_command(self):
        rule = permissions.bundled_udev_rule_path()
        cmd = (
            f'sudo install -Dm644 "{rule}" /etc/udev/rules.d/70-10remotehub.rules && '
            "sudo udevadm control --reload-rules && sudo udevadm trigger"
        )
        app = QApplication.instance()
        if app is not None:
            app.clipboard().setText(cmd)
        self.c.tell("已复制 udev 规则安装命令到剪贴板；请在终端粘贴执行一次，重新插拔接收器后点「重新检查权限与连接」。")

    def refresh_linux_udev(self):
        if not sys.platform.startswith("linux") or not hasattr(self, "linux_udev_pill"):
            return
        st = permissions.linux_access_status()
        rule_ok = st.get("udev_rule") == "已安装"
        usb_txt = st.get("usb", "未检测")
        uinput_txt = st.get("uinput", "未检测")
        self.linux_udev_pill.show_status(StatusView(st.get("udev_rule", "未安装"), "", "ok" if rule_ok else "neutral"))
        self.linux_usb_pill.show_status(
            StatusView(usb_txt, "", "ok" if usb_txt in ("已授权", "已安装规则") else "neutral")
        )
        self.linux_uinput_pill.show_status(
            StatusView(uinput_txt, "", "ok" if uinput_txt in ("已授权", "X11 免配置") else "neutral")
        )
        if rule_ok and usb_txt in ("已授权", "已安装规则"):
            self.linux_udev_detail.setText("udev 权限规则已就绪：普通桌面用户可直接独占读取遥控器全部按键（含 # 号键）与语音。")
        else:
            self.linux_udev_detail.setText(
                "安装一次 udev 规则（TAG+=\"uaccess\"）即可免 root 读取遥控器全部按键与语音，并在 Wayland 下启用 /dev/uinput。"
            )

    def _start_win_binding(self, action: str, instance_ids: list[str], *, confirm: bool = False):
        if getattr(self, "_win_driver_pending", None) is not None:
            return
        from .. import win_usb_driver
        paused = False
        pause_fn = getattr(self.c, "pause_remote_input", None)
        if callable(pause_fn):
            paused = bool(pause_fn())
        try:
            handle, result_path = win_usb_driver.launch_elevated_action(action, instance_ids, confirm=confirm)
        except win_usb_driver.DriverActionCancelled as exc:
            if paused:
                self.c.resume_remote_input()
            self.c.tell(str(exc))
            return
        except Exception as exc:
            if paused:
                self.c.resume_remote_input()
            self.c.fail(str(exc))
            return
        self._win_driver_paused = paused
        self._win_driver_pending = (handle, result_path, action)
        for control in self.takeover_buttons:
            control.setEnabled(False)
        self.c.tell(
            "正在让 App 接管这个接收器，请在弹出的系统确认框中点「是」…"
            if action == "bind"
            else "正在恢复系统驱动，请在弹出的系统确认框中点「是」…"
        )
        self._win_driver_timer.start()

    def _ask_keep_takeover(self, result_path):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMessageBox
        from .. import win_usb_driver

        box = QMessageBox(self)
        box.setWindowTitle("确认接管")
        box.setIcon(QMessageBox.Icon.Question)
        keep = box.addButton("保留接管", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("恢复系统驱动", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        left = [15]

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
            self._win_confirm_box = None

        box.finished.connect(answered)
        self._win_confirm_box = box
        box.open()

    def _close_win_confirm_box(self):
        box = getattr(self, "_win_confirm_box", None)
        if box is not None:
            box.done(0)
            self._win_confirm_box = None

    def _poll_win_binding(self):
        pending = getattr(self, "_win_driver_pending", None)
        if pending is None:
            self._win_driver_timer.stop()
            return
        from .. import win_usb_driver
        handle, result_path, _action = pending
        done, exit_code = win_usb_driver.poll_elevated_process(handle)
        if not done:
            if (getattr(self, "_win_confirm_box", None) is None
                    and win_usb_driver.read_action_stage(result_path).get("stage") == "awaiting_confirm"):
                self._ask_keep_takeover(result_path)
            return
        self._win_driver_timer.stop()
        self._win_driver_pending = None
        self._close_win_confirm_box()
        paused = getattr(self, "_win_driver_paused", False)
        self._win_driver_paused = False
        result = win_usb_driver.read_action_result(result_path, exit_code)
        if paused:
            self.c.resume_remote_input()
        self.refresh_takeovers()
        self.c.retry_connection()
        if result.get("ok"):
            self.c.tell(result.get("message") or "驱动切换已完成。")
        else:
            self.c.fail(result.get("error") or "驱动切换失败。")

    def _current_usb_pairs(self) -> set[tuple[int, int]]:
        pairs: set[tuple[int, int]] = set()
        if getattr(self.c, "uses_receiver", False):
            pairs.add((0x0416, 0x0602))
        remote = getattr(self.c, "remote", None)
        if remote is not None:
            for conn in getattr(remote, "connections", ()) or ():
                if getattr(conn, "transport", "") == "USB" and getattr(conn, "vendor", 0) and getattr(conn, "product", 0):
                    pairs.add((int(conn.vendor), int(conn.product)))
        return pairs

    def _receiver_groups(self):
        """``{(vendor, product): (name, [all managed interfaces])}`` for present USB receivers."""
        from .. import win_usb_driver

        groups = {}
        _, receivers = win_usb_driver.list_managed_devices()
        for item in receivers:
            groups.setdefault((item.vendor, item.product), (item.name, []))[1].append(item)
        return groups

    def taken_over(self):
        """``{(vendor, product): (name, [restorable interfaces])}`` of receivers bound to WinUSB."""
        groups = {}
        for pair, (name, items) in self._receiver_groups().items():
            restorable = [item for item in items if item.restorable]
            if restorable:
                groups[pair] = (name, restorable)
        return groups

    def bind_receiver(self, pair):
        group = self._receiver_groups().get(pair)
        targets = [d.instance_id for d in (group[1] if group else ()) if not d.bound_winusb]
        if not targets:
            self.c.tell("这个接收器已由 App 接管。")
            self.refresh_takeovers()
            return
        self._start_win_binding("bind", targets, confirm=(pair != (0x0416, 0x0602)))

    def restore_receiver(self, pair):
        group = self.taken_over().get(pair)
        if not group:
            self.c.tell("这只接收器已是系统驱动。")
            self.refresh_takeovers()
            return
        self._start_win_binding("restore", [d.instance_id for d in group[1]])

    def refresh_takeovers(self):
        if sys.platform != "win32" or not hasattr(self, "takeover_layout"):
            return
        while self.takeover_layout.count():
            item = self.takeover_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.takeover_buttons = []
        all_groups = self._receiver_groups()
        current_pairs = self._current_usb_pairs()
        busy = getattr(self, "_win_driver_pending", None) is not None
        shown = 0
        for (vendor, product), (name, items) in sorted(all_groups.items()):
            restorable = [d for d in items if d.restorable]
            unbound = [d for d in items if not d.bound_winusb]
            if not restorable and (vendor, product) not in current_pairs:
                continue
            shown += 1
            controls = []
            if unbound:
                bind_btn = button("让 App 接管这个接收器", lambda _=False, pair=(vendor, product): self.bind_receiver(pair))
                bind_btn.setEnabled(not busy)
                self.takeover_buttons.append(bind_btn)
                controls.append(bind_btn)
            if restorable:
                restore = button("恢复系统驱动", lambda _=False, pair=(vendor, product): self.restore_receiver(pair))
                restore.setEnabled(not busy)
                self.takeover_buttons.append(restore)
                controls.append(restore)
            state = "部分接管" if (restorable and unbound) else ("已接管" if restorable else "未接管")
            text = label(f"USB {vendor:04X}:{product:04X} · {state}", wrap=False)
            text.setToolTip(name)
            self.takeover_layout.addWidget(row(text, None, *controls, spacing=8))
        self.takeover_empty.setVisible(shown == 0)

    def install_helper(self, remove=False):
        if remove:
            from PySide6.QtWidgets import QMessageBox
            answer = QMessageBox.question(
                self, "卸载读取组件", "卸载后，按键映射和遥控器语音都会停止。确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            self.c.install_collector(remove)
            self.c.tell("已打开读取组件的卸载程序。" if remove else "已打开安装程序，装好后点「重新检查权限与连接」。")
        except Exception as exc:
            self.c.fail(str(exc))

    def retry(self):
        self.refresh_helper()
        self.refresh_permissions()
        self.refresh_linux_udev()
        self.c.retry_connection()

    def show_permission_overlay(self, name):
        bundle = permissions.running_app_bundle()
        if bundle is None:
            return
        # The drag target sits in the Settings window, so the main window steps
        # aside instead of covering it; the guide panel is a separate tool window.
        window = self.window()
        if window is not None and window.isVisible() and not window.isMinimized():
            window.showMinimized()
        if self.permission_overlay is None:
            from .permission_overlay import PermissionOverlay
            self.permission_overlay = PermissionOverlay(bundle)
            self.permission_overlay.failed.connect(self.c.fail)
            self.permission_overlay.authorized.connect(self.refresh_permissions)
        self.permission_overlay.begin(name)

    def close_permission_overlay(self):
        if self.permission_overlay is not None:
            self.permission_overlay.close()
