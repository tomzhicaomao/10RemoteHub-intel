"""Per-device Windows key suppression via dedicated-thread Raw Input + WH_KEYBOARD_LL.

Why this architecture instead of a kernel filter driver or global DLL injection:
- Zero kernel drivers, zero admin privileges, zero DLL injection, and zero
  leftover OS state on crash (Windows destroys the hook immediately if the
  process exits or is killed).
- Works identically for both Bluetooth HID remotes and 2.4 GHz USB HID remotes.
- A dedicated lightweight Win32 thread owns *both* a hidden ``HWND_MESSAGE``
  Raw Input sink (``RIDEV_INPUTSINK``) and a ``WH_KEYBOARD_LL`` hook:
  1. Injected keys (``LLKHF_INJECTED`` / ``LLKHF_LOWER_IL_INJECTED``, such as
     our own ``SendInput`` mapped actions) are passed through immediately.
  2. Keys not in the active remote's learned trigger set are passed through
     immediately (< 0.05 ms).
  3. For watched keys, the hook drains pending ``WM_INPUT`` packets on the same
     thread (with a tiny bounded wait if the raw packet is still in flight) to
     determine the exact source ``hDevice``.
  4. Only events originating from the selected remote's handles are swallowed;
     events from the built-in keyboard or unknown sources always pass through
     (strict fail-open guarantee).

Known Windows platform boundary (UIPI):
- Ordinary-privilege ``WH_KEYBOARD_LL`` hooks do not intercept input directed
  at elevated (Administrator) foreground windows — the same UIPI boundary that
  applies to ``SendInput`` and UIA auto-focus. For USB receivers that require
  driver-level isolation even in elevated windows (or drop non-standard usages
  such as ``#`` on ``0416:0602``), binding the key interface to WinUSB (one-click takeover in the add dialog)
  provides exclusive hardware-level takeover.
"""
from __future__ import annotations

from collections import deque
import ctypes as c
import sys
import threading
import time
from typing import Callable, Iterable

from .input_codes import windows_keyboard, windows_scan_for_trigger, windows_vk_for_consumer_trigger

# Win32 hook & message constants
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_INPUT_DEVICE_CHANGE = 0x00FE
WM_INPUT = 0x00FF
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

PM_REMOVE = 0x0001
QS_RAWINPUT = 0x0400

LLKHF_EXTENDED = 0x01
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80

# Correlation windows (seconds)
DEFAULT_CORRELATION_WINDOW_S = 0.050   # recent WM_INPUT matching window
DEFAULT_WAIT_SLICE_MS = 6              # bounded wait if WM_INPUT lags WH_KEYBOARD_LL


class WatchSet:
    """Normalized set of keyboard scan codes and consumer VKs to watch for one remote."""

    def __init__(self, triggers: Iterable[str] = ()):
        scans: set[tuple[int, bool]] = set()
        vks: set[int] = set()
        valid_triggers: list[str] = []
        seen: set[str] = set()
        for raw in triggers:
            if not isinstance(raw, str):
                continue
            trigger = raw.strip()
            if not trigger or trigger in seen:
                continue
            scan = windows_scan_for_trigger(trigger)
            if scan is not None:
                scans.add(scan)
                seen.add(trigger)
                valid_triggers.append(trigger)
                continue
            vk = windows_vk_for_consumer_trigger(trigger)
            if vk is not None:
                vks.add(vk)
                seen.add(trigger)
                valid_triggers.append(trigger)
        self.scans: frozenset[tuple[int, bool]] = frozenset(scans)
        self.vks: frozenset[int] = frozenset(vks)
        self.triggers: tuple[str, ...] = tuple(valid_triggers)

    @property
    def count(self) -> int:
        return len(self.triggers)

    def match_hook_event(self, vk: int, scan: int, flags: int) -> tuple | None:
        """Return the correlation key ``('scan', scan, ext)`` or ``('vk', vk)`` if watched."""
        if flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED):
            return None
        if vk in self.vks:
            return ("vk", vk)
        if scan:
            ext = bool(flags & LLKHF_EXTENDED)
            if (scan, ext) in self.scans:
                return ("scan", scan, ext)
        return None


class CorrelationEngine:
    """Thread-safe, deterministic Raw Input <-> WH_KEYBOARD_LL source matcher.

    Fail-open invariant: if a key's hardware source cannot be proven to be the
    selected remote (e.g. main keyboard, injected event, or timeout), ``decide``
    always returns ``False`` (pass through).
    """

    def __init__(
        self,
        *,
        allowed_handles: Iterable[int] = (),
        triggers: Iterable[str] = (),
        window_s: float = DEFAULT_CORRELATION_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._lock = threading.Lock()
        self._allowed: set[int] = {int(h) for h in allowed_handles if h}
        self._watch = WatchSet(triggers)
        self._window_s = max(0.005, float(window_s))
        self._clock = clock
        # Recent raw events: deque of (timestamp, key_id, down, is_remote)
        self._recent: deque[tuple[float, tuple, bool, bool]] = deque(maxlen=128)
        # Keys currently held down by the remote / by other devices
        self._remote_held: set[tuple] = set()
        self._other_held: set[tuple] = set()

    @property
    def watched_count(self) -> int:
        with self._lock:
            return self._watch.count

    def update_triggers(self, triggers: Iterable[str]) -> int:
        with self._lock:
            self._watch = WatchSet(triggers)
            return self._watch.count

    def update_handles(self, allowed_handles: Iterable[int]) -> None:
        with self._lock:
            self._allowed = {int(h) for h in allowed_handles if h}

    def release_device(self, handle: int) -> None:
        with self._lock:
            if int(handle) in self._allowed:
                self._remote_held.clear()
            else:
                self._other_held.clear()

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()

    def record_raw_keyboard(self, handle: int, scan: int, extended: bool, down: bool) -> bool:
        """Record a Raw Input keyboard event. Returns True if from the selected remote."""
        now = self._clock()
        key_id = ("scan", int(scan), bool(extended))
        with self._lock:
            is_remote = int(handle) in self._allowed
            self._prune_locked(now)
            self._recent.append((now, key_id, bool(down), is_remote))
            held = self._remote_held if is_remote else self._other_held
            if down:
                held.add(key_id)
            else:
                held.discard(key_id)
            return is_remote

    def record_raw_consumer(self, handle: int, trigger: str, down: bool) -> bool:
        """Record a Raw Input consumer-control event. Returns True if from the selected remote."""
        now = self._clock()
        vk = windows_vk_for_consumer_trigger(trigger)
        with self._lock:
            is_remote = int(handle) in self._allowed
            if vk is not None:
                key_id = ("vk", vk)
                self._prune_locked(now)
                self._recent.append((now, key_id, bool(down), is_remote))
                held = self._remote_held if is_remote else self._other_held
                if down:
                    held.add(key_id)
                else:
                    held.discard(key_id)
            return is_remote

    def classify_hook_key(self, vk: int, scan: int, flags: int) -> tuple[tuple | None, bool]:
        """Return ``(key_id, down)`` if this hook event is watched, else ``(None, down)``."""
        down = not bool(flags & LLKHF_UP)
        with self._lock:
            key_id = self._watch.match_hook_event(vk, scan, flags)
        return key_id, down

    def decide_from_recorded(self, key_id: tuple, down: bool) -> bool | None:
        """Decide whether to swallow ``(key_id, down)`` from already-recorded raw input.

        Returns:
            ``True``  -> proven from target remote (SWALLOW)
            ``False`` -> proven from another device such as main keyboard (PASS THROUGH)
            ``None``  -> no matching raw event recorded yet (caller may wait briefly then fail-open)
        """
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            # Match the oldest unconsumed raw event for this exact (key_id, down)
            for idx, (_ts, rec_key, rec_down, is_remote) in enumerate(self._recent):
                if rec_key == key_id and rec_down == down:
                    del self._recent[idx]
                    return is_remote
            # If the remote is holding this key and no other keyboard holds it,
            # Windows key-repeat or release belongs to the remote.
            if key_id in self._remote_held and key_id not in self._other_held:
                return True
            if key_id in self._other_held and key_id not in self._remote_held:
                return False
            return None

    def decide_hook(
        self,
        vk: int,
        scan: int,
        flags: int,
        *,
        drain: Callable[[], None] | None = None,
        wait_and_drain: Callable[[], bool] | None = None,
    ) -> bool:
        """Full decision entry used by ``LowLevelKeyboardProc`` (strictly fail-open)."""
        key_id, down = self.classify_hook_key(vk, scan, flags)
        if key_id is None:
            return False
        if drain is not None:
            drain()
        verdict = self.decide_from_recorded(key_id, down)
        if verdict is not None:
            return verdict
        if wait_and_drain is not None and wait_and_drain():
            verdict = self.decide_from_recorded(key_id, down)
            if verdict is not None:
                return verdict
        # Strict fail-open: never swallow an unattributed keystroke.
        return False


class _KBDLLHOOKSTRUCT(c.Structure):
    _fields_ = [
        ("vkCode", c.c_uint32),
        ("scanCode", c.c_uint32),
        ("flags", c.c_uint32),
        ("time", c.c_uint32),
        ("dwExtraInfo", c.c_size_t),
    ]


class _RID(c.Structure):
    _fields_ = [
        ("page", c.c_uint16),
        ("usage", c.c_uint16),
        ("flags", c.c_uint32),
        ("target", c.c_void_p),
    ]


class _RAWINPUTHEADER(c.Structure):
    _fields_ = [
        ("kind", c.c_uint32),
        ("size", c.c_uint32),
        ("device", c.c_void_p),
        ("param", c.c_size_t),
    ]


class _RAWKEYBOARD(c.Structure):
    _fields_ = [
        ("scan", c.c_uint16),
        ("flags", c.c_uint16),
        ("reserved", c.c_uint16),
        ("vk", c.c_uint16),
        ("message", c.c_uint32),
        ("extra", c.c_uint32),
    ]


class WindowsHIDSuppressor:
    """Dedicated-thread Win32 Raw Input + WH_KEYBOARD_LL per-device suppressor.

    When active, this thread owns the process's Raw Input registration for
    keyboard (1, 6) and consumer control (12, 1), forwards target remote edges
    to ``on_edge`` / ``on_removed``, and selectively swallows only target remote
    keys in ``WH_KEYBOARD_LL`` while leaving the main keyboard untouched.
    """

    def __init__(self, *, wait_ms: int = DEFAULT_WAIT_SLICE_MS):
        self._wait_ms = max(0, int(wait_ms))
        self._engine = CorrelationEngine()
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._startup_error: str = ""
        self._on_edge: Callable[[int, str, bool], None] | None = None
        self._on_removed: Callable[[int], None] | None = None
        self._allowed: set[int] = set()
        self._keyboard_muted: bool = False
        self._consumers: dict[int, object] = {}
        self._held: dict[int, set[str]] = {}
        self.active: bool = False
        self.error: str = ""
        self._active_triggers: tuple[str, ...] = ()

    @property
    def active_triggers(self) -> tuple[str, ...]:
        return self._active_triggers

    @property
    def suppressed_count(self) -> int:
        return self._engine.watched_count if self.active else 0

    def start(
        self,
        *,
        allowed_handles: Iterable[int],
        triggers: Iterable[str],
        on_edge: Callable[[int, str, bool], None],
        on_removed: Callable[[int], None],
        keyboard_muted: bool = False,
    ) -> bool:
        """Start the dedicated Raw Input + WH_KEYBOARD_LL suppression thread."""
        self.clear()
        allowed = {int(h) for h in allowed_handles if h}
        watch = WatchSet(triggers)
        if not allowed or watch.count == 0:
            self.active = False
            self.error = ""
            return False
        if sys.platform != "win32":
            self.active = False
            self.error = "非 Windows 平台"
            return False

        self._allowed = allowed
        self._keyboard_muted = bool(keyboard_muted)
        self._on_edge = on_edge
        self._on_removed = on_removed
        self._active_triggers = watch.triggers
        self._engine = CorrelationEngine(allowed_handles=allowed, triggers=watch.triggers)
        self._consumers.clear()
        self._held.clear()
        self._ready.clear()
        self._stop_event.clear()
        self._startup_error = ""
        self._thread_id = 0

        self._thread = threading.Thread(
            target=self._run_win32_loop,
            name="remotehub-win-suppress",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            self.clear()
            self.error = "按键接管线程启动超时"
            return False
        if self._startup_error:
            err = self._startup_error
            self.clear()
            self.error = err
            return False
        self.active = True
        self.error = ""
        return True

    def sync(self, triggers: Iterable[str]) -> bool:
        """Dynamically update watched triggers while running."""
        if not self.active:
            return False
        watch = WatchSet(triggers)
        if watch.triggers == self._active_triggers:
            return False
        self._active_triggers = watch.triggers
        self._engine.update_triggers(watch.triggers)
        return True

    def ensure(self) -> bool:
        """Verify the hook thread is still alive."""
        if not self.active:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self.active = False
        self.error = "原按键屏蔽线程已停止"
        return False

    def clear(self) -> None:
        """Stop the hook thread, unhook WH_KEYBOARD_LL, and unregister Raw Input."""
        self._stop_event.set()
        thread = self._thread
        tid = self._thread_id
        if tid and sys.platform == "win32":
            try:
                c.WinDLL("user32", use_last_error=True).PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = 0
        self._on_edge = None
        self._on_removed = None
        self._allowed = set()
        self._consumers.clear()
        self._held.clear()
        self._active_triggers = ()
        self.active = False
        self.error = ""

    # -- Win32 native thread loop ---------------------------------------------

    def _run_win32_loop(self) -> None:
        from ctypes import wintypes as w
        from .windows_hid import ConsumerReports

        user = c.WinDLL("user32", use_last_error=True)
        kernel = c.WinDLL("kernel32", use_last_error=True)

        user.CreateWindowExW.argtypes = [
            w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
            c.c_int, c.c_int, c.c_int, c.c_int,
            w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
        ]
        user.CreateWindowExW.restype = w.HWND
        user.DestroyWindow.argtypes = [w.HWND]
        user.DestroyWindow.restype = w.BOOL
        user.RegisterRawInputDevices.argtypes = [c.POINTER(_RID), w.UINT, w.UINT]
        user.RegisterRawInputDevices.restype = w.BOOL
        user.GetRawInputData.argtypes = [w.HANDLE, w.UINT, c.c_void_p, c.POINTER(w.UINT), w.UINT]
        user.GetRawInputData.restype = w.UINT
        user.GetRawInputDeviceInfoW.argtypes = [w.HANDLE, w.UINT, c.c_void_p, c.POINTER(w.UINT)]
        user.GetRawInputDeviceInfoW.restype = w.UINT
        user.SetWindowsHookExW.argtypes = [c.c_int, c.c_void_p, w.HINSTANCE, w.DWORD]
        user.SetWindowsHookExW.restype = w.HHOOK
        user.UnhookWindowsHookEx.argtypes = [w.HHOOK]
        user.UnhookWindowsHookEx.restype = w.BOOL
        user.CallNextHookEx.argtypes = [w.HHOOK, c.c_int, c.c_size_t, c.c_ssize_t]
        user.CallNextHookEx.restype = c.c_ssize_t
        user.PeekMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
        user.PeekMessageW.restype = w.BOOL
        user.GetMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        user.GetMessageW.restype = w.BOOL
        user.TranslateMessage.argtypes = [c.POINTER(w.MSG)]
        user.DispatchMessageW.argtypes = [c.POINTER(w.MSG)]
        user.MsgWaitForMultipleObjects.argtypes = [w.DWORD, c.c_void_p, w.BOOL, w.DWORD, w.DWORD]
        user.MsgWaitForMultipleObjects.restype = w.DWORD
        kernel.GetCurrentThreadId.restype = w.DWORD

        self._thread_id = int(kernel.GetCurrentThreadId())
        hwnd = None
        hook = None
        registered = False

        def handle_raw_input(lparam: int) -> None:
            size = w.UINT(0)
            user.GetRawInputData(lparam, 0x10000003, None, c.byref(size), c.sizeof(_RAWINPUTHEADER))
            if not c.sizeof(_RAWINPUTHEADER) <= size.value <= 4096:
                return
            buf = c.create_string_buffer(size.value)
            got = user.GetRawInputData(lparam, 0x10000003, buf, c.byref(size), c.sizeof(_RAWINPUTHEADER))
            if got == 0xFFFFFFFF or got != size.value:
                return
            header = _RAWINPUTHEADER.from_buffer(buf)
            dev_handle = int(header.device or 0)
            offset = c.sizeof(_RAWINPUTHEADER)
            if header.kind == 1 and size.value >= offset + c.sizeof(_RAWKEYBOARD):
                k = _RAWKEYBOARD.from_buffer(buf, offset)
                if k.vk == 255 or not k.scan:
                    return
                down = not bool(k.flags & 1)
                extended = bool(k.flags & 2)
                is_remote = self._engine.record_raw_keyboard(dev_handle, k.scan, extended, down)
                if is_remote and not self._keyboard_muted:
                    trigger = windows_keyboard(k.scan, k.flags) or f"win:{k.scan}:{k.flags & 6}"
                    held = self._held.setdefault(dev_handle, set())
                    cb = self._on_edge
                    if down and trigger not in held:
                        held.add(trigger)
                        if cb is not None:
                            cb(dev_handle, trigger, True)
                    elif not down and trigger in held:
                        held.remove(trigger)
                        if cb is not None:
                            cb(dev_handle, trigger, False)
            elif header.kind == 2 and size.value >= offset + 8:
                width = c.c_uint32.from_buffer(buf, offset).value
                count = c.c_uint32.from_buffer(buf, offset + 4).value
                if not (width and width * count <= size.value - offset - 8):
                    return
                parser = self._consumers.get(dev_handle)
                if parser is None:
                    try:
                        parser = ConsumerReports(user, dev_handle)
                    except Exception:
                        parser = False
                    self._consumers[dev_handle] = parser
                if not parser:
                    return
                cb = self._on_edge
                for i in range(count):
                    data = bytes(buf[offset + 8 + i * width: offset + 8 + (i + 1) * width])
                    try:
                        events = parser.decode(data)
                    except Exception:
                        continue
                    for trigger, down in events:
                        is_remote = self._engine.record_raw_consumer(dev_handle, trigger, down)
                        if is_remote and cb is not None:
                            cb(dev_handle, trigger, down)

        def handle_device_change(wparam: int, lparam: int) -> None:
            if wparam != 2:
                return
            dev_handle = int(lparam or 0)
            self._engine.release_device(dev_handle)
            self._held.pop(dev_handle, None)
            self._consumers.pop(dev_handle, None)
            if dev_handle in self._allowed and self._on_removed is not None:
                self._on_removed(dev_handle)

        def drain_raw_input() -> None:
            msg = w.MSG()
            while user.PeekMessageW(c.byref(msg), hwnd, WM_INPUT_DEVICE_CHANGE, WM_INPUT, PM_REMOVE):
                if msg.message == WM_INPUT:
                    handle_raw_input(int(msg.lParam))
                elif msg.message == WM_INPUT_DEVICE_CHANGE:
                    handle_device_change(int(msg.wParam), int(msg.lParam))

        def wait_and_drain() -> bool:
            if self._wait_ms <= 0:
                return False
            res = user.MsgWaitForMultipleObjects(0, None, False, self._wait_ms, QS_RAWINPUT)
            if res == 0:
                drain_raw_input()
                return True
            drain_raw_input()
            return False

        hook_type = c.WINFUNCTYPE(c.c_ssize_t, c.c_int, c.c_size_t, c.c_ssize_t)

        def low_level_keyboard_proc(code: int, wparam: int, lparam: int) -> int:
            try:
                if code == HC_ACTION and lparam and wparam in (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
                    info = _KBDLLHOOKSTRUCT.from_address(lparam)
                    flags = int(info.flags)
                    if wparam in (WM_KEYUP, WM_SYSKEYUP):
                        flags |= LLKHF_UP
                    if self._engine.decide_hook(
                        int(info.vkCode),
                        int(info.scanCode),
                        flags,
                        drain=drain_raw_input,
                        wait_and_drain=wait_and_drain,
                    ):
                        return 1
            except Exception:
                pass
            return user.CallNextHookEx(hook, code, wparam, lparam)

        hook_callback = hook_type(low_level_keyboard_proc)

        try:
            # Built-in system class "Message" / "Static" works for HWND_MESSAGE without RegisterClassExW.
            hwnd = user.CreateWindowExW(0, "STATIC", "10RemoteHubSuppress", 0, 0, 0, 0, 0, c.c_void_p(-3), None, None, None)
            if not hwnd:
                raise OSError(f"创建隐藏输入窗口失败（错误 {c.get_last_error()}）")
            # RIDEV_INPUTSINK | RIDEV_DEVNOTIFY = 0x2100
            reg = (_RID * 2)(_RID(1, 6, 0x2100, hwnd), _RID(12, 1, 0x2100, hwnd))
            if not user.RegisterRawInputDevices(reg, 2, c.sizeof(_RID)):
                raise OSError(f"注册 Raw Input 失败（错误 {c.get_last_error()}）")
            registered = True
            hook = user.SetWindowsHookExW(WH_KEYBOARD_LL, hook_callback, None, 0)
            if not hook:
                raise OSError(f"安装低级键盘钩子失败（错误 {c.get_last_error()}）")
        except Exception as exc:
            self._startup_error = str(exc)
            if hook:
                user.UnhookWindowsHookEx(hook)
            if registered:
                unreg = (_RID * 2)(_RID(1, 6, 1, None), _RID(12, 1, 1, None))
                user.RegisterRawInputDevices(unreg, 2, c.sizeof(_RID))
            if hwnd:
                user.DestroyWindow(hwnd)
            self._ready.set()
            return

        self._ready.set()
        msg = w.MSG()
        try:
            while not self._stop_event.is_set():
                ret = user.GetMessageW(c.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message == WM_INPUT:
                    handle_raw_input(int(msg.lParam))
                elif msg.message == WM_INPUT_DEVICE_CHANGE:
                    handle_device_change(int(msg.wParam), int(msg.lParam))
                else:
                    user.TranslateMessage(c.byref(msg))
                    user.DispatchMessageW(c.byref(msg))
        finally:
            if hook:
                user.UnhookWindowsHookEx(hook)
            if registered:
                unreg = (_RID * 2)(_RID(1, 6, 1, None), _RID(12, 1, 1, None))
                user.RegisterRawInputDevices(unreg, 2, c.sizeof(_RID))
            if hwnd:
                user.DestroyWindow(hwnd)
            # Keep callback alive until after UnhookWindowsHookEx completes.
            del hook_callback
