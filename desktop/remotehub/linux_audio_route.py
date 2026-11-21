"""Temporary Linux default-microphone switching over PulseAudio / PipeWire-Pulse.

Switches the system default input source to `remotehub.microphone` while a USB
voice session is active and restores the user's original source immediately on
release, respecting any manual override made mid-session. All `pactl` IPC runs
on a serialized background worker so the Qt GUI thread never blocks.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess
import time

from PySide6.QtCore import QObject, Qt, Signal

from .virtual_mic import LINUX_MIC_NODE


def _run_pactl(args: list[str], *, executable: str | None = None) -> str:
    exe = executable or shutil.which("pactl")
    if not exe:
        raise RuntimeError("未找到 pactl 音频工具，无法自动切换系统默认麦克风")
    res = subprocess.run([exe, *args], capture_output=True, text=True, timeout=3, check=False)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"音频路由命令失败：{detail or args[0]}")
    return res.stdout.strip()


def current_default_source(*, runner=_run_pactl) -> str:
    return runner(["get-default-source"]).strip()


def list_sources(*, runner=_run_pactl) -> list[str]:
    out = runner(["list", "short", "sources"])
    names = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip():
            names.append(parts[1].strip())
    return names


def set_default_source(source_name: str, *, runner=_run_pactl) -> None:
    runner(["set-default-source", source_name])


class LinuxInputRoute(QObject):
    ready = Signal()
    failed = Signal(str)
    released = Signal()
    measured = Signal(str, float)
    _done = Signal(str, bool, str, float)

    def __init__(self, parent=None, *, runner=_run_pactl, target_source: str = LINUX_MIC_NODE):
        super().__init__(parent)
        self._runner = runner
        self.target_source = target_source
        self.saved_source = ""
        self.state = "idle"
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rh-linux-audio")
        self._done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    def _acquire_sync(self) -> tuple[bool, str, float]:
        started = time.monotonic()
        if self.saved_source:
            self._restore_sync()
        sources = list_sources(runner=self._runner)
        if self.target_source not in sources:
            raise RuntimeError("没有找到 10RemoteHub 虚拟麦克风输入源，请在语音设置点击「启用虚拟麦克风」")
        original = current_default_source(runner=self._runner)
        if not original:
            raise RuntimeError("无法读取当前系统默认麦克风，本次未切换")
        if original != self.target_source:
            self.saved_source = original
            try:
                set_default_source(self.target_source, runner=self._runner)
                if current_default_source(runner=self._runner) != self.target_source:
                    raise RuntimeError("系统默认麦克风切换未确认，本次语音已取消")
            except Exception:
                try:
                    self._restore_sync()
                except Exception:
                    pass
                raise
        self.state = "acquired"
        return True, "", (time.monotonic() - started) * 1000.0

    def _restore_sync(self) -> float:
        started = time.monotonic()
        if not self.saved_source:
            self.state = "idle"
            return 0.0
        current = current_default_source(runner=self._runner)
        if not current:
            self.state = "restore_failed"
            raise RuntimeError("无法读取当前默认麦克风，保留恢复状态供重试")
        # Respect a manual change made by the user while recording.
        if current == self.target_source and current != self.saved_source:
            set_default_source(self.saved_source, runner=self._runner)
            if current_default_source(runner=self._runner) != self.saved_source:
                self.state = "restore_failed"
                raise RuntimeError("原默认麦克风恢复未确认，请在系统声音设置中检查")
        self.saved_source = ""
        self.state = "idle"
        return (time.monotonic() - started) * 1000.0

    def acquire(self):
        if self._closed:
            return
        if self.state == "acquired":
            self.ready.emit()
            return

        def job():
            try:
                ok, msg, elapsed = self._acquire_sync()
            except Exception as exc:
                ok, msg, elapsed = False, str(exc), 0.0
            if not self._closed:
                try:
                    self._done.emit("acquire", ok, msg, elapsed)
                except RuntimeError:
                    pass

        self._executor.submit(job)

    def release(self):
        if self._closed:
            return

        def job():
            try:
                elapsed = self._restore_sync()
                ok, msg = True, ""
            except Exception as exc:
                ok, msg, elapsed = False, str(exc), 0.0
            if not self._closed:
                try:
                    self._done.emit("release", ok, msg, elapsed)
                except RuntimeError:
                    pass

        self._executor.submit(job)

    def _on_done(self, op: str, ok: bool, message: str, elapsed: float):
        if self._closed:
            return
        if op == "acquire":
            if ok:
                self.measured.emit("acquire", elapsed)
                self.ready.emit()
            else:
                self.failed.emit(message)
        elif op == "release":
            if ok:
                if elapsed > 0:
                    self.measured.emit("release", elapsed)
            else:
                self.failed.emit(message)
            self.released.emit()

    def close(self):
        self._closed = True
        try:
            if self.saved_source or self.state != "idle":
                self._executor.submit(self._restore_sync).result(timeout=2.5)
        except Exception:
            pass
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
