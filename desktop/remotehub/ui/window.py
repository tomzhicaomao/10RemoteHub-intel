"""Main window shell: sidebar | header + page stack + feedback bar, plus tray."""
from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QStackedWidget,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)
from platformdirs import user_config_path

from ..branding import APP_NAME
from ..controller import AppController
from ..icons import app_icon, tray_icon, set_dock_visible
from ..theme import apply_theme
from .general import GeneralPage
from .input_overlays import NineKeyOverlay, VoiceStatusOverlay
from .mapping import MappingPage
from .osd import CheatSheetOverlay, GridLauncherOverlay, ProfileToastOverlay
from .remote_list import RemoteListPage
from .sidebar import Sidebar
from .voice import VoicePage
from .widgets import button, label, scroll

DEFAULT_SIZE = (1280, 840)
MINIMUM_SIZE = (1200, 720)
MAPPING_HINT = "选择遥控器上的可配置按键，为它分配动作。"


class MainWindow(QMainWindow):
    def __init__(self, directory=None, safe=False):
        super().__init__()
        directory = Path(directory) if directory else user_config_path("RemoteHub", appauthor=False)
        self.controller = c = AppController(directory, self, auto_mapping=not safe)
        c.host = self
        self.tray = None
        self._relaunch_pending = False
        self._adding_new = False
        self.setup_dialog = None
        self.setWindowTitle(APP_NAME + " · 遥控器与语音")
        self.setWindowIcon(app_icon())
        apply_theme(self)
        self.setMinimumSize(*MINIMUM_SIZE)
        self.resize(*DEFAULT_SIZE)
        if not safe:
            screen = QApplication.primaryScreen().availableGeometry()
            self.resize(min(DEFAULT_SIZE[0], screen.width() - 40), min(DEFAULT_SIZE[1], screen.height() - 40))

        self.safe = safe
        self.add_dialog = None
        self.remotes_page = RemoteListPage(c)
        self.mapping = MappingPage(c)
        self.general = GeneralPage(c)
        self.voice_page = VoicePage(c)
        from .enrollment_voice import VoiceEnrollment
        self.voice_guide = VoiceEnrollment(c, lambda: c.remote, lambda: c.hid_device, self.show_mapping)
        self.voice_page.steps.insertWidget(1, self.voice_guide)   # right after step 1「语音来源」
        c.remote_key.connect(self.voice_guide.observe_key)
        self.cheat_sheet_osd = CheatSheetOverlay(c, animated=not safe)
        self.grid_launcher_osd = GridLauncherOverlay(c, animated=not safe)
        self.profile_toast_osd = ProfileToastOverlay(c, animated=not safe)
        self.nine_key_osd = NineKeyOverlay(c, animated=not safe)
        self.voice_osd = VoiceStatusOverlay(c, animated=not safe)
        self.pages = [self.remotes_page, self.mapping, self.voice_page, self.general]
        root = QWidget()
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        mod = "⌘" if QApplication.platformName() == "cocoa" else "Ctrl+"
        self.sidebar = Sidebar(c, [(p.title, f"{mod}{i + 1}") for i, p in enumerate(self.pages)])
        self.sidebar.page_requested.connect(self.show_page)
        self.sidebar.retry_requested.connect(self.general.retry)
        self.sidebar.settings_requested.connect(self.show_general)
        self.sidebar.add_requested.connect(self.add_remote)
        self.sidebar.learn_requested.connect(self.learn_more_keys)
        self.sidebar.setup_requested.connect(lambda: self.open_setup(force=True))
        self.remotes_page.add_requested.connect(self.add_remote)
        self.remotes_page.learn_requested.connect(self.learn_more_keys)
        self.remotes_page.open_mapping.connect(self.show_mapping)
        self.inspector.voice_settings_requested.connect(lambda: self.show_page(self.pages.index(self.voice_page)))
        self.inspector.voice_editor.microphone_setup_requested.connect(self.show_microphone_setup)
        shell.addWidget(self.sidebar)

        workspace = QWidget()
        workspace.setObjectName("workspace")
        shell.addWidget(workspace, 1)
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(24, 20, 24, 8)
        layout.setSpacing(10)
        header = QHBoxLayout()
        headings = QVBoxLayout()
        headings.setSpacing(3)
        self.page_title = label("", "title")
        self.page_subtitle = label("", "muted")
        headings.addWidget(self.page_title)
        headings.addWidget(self.page_subtitle)
        header.addLayout(headings, 1)
        layout.addLayout(header)
        # Offered when a catalog receiver is plugged in that no remote accepts yet.
        self.detected_model = None
        self.dismissed_models = set()
        self.detected_text = QLabel("")
        self.detected_text.setWordWrap(True)
        self.detected_add = button("添加这只遥控器", self.add_detected, "primary")
        self.detected_ignore = button("忽略", self.ignore_detected, "link")
        self.detected_banner = QWidget()
        self.detected_banner.setObjectName("banner")
        banner_row = QHBoxLayout(self.detected_banner)
        banner_row.setContentsMargins(12, 8, 12, 8)
        banner_row.addWidget(self.detected_text, 1)
        banner_row.addWidget(self.detected_ignore)
        banner_row.addWidget(self.detected_add)
        self.detected_banner.hide()
        layout.addWidget(self.detected_banner)
        self.stack = QStackedWidget()
        for page in self.pages:
            self.stack.addWidget(scroll(page))
        layout.addWidget(self.stack, 1)
        self.feedback = QLabel(MAPPING_HINT)
        self.feedback.setObjectName("feedback")
        self.feedback.setWordWrap(True)
        self.feedback.setMinimumHeight(28)
        self.undo_button = button("撤销", c.undo_action, "link")
        feedback_row = QHBoxLayout()
        feedback_row.setContentsMargins(0, 0, 0, 0)
        feedback_row.addWidget(self.feedback, 1)
        feedback_row.addWidget(self.undo_button)
        layout.addLayout(feedback_row)

        for i in range(len(self.pages)):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, lambda i=i: self.show_page(i))
        self.tray_menu = self._build_tray_menu()
        c.message.connect(self.show_message)
        c.relaunch_requested.connect(self.relaunch)
        c.physical_key.connect(self.show_physical_key)
        c.device_changed.connect(self.update_tray)
        c.device_changed.connect(self.refresh_tray_remotes)
        c.mapping_changed.connect(self.update_undo)
        c.profiles_changed.connect(self.update_undo)
        c.profiles_changed.connect(self.refresh_tray_profiles)
        c.profiles_changed.connect(self.update_tray)
        c.remotes_changed.connect(self.update_navigation)
        c.remotes_changed.connect(self.refresh_tray_remotes)
        c.remotes_changed.connect(self.update_tray)
        c.remotes_changed.connect(self.check_detected_receiver)
        c.device_changed.connect(self.check_detected_receiver)
        self.update_undo()
        self.refresh_tray_remotes()
        self.refresh_tray_profiles()
        self.update_navigation()
        self.show_page(self.pages.index(self.mapping) if c.has_remote else 0)
        self.canvas.setFocus()

        if not safe and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(tray_icon(), self)
            self.tray.setContextMenu(self.tray_menu)
            self.update_tray()
            self.tray.activated.connect(
                lambda reason: self.restore_from_tray() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
            self.tray.show()
        c.update_found.connect(self.offer_update)
        c.update_none.connect(self.updates_up_to_date)
        c.update_error.connect(self.update_failed)
        c.update_downloaded.connect(self.install_downloaded_update)
        if not safe:
            from ..mac_quit import install as install_quit_handler

            # Dock「退出」works with a dialog open. AppKit registers its own quit
            # handler when the event loop starts, so install ours after that.
            QTimer.singleShot(0, lambda: install_quit_handler(self.request_quit))
            c.start()
            # First run: explain the reader component before the user sees errors.
            QTimer.singleShot(600, self.show_onboarding_once)
            if sys.platform == "darwin":
                QTimer.singleShot(8000, self.check_updates_quiet)
        if c.load_error:
            c.fail(c.load_error)

    # -- convenience accessors (tests, key_survey parity) ----------------------

    @property
    def canvas(self):
        return self.mapping.canvas

    @property
    def inspector(self):
        return self.mapping.inspector


    # -- shell -------------------------------------------------------------------

    def show_page(self, index):
        if not self.controller.has_remote and self.pages[index] in (self.mapping, self.voice_page):
            index = self.pages.index(self.remotes_page)
        self.stack.setCurrentIndex(index)
        page = self.pages[index]
        self.page_title.setText(page.title)
        self.page_subtitle.setText(page.subtitle)
        self.sidebar.set_page(index, remotes_page=page is self.remotes_page, mapping_page=page is self.mapping)
        if self.feedback.text() in ("", MAPPING_HINT) and not self.feedback.property("error"):
            # The idle hint is about key mapping; other pages start with an empty bar.
            self.feedback.setText(MAPPING_HINT if page is self.mapping else "")
        if hasattr(page, "on_show"):
            page.on_show()

    # -- updates ---------------------------------------------------------------

    def check_updates_quiet(self):
        import time
        try:
            last = float(self.controller.settings.get("last_update_check", "0"))
        except ValueError:
            last = 0
        if time.time() - last < 24 * 3600:
            return
        self.manual_update_check = False
        self.controller.check_updates()

    def check_updates_manual(self):
        self.manual_update_check = True
        self.controller.tell("正在检查更新…")
        self.controller.check_updates(manual=True)

    def updates_up_to_date(self):
        import time
        self.controller.settings["last_update_check"] = str(int(time.time()))
        self.controller.save()
        if getattr(self, "manual_update_check", False):
            self.controller.tell("已经是最新版本。")

    def update_failed(self, text):
        if getattr(self, "manual_update_check", False):
            self.controller.fail(text)

    def offer_update(self, update):
        import time
        version, _ = self.controller.local_version()
        self.controller.settings["last_update_check"] = str(int(time.time()))
        self.controller.save()
        if self.controller.settings.get("update_offered") == update.version:
            return
        notes = update.notes.strip()
        answer = QMessageBox.question(
            self, "发现新版本",
            f"发现新版本 {update.version}（当前 {version}）。" + (f"\n\n{notes}\n\n" if notes else "\n\n")
            + "现在下载并安装？安装时会退出应用，完成后自动重新打开。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        self.controller.settings["update_offered"] = update.version
        self.controller.save()
        if answer == QMessageBox.StandardButton.Yes:
            self.pending_update = update
            self.controller.tell("正在下载更新，完成后会自动重启应用。")
            self.controller.download_update(update)

    def install_downloaded_update(self, archive):
        import os, subprocess, tempfile
        from pathlib import Path
        from .. import updates
        try:
            updates_root = Path.home() / "Library/Caches/RemoteHub/updates"
            updates_root.mkdir(parents=True, exist_ok=True)
            workdir = Path(tempfile.mkdtemp(prefix="staged-", dir=updates_root))
            app = updates.stage_update(Path(archive), workdir)
            updates.verify_app(app, updates.CERTIFICATE_SHA1)
            script = Path(tempfile.mkdtemp(prefix="rh-updater-")) / "install.sh"
            script.write_text(updates.install_script(
                app.resolve(), Path("/Applications/10RemoteHub.app"), os.getpid(),
                workdir / "previous.app"))
            script.chmod(0o755)
            subprocess.Popen(["/bin/sh", str(script)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.controller.fail("安装更新失败：" + str(exc))
            return
        self.quit_app()

    def show_mapping(self):
        self.show_page(self.pages.index(self.mapping))

    def update_navigation(self):
        has_remote = self.controller.has_remote
        self.voice_guide.refresh()   # shown only for 「仅触发识别」
        for page in (self.mapping, self.voice_page):
            self.sidebar.nav.button(self.pages.index(page)).setEnabled(has_remote)
        if not has_remote and self.stack.currentWidget() is not None:
            current = self.pages[self.stack.currentIndex()]
            if current in (self.mapping, self.voice_page):
                self.show_page(self.pages.index(self.remotes_page))

    # -- remotes -----------------------------------------------------------------

    def _open_enrollment(self, **options):
        from ..device_probe import DeviceProbeDialog

        c = self.controller
        if self.add_dialog is not None or not self.inspector.confirm_text_saved():
            return None
        if not c.pause_remote_input():
            c.fail("当前遥控器读取组件未能释放设备，请重新检测或拔插接收器后再试。")
            c.resume_remote_input()
            return None
        reader = None if self.safe else c.enrollment_reader()
        parent = getattr(self, "onboarding", None) or self
        dialog = DeviceProbeDialog(reader, parent, connection_users=self._connection_users,
                                   usb_devices=lambda: c.monitor.devices, **options)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._enrollment_closed)
        self.add_dialog = dialog
        self._adding_new = options.get("draft") is None
        dialog.open()
        return dialog

    def _connection_users(self, device):
        return [remote.name for remote in self.controller.shared_connection_users(device)]

    def _enrollment_closed(self, result):
        self.add_dialog = None
        self.controller.resume_remote_input()
        if result and self.controller.has_remote:
            self.show_mapping()
            # A new remote goes straight on to what it still needs; the first-run guide has its own step.
            if self._adding_new and getattr(self, "onboarding", None) is None:
                QTimer.singleShot(0, self.open_setup)

    def open_setup(self, force=False):
        """The setup checklist for the current remote; unasked only when something is left to do."""
        from .setup_checklist import SetupDialog, setup_items

        if self.controller.remote is None or self.setup_dialog is not None:
            return None
        if not force and all(item.done for item in setup_items(self.controller, lambda _page: None)):
            return None
        dialog = SetupDialog(self.controller, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.navigate.connect(self._setup_navigate)
        dialog.finished.connect(self._setup_closed)
        self.setup_dialog = dialog
        dialog.open()
        return dialog

    def _setup_closed(self, _result):
        self.setup_dialog = None
        self.sidebar.refresh_device()     # permissions granted meanwhile change the "还差 N 项"

    def _setup_navigate(self, page):
        from .setup_checklist import ADD, SETTINGS, VOICE

        if page == SETTINGS:
            self.show_general()
        elif page == VOICE:
            self.show_page(self.pages.index(self.voice_page))
        elif page == ADD:
            self.add_remote()

    def check_detected_receiver(self):
        """Offer to add a plugged-in receiver or paired Bluetooth remote that is in the
        catalog but not accepted by any saved remote."""
        from .. import models

        c = self.controller
        catalog = [m for m in models.load() if m.confirmation_button]
        present = {(d.vendor, d.product) for d in c.monitor.devices}
        accepted = {(conn.vendor, conn.product) for remote in c.remotes for conn in remote.connections
                    if conn.transport == "USB"}
        found, kind = None, ""
        for model in catalog:
            if (model.transport == models.USB_RECEIVER and model.pair in present and model.pair not in accepted
                    and model.id not in self.dismissed_models):
                found, kind = model, "接收器"
                break
        if found is None:
            for device in c.discovered:
                if device.transport != "蓝牙" or any(remote.accepts(device) for remote in c.remotes):
                    continue
                match = next((m for m in models.enrollment_candidates(device, catalog)
                              if m.id not in self.dismissed_models), None)
                if match is not None:
                    found, kind = match, "蓝牙遥控器"
                    break
        self.detected_model = found
        if found is not None:
            self.detected_text.setText(
                f"检测到可直接接入的{kind}（{found.vendor:04X}:{found.product:04X}），还没有添加。点「添加这只遥控器」即可导入你的照片并学习按键。"
            )
        self.detected_banner.setVisible(found is not None and self.add_dialog is None)

    def add_detected(self):
        if self.detected_model is not None:
            self.add_remote()
            self.detected_banner.hide()

    def ignore_detected(self):
        if self.detected_model is not None:
            self.dismissed_models.add(self.detected_model.id)
        self.check_detected_receiver()

    def add_remote(self):
        """Connect, try a key, identify its protocols, learn keys on a photo, then save."""
        return self._open_enrollment(install=self.controller.add_remote)

    def learn_more_keys(self, identity=None):
        from copy import deepcopy

        c = self.controller
        if identity and identity != c.store.active:
            if not self.confirm_text_saved() or not c.select_remote(identity):
                return None
        if c.remote is None or c.remote.is_fixed_model:
            c.fail("请选择一只非配套遥控器，再学习更多按键。")
            return None
        return self._open_enrollment(install=c.update_remote, device=c.hid_device, draft=deepcopy(c.remote))

    def show_onboarding_once(self):
        if self.controller.onboarding_needed():
            self.open_onboarding()

    def open_onboarding(self):
        from .onboarding import OnboardingDialog
        dialog = OnboardingDialog(self.controller, self)
        dialog.open_mapping.connect(self.show_mapping)
        dialog.add_remote.connect(self.add_remote)
        dialog.open_voice.connect(lambda: self.show_page(self.pages.index(self.voice_page)))
        dialog.open_settings.connect(self.show_general)
        self.onboarding = dialog
        try:
            dialog.exec()
        finally:
            self.onboarding = None

    def show_general(self):
        self.show_page(self.pages.index(self.general))
        self.restore_from_tray()

    def show_microphone_setup(self):
        self.show_page(self.pages.index(self.voice_page))
        area = self.stack.currentWidget()
        target = self.voice_page.microphone_setup
        # Let the newly visible page finish layout before scrolling to its action.
        QTimer.singleShot(0, lambda: area.ensureWidgetVisible(target, 0, 50))
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def add_permission_actions(self, menu):
        if sys.platform == "darwin":
            menu.addAction("辅助功能授权…", lambda: self.controller.open_permission("accessibility"))
            menu.addAction("输入监控授权…", lambda: self.controller.open_permission("input"))
        menu.addAction("查看权限与连接状态…", self.show_general)

    def show_physical_key(self, trigger, down):
        if not down or getattr(self, "onboarding", None) is not None:
            return
        button = next((b for b in self.controller.profile.buttons if b.matches_trigger(trigger)), None)
        if button is None:
            return
        runtime_layer = self.controller.active_runtime_layer()
        effective = self.controller.effective_runtime_button(button)
        unconfigured = all(action.kind == "none" for action in
                           (effective.action, effective.double_action, effective.long_action))
        if runtime_layer is not None and runtime_layer.overrides.get(button.id):
            self.controller.tell(f"已收到「{button.name}」按键（{runtime_layer.name} 层）" + ("，无操作" if unconfigured else ""))
        else:
            self.controller.tell(f"已收到「{button.name}」按键" + ("，未配置动作" if unconfigured else ""))

    def show_message(self, text, error):
        self.feedback.setProperty("error", bool(error))
        self.feedback.style().unpolish(self.feedback)
        self.feedback.style().polish(self.feedback)
        self.feedback.setText(text)

    def update_undo(self):
        state = self.controller.action_undo
        self.undo_button.setVisible(bool(state and state[0] == self.controller.profile.id))

    def _build_tray_menu(self) -> QMenu:
        c = self.controller
        menu = QMenu(self)
        menu.addAction("打开 " + APP_NAME, self.restore_from_tray)
        self.tray_remote_menu = menu.addMenu("切换遥控器")
        self.tray_remote_menu.aboutToShow.connect(self.refresh_tray_remotes)
        self.tray_remote_group = QActionGroup(self.tray_remote_menu)
        self.tray_remote_group.setExclusive(True)
        self.tray_profile_menu = menu.addMenu("配置方案")
        self.tray_profile_menu.aboutToShow.connect(self.refresh_tray_profiles)
        self.tray_profile_group = QActionGroup(self.tray_profile_menu)
        self.tray_profile_group.setExclusive(True)
        menu.addSeparator()
        menu.addAction("通用设置", self.show_general)
        self.add_permission_actions(menu.addMenu("权限设置"))
        menu.addAction("重新运行新手引导…", self.open_onboarding)
        if sys.platform == "darwin":
            menu.addAction("检查更新…", self.check_updates_manual)
        menu.addAction("停止语音", c.stop_voice)
        if sys.platform == "darwin":
            menu.addAction("卸载 " + APP_NAME + "…", self.uninstall_app)
        menu.addAction("退出", self.request_quit)
        return menu

    def refresh_tray_remotes(self):
        menu = getattr(self, "tray_remote_menu", None)
        if menu is None:
            return
        menu.clear()
        self.tray_remote_group = QActionGroup(menu)
        self.tray_remote_group.setExclusive(True)
        remotes = self.controller.remotes
        if not remotes:
            empty = menu.addAction("未添加遥控器")
            empty.setEnabled(False)
            return
        active_id = self.controller.store.active
        paused = self.controller.remote_input_paused
        for remote in remotes:
            act = menu.addAction(remote.name)
            act.setCheckable(True)
            act.setChecked(remote.id == active_id)
            act.setEnabled(not paused)
            self.tray_remote_group.addAction(act)
            act.triggered.connect(lambda checked=False, ident=remote.id: self.select_remote_from_tray(ident))

    def select_remote_from_tray(self, identity: str):
        if identity == self.controller.store.active:
            self.refresh_tray_remotes()
            return
        if self.isVisible() and not self.sidebar.confirm_text_saved():
            self.refresh_tray_remotes()
            return
        if not self.controller.select_remote(identity):
            self.refresh_tray_remotes()
            return
        remotes = self.controller.remotes
        idx = next((i for i, r in enumerate(remotes) if r.id == self.controller.store.active), -1)
        if 0 <= idx < len(remotes):
            badge = f"{idx + 1}/{len(remotes)}"
            self.controller.overlays.show_toast(remotes[idx].name, subtitle="已切换遥控器", badge=badge)
        self.refresh_tray_remotes()
        self.refresh_tray_profiles()

    def refresh_tray_profiles(self):
        menu = getattr(self, "tray_profile_menu", None)
        if menu is None:
            return
        menu.clear()
        self.tray_profile_group = QActionGroup(menu)
        self.tray_profile_group.setExclusive(True)
        names = self.controller.profile_names()
        for idx, name in enumerate(names):
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(idx == self.controller.index)
            self.tray_profile_group.addAction(act)
            act.triggered.connect(lambda checked=False, i=idx: self.select_profile_from_tray(i))

    def select_profile_from_tray(self, index: int):
        if index == self.controller.index:
            self.refresh_tray_profiles()
            return
        if self.isVisible() and not self.sidebar.confirm_text_saved():
            self.refresh_tray_profiles()
            return
        self.controller.select_profile(index)
        names = self.controller.profile_names()
        if names and 0 <= self.controller.index < len(names):
            badge = f"{self.controller.index + 1}/{len(names)}"
            self.controller.overlays.show_toast(names[self.controller.index], subtitle="已切换配置方案", badge=badge)

    def update_tray(self):
        if self.tray:
            names = self.controller.profile_names()
            scheme = names[self.controller.index] if len(names) > 1 else ""
            view = (self.controller.device_view() if self.controller.uses_dedicated_reader()
                    else self.controller.remote_view())
            parts = [APP_NAME] + ([scheme] if scheme else []) + [view.title]
            self.tray.setToolTip(" · ".join(parts))

    def shutdown(self):
        self.general.close_permission_overlay()
        self.cheat_sheet_osd.close()
        self.grid_launcher_osd.close()
        self.profile_toast_osd.close()
        self.nine_key_osd.close()
        self.voice_osd.close()
        self.controller.shutdown()
        if self.tray:
            self.tray.hide()

    def uninstall_app(self):
        """Quit first, then let Installer remove the App and its system pieces."""
        from PySide6.QtWidgets import QMessageBox
        from ..devices import resources
        from ..installers import open_package
        package = resources() / "components" / "RemoteHub-Uninstall.pkg"
        if not package.is_file():
            self.controller.fail("当前版本没有附带卸载程序，请从发布页下载卸载包。")
            return
        answer = QMessageBox.question(
            self, APP_NAME,
            "将退出 " + APP_NAME + " 并打开卸载程序。\n\n"
            "按键映射和语音设置会保留。卸载会删除主程序、后台读取组件和虚拟麦克风。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            open_package(package)
        except Exception as exc:
            self.controller.fail("无法打开卸载程序：" + str(exc))
            return
        # Installer refuses to run while the App is open; exit right after it starts.
        QTimer.singleShot(800, self.quit_app)

    def quit_app(self):
        if self._relaunch_pending:
            from ..relaunch import schedule_relaunch

            self._relaunch_pending = False      # one waiter per request, even if quit runs twice
            if not schedule_relaunch():
                self.controller.fail("无法自动重新打开，请退出后手动打开 " + APP_NAME + "。")
                return
        self.shutdown()
        QApplication.quit()

    def relaunch(self):
        """Quit through the normal path, then let a detached waiter open the app again."""
        self._relaunch_pending = True
        if not self.request_quit():
            self._relaunch_pending = False

    def request_quit(self):
        """Quit from the Dock or tray: close open dialogs first. A dialog with unsaved
        keys asks before closing; answering「取消」keeps the app running."""
        from PySide6.QtWidgets import QDialog

        for _ in range(10):
            dialog = QApplication.activeModalWidget()
            if dialog is None:
                break
            dialog.close()
            if dialog.isVisible():
                return False
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QDialog) and widget.isVisible():
                widget.close()
                if widget.isVisible():
                    return False
        QTimer.singleShot(0, self.quit_app)   # let closed dialogs unwind first
        return True

    def showNormal(self):
        set_dock_visible(True)
        super().showNormal()

    def restore_from_tray(self):
        """Restore the window from the system tray while preserving WindowMaximized."""
        set_dock_visible(True)
        was_maximized = bool(self.isMaximized() or (self.windowState() & Qt.WindowState.WindowMaximized))
        if was_maximized:
            self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
            self.show()
        else:
            super().showNormal()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self):
        self.hide()
        set_dock_visible(False)

    def closeEvent(self, event):
        if self.tray and not self.controller.shutting_down:
            self.hide_to_tray()
            event.ignore()
        else:
            self.shutdown()
            event.accept()
