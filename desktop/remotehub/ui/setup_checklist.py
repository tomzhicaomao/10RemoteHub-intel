"""What still has to be set up before the current remote works: one list for every entry.

The same items appear in the first-run guide, right after a remote is added, and from
the sidebar. Required items are what key input needs on this platform; the virtual
microphone is optional and only listed when the remote's own microphone can be used.
"""
from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Callable

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import permissions
from ..branding import APP_NAME
from ..controller import StatusView
from ..theme import TOKENS, stylesheet
from .widgets import Pill, button, label, row

# Pages an item can send the user to; the host decides how to get there.
SETTINGS, VOICE, ADD = "settings", "voice", "add"


@dataclass
class SetupItem:
    key: str
    title: str
    required: bool
    done: bool
    status: str
    detail: str
    action: str = ""
    run: Callable[[], None] | None = None


def _mac_component_installed() -> bool:
    try:
        from ..mac_device_helper import installation_status
        return installation_status()[0] == 0
    except Exception:
        return False


def _windows_0416_interfaces():
    """Live present WinUSB-manageable interfaces for the bundled 0416:0602 receiver."""
    try:
        from .. import win_usb_driver
        _, receivers = win_usb_driver.list_managed_devices()
        return win_usb_driver.receiver_interfaces(receivers, 0x0416, 0x0602)
    except Exception:
        return []


def _windows_keys_taken_over() -> bool:
    live = _windows_0416_interfaces()
    if live:
        return all(d.bound_winusb for d in live)
    try:
        from ..windows_driver import binding_detail
        return binding_detail().get("keyboard") == "winusb"
    except Exception:
        return False


def setup_items(
    c,
    navigate: Callable[[str], None],
    *,
    run_takeover: Callable[[], None] | None = None,
    takeover_busy: bool = False,
) -> list[SetupItem]:
    """Items for the current remote on this platform, required ones first."""
    platform = sys.platform
    receiver = c.uses_receiver
    items: list[SetupItem] = []

    def act(callback):
        def run():
            try:
                callback()
            except Exception as exc:
                c.fail(str(exc))
        return run

    if platform == "darwin":
        if receiver:
            done = _mac_component_installed()
            items.append(SetupItem(
                "component", "读取组件", True, done, "已安装" if done else "未安装",
                "配套接收器被系统独占，要装一个后台小程序才能读按键和声音。安装要输入一次开机密码。",
                "安装", act(c.install_collector)))
        elif c.has_remote:
            granted = permissions.permission_status().get("input") == "已授权"
            items.append(SetupItem(
                "input", "输入监控权限", True, granted, "已授权" if granted else "未授权",
                "读取 USB 和蓝牙遥控器的按键需要它。勾选后回到这里。",
                "去授权", act(lambda: c.open_permission("input"))))
        granted = permissions.permission_status().get("accessibility") == "已授权"
        items.append(SetupItem(
            "accessibility", "辅助功能权限", True, granted, "已授权" if granted else "未授权",
            "用来替你按快捷键、打开应用和输入文字。第一次要在系统设置里勾选。",
            "去授权", act(lambda: c.open_permission("accessibility"))))
    elif platform == "win32":
        if receiver:
            done = _windows_keys_taken_over()
            if takeover_busy:
                status, action, run = "正在接管…", "正在接管…", None
            elif done:
                status, action, run = "已接管", "", None
            else:
                status = "可稍后"
                action = "让 App 接管这个接收器"
                run = run_takeover if run_takeover is not None else (lambda: navigate(SETTINGS))
            items.append(SetupItem(
                "takeover", "让 App 接管接收器", False, done, status,
                "不接管也能用基础按键；要读全部按键（含 # 号键）和遥控器麦克风，"
                "点右侧「让 App 接管这个接收器」（需管理员授权，随时可在「通用设置」恢复系统驱动）。",
                action, run))
    elif platform.startswith("linux"):
        st = permissions.linux_access_status()
        done = st.get("udev_rule") == "已安装" and st.get("usb") in ("已授权", "已安装规则")
        items.append(SetupItem(
            "udev", "设备权限（udev 规则）", True, done, "已配置" if done else "待配置",
            "在「通用设置」复制安装命令执行一次，即可以普通用户读取全部按键与语音，并在 Wayland 下模拟按键。",
            "前往通用设置", lambda: navigate(SETTINGS)))

    if c.supports_remote_mic:
        ready = bool(c.voice.output_id)
        view = c.microphone_view()
        if platform == "darwin":
            action = "" if ready else ("重新打开 App" if c.microphone_needs_relaunch
                                      else "完成设置" if c.microphone_installed else "安装")
            run = act(c.setup_virtual_mic)
        else:
            action, run = ("" if ready else "前往语音设置"), (lambda: navigate(VOICE))
        items.append(SetupItem(
            "microphone", "虚拟麦克风（用遥控器说话时需要）", False, ready, view.title,
            "把遥控器收到的声音交给识别软件。只用电脑麦克风说话可以跳过。", action, run))
    return items


def pending_required(c) -> int:
    return sum(1 for item in setup_items(c, lambda _page: None) if item.required and not item.done)


class SetupChecklist(QWidget):
    """Cards for each item, refreshed while visible (permissions change outside the app)."""

    navigate = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.items: list[SetupItem] = []
        self.pills: dict[str, Pill] = {}
        self._driver_pending = None
        self._driver_paused = False
        self._driver_timer = QTimer(self)
        self._driver_timer.setInterval(250)
        self._driver_timer.timeout.connect(self._poll_windows_takeover)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(8)
        self._cards_host = QWidget(self)
        self.box = QVBoxLayout(self._cards_host)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(8)
        self._outer.addWidget(self._cards_host)
        self.feedback = label("", "caption")
        self.feedback.hide()
        self._outer.addWidget(self.feedback)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh)
        controller.device_changed.connect(self.refresh)
        controller.voice_changed.connect(self.refresh)
        self.refresh()

    def _set_feedback(self, text: str):
        self.feedback.setText(text)
        self.feedback.setVisible(bool(text))

    def _start_windows_takeover(self):
        if self._driver_pending is not None:
            return
        from .. import win_usb_driver

        ifaces = _windows_0416_interfaces()
        if not ifaces:
            msg = "未检测到已插入的 0416:0602 USB 接收器。请插好接收器后再试。"
            self._set_feedback(msg)
            self.c.fail(msg)
            return
        targets = [d.instance_id for d in ifaces if not d.bound_winusb]
        if not targets:
            self.refresh()
            self.c.retry_connection()
            msg = "这个接收器已由 App 接管。"
            self._set_feedback(msg)
            self.c.tell(msg)
            return
        paused = False
        pause_fn = getattr(self.c, "pause_remote_input", None)
        if callable(pause_fn):
            paused = bool(pause_fn())
        try:
            handle, result_path = win_usb_driver.launch_elevated_action("bind", targets, confirm=False)
        except win_usb_driver.DriverActionCancelled as exc:
            if paused:
                self.c.resume_remote_input()
            self._set_feedback(str(exc))
            self.c.tell(str(exc))
            return
        except Exception as exc:
            if paused:
                self.c.resume_remote_input()
            self._set_feedback(str(exc))
            self.c.fail(str(exc))
            return
        self._driver_paused = paused
        self._driver_pending = (handle, result_path)
        msg = "正在让 App 接管这个接收器，请在弹出的系统确认框中点「是」…"
        self._set_feedback(msg)
        self.c.tell(msg)
        self.refresh()
        self._driver_timer.start()

    def _poll_windows_takeover(self):
        if self._driver_pending is None:
            self._driver_timer.stop()
            return
        from .. import win_usb_driver

        handle, result_path = self._driver_pending
        done, exit_code = win_usb_driver.poll_elevated_process(handle)
        if not done:
            return
        self._driver_timer.stop()
        self._driver_pending = None
        paused = self._driver_paused
        self._driver_paused = False
        result = win_usb_driver.read_action_result(result_path, exit_code)
        if paused:
            self.c.resume_remote_input()
        self.c.retry_connection()
        self.refresh()
        if result.get("ok"):
            msg = result.get("message") or "已由 App 接管这只接收器。如按键暂无响应，重新插拔一次接收器即可。"
            self._set_feedback(msg)
            self.c.tell(msg)
        else:
            err = result.get("error") or "接管接收器失败，请重试。"
            self._set_feedback(err)
            self.c.fail(err)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self):
        items = setup_items(
            self.c,
            self.navigate.emit,
            run_takeover=self._start_windows_takeover,
            takeover_busy=self._driver_pending is not None,
        )
        shape = [(i.key, i.action, i.run is not None) for i in items]
        if shape != [(i.key, i.action, i.run is not None) for i in self.items]:
            self._rebuild(items)
        self.items = items
        for item in items:
            self.pills[item.key].show_status(StatusView(item.status, "", "ok" if item.done else "neutral"))

    def _rebuild(self, items):
        while self.box.count():
            widget = self.box.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.pills.clear()
        for heading, group in (("必须完成", [i for i in items if i.required]),
                               ("可选", [i for i in items if not i.required])):
            if not group:
                continue
            self.box.addWidget(label(heading, "section"))
            for item in group:
                self.box.addWidget(self._card(item))

    def _card(self, item):
        card = QFrame()
        card.setObjectName("guideCard")
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(6)
        line = QHBoxLayout()
        title = QLabel(item.title)
        title.setObjectName("guideStep")
        line.addWidget(title, 1)
        pill = Pill(item.status)
        self.pills[item.key] = pill
        line.addWidget(pill)
        if item.action:
            btn = button(item.action, item.run or (lambda: None), "primary" if item.required else None)
            btn.setEnabled(item.run is not None)
            line.addWidget(btn)
        box.addLayout(line)
        box.addWidget(label(item.detail, "caption"))
        return card


GUIDE_STYLE = f"""
    QFrame#guideCard {{ background: {TOKENS['SUNKEN']};
        border: 1px solid {TOKENS['LINE']}; border-radius: 12px; }}
    QLabel#guideTitle {{ font-size: 22px; font-weight: 600; }}
    QLabel#guideStep {{ font-size: 15px; font-weight: 600; }}
    QLabel#guideBody {{ color: {TOKENS['MUTED']}; font-size: 13px; }}
"""


class SetupDialog(QDialog):
    """Shown after a remote is added, and from the sidebar while required items remain."""

    navigate = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle(APP_NAME + " · 完成设置")
        self.setMinimumWidth(600)
        self.setStyleSheet(stylesheet() + f"QDialog {{ background: {TOKENS['PANEL']}; }}" + GUIDE_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(12)
        name = controller.remote.name if controller.remote is not None else "遥控器"
        heading = QLabel(f"还差几步，「{name}」就能用了")
        heading.setObjectName("guideTitle")
        heading.setWordWrap(True)
        outer.addWidget(heading)
        self.summary = QLabel("")
        self.summary.setObjectName("guideBody")
        self.summary.setWordWrap(True)
        outer.addWidget(self.summary)
        self.checklist = SetupChecklist(controller, self)
        self.checklist.navigate.connect(self._navigate)
        outer.addWidget(self.checklist)
        outer.addStretch(1)
        self.later = button("稍后再说", self.reject)
        self.done_button = button("完成", self.accept, "primary")
        outer.addWidget(row(self.later, None, self.done_button))
        self.checklist.timer.timeout.connect(self._sync)
        controller.device_changed.connect(self._sync)
        self._sync()

    def _sync(self):
        left = sum(1 for i in self.checklist.items if i.required and not i.done)
        self.summary.setText(
            f"还有 {left} 项必须完成，做完这里会自动打勾。以后也可以在左侧遥控器卡片点「完成设置」回到这里。"
            if left else "必须的都已完成，遥控器按键可以用了。可选项需要时再做。")
        self.later.setVisible(bool(left))

    def _navigate(self, page):
        self.accept()
        self.navigate.emit(page)
