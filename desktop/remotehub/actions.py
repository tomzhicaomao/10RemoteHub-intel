"""Platform actions. File paths never pass through a shell."""
from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .branding import APP_NAME
from .launch import LaunchRequest
from .model import Action, MODIFIERS, validate_keys

REPEATABLE_COMMON = {"scroll_up", "scroll_down", "page_up", "page_down", "volume_up", "volume_down"}
REPEATABLE_KEYS = {"up", "down", "left", "right", "pageup", "pagedown"}

_WIN32_PUNCT_VK = {
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
}

_UINPUT_ALIASES = {
    "cmd": "LEFTMETA",
    "ctrl": "LEFTCTRL",
    "alt": "LEFTALT",
    "shift": "LEFTSHIFT",
    "enter": "ENTER",
    "esc": "ESC",
    "tab": "TAB",
    "space": "SPACE",
    "backspace": "BACKSPACE",
    "delete": "DELETE",
    "insert": "INSERT",
    "home": "HOME",
    "end": "END",
    "pageup": "PAGEUP",
    "pagedown": "PAGEDOWN",
    "up": "UP",
    "down": "DOWN",
    "left": "LEFT",
    "right": "RIGHT",
    "print_screen": "SYSRQ",
    "-": "MINUS",
    "=": "EQUAL",
    "[": "LEFTBRACE",
    "]": "RIGHTBRACE",
    "\\": "BACKSLASH",
    ";": "SEMICOLON",
    "'": "APOSTROPHE",
    ",": "COMMA",
    ".": "DOT",
    "/": "SLASH",
    "`": "GRAVE",
}

_UINPUT_MEDIA = {
    "volume_up": "KEY_VOLUMEUP",
    "volume_down": "KEY_VOLUMEDOWN",
    "mute": "KEY_MUTE",
    "play_pause": "KEY_PLAYPAUSE",
    "previous_track": "KEY_PREVIOUSSONG",
    "next_track": "KEY_NEXTSONG",
}


def win32_vk_code(name: str) -> int | None:
    """Return the Windows Virtual-Key code for keys that must avoid KEYEVENTF_UNICODE."""
    if len(name) == 1 and "a" <= name <= "z":
        return 0x41 + (ord(name) - ord("a"))
    if len(name) == 1 and "0" <= name <= "9":
        return 0x30 + (ord(name) - ord("0"))
    if name in _WIN32_PUNCT_VK:
        return _WIN32_PUNCT_VK[name]
    if name == "print_screen":
        return 0x2C
    if name.startswith("f") and name[1:].isdigit():
        num = int(name[1:])
        if 1 <= num <= 24:
            return 0x70 + (num - 1)
    return None


def is_repeatable(action):
    if action.kind == "common":
        return action.target in REPEATABLE_COMMON
    if action.kind != "shortcut":
        return False
    main = [key for key in action.keys if key not in MODIFIERS]
    return len(main) == 1 and main[0] in REPEATABLE_KEYS


def launch_command(action: Action, platform=sys.platform):
    action.validate()
    target = Path(action.target).expanduser().absolute()
    if not target.exists():
        raise FileNotFoundError(f"找不到：{target}。文件可能已移动，或磁盘尚未连接。")
    application = Path(action.application).expanduser().absolute() if action.application else None
    if application and not application.exists():
        raise FileNotFoundError(f"指定的打开方式不存在：{application}")
    if platform == "darwin":
        if action.kind == "app":
            return ["/usr/bin/open", "-a", str(target)]
        return ["/usr/bin/open", "-a", str(application), str(target)] if application else ["/usr/bin/open", str(target)]
    if platform == "win32":
        if action.kind == "app":
            if target.suffix.casefold() == ".lnk":
                return None  # os.startfile preserves shortcut arguments and working dir
            return [str(target)]
        if application:
            if application.suffix.casefold() == ".lnk":
                from .win_uia import resolve_windows_shortcut
                resolved = resolve_windows_shortcut(application)
                if not resolved or not Path(resolved).exists():
                    raise ValueError("Windows 指定打开方式请选择可执行文件 (.exe)，或指向有效程序的快捷方式")
                return [resolved, str(target)]
            return [str(application), str(target)]
        return None  # ShellExecute through os.startfile, not cmd.exe
    if str(platform).startswith("linux"):
        if action.kind == "app" or application:
            app = application or target
            if app.suffix == ".desktop":
                if application:
                    raise ValueError("Linux 指定打开方式请选择可执行文件，而非 .desktop 启动器")
                return ["gio", "launch", str(app)]
            return [str(app), str(target)] if application else [str(app)]
        return ["xdg-open", str(target)]
    raise RuntimeError("当前版本仅支持 macOS、Windows 与 Linux")


class LinuxUInputBackend:
    """Shared Wayland uinput virtual keyboard + pointer device."""

    _shared: LinuxUInputBackend | None = None

    @classmethod
    def shared(cls, sleeper=time.sleep) -> LinuxUInputBackend:
        if cls._shared is None:
            cls._shared = cls(sleeper=sleeper)
        return cls._shared

    def __init__(self, *, sleeper=time.sleep, uinput_factory=None):
        self.sleeper = sleeper
        self._uinput_factory = uinput_factory
        self.dev = None
        self.ecodes = None

    def _open(self):
        if self.dev is not None:
            return self.dev
        try:
            from evdev import UInput, ecodes
        except Exception as exc:
            if self._uinput_factory is None:
                raise RuntimeError(f"当前环境缺少 Linux evdev 组件：{exc}") from exc
            from types import SimpleNamespace
            UInput = None
            ecodes = SimpleNamespace(
                EV_KEY=1, EV_REL=2, REL_X=0, REL_Y=1, REL_WHEEL=8,
                BTN_LEFT=0x110, BTN_RIGHT=0x111, BTN_MIDDLE=0x112,
                KEY_SYSRQ=99, KEY_A=30, KEY_LEFTCTRL=29, KEY_LEFTSHIFT=42,
                KEY_LEFTALT=56, KEY_LEFTMETA=125, KEY_ENTER=28, KEY_ESC=1,
                KEY_MUTE=113, KEY_VOLUMEDOWN=114, KEY_VOLUMEUP=115,
                KEY_PLAYPAUSE=164, KEY_PREVIOUSSONG=165, KEY_NEXTSONG=163,
            )
        self.ecodes = ecodes
        key_caps = list(range(1, 256)) + [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE]
        events = {
            ecodes.EV_KEY: key_caps,
            ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL],
        }
        factory = self._uinput_factory or UInput
        try:
            self.dev = factory(name=f"{APP_NAME} Virtual Input", events=events)
        except OSError as exc:
            raise PermissionError(
                "Wayland 会话需要访问 /dev/uinput 才能发送按键与鼠标操作；"
                "请前往「通用设置」复制安装 udev 规则命令并重新登录后再试"
            ) from exc
        # Let libinput discover the newly created virtual device before first key/click.
        self.sleeper(0.08)
        return self.dev

    def send_key(self, key: str, down: bool):
        dev = self._open()
        e = self.ecodes
        symbol = _UINPUT_ALIASES.get(key, key.upper())
        code = getattr(e, f"KEY_{symbol}", None)
        if code is None:
            raise ValueError(f"Linux uinput 不支持此键：{key}")
        dev.write(e.EV_KEY, code, 1 if down else 0)
        dev.syn()

    def send_media(self, operation: str):
        dev = self._open()
        e = self.ecodes
        attr = _UINPUT_MEDIA.get(operation)
        code = getattr(e, attr, None) if attr else None
        if code is None:
            raise ValueError("未知媒体操作")
        dev.write(e.EV_KEY, code, 1)
        dev.syn()
        dev.write(e.EV_KEY, code, 0)
        dev.syn()

    def send_mouse_button(self, button: str, down: bool):
        dev = self._open()
        e = self.ecodes
        mapping = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}
        code = mapping.get(button)
        if code is None:
            raise ValueError("不支持的鼠标按键")
        dev.write(e.EV_KEY, code, 1 if down else 0)
        dev.syn()

    def scroll(self, amount: int):
        dev = self._open()
        e = self.ecodes
        dev.write(e.EV_REL, e.REL_WHEEL, int(amount))
        dev.syn()

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None


def _win32_mouse_wheel(amount: int) -> bool:
    """Inject vertical mouse wheel via Win32 SendInput (WHEEL_DELTA = 120 per notch)."""
    import ctypes as c
    from ctypes import wintypes as w

    user32 = getattr(getattr(c, "windll", None), "user32", None)
    if user32 is None:
        return False

    class MOUSEINPUT(c.Structure):
        _fields_ = [
            ("dx", w.LONG),
            ("dy", w.LONG),
            ("mouseData", w.DWORD),
            ("dwFlags", w.DWORD),
            ("time", w.DWORD),
            ("dwExtraInfo", c.c_size_t),
        ]

    class _INPUTUNION(c.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(c.Structure):
        _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]

    delta = int(amount) * 120
    inp = INPUT(
        type=0,  # INPUT_MOUSE
        u=_INPUTUNION(
            mi=MOUSEINPUT(
                dx=0,
                dy=0,
                mouseData=w.DWORD(delta & 0xFFFFFFFF).value,
                dwFlags=0x0800,  # MOUSEEVENTF_WHEEL
                time=0,
                dwExtraInfo=0,
            )
        ),
    )
    sent = user32.SendInput(1, c.byref(inp), c.sizeof(INPUT))
    return bool(sent == 1)


def _lock_windows_workstation():
    import ctypes as c

    user32 = getattr(getattr(c, "windll", None), "user32", None)
    if user32 is None:
        raise RuntimeError("当前环境无法调用 Windows LockWorkStation")
    if not user32.LockWorkStation():
        raise RuntimeError("系统拒绝锁屏请求")
    return "已锁屏"


def _lock_linux_session(keys: KeyOutput):
    for cmd in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"]):
        exe = shutil.which(cmd[0])
        if exe:
            proc = subprocess.run([exe, *cmd[1:]], capture_output=True, timeout=3)
            if proc.returncode == 0:
                return "已锁屏"
    return keys.shortcut(["cmd", "l"])


def _use_linux_uinput() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    from .linux_desktop import is_wayland_session
    return is_wayland_session()


class KeyOutput:
    def __init__(self, *, sleeper=time.sleep):
        self.sleeper = sleeper
        self.held = []
        self._owners = {}
        self.controller = None
        self.key_enum = None
        self.key_code_cls = None
        self._mouse_controller = None
        self._uinput: LinuxUInputBackend | None = None
        self._using_native_backend = False
        self.event_source = None

        self.log = lambda event, **fields: None

    def _backend(self):
        if self.controller is None:
            from pynput.keyboard import Controller, Key, KeyCode
            self.controller = Controller()
            self.key_enum = Key
            self.key_code_cls = KeyCode
            self._using_native_backend = True
        return self.controller

    def _uinput_backend(self) -> LinuxUInputBackend:
        if self._uinput is None:
            self._uinput = LinuxUInputBackend.shared(sleeper=self.sleeper)
        return self._uinput

    def _mouse_scroll_backend(self):
        if self._mouse_controller is None:
            from pynput.mouse import Controller
            self._mouse_controller = Controller()
        return self._mouse_controller

    def _check_win32_uipi(self):
        if sys.platform == "win32" and self._using_native_backend:
            from .win_uia import foreground_uipi_blocked
            if foreground_uipi_blocked():
                raise PermissionError(
                    "当前前台窗口以管理员身份运行，Windows 安全策略（UIPI）阻止普通权限程序发送按键；"
                    "如需控制管理员窗口，请以管理员身份启动 10RemoteHub"
                )

    def _key(self, name):
        aliases = {"pageup": "page_up", "pagedown": "page_down"}
        mapped = aliases.get(name, name)
        if self.key_enum is not None and hasattr(self.key_enum, mapped):
            return getattr(self.key_enum, mapped)
        if sys.platform == "win32" and self.key_code_cls is not None:
            vk = win32_vk_code(name)
            if vk is not None:
                return self.key_code_cls.from_vk(vk)
        if sys.platform.startswith("linux") and self.key_code_cls is not None:
            if name == "print_screen" and hasattr(self.key_enum, "print_screen"):
                return getattr(self.key_enum, "print_screen")
            if name.startswith("f") and name[1:].isdigit() and hasattr(self.key_code_cls, "_from_symbol"):
                sym = self.key_code_cls._from_symbol(name.upper())
                if sym is not None:
                    return sym
        return getattr(self.key_enum, mapped, name) if self.key_enum is not None else name

    def _mac(self, name, down, keys):
        import Quartz as q
        from ApplicationServices import AXIsProcessTrusted
        codes = dict(zip("asdfhgzxcvbqwerty", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 17, 16]))
        codes.update(dict(zip("123465=97-80]ou[ip\nlj'k;\\,/nm.", [18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47])))
        codes.update({"enter": 36, "tab": 48, "space": 49, "`": 50, "backspace": 51, "esc": 53, "cmd": 55, "shift": 56, "alt": 58, "ctrl": 59, "delete": 117, "insert": 114, "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126})
        codes.update(dict(zip([f"f{i}" for i in range(1, 21)], [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111, 105, 107, 113, 106, 64, 79, 80, 90])))
        if name not in codes:
            raise ValueError(f"macOS 不支持此键：{name}")
        if not AXIsProcessTrusted():
            raise PermissionError(f"请在系统设置中允许 {APP_NAME} 的辅助功能权限")
        flags = 0
        for k, mask in {"cmd": q.kCGEventFlagMaskCommand, "shift": q.kCGEventFlagMaskShift, "alt": q.kCGEventFlagMaskAlternate, "ctrl": q.kCGEventFlagMaskControl}.items():
            if k in keys:
                flags |= mask
        if self.event_source is None:
            self.event_source = q.CGEventSourceCreate(q.kCGEventSourceStateHIDSystemState)
        event = q.CGEventCreateKeyboardEvent(self.event_source, codes[name], down)
        q.CGEventSetFlags(event, flags)
        q.CGEventSetIntegerValueField(event, q.kCGEventSourceUserData, 0x52485542)
        if name in MODIFIERS:
            q.CGEventSetType(event, q.kCGEventFlagsChanged)
        q.CGEventPost(q.kCGHIDEventTap, event)
        if name in MODIFIERS or name == "tab":
            self.log("injected_key", key=name, down=down, flags=list(keys),
                     system_flags=int(q.CGEventSourceFlagsState(q.kCGEventSourceStateHIDSystemState)))

    def _send(self, key, down, flags):
        if sys.platform == "darwin":
            return self._mac(key, down, flags)
        if self.controller is None and _use_linux_uinput():
            return self._uinput_backend().send_key(key, down)
        backend = self._backend()
        if down:
            self._check_win32_uipi()
            backend.press(self._key(key))
        else:
            backend.release(self._key(key))

    def owned_modifiers(self, owner=None):
        if owner is not None:
            return set(self._owners.get(owner, set()))
        return {key for keys in self._owners.values() for key in keys}

    def _modifiers(self):
        owned = self.owned_modifiers()
        return [key for key in MODIFIERS if key in owned or key in self.held]

    def hold(self, owner, keys):
        validate_keys(keys, allow_modifier=True, platform=sys.platform)
        if not owner or any(key not in MODIFIERS for key in keys):
            raise ValueError("只能按住修饰键")
        for key in keys:
            if key in self.held or any(key in owned and name != owner for name, owned in self._owners.items()):
                raise RuntimeError("这个修饰键正被另一个动作按住，请先释放")
        for key in list(self._owners.get(owner, set()) - set(keys)):
            self._release_owned_key(owner, key)
        owner_keys = self._owners.setdefault(owner, set())
        for key in keys:
            if key not in owner_keys:
                self._send(key, True, [*self._modifiers(), key])
                owner_keys.add(key)

    def _release_owned_key(self, owner, key):
        owner_keys = self._owners.get(owner, set())
        owner_keys.discard(key)
        if not owner_keys:
            self._owners.pop(owner, None)
        if key not in self.held and not any(key in owned for owned in self._owners.values()):
            self._send(key, False, self._modifiers())

    def release_owned(self, owner):
        errors = []
        for key in list(self._owners.get(owner, set())):
            try:
                self._release_owned_key(owner, key)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def release_all_owned(self):
        errors = []
        for owner in list(self._owners):
            try:
                self.release_owned(owner)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def chord(self, keys, hold_ms=0):
        validate_keys(keys, allow_modifier=True, platform=sys.platform)
        held = self._modifiers()
        flags = held + [key for key in keys if key not in held]
        sent = []
        try:
            for key in keys:
                if key in held:
                    continue
                sent.append(key)
                self._send(key, True, flags)
            if hold_ms:
                self.sleeper(hold_ms / 1000)
        finally:
            for key in reversed(sent):
                sent.remove(key)
                remaining = [flag for flag in held + sent if flag in MODIFIERS]
                self._send(key, False, remaining)

    def press(self, keys):
        validate_keys(keys, allow_modifier=True, platform=sys.platform)
        if self.held or any(key in self.owned_modifiers() for key in keys):
            raise RuntimeError("另一个按键动作尚未释放")
        try:
            for key in keys:
                self.held.append(key)
                self._send(key, True, self._modifiers())
        except Exception:
            self.release()
            raise

    def release(self):
        errors = []
        while self.held:
            key = self.held.pop()
            try:
                self._send(key, False, self._modifiers())
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def shortcut(self, keys):
        validate_keys(keys, platform=sys.platform)
        if self.held:
            raise RuntimeError("另一个按键动作尚未释放")
        self.chord(keys)

    def media(self, operation):
        from .common_actions import MEDIA_ACTIONS
        if operation not in MEDIA_ACTIONS:
            raise ValueError("未知媒体操作")
        if sys.platform == "darwin":
            from ApplicationServices import AXIsProcessTrusted
            if not AXIsProcessTrusted():
                raise PermissionError(f"请在系统设置中允许 {APP_NAME} 的辅助功能权限")
        if self.controller is None and _use_linux_uinput():
            return self._uinput_backend().send_media(operation)
        names = {
            "volume_up": "media_volume_up",
            "volume_down": "media_volume_down",
            "mute": "media_volume_mute",
            "play_pause": "media_play_pause",
            "previous_track": "media_previous",
            "next_track": "media_next",
        }
        backend = self._backend()
        self._check_win32_uipi()
        key = getattr(self.key_enum, names[operation])
        backend.press(key)
        backend.release(key)

    def scroll(self, operation, with_held=False):
        if operation not in ("scroll_up", "scroll_down"):
            raise ValueError("未知滚动操作")
        amount = 3 if operation == "scroll_up" else -3
        if sys.platform == "darwin":
            import Quartz as q
            from ApplicationServices import AXIsProcessTrusted
            if not AXIsProcessTrusted():
                raise PermissionError(f"请在系统设置中允许 {APP_NAME} 的辅助功能权限")
            event = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, amount)
            if with_held:
                flags = 0
                for key, mask in {
                    "cmd": q.kCGEventFlagMaskCommand,
                    "shift": q.kCGEventFlagMaskShift,
                    "alt": q.kCGEventFlagMaskAlternate,
                    "ctrl": q.kCGEventFlagMaskControl,
                }.items():
                    if key in self._modifiers():
                        flags |= mask
                q.CGEventSetFlags(event, flags)
            q.CGEventSetIntegerValueField(event, q.kCGEventSourceUserData, 0x52485542)
            q.CGEventPost(q.kCGHIDEventTap, event)
            return
        if self.controller is not None and hasattr(self.controller, "scroll"):
            self.controller.scroll(0, amount)
            return
        if self.controller is None and _use_linux_uinput():
            self._uinput_backend().scroll(amount)
            return
        if sys.platform == "win32":
            self._check_win32_uipi()
            if _win32_mouse_wheel(amount):
                return
        self._mouse_scroll_backend().scroll(0, amount)

    def input_text(self, text):
        # One plain-text paste handles Unicode and line breaks across backends.
        # Keep it on the clipboard: timed restoration can race the receiving app.
        from PySide6.QtWidgets import QApplication
        Action("text", text=text).validate()
        if self.held:
            raise RuntimeError("请先结束当前语音输入或按键动作，再输入文本")
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("文本输入需要运行桌面应用")
        app.clipboard().setText(text)
        if sys.platform == "darwin":
            self.shortcut(["cmd", "v"])
        elif sys.platform.startswith("linux"):
            if _use_linux_uinput():
                wl_copy = shutil.which("wl-copy")
                if wl_copy:
                    try:
                        subprocess.run([wl_copy], input=text.encode("utf-8"), timeout=1.0, check=False)
                    except Exception:
                        pass
                self.shortcut(["ctrl", "v"])
            else:
                from .linux_desktop import is_x11_foreground_terminal
                if is_x11_foreground_terminal():
                    self.shortcut(["ctrl", "shift", "v"])
                else:
                    self.shortcut(["ctrl", "v"])
        else:
            self.shortcut(["ctrl", "v"])
        return "已请求输入文本"


class MouseOutput:
    """Direct mouse button press/release and multi-click state at the current cursor."""

    DOUBLE_CLICK_WINDOW_S = 0.50
    DOUBLE_CLICK_MAX_DIST = 6.0

    def __init__(self, keys: KeyOutput | None = None, *, clock=time.monotonic):
        self.keys = keys
        self.clock = clock
        self.controller = None
        self.button_enum = None
        self._uinput: LinuxUInputBackend | None = None
        self._owners: dict[str, tuple[str, int]] = {}  # owner -> (button, click_state)
        self._last_click: dict[str, tuple[float, tuple[float, float], int]] = {}

    def _backend(self):
        if self.controller is None:
            from pynput.mouse import Button, Controller
            self.controller = Controller()
            self.button_enum = Button
        return self.controller

    def _uinput_backend(self) -> LinuxUInputBackend:
        if self._uinput is None:
            sleeper = self.keys.sleeper if self.keys is not None else time.sleep
            self._uinput = LinuxUInputBackend.shared(sleeper=sleeper)
        return self._uinput

    def _cursor_point(self) -> tuple[float, float]:
        if sys.platform == "darwin":
            import Quartz as q
            loc = q.CGEventGetLocation(q.CGEventCreate(None))
            return float(loc.x), float(loc.y)
        if self.controller is None and _use_linux_uinput():
            try:
                from PySide6.QtGui import QCursor
                pos = QCursor.pos()
                return float(pos.x()), float(pos.y())
            except Exception:
                return 0.0, 0.0
        pos = self._backend().position
        return float(pos[0]), float(pos[1])

    def _modifiers(self) -> list[str]:
        return self.keys._modifiers() if self.keys is not None else []

    def _mac_mouse(self, button: str, down: bool, click_state: int, modifiers: list[str]):
        import Quartz as q
        from ApplicationServices import AXIsProcessTrusted
        if not AXIsProcessTrusted():
            raise PermissionError(f"请在系统设置中允许 {APP_NAME} 的辅助功能权限")
        types = {
            "left": (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp, q.kCGMouseButtonLeft),
            "right": (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp, q.kCGMouseButtonRight),
            "middle": (q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp, q.kCGMouseButtonCenter),
        }
        down_t, up_t, btn_const = types[button]
        loc = q.CGEventGetLocation(q.CGEventCreate(None))
        event = q.CGEventCreateMouseEvent(None, down_t if down else up_t, loc, btn_const)
        q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, int(click_state))
        q.CGEventSetIntegerValueField(event, q.kCGEventSourceUserData, 0x52485542)
        flags = 0
        for k, mask in {
            "cmd": q.kCGEventFlagMaskCommand,
            "shift": q.kCGEventFlagMaskShift,
            "alt": q.kCGEventFlagMaskAlternate,
            "ctrl": q.kCGEventFlagMaskControl,
        }.items():
            if k in modifiers:
                flags |= mask
        if flags:
            q.CGEventSetFlags(event, flags)
        q.CGEventPost(q.kCGHIDEventTap, event)

    def _send_mouse(self, button: str, down: bool, click_state: int, modifiers: list[str]):
        if sys.platform == "darwin":
            return self._mac_mouse(button, down, click_state, modifiers)
        if self.controller is None and _use_linux_uinput():
            return self._uinput_backend().send_mouse_button(button, down)
        backend = self._backend()
        btn = getattr(self.button_enum, button)
        if down:
            backend.press(btn)
        else:
            backend.release(btn)

    def _next_click_state(self, button: str) -> int:
        now = self.clock()
        pt = self._cursor_point()
        prev = self._last_click.get(button)
        if prev is not None:
            prev_t, prev_pt, prev_state = prev
            dist = math.hypot(pt[0] - prev_pt[0], pt[1] - prev_pt[1])
            if now - prev_t <= self.DOUBLE_CLICK_WINDOW_S and dist <= self.DOUBLE_CLICK_MAX_DIST:
                state = min(3, prev_state + 1)
                self._last_click[button] = (now, pt, state)
                return state
        self._last_click[button] = (now, pt, 1)
        return 1

    def hold(self, owner: str, button: str):
        if button not in ("left", "right", "middle") or not owner:
            raise ValueError("不支持的鼠标按键")
        existing = self._owners.get(owner)
        if existing is not None:
            if existing[0] == button:
                return
            self.release(owner)
        for other_owner, (held_btn, _) in self._owners.items():
            if held_btn == button and other_owner != owner:
                raise RuntimeError("鼠标该按键正被另一操作按住，请先松开")
        click_state = self._next_click_state(button)
        self._owners[owner] = (button, click_state)
        try:
            self._send_mouse(button, True, click_state, self._modifiers())
        except Exception:
            self._owners.pop(owner, None)
            raise

    def release(self, owner: str):
        held = self._owners.pop(owner, None)
        if held is None:
            return
        button, click_state = held
        if any(b == button for b, _ in self._owners.values()):
            return
        self._send_mouse(button, False, click_state, self._modifiers())

    def release_all(self):
        errors = []
        for owner in list(self._owners):
            try:
                self.release(owner)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def click(self, target: str) -> str:
        mods = self._modifiers()
        if target == "left_double":
            for state in (1, 2):
                self._send_mouse("left", True, state, mods)
                self._send_mouse("left", False, state, mods)
            return "已双击鼠标左键"
        if target not in ("left", "right", "middle"):
            raise ValueError("不支持的鼠标操作")
        self._send_mouse(target, True, 1, mods)
        self._send_mouse(target, False, 1, mods)
        labels = {"left": "左键", "right": "右键", "middle": "中键"}
        return f"已点击鼠标{labels[target]}"


class ActionRunner:
    def __init__(self, keys=None, voice=None, *, holds=None, mouse=None, mouse_holds=None, switcher=None, overlay=None, macro=None, cycle_profile=None, ax_executor=None, text_enter=None):
        self.keys = keys or KeyOutput()
        self.voice = voice
        self.overlay = overlay
        self.macro = macro
        self.cycle_profile = cycle_profile
        self.ax_executor = ax_executor
        self.text_enter = text_enter
        self._ax_backend = None
        self._ax_cache = {}
        self._fallback_macros = None
        from .app_switcher import AppSwitcher
        from .holds import InputHolds, MouseHolds
        self.holds = holds or InputHolds(self.keys)
        self.mouse = mouse or MouseOutput(self.keys)
        self.mouse_holds = mouse_holds or MouseHolds(self.mouse)
        self.switcher = switcher or AppSwitcher(self.holds)
        from .windows import WindowController
        self.windows = WindowController()
        self.system_hotkeys = None
        if sys.platform == "darwin":
            from .system_hotkeys import SystemHotkeys
            self.system_hotkeys = SystemHotkeys()

    def run(self, action: Action, down=True, owner="mapping"):
        action.validate(sys.platform)
        if action.kind == "voice":
            if self.voice is None:
                raise RuntimeError("请先配置语音输入")
            return self.voice(down)
        if action.kind == "hold":
            if action.target == "latch":
                if down:
                    self.holds.toggle(owner, action.keys)
            elif down:
                self.holds.hold(owner, action.keys)
            else:
                self.holds.release(owner)
            return
        if action.kind == "mouse":
            if owner in ("macro", "grid") or action.target == "left_double":
                if down:
                    return self.mouse.click(action.target)
                return
            if down:
                self.mouse_holds.hold(owner, action.target)
            else:
                self.mouse_holds.release(owner)
            return
        if action.kind == "cheat_sheet":
            if self.overlay is None:
                raise RuntimeError("当前环境未启用屏幕浮层")
            return self.overlay(action, down, owner)
        if action.kind == "grid":
            if not down:
                return
            if self.overlay is None:
                raise RuntimeError("当前环境未启用屏幕浮层")
            return self.overlay(action, True, owner)
        if action.kind == "macro":
            if not down:
                return
            if self.macro is not None:
                return self.macro(action, owner)
            if self._fallback_macros is None:
                from .macro_runner import MacroRunner
                self._fallback_macros = MacroRunner(
                    lambda step_act, _idx: self.run(step_act, True, owner="macro"),
                    send_enter=lambda: self.keys.shortcut(["enter"]),
                )
            return self._fallback_macros.start(action, owner)
        if action.kind == "cycle_profile":
            if not down:
                return
            if self.cycle_profile is None:
                raise RuntimeError("当前环境不支持切换配置方案")
            return self.cycle_profile(owner)
        if not down or action.kind == "none":
            return
        if action.kind == "shortcut":
            return self.keys.shortcut(action.keys)
        if action.kind == "text":
            result = self.keys.input_text(action.text)
            if action.enter and owner != "macro":
                if self.text_enter is not None:
                    self.text_enter(owner)
                else:
                    self.keys.shortcut(["enter"])
            return result
        if action.kind in ("ui_element", "app_menu"):
            if self.ax_executor is not None:
                return self.ax_executor(action)
            if sys.platform not in ("darwin", "win32"):
                raise RuntimeError("界面控件与应用菜单动作当前仅支持 macOS 与 Windows")
            from .ax_actions import create_ax_action_backend, execute_app_menu, execute_ui_element
            if self._ax_backend is None:
                self._ax_backend = create_ax_action_backend()
            if action.kind == "ui_element":
                return execute_ui_element(self._ax_backend, action, cache=self._ax_cache)
            return execute_app_menu(self._ax_backend, action)
        if action.kind == "common":
            from .common_actions import MEDIA_ACTIONS, SCROLL_ACTIONS, common_keys, open_system_settings
            if self.system_hotkeys is not None and action.target in self.system_hotkeys.bindings.keys() | self.system_hotkeys.errors.keys():
                if self.keys.held or self.holds.active:
                    raise RuntimeError("请先结束语音输入或释放修饰键，再执行系统快捷键")
                return self.system_hotkeys.execute(action.target, log=self.keys.log)
            if action.target == "app_switcher":
                return self.switcher.advance(owner)
            if action.target == "system_settings":
                return open_system_settings()
            if action.target == "lock_screen" and sys.platform == "win32":
                return _lock_windows_workstation()
            if action.target == "lock_screen" and sys.platform.startswith("linux"):
                return _lock_linux_session(self.keys)
            if action.target in MEDIA_ACTIONS:
                return self.keys.media(action.target)
            if action.target in SCROLL_ACTIONS:
                return self.keys.scroll(action.target, with_held=self.holds.active)
            return self.keys.shortcut(common_keys(action.target))
        if action.kind == "minimize_window":
            return self.windows.minimize_current()
        if action.kind == "toggle_app":
            return self.windows.toggle(action.target, lambda: self.run(Action("app", target=action.target)))
        if action.kind in ("close_window", "quit_app"):
            return self.windows.close_current(action.kind == "quit_app")
        cmd = launch_command(action)
        if cmd is None:
            os.startfile(str(Path(action.target).expanduser().absolute()))
            return
        if sys.platform.startswith("linux") and cmd[0] == "gio" and not shutil.which("gio"):
            from .linux_desktop import launch_desktop_entry_command
            fallback_cmd = launch_desktop_entry_command(action.target)
            if fallback_cmd:
                cmd = fallback_cmd
        if not Path(cmd[0]).is_absolute() and not shutil.which(cmd[0]):
            raise RuntimeError(f"本系统缺少打开工具：{cmd[0]}")
        if sys.platform == "darwin":
            return LaunchRequest(cmd)
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
