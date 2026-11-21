"""Dialogs for picking macOS Accessibility UI controls and menu bar commands."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QVBoxLayout,
)

from ..app_layers import layer_app_from_path, running_layer_apps
from ..ax_actions import (
    CapturedControl, MenuCommand, find_interactive_candidate, is_menubar_element,
    list_app_menus, sanitize_control_label,
)
from ..chat_focus import App
from ..model import Action, normalize_app_layer_id
from ..presentation import ui_role_label
from ..theme import repolish, stylesheet
from .widgets import ScrollSafeComboBox, button, label, row, set_role

AUTO_FRONT_APP_ID = "*"


def _default_running_apps() -> list[App]:
    if sys.platform not in ("darwin", "win32"):
        return []
    result: list[App] = []
    seen: set[str] = set()
    for item in running_layer_apps(sys.platform):
        key = item.app_id.casefold()
        if key not in seen:
            seen.add(key)
            result.append(App(item.app_id, 0, item.name))
    if sys.platform == "win32":
        from ..win_uia import discover_start_menu_apps
        for inst in discover_start_menu_apps():
            key = inst.app_id.casefold()
            if key not in seen:
                seen.add(key)
                result.append(App(inst.app_id, 0, inst.name))
    return result


def _browse_windows_exe(parent) -> App | None:
    start_dir = ""
    for candidate in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path.home(),
    ):
        if candidate.is_dir():
            start_dir = str(candidate)
            break
    chosen, _ = QFileDialog.getOpenFileName(
        parent,
        "选择目标软件（.exe 或快捷方式 .lnk）",
        start_dir,
        "应用程序与快捷方式 (*.exe *.lnk);;所有文件 (*)",
    )
    if not chosen:
        return None
    layer_app = layer_app_from_path(chosen, platform="win32")
    return App(layer_app.app_id, 0, layer_app.name)


class UiControlPickerDialog(QDialog):
    """Hover-to-capture picker for learning a clickable button or focusable area."""

    _probe_ready = Signal(int, object, object)  # token, payload_or_None, error_str_or_None

    def __init__(
        self,
        initial: Action | None = None,
        parent=None,
        *,
        default_app_id: str = "",
        default_app_name: str = "",
        apps_provider=None,
        backend_factory=None,
    ):
        super().__init__(parent)
        self._apps_provider = apps_provider or _default_running_apps
        self._backend_factory = backend_factory
        self._backend = None
        self._executor = None
        self._probe_busy = False
        self._capturing = False
        self._capture_token = 0
        self._capture_left = 0.0
        self._capture_message = ""
        self._hover_sig = ""
        self._hover_point = None
        self._hover_streak = 0
        self._captured: CapturedControl | None = None
        self._result_action: Action | None = None

        self._default_app_id = normalize_app_layer_id(default_app_id)
        self._default_app_name = default_app_name

        if initial is not None and initial.kind == "ui_element":
            spec = initial.spec or {}
            app_name = (spec.get("app_name") or initial.application).strip()
            self._default_app_id = normalize_app_layer_id(initial.application) or self._default_app_id
            self._default_app_name = app_name or self._default_app_name
            self._captured = CapturedControl(
                app=App(initial.application, 0, app_name),
                op=initial.target if initial.target in ("press", "focus") else "press",
                role=str(spec.get("role") or ""),
                label=str(spec.get("label") or ""),
                signature=str(spec.get("signature") or ""),
            )

        self.setWindowTitle("指定界面控件")
        self.setModal(True)
        self.resize(540, 460)
        self.setMinimumSize(480, 430)
        self.setStyleSheet(stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        root.addWidget(label("选择无快捷键按钮或可聚焦区域", "heading"))
        root.addWidget(label(
            "指向目标软件中的按钮或滚动/输入区域即可学下结构指纹，悬停保持约 1 秒自动锁定，不需要真正点击触发。",
            "caption",
        ))

        self.app_box = ScrollSafeComboBox()
        self.app_box.setAccessibleName("目标应用")
        self.refresh_apps_button = button("刷新应用", self.refresh_apps)

        self.op_box = ScrollSafeComboBox()
        self.op_box.setAccessibleName("操作方式")
        press_title = (
            "点击按钮 / 开关（AXPress，无需快捷键）"
            if sys.platform == "darwin"
            else "点击按钮 / 开关（UI 自动化直点，无需快捷键）"
        )
        self.op_box.addItem(press_title, "press")
        self.op_box.addItem("聚焦 / 锁定区域（方便滚轮与方向键翻页）", "focus")
        if self._captured is not None:
            idx = self.op_box.findData(self._captured.op)
            if idx >= 0:
                self.op_box.setCurrentIndex(idx)
        self.op_box.currentIndexChanged.connect(self._on_op_changed)

        fields = QGridLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setHorizontalSpacing(8)
        fields.setVerticalSpacing(10)
        fields.setColumnStretch(1, 1)
        fields.addWidget(label("目标应用", "caption", wrap=False), 0, 0)
        fields.addWidget(self.app_box, 0, 1)
        fields.addWidget(label("执行动作", "caption", wrap=False), 1, 0)
        fields.addWidget(self.op_box, 1, 1)
        root.addLayout(fields)

        self.capture_button = button("开始悬停捕捉（20 秒）", self.toggle_capture, "primary")
        self.capture_hint = label("", "caption")
        self.browse_exe_button = button("选择 EXE…", self.browse_target_app) if sys.platform == "win32" else None
        action_controls = [self.capture_button, self.refresh_apps_button]
        if self.browse_exe_button is not None:
            action_controls.append(self.browse_exe_button)
        action_controls.append(None)
        root.addWidget(row(*action_controls, spacing=8))
        self.status_banner = label("", "caption")
        self.status_banner.setObjectName("banner")
        self.status_banner.setMinimumHeight(48)
        root.addWidget(self.status_banner)

        self.detail_label = label("尚未指定控件", "value")
        root.addWidget(self.detail_label)

        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(80)
        self.name_edit.setPlaceholderText("显示名称，例如：下一页 / 新建对话 / 聊天列表")
        self.name_edit.setAccessibleName("控件显示名称")
        if self._captured is not None:
            self.name_edit.setText(self._captured.label)
        root.addWidget(row(label("显示名称", "caption", wrap=False), self.name_edit, spacing=8))

        self.error_label = label("", wrap=True)
        self.error_label.setObjectName("inlineError")
        self.error_label.hide()
        root.addWidget(self.error_label)

        root.addStretch(1)

        self.cancel_button = button("取消", self.reject)
        self.save_button = button("保存控件动作", self._accept_action, "primary")
        self.save_button.setDefault(True)
        root.addWidget(row(None, self.cancel_button, self.save_button, spacing=8))

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(180)
        self.poll_timer.timeout.connect(self._poll_tick)
        self._probe_ready.connect(self._on_probe_ready)

        self.refresh_apps()
        self._refresh_state()

    def result_action(self) -> Action | None:
        return self._result_action

    def browse_target_app(self):
        try:
            picked = _browse_windows_exe(self)
        except Exception as exc:
            self._show_error(str(exc))
            return
        if picked is None:
            return
        self.error_label.hide()
        idx = self.app_box.findData(picked.bundle)
        if idx < 0:
            self.app_box.addItem(picked.name or picked.bundle, picked.bundle)
            idx = self.app_box.count() - 1
        self.app_box.setCurrentIndex(idx)

    def refresh_apps(self):
        current_id = self.app_box.currentData() or self._default_app_id
        apps = list(self._apps_provider())
        self.app_box.blockSignals(True)
        self.app_box.clear()
        self.app_box.addItem("✦ 自动识别切到的前台软件（支持任意 EXE）", AUTO_FRONT_APP_ID)
        known = set()
        for app in apps:
            norm = normalize_app_layer_id(app.bundle)
            if not norm or norm.casefold() in known:
                continue
            known.add(norm.casefold())
            self.app_box.addItem(app.name or norm, norm)
        if self._default_app_id and self._default_app_id.casefold() not in known:
            self.app_box.addItem(self._default_app_name or self._default_app_id, self._default_app_id)
        if current_id:
            for i in range(self.app_box.count()):
                if str(self.app_box.itemData(i)).casefold() == str(current_id).casefold():
                    self.app_box.setCurrentIndex(i)
                    break
        self.app_box.blockSignals(False)

    def _on_op_changed(self):
        if self._capturing:
            self.stop_capture()
        if self._captured is not None and self._captured.op != self.op_box.currentData():
            self._captured = None
            self.name_edit.clear()
        self._refresh_state()

    def toggle_capture(self):
        if self._capturing:
            self.stop_capture()
        else:
            self.start_capture()

    def start_capture(self):
        app_id = self.app_box.currentData() or ""
        if not app_id:
            self._show_error("请先选择目标应用（先打开该应用再点「刷新应用」）。")
            return
        self.error_label.hide()
        self._capturing = True
        self._capture_token += 1
        self._capture_left = 20.0
        target_name = (
            "目标软件窗口"
            if app_id == AUTO_FRONT_APP_ID
            else (self.app_box.currentText() or "目标应用")
        )
        self._capture_message = f"请切到「{target_name}」，把鼠标停在目标控件上约 1 秒"
        self._hover_sig = ""
        self._hover_point = None
        self._hover_streak = 0
        self.poll_timer.start()
        self._refresh_capture_banner(self._capture_message)
        self._refresh_state()

    def stop_capture(self):
        self._capturing = False
        self._capture_token += 1
        self.poll_timer.stop()
        self._refresh_state()

    def _ensure_backend(self):
        if self._backend is None:
            if self._backend_factory is not None:
                self._backend = self._backend_factory()
            else:
                from ..ax_actions import create_ax_action_backend
                self._backend = create_ax_action_backend()
        return self._backend

    def _poll_tick(self):
        if not self._capturing:
            return
        self._capture_left = max(0.0, self._capture_left - 0.18)
        if self._capture_left <= 0.0:
            self._capture_message = "捕捉超时，请点「开始悬停捕捉」重试。"
            self.stop_capture()
            self.status_banner.setText(self._capture_message)
            return
        if self._probe_busy:
            self._refresh_capture_banner()
            return
        target_app_id = str(self.app_box.currentData() or "")
        op = str(self.op_box.currentData() or "press")
        token = self._capture_token
        self._probe_busy = True

        def job():
            backend = self._ensure_backend()
            front = backend.frontmost()
            if front is None:
                return None, None
            if target_app_id != AUTO_FRONT_APP_ID and (
                normalize_app_layer_id(front.bundle).casefold() != normalize_app_layer_id(target_app_id).casefold()
            ):
                return ("wrong_app", front.name), None
            pt = backend.mouse_point()
            backend.prepare(front, lambda: False)
            elem = backend.element_at_cursor(front)
            if elem is not None and is_menubar_element(backend, elem):
                return ("menubar", front.name), None
            window = (backend.read(elem, "AXWindow") if elem is not None else None) or backend.window(front)
            if window is None:
                return ("no_window", front.name), None
            found = find_interactive_candidate(backend, elem, window, op)
            if found is None:
                return ("no_match", front.name, pt), None
            _node, role, detected_label, sig = found
            return ("candidate", front, op, role, detected_label, sig, pt), None

        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ax-capture")
        fut = self._executor.submit(job)

        def done(future):
            try:
                payload, err = future.result()
            except Exception as exc:
                payload, err = None, str(exc)
            try:
                self._probe_ready.emit(token, payload, err)
            except RuntimeError:
                pass

        fut.add_done_callback(done)
        self._refresh_capture_banner()

    def _on_probe_ready(self, token: int, payload, error: str | None):
        self._probe_busy = False
        if not self._capturing or token != self._capture_token:
            return
        if error:
            self.stop_capture()
            self._show_error(error)
            return
        if payload is None:
            self._hover_sig = ""
            self._hover_streak = 0
            target_name = self.app_box.currentText() or "目标应用"
            self._refresh_capture_banner(f"请切到「{target_name}」，把鼠标停在目标控件上约 1 秒")
            return
        kind = payload[0]
        if kind == "wrong_app":
            self._hover_sig = ""
            self._hover_streak = 0
            target_name = self.app_box.currentText()
            self._refresh_capture_banner(f"等待切换到「{target_name}」（当前在前台：{payload[1]}）")
            return
        if kind == "no_window":
            self._hover_sig = ""
            self._hover_streak = 0
            self._refresh_capture_banner(f"已切换到「{payload[1]}」，等待应用窗口就绪…")
            return
        if kind == "menubar":
            self._hover_sig = ""
            self._hover_streak = 0
            self._refresh_capture_banner("检测到顶部菜单栏项，请改用「应用菜单命令」选择，无需悬停捕捉")
            return
        if kind == "no_match":
            self._hover_sig = ""
            self._hover_streak = 0
            op_name = "可点击控件" if self.op_box.currentData() == "press" else "可聚焦区域"
            self._refresh_capture_banner(f"未识别到唯一{op_name}，请将鼠标正对按钮后再停顿约 1 秒")
            return
        if kind == "candidate":
            _k, front, op, role, detected_label, sig, pt = payload
            still = (
                self._hover_sig == sig
                and self._hover_point is not None
                and abs(pt[0] - self._hover_point[0]) <= 8.0
                and abs(pt[1] - self._hover_point[1]) <= 8.0
            )
            self._hover_sig = sig
            self._hover_point = pt
            self._hover_streak = (self._hover_streak + 1) if still else 1
            role_title = ui_role_label(role)
            if self._hover_streak >= 4:
                self._captured = CapturedControl(
                    app=front, op=op, role=role, label=detected_label, signature=sig
                )
                self.name_edit.setText(detected_label)
                self._capture_message = f"已自动锁定「{detected_label}」（{role_title}）。可修改显示名称后点击保存。"
                self.stop_capture()
                self.status_banner.setText(self._capture_message)
                self.raise_()
                self.activateWindow()
                return
            self._refresh_capture_banner(
                f"识别到 {role_title}「{detected_label}」— 保持不动自动锁定（{self._hover_streak}/4）"
            )

    def _refresh_capture_banner(self, custom_text: str = ""):
        if custom_text:
            self._capture_message = custom_text
        msg = self._capture_message or f"请切到「{self.app_box.currentText() or '目标应用'}」，把鼠标停在目标控件上约 1 秒"
        text = f"{msg}（剩余 {int(self._capture_left) + 1}s）"
        if self.status_banner.text() != text:
            self.status_banner.setText(text)

    def _refresh_state(self):
        self.capture_button.setText("停止捕捉" if self._capturing else "开始悬停捕捉（20 秒）")
        set_role(self.capture_button, "pending" if self._capturing else "primary")
        repolish(self.capture_button)
        self.app_box.setEnabled(not self._capturing)
        self.refresh_apps_button.setEnabled(not self._capturing)
        if not self._capturing and not self.status_banner.text():
            self.status_banner.setText("点击上方按钮后，切到目标软件把鼠标停在控件上即可自动锁定（无需真点击）。")
        if self._captured is None:
            self.detail_label.setText("尚未指定界面控件")
            self.save_button.setEnabled(False)
        else:
            op_name = "聚焦区域" if self._captured.op == "focus" else "点击控件"
            role_title = ui_role_label(self._captured.role)
            app_name = self._captured.app.name or self._captured.app.bundle
            self.detail_label.setText(f"已定位：{app_name} · {op_name} · {role_title}「{self._captured.label}」")
            self.save_button.setEnabled(not self._capturing)

    def _show_error(self, msg: str):
        self.error_label.setText(msg)
        self.error_label.show()

    def _accept_action(self):
        if self._captured is None:
            self._show_error("请先捕捉一个界面控件。")
            return
        app_id = normalize_app_layer_id(self._captured.app.bundle)
        app_name = (self._captured.app.name or app_id).strip()[:100]
        label_text = sanitize_control_label(self.name_edit.text()) or self._captured.label
        action = Action(
            kind="ui_element",
            target=self._captured.op,
            application=app_id,
            spec={
                "app_name": app_name,
                "role": self._captured.role,
                "label": label_text,
                "signature": self._captured.signature,
            },
        )
        try:
            action.validate(sys.platform)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._result_action = action
        self._cleanup()
        self.accept()

    def _cleanup(self):
        self.stop_capture()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def closeEvent(self, event):
        self._cleanup()
        super().closeEvent(event)

    def reject(self):
        self._cleanup()
        super().reject()


class AppMenuPickerDialog(QDialog):
    """Searchable picker for reading and choosing a menu bar command of a macOS app."""

    def __init__(
        self,
        initial: Action | None = None,
        parent=None,
        *,
        default_app_id: str = "",
        default_app_name: str = "",
        apps_provider=None,
        menu_loader=None,
    ):
        super().__init__(parent)
        self._apps_provider = apps_provider or _default_running_apps
        self._menu_loader = menu_loader
        self._commands: list[MenuCommand] = []
        self._current_app: App | None = None
        self._result_action: Action | None = None

        self._default_app_id = normalize_app_layer_id(default_app_id)
        self._default_app_name = default_app_name
        initial_path: tuple[str, ...] = ()
        restrict_app = True

        if initial is not None and initial.kind == "app_menu":
            spec = initial.spec or {}
            initial_path = tuple(str(p).strip() for p in spec.get("path", []) if str(p).strip())
            if initial.application:
                self._default_app_id = normalize_app_layer_id(initial.application)
                self._default_app_name = str(spec.get("app_name") or initial.application)
                restrict_app = True
            else:
                restrict_app = False

        self.setWindowTitle("选择应用菜单命令")
        self.setModal(True)
        self.resize(560, 460)
        self.setMinimumSize(480, 380)
        self.setStyleSheet(stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        root.addWidget(label("从应用顶部菜单栏选择命令", "heading"))
        root.addWidget(label("调用软件原生菜单项（如 显示 → 隐藏侧边栏），不依赖也不占用键盘快捷键。", "caption"))

        self.app_box = ScrollSafeComboBox()
        self.app_box.setAccessibleName("目标应用")
        self.app_box.activated.connect(self.reload_menus)
        self.refresh_button = button("刷新菜单", self.reload_menus)
        root.addWidget(row(label("目标应用", "caption", wrap=False), self.app_box, spacing=8))

        self.restrict_check = QCheckBox("仅在此应用在前台时执行（推荐）")
        self.restrict_check.setChecked(restrict_app)
        self.browse_exe_button = button("选择 EXE…", self.browse_menu_app) if sys.platform == "win32" else None
        menu_bar_controls = [self.restrict_check, None]
        if self.browse_exe_button is not None:
            menu_bar_controls.append(self.browse_exe_button)
        menu_bar_controls.append(self.refresh_button)
        root.addWidget(row(*menu_bar_controls, spacing=8))

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入关键词过滤菜单，例如：边栏、导出、新建、粘贴…")
        self.search_edit.setAccessibleName("搜索菜单命令")
        self.search_edit.textChanged.connect(self._populate_list)
        root.addWidget(self.search_edit)

        self.menu_list = QListWidget()
        self.menu_list.setAccessibleName("可用菜单命令列表")
        self.menu_list.itemDoubleClicked.connect(lambda _item: self._accept_action())
        self.menu_list.currentItemChanged.connect(lambda *_: self._sync_save_enabled())
        root.addWidget(self.menu_list, 1)

        self.status_label = label("", "caption")
        root.addWidget(self.status_label)

        self.error_label = label("", wrap=True)
        self.error_label.setObjectName("inlineError")
        self.error_label.hide()
        root.addWidget(self.error_label)

        self.cancel_button = button("取消", self.reject)
        self.save_button = button("保存菜单命令", self._accept_action, "primary")
        self.save_button.setDefault(True)
        root.addWidget(row(None, self.cancel_button, self.save_button, spacing=8))

        self._initial_path = initial_path
        self.refresh_apps()
        self.reload_menus()

    def result_action(self) -> Action | None:
        return self._result_action

    def browse_menu_app(self):
        try:
            picked = _browse_windows_exe(self)
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        if picked is None:
            return
        self.error_label.hide()
        idx = self.app_box.findData(picked.bundle)
        if idx < 0:
            self.app_box.addItem(picked.name or picked.bundle, picked.bundle)
            idx = self.app_box.count() - 1
        self.app_box.setCurrentIndex(idx)
        self.reload_menus()

    def refresh_apps(self):
        current_id = self.app_box.currentData() or self._default_app_id
        apps = list(self._apps_provider())
        self.app_box.blockSignals(True)
        self.app_box.clear()
        known = set()
        for app in apps:
            norm = normalize_app_layer_id(app.bundle)
            if not norm or norm.casefold() in known:
                continue
            known.add(norm.casefold())
            self.app_box.addItem(app.name or norm, norm)
        if self._default_app_id and self._default_app_id.casefold() not in known:
            self.app_box.addItem(self._default_app_name or self._default_app_id, self._default_app_id)
        if current_id:
            for i in range(self.app_box.count()):
                if str(self.app_box.itemData(i)).casefold() == str(current_id).casefold():
                    self.app_box.setCurrentIndex(i)
                    break
        self.app_box.blockSignals(False)

    def reload_menus(self):
        self.error_label.hide()
        app_id = str(self.app_box.currentData() or "")
        if not app_id:
            self._commands = []
            self._current_app = None
            self.status_label.setText("请先打开目标应用，再点击「刷新菜单」。")
            self._populate_list()
            return
        try:
            if self._menu_loader is not None:
                app, commands = self._menu_loader(app_id)
            else:
                from ..ax_actions import create_ax_action_backend
                app, commands = list_app_menus(create_ax_action_backend(), app_id)
        except Exception as exc:
            self._commands = []
            self._current_app = App(app_id, 0, self.app_box.currentText() or app_id)
            self._populate_list()
            self.error_label.setText(str(exc))
            self.error_label.show()
            self.status_label.setText("")
            return
        self._current_app = app
        self._commands = list(commands)
        self.status_label.setText(f"已读取「{app.name or app.bundle}」的 {len(self._commands)} 条菜单命令")
        self._populate_list()

    def _populate_list(self):
        needle = self.search_edit.text().strip().casefold()
        selected_path = None
        current = self.menu_list.currentItem()
        if current is not None:
            selected_path = current.data(Qt.ItemDataRole.UserRole)
        elif self._initial_path:
            selected_path = self._initial_path
            self._initial_path = ()

        self.menu_list.clear()
        for cmd in self._commands:
            text = " → ".join(cmd.path)
            if needle and needle not in text.casefold():
                continue
            label_str = text if cmd.enabled else f"{text}（当前灰显，使用时可用即会触发）"
            item = QListWidgetItem(label_str)
            item.setData(Qt.ItemDataRole.UserRole, cmd.path)
            self.menu_list.addItem(item)
            if selected_path and tuple(selected_path) == cmd.path:
                self.menu_list.setCurrentItem(item)
        if self.menu_list.currentItem() is None and self.menu_list.count() > 0:
            self.menu_list.setCurrentRow(0)
        self._sync_save_enabled()

    def _sync_save_enabled(self):
        self.save_button.setEnabled(self.menu_list.currentItem() is not None)

    def _accept_action(self):
        item = self.menu_list.currentItem()
        if item is None:
            self.error_label.setText("请先从列表中选择一条菜单命令。")
            self.error_label.show()
            return
        path = list(item.data(Qt.ItemDataRole.UserRole) or ())
        app_id = normalize_app_layer_id(str(self.app_box.currentData() or ""))
        app_name = (
            (self._current_app.name if self._current_app else self.app_box.currentText()) or app_id
        ).strip()[:100]
        restrict = self.restrict_check.isChecked()
        action = Action(
            kind="app_menu",
            application=app_id if restrict else "",
            spec={
                "app_name": app_name if restrict else "",
                "path": path,
            },
        )
        try:
            action.validate(sys.platform)
        except ValueError as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        self._result_action = action
        self.accept()
