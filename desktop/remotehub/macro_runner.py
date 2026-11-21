"""Event-driven non-blocking executor for multi-step Action Chains (Macros).

Never sleeps on the GUI thread: inter-step delays use a single-shot QTimer so
remote cancel keys (tv-back / same button), voice interrupts, profile switches,
and shutdown remain responsive at every millisecond.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QObject, QTimer, Signal

from .model import Action, MacroStep


class MacroRunner(QObject):
    # Give target apps time to consume a clipboard paste before the next step mutates clipboard or sends Enter.
    TEXT_PASTE_SETTLE_MS = 120

    state_changed = Signal()
    finished = Signal(str)
    aborted = Signal(str)
    failed = Signal(str)
    notice = Signal(str)

    def __init__(self, execute_step, parent=None, *, send_enter=None):
        super().__init__(parent)
        self._execute_step = execute_step
        self._send_enter = send_enter
        self._active = False
        self._run_id = 0
        self._owner = ""
        self._title = ""
        self._steps: list[MacroStep] = []
        self._index = 0
        self._pending_enter_delay: int | None = None
        self._consumed_releases: set[str] = set()

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._tick)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def current_step(self) -> int:
        return self._index

    @property
    def total_steps(self) -> int:
        return len(self._steps)

    def has_pending_release(self, trigger: str) -> bool:
        return bool(trigger and trigger in self._consumed_releases)

    def clear_physical_press(self, trigger: str):
        self._consumed_releases.discard(trigger)

    def start(self, action: Action, owner: str = "", *, currently_pressed: bool = False):
        action.validate(sys.platform)
        if action.kind != "macro":
            raise ValueError("不是有效的动作链")
        if self._active:
            if owner and owner == self._owner:
                if currently_pressed:
                    self._consumed_releases.add(owner)
                self.cancel("已中止动作链。")
                return
            self.cancel(silent=True)

        self._run_id += 1
        run_id = self._run_id
        self._active = True
        self._owner = owner or ""
        self._title = action.target.strip() or f"动作链（{len(action.steps)} 步）"
        self._steps = list(action.steps)
        self._index = 0
        self._pending_enter_delay = None
        self.state_changed.emit()

        self._advance(run_id)
        if self._active and self._run_id == run_id and currently_pressed and owner:
            self._consumed_releases.add(owner)

    def cancel(self, reason: str = "", *, silent: bool = False):
        self.timer.stop()
        was_active = self._active
        self._stop_state()
        if was_active and not silent and reason:
            self.aborted.emit(reason)

    def close(self):
        self.cancel(silent=True)
        self._consumed_releases.clear()

    def _stop_state(self):
        self.timer.stop()
        if not self._active and not self._steps:
            return
        self._active = False
        self._owner = ""
        self._title = ""
        self._steps = []
        self._index = 0
        self._pending_enter_delay = None
        self.state_changed.emit()

    def _tick(self):
        if not self._active:
            return
        run_id = self._run_id
        if self._pending_enter_delay is not None:
            after_delay = self._pending_enter_delay
            self._pending_enter_delay = None
            try:
                if self._send_enter is not None:
                    self._send_enter()
                else:
                    self._execute_step(Action("shortcut", ["enter"]), max(0, self._index - 1))
            except Exception as exc:
                if self._run_id == run_id:
                    title = self._title
                    self._stop_state()
                    self.failed.emit(f"「{title}」第 {max(1, self._index)} 步发送回车失败：{exc}")
                return
            if not self._active or self._run_id != run_id:
                return
            if self._index >= len(self._steps):
                title = self._title
                total = len(self._steps)
                self._stop_state()
                self.finished.emit(f"已执行「{title}」（共 {total} 步）。")
                return
            if after_delay > 0:
                self.timer.start(after_delay)
                self.notice.emit(f"正在执行「{self._title}」（{self._index}/{len(self._steps)}）…")
                self.state_changed.emit()
                return
        self._advance(self._run_id)

    def _advance(self, run_id: int):
        while self._active and self._run_id == run_id and self._index < len(self._steps):
            idx = self._index
            step = self._steps[idx]
            self._index = idx + 1
            try:
                self._execute_step(step.action, idx)
            except Exception as exc:
                if self._run_id == run_id:
                    title = self._title
                    self._stop_state()
                    self.failed.emit(f"「{title}」第 {idx + 1} 步失败：{exc}")
                return
            if not self._active or self._run_id != run_id:
                return
            if step.action.kind == "text" and step.action.enter:
                self._pending_enter_delay = step.delay_ms
                self.timer.start(self.TEXT_PASTE_SETTLE_MS)
                self.notice.emit(f"正在执行「{self._title}」（粘贴后自动回车）…")
                self.state_changed.emit()
                return
            if self._index >= len(self._steps):
                title = self._title
                total = len(self._steps)
                self._stop_state()
                self.finished.emit(f"已执行「{title}」（共 {total} 步）。")
                return
            delay_ms = step.delay_ms
            if step.action.kind == "text":
                delay_ms = max(delay_ms, self.TEXT_PASTE_SETTLE_MS)
            if delay_ms > 0:
                self.timer.start(delay_ms)
                self.notice.emit(f"正在执行「{self._title}」（{self._index}/{len(self._steps)}）…")
                self.state_changed.emit()
                return
            self.state_changed.emit()

    def handle_key(self, trigger: str, down: bool, role: str | None, *, is_repeat: bool = False) -> str:
        """Intercept remote keys while a delayed macro is waiting between steps.

        Returns "consumed" if absorbed, or "none" when idle.
        """
        if not down and trigger in self._consumed_releases:
            self._consumed_releases.discard(trigger)
            return "consumed"
        if not self._active:
            return "none"
        if not down or is_repeat:
            return "consumed"
        self._consumed_releases.add(trigger)
        if role == "tv-back" or (self._owner and trigger == self._owner):
            self.cancel("已中止动作链。")
            return "consumed"
        self.notice.emit("动作链执行中，按返回键可立即中止。")
        return "consumed"
