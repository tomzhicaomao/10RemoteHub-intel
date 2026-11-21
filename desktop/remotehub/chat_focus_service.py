"""Qt facade with one bounded background AX job at a time."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from threading import Event
from pathlib import Path
import sys

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from .chat_focus import FocusEngine, Preferences, ensure_focused_for_voice
from .chat_focus_log import FocusLog


class ChatFocusService(QObject):
    configuration_changed = Signal()
    updated = Signal()
    _completed = Signal(object)
    _voice_completed = Signal(object, str, object)
    _activated = Signal()

    def __init__(self, directory, parent=None, *, live=True, backend=None):
        super().__init__(parent)
        self.preferences = Preferences(directory)
        self._log = FocusLog(Path(directory) / "logs")
        self._log.write("started", enabled=self.preferences.data["enabled"])
        self.supported = sys.platform in ("darwin", "win32") or backend is not None
        self.live = live
        self.message = self.preferences.error
        self.state = {"states": {}, "selecting": "", "candidate_ready": False, "seconds_left": 0}
        self._backend = backend
        self._observer = None
        self._engine = None
        self._executor = None
        self._busy = False
        self._closed = False
        self._needs_polling = bool(self.preferences.data["enabled"] and self._has_enabled_apps())
        self._wake_pending = False
        self._cancel = Event()
        self._commands = []
        self._voice_callbacks = {}
        self._completed.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self._voice_completed.connect(self._on_voice_completed, Qt.ConnectionType.QueuedConnection)
        self._activated.connect(self._wake, Qt.ConnectionType.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self._drain)
        self._sync_schedule()

    @property
    def data(self):
        return self.preferences.data

    def _has_enabled_apps(self):
        return any(entry.get("enabled") for entry in self.preferences.data["apps"].values())

    def _active_watch(self):
        if self._closed or not self.supported or not self.live or self.preferences.error:
            return False
        return bool(self._commands or self.state["selecting"]
                    or (self.data["enabled"] and self._has_enabled_apps()))

    def _event_driven(self):
        return self._observer is not None or bool(getattr(self._backend, "notifies_activation", False))

    def _frontmost_app(self):
        return self._observer.current_app if self._observer is not None else None

    def _sync_schedule(self):
        if not self._active_watch():
            self.timer.stop()
            if self._observer is not None:
                self._observer.close()
                self._observer = None
            return
        if self._observer is None and self._backend is None:
            if sys.platform == "darwin":
                from .mac_chat_focus import MacWorkspaceObserver
                self._observer = MacWorkspaceObserver(self._activated.emit)
            elif sys.platform == "win32":
                from .win_uia import WinEventObserver
                self._observer = WinEventObserver(self._activated.emit)
        should_poll = (not self._event_driven()) or self._needs_polling or bool(self._commands)
        if should_poll:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()

    def _wake(self):
        if self._closed or not self.supported or not self.live or self.preferences.error:
            return
        if self._busy:
            self._wake_pending = True
            return
        self._needs_polling = True
        self._sync_schedule()
        self._drain()

    def applications(self):
        if not self.supported or not self.live:
            return []
        if self._backend is not None:
            return self._backend.applications()
        if sys.platform == "win32":
            from .win_uia import win_chat_focus_candidates
            return win_chat_focus_candidates()
        from .mac_chat_focus import running_apps
        return running_apps()

    def _change(self, mutation, *, trigger=True):
        data = deepcopy(self.data)
        mutation(data)
        self.preferences.save(data)  # Failure leaves config and runtime unchanged.
        self._invalidate()
        self.message = ""
        self._needs_polling = bool(self.data["enabled"] and self._has_enabled_apps()) or bool(self.state["selecting"])
        self._sync_schedule()
        self.configuration_changed.emit()
        self.updated.emit()
        if trigger and self._active_watch():
            self._drain()

    def _invalidate(self):
        self._cancel.set()
        self._cancel = Event()

    def enable(self, value):
        self._change(lambda data: data.update(enabled=bool(value)), trigger=bool(value))
        if not value:
            if self._engine is not None:
                self._commands = [("cancel", "")]
                self._sync_schedule()
                self._drain()
            else:
                self._commands.clear()
                self._sync_schedule()

    def add(self, app):
        if app.bundle in self.data["apps"]:
            return
        self._change(lambda data: data["apps"].update({app.bundle: {"name": app.name, "mode": "auto", "enabled": True}}))

    def remove(self, bundle):
        self._change(lambda data: data["apps"].pop(bundle, None))
        if self.state["selecting"] == bundle:
            self.cancel_binding()

    def set_app_enabled(self, bundle, value):
        self._change(lambda data: data["apps"][bundle].update(enabled=bool(value)))
        if not value and self.state["selecting"] == bundle:
            self.cancel_binding()

    def begin_binding(self, bundle):
        if not self.live or not self.supported:
            raise ValueError("当前预览模式不会读取或聚焦外部应用")
        if bundle not in self.data["apps"]:
            raise ValueError("请先添加应用")
        self._invalidate()
        self._commands = [("begin", bundle)]
        self._needs_polling = True
        self.message = "切到目标应用，点聊天输入框；再返回这里点击「确认指定」。30 秒内可重新选择。"
        self._sync_schedule()
        self.updated.emit()
        self._drain()

    def confirm_binding(self):
        bundle = self.state["selecting"]
        if not bundle or not self.state["candidate_ready"]:
            raise ValueError("还没有取得聊天输入框")
        self._change(lambda data: data["apps"][bundle].update(mode="manual"), trigger=False)
        self._commands = [("confirm", bundle)]
        self._needs_polling = True
        self._sync_schedule()
        self._drain()

    def cancel_binding(self):
        self._invalidate()
        self._commands = [("cancel", "")]
        self.message = "已取消指定"
        self._sync_schedule()
        self.updated.emit()
        self._drain()

    def _drain(self):
        if self._closed or self._busy or not self.supported or not self.live or self.preferences.error:
            return
        if not self.data["enabled"] and not self._commands and not self.state["selecting"]:
            self._sync_schedule()
            return  # Default-off never instantiates the native backend.
        if not self._has_enabled_apps() and not self._commands and not self.state["selecting"]:
            self._sync_schedule()
            return
        self._wake_pending = False
        if self._observer is not None:
            self._observer.refresh()
        command = self._commands.pop(0) if self._commands else ("tick", "")
        token, preferences = self._cancel, deepcopy(self.data)
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-focus")
        self._busy = True
        future = self._executor.submit(self._work, command, preferences, token)

        def done(future):
            try:
                result = future.result()
                self._completed.emit((token, result))
            except Exception:
                # The QObject may have been deleted during application shutdown.
                pass
        future.add_done_callback(done)

    def _platform_context(self):
        if sys.platform == "darwin":
            import objc
            return objc.autorelease_pool()
        if sys.platform == "win32":
            from .win_uia import ensure_thread_com
            ensure_thread_com()
            return nullcontext()
        return nullcontext()

    def _create_default_backend(self):
        provider = self._frontmost_app if self._observer is not None else None
        if sys.platform == "win32":
            from .win_uia import WinUIAFocusBackend
            return WinUIAFocusBackend(frontmost_provider=provider)
        from .mac_chat_focus import MacFocusBackend
        return MacFocusBackend(frontmost_provider=provider)

    def _work(self, command, preferences, token):
        try:
            with self._platform_context():
                if token.is_set():
                    return {}, ""
                if self._engine is None:
                    if self._backend is None:
                        self._backend = self._create_default_backend()
                    self._engine = FocusEngine(self._backend, log=self._log.write)
                self._engine.configure(preferences)
                kind, bundle = command
                if kind == "begin":
                    self._engine.begin(bundle)
                elif kind == "cancel":
                    self._engine.cancel()
                elif kind == "confirm":
                    locator = self._engine.confirm(bundle)
                    state = self._engine.snapshot()
                    state["learned"] = {"bundle": bundle, "locator": locator}
                    state["polling"] = self._engine.needs_polling
                    return state, ""
                else:
                    self._engine.tick(token.is_set)
                state = self._engine.snapshot()
                state["polling"] = self._engine.needs_polling
                return state, ""
        except Exception:
            self._log.write("worker_error")
            state = dict(self._engine.snapshot() if self._engine else self.state)
            state["polling"] = self._engine.needs_polling if self._engine else False
            err_msg = (
                "无法完成操作，请检查辅助功能权限，或重新指定输入框。"
                if sys.platform == "darwin"
                else "无法完成操作，请确认目标窗口已打开且未以管理员权限运行，或重新指定输入框。"
            )
            return state, err_msg

    def _finished(self, payload):
        self._busy = False
        token, (state, message) = payload
        if self._closed:
            return
        if token is not self._cancel:
            self._sync_schedule()
            if self._commands or self._wake_pending:
                self._wake_pending = False
                self._needs_polling = True
                self._sync_schedule()
                self._drain()
            return
        previous_state, previous_message = self.state, self.message
        if state:
            state = dict(state)
            learned = state.pop("learned", None)
            self._needs_polling = bool(state.pop("polling", False))
            self.state = state
            if learned:
                bundle, locator = learned["bundle"], learned["locator"]
                def persist(data):
                    entry = data["apps"][bundle]
                    entry.pop("locator", None)
                    if locator is not None:
                        entry["locator"] = locator
                try:
                    self._change(persist, trigger=False)
                    self._log.write("binding_saved", bundle=bundle, persistent=locator is not None)
                    message = ("已保存输入框定位规则，重启后会自动重新查找。" if locator is not None else
                               "本次已指定；此输入框暂不能唯一匹配，重启后需要重新指定。")
                except (OSError, ValueError):
                    self._log.write("binding_save_failed", bundle=bundle)
                    message = "本次已指定，但定位规则保存失败；重启后不会保留这次指定。"
                previous_message = self.message
        if message:
            self.message = message
        self._sync_schedule()
        if self.state != previous_state or self.message != previous_message:
            self.updated.emit()
        if self._commands or self._wake_pending:
            self._wake_pending = False
            self._needs_polling = True
            self._sync_schedule()
            self._drain()

    def should_focus_for_voice(self) -> str | None:
        """Return enabled bundle id if frontmost app qualifies for pre-voice focus."""
        if self._closed or not self.supported or not self.live or self.preferences.error:
            return None
        if not self.data.get("enabled") or not self._has_enabled_apps():
            return None
        app = None
        if self._observer is not None:
            app = self._observer.refresh()
        elif self._backend is not None:
            app = self._backend.frontmost()
        if app is None:
            return None
        entry = self.data["apps"].get(app.bundle)
        if not entry or not entry.get("enabled"):
            return None
        return app.bundle

    def request_voice_focus(self, bundle: str, token: int, callback) -> None:
        """Run one bounded pre-voice focus check on the single AX worker and notify callback(token, status)."""
        if self._closed or not self.supported or not self.live or self.preferences.error:
            callback(token, "disabled")
            return
        self._voice_callbacks[token] = callback
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-focus")
        preferences = deepcopy(self.data)
        future = self._executor.submit(self._voice_work, bundle, preferences)

        def done(fut):
            try:
                status, state = fut.result()
            except Exception:
                status, state = "unavailable", None
            if not self._closed:
                try:
                    self._voice_completed.emit(token, status, state)
                except Exception:
                    pass

        future.add_done_callback(done)

    def _voice_work(self, bundle: str, preferences: dict):
        with self._platform_context():
            if self._engine is None:
                if self._backend is None:
                    self._backend = self._create_default_backend()
                self._engine = FocusEngine(self._backend, log=self._log.write)
            self._engine.configure(preferences)
            status = ensure_focused_for_voice(self._engine, bundle, lambda: self._closed)
            state = self._engine.snapshot()
            self._log.write("voice_focus_guard", bundle=bundle, status=status)
            return status, state

    def _on_voice_completed(self, token: int, status: str, state):
        callback = self._voice_callbacks.pop(token, None)
        if not self._closed:
            if state:
                self.state = dict(state)
            self.updated.emit()
        if callback is not None:
            callback(token, status)

    def shutdown(self):
        self._closed = True
        self._voice_callbacks.clear()
        self._cancel.set()
        self.timer.stop()
        if self._observer is not None:
            self._observer.close()
            self._observer = None
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._log.close()
