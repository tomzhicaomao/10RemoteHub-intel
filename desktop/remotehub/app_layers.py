"""Context-aware foreground application discovery and event-driven layer matching."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import plistlib
import sys

from PySide6.QtCore import QObject, QTimer, Signal

from .model import AppLayer, Profile, SELF_APP_IDS, normalize_app_layer_id, validate_app_layer_id

WINDOWS_SYSTEM_HOSTS = {"applicationframehost.exe", "shellexperiencehost.exe", "searchhost.exe", "winlogon.exe"}


@dataclass(frozen=True)
class LayerApp:
    app_id: str
    name: str


def running_layer_apps(platform: str | None = None) -> list[LayerApp]:
    """Return currently open desktop applications (including browsers), excluding self."""
    target_platform = platform or sys.platform
    if target_platform == "darwin":
        from AppKit import NSWorkspace
        found: dict[str, LayerApp] = {}
        own_pid = os.getpid()
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if app.activationPolicy() != 0 or int(app.processIdentifier()) == own_pid:
                continue
            bundle = str(app.bundleIdentifier() or "").strip()
            if not bundle:
                url = app.executableURL()
                if url and url.path():
                    bundle = normalize_app_layer_id("exe:" + Path(str(url.path())).name)
            else:
                bundle = normalize_app_layer_id(bundle)
            if not bundle or bundle.casefold() in SELF_APP_IDS:
                continue
            try:
                validate_app_layer_id(bundle)
            except ValueError:
                continue
            name = str(app.localizedName() or bundle).strip()[:100] or bundle
            found.setdefault(bundle, LayerApp(bundle, name))
        return sorted(found.values(), key=lambda item: (item.name.casefold(), item.app_id))

    if target_platform == "win32":
        from .win_uia import pe_friendly_name
        from .windows import WinWindows
        found: dict[str, LayerApp] = {}
        own_pid = os.getpid()
        for win in WinWindows()._all():
            if win.pid == own_pid or not win.executable:
                continue
            win_path = PureWindowsPath(win.executable)
            exe_file = win_path.name
            if not exe_file or exe_file.casefold() in WINDOWS_SYSTEM_HOSTS:
                continue
            app_id = normalize_app_layer_id("exe:" + exe_file)
            if app_id.casefold() in SELF_APP_IDS:
                continue
            try:
                validate_app_layer_id(app_id)
            except ValueError:
                continue
            display = pe_friendly_name(win.executable) or win_path.stem or exe_file
            found.setdefault(app_id, LayerApp(app_id, display[:100]))
        return sorted(found.values(), key=lambda item: (item.name.casefold(), item.app_id))

    if str(target_platform).startswith("linux"):
        from .linux_desktop import is_wayland_session, match_x11_window_to_layer_app, scan_desktop_entries
        if is_wayland_session() or not os.environ.get("DISPLAY"):
            return []
        try:
            from .windows import X11Windows
            x11 = X11Windows()
        except Exception:
            return []
        found: dict[str, LayerApp] = {}
        own_pid = os.getpid()
        try:
            catalog = scan_desktop_entries()
            for win in x11._all():
                if win.pid == own_pid:
                    continue
                wm_cls = tuple(win.handle.get_wm_class() or ())
                matched = match_x11_window_to_layer_app(win.executable, wm_cls, catalog)
                if matched is None:
                    continue
                app_id, display = matched
                found.setdefault(app_id.casefold(), LayerApp(app_id, display[:100]))
        finally:
            x11.close_display()
        return sorted(found.values(), key=lambda item: (item.name.casefold(), item.app_id))

    return []


def layer_app_from_path(path: str | Path, platform: str | None = None) -> LayerApp:
    """Build a privacy-safe LayerApp identifier from a chosen .app, .exe/.lnk, or .desktop/binary."""
    target_platform = platform or sys.platform
    candidate = Path(path).expanduser()
    if target_platform == "darwin":
        if candidate.suffix.casefold() != ".app":
            raise ValueError("请选择 macOS 应用程序（.app）")
        plist_path = candidate / "Contents" / "Info.plist"
        if not plist_path.is_file():
            raise ValueError("所选应用缺少 Info.plist")
        with plist_path.open("rb") as source:
            info = plistlib.load(source)
        raw_id = str(info.get("CFBundleIdentifier") or "").strip()
        if not raw_id:
            exe = str(info.get("CFBundleExecutable") or candidate.stem).strip()
            raw_id = "exe:" + exe
        app_id = normalize_app_layer_id(raw_id)
        validate_app_layer_id(app_id)
        name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or candidate.stem).strip()[:100]
        return LayerApp(app_id, name or candidate.stem[:100])

    if target_platform == "win32":
        from .win_uia import pe_friendly_name, resolve_windows_app_alias, resolve_windows_shortcut
        orig_win = PureWindowsPath(str(path))
        resolved_str = resolve_windows_shortcut(path) if orig_win.suffix.casefold() == ".lnk" else resolve_windows_app_alias(path)
        win_candidate = PureWindowsPath(resolved_str)
        if win_candidate.suffix.casefold() != ".exe":
            raise ValueError("请选择 Windows 应用程序（.exe）或快捷方式（.lnk）")
        app_id = normalize_app_layer_id("exe:" + win_candidate.name)
        validate_app_layer_id(app_id)
        if orig_win.suffix.casefold() == ".lnk" and orig_win.stem.strip():
            name = orig_win.stem.strip()[:100]
        else:
            name = (pe_friendly_name(resolved_str) or win_candidate.stem or win_candidate.name).strip()[:100]
        return LayerApp(app_id, name)

    if str(target_platform).startswith("linux"):
        from .linux_desktop import parse_desktop_file
        if candidate.suffix.casefold() == ".desktop":
            parsed = parse_desktop_file(candidate)
            if parsed is not None:
                return LayerApp(parsed.app_id, parsed.name)
            app_id = validate_app_layer_id(normalize_app_layer_id(candidate.stem))
            return LayerApp(app_id, candidate.stem[:100])
        if not candidate.name:
            raise ValueError("请选择 Linux 应用程序（.desktop 或可执行文件）")
        app_id = validate_app_layer_id(normalize_app_layer_id("exe:" + candidate.name))
        return LayerApp(app_id, (candidate.stem or candidate.name)[:100])

    raise ValueError("当前平台暂不支持从路径选择应用层")


def match_app_layer(profile: Profile, app_id: str | None) -> AppLayer | None:
    """Return the active AppLayer matching foreground app_id, or None for Base Layer."""
    if not app_id:
        return None
    key = normalize_app_layer_id(app_id).casefold()
    if not key or key in SELF_APP_IDS:
        return None
    for layer in profile.app_layers:
        if layer.enabled and normalize_app_layer_id(layer.app_id).casefold() == key:
            return layer
    return None


class ForegroundAppTracker(QObject):
    """Foreground application resolver across macOS, Windows, and Linux X11."""

    changed = Signal(str, str)  # app_id, name

    def __init__(self, parent=None, *, live: bool = True, provider=None):
        super().__init__(parent)
        self.live = live
        self._provider = provider
        self._observer = None
        self._win_helper = None
        self._win_cache: tuple[int, int, LayerApp | None] = (0, 0, None)
        self._x11 = None
        self._x11_catalog = None
        self._x11_cache: tuple[int, LayerApp | None] = (0, None)
        self._x11_timer: QTimer | None = None
        self._last_emitted: tuple[str, str] | None = None
        self._closed = False

    def set_watching(self, active: bool):
        if self._closed or not self.live or self._provider is not None:
            return
        if not active:
            if self._observer is not None:
                self._observer.close()
                self._observer = None
            if self._x11_timer is not None:
                self._x11_timer.stop()
            return
        if sys.platform == "darwin" and self._observer is None:
            from .mac_chat_focus import MacWorkspaceObserver
            self._observer = MacWorkspaceObserver(self._on_observer_activated)
        elif sys.platform == "win32" and self._observer is None:
            from .win_uia import WinEventObserver
            self._observer = WinEventObserver(self._on_observer_activated)
        elif sys.platform.startswith("linux"):
            from .linux_desktop import is_wayland_session
            if not is_wayland_session() and os.environ.get("DISPLAY"):
                if self._x11_timer is None:
                    self._x11_timer = QTimer(self)
                    self._x11_timer.setInterval(450)
                    self._x11_timer.timeout.connect(self._poll_x11_active_window)
                if not self._x11_timer.isActive():
                    self._x11_timer.start()

    def _emit_current(self):
        current = self.current_app()
        pair = (current.app_id, current.name) if current is not None else ("", "")
        if pair != self._last_emitted:
            self._last_emitted = pair
            self.changed.emit(*pair)

    def _on_observer_activated(self):
        if self._closed or self._observer is None:
            return
        self._emit_current()

    def _poll_x11_active_window(self):
        if self._closed:
            return
        self._emit_current()

    def current_app(self) -> LayerApp | None:
        if self._closed:
            return None
        if self._provider is not None:
            return self._provider()
        if not self.live:
            return None
        if sys.platform == "darwin":
            if self._observer is not None:
                app = self._observer.current_app
            else:
                from AppKit import NSWorkspace
                from .mac_chat_focus import _app_from_running
                app = _app_from_running(NSWorkspace.sharedWorkspace().frontmostApplication())
            if app is None:
                from AppKit import NSWorkspace
                front = NSWorkspace.sharedWorkspace().frontmostApplication()
                if front is None or int(front.processIdentifier()) == os.getpid():
                    return None
                url = front.executableURL()
                if not url or not url.path():
                    return None
                app_id = normalize_app_layer_id("exe:" + Path(str(url.path())).name)
                if not app_id or app_id.casefold() in SELF_APP_IDS:
                    return None
                name = str(front.localizedName() or Path(str(url.path())).name).strip()[:100]
                return LayerApp(app_id, name or app_id)
            if app.pid == os.getpid():
                return None
            app_id = normalize_app_layer_id(app.bundle)
            if not app_id or app_id.casefold() in SELF_APP_IDS:
                return None
            return LayerApp(app_id, app.name)
        if sys.platform == "win32":
            return self._current_windows_app()
        if sys.platform.startswith("linux"):
            return self._current_linux_app()
        return None

    def current_app_id(self) -> str:
        app = self.current_app()
        return app.app_id if app is not None else ""

    def current_app_name(self) -> str:
        app = self.current_app()
        return app.name if app is not None else ""

    def _current_windows_app(self) -> LayerApp | None:
        try:
            from .win_uia import pe_friendly_name
            if self._win_helper is None:
                from .windows import WinWindows
                self._win_helper = WinWindows()
            hwnd = int(self._win_helper.user.GetForegroundWindow() or 0)
            if not hwnd:
                return None
            pid = self._win_helper.w.DWORD()
            self._win_helper.user.GetWindowThreadProcessId(hwnd, self._win_helper.c.byref(pid))
            pid_val = int(pid.value)
            if pid_val == os.getpid():
                return None
            cached_hwnd, cached_pid, cached_app = self._win_cache
            if (hwnd, pid_val) == (cached_hwnd, cached_pid):
                return cached_app
            info = self._win_helper._info(hwnd)
            result = None
            if info and info.executable and getattr(info, "pid", pid_val) != os.getpid():
                win_path = PureWindowsPath(info.executable)
                exe_name = win_path.name
                if exe_name and exe_name.casefold() not in WINDOWS_SYSTEM_HOSTS:
                    app_id = normalize_app_layer_id("exe:" + exe_name)
                    if app_id.casefold() not in SELF_APP_IDS:
                        display = pe_friendly_name(info.executable) or win_path.stem or exe_name
                        result = LayerApp(app_id, display[:100])
            self._win_cache = (hwnd, pid_val, result)
            return result
        except Exception:
            return None

    def _current_linux_app(self) -> LayerApp | None:
        try:
            from .linux_desktop import is_wayland_session, match_x11_window_to_layer_app, scan_desktop_entries
            if is_wayland_session() or not os.environ.get("DISPLAY"):
                return None
            if self._x11 is None:
                from .windows import X11Windows
                self._x11 = X11Windows()
            wid = self._x11.active_window_id()
            if not wid:
                return None
            cached_wid, cached_app = self._x11_cache
            if wid == cached_wid:
                return cached_app
            info = self._x11._info(wid)
            result = None
            if info is not None and info.pid != os.getpid():
                if self._x11_catalog is None:
                    self._x11_catalog = scan_desktop_entries()
                wm_cls = tuple(info.handle.get_wm_class() or ())
                matched = match_x11_window_to_layer_app(info.executable, wm_cls, self._x11_catalog)
                if matched is not None:
                    result = LayerApp(matched[0], matched[1][:100])
            self._x11_cache = (wid, result)
            return result
        except Exception:
            if self._x11 is not None:
                self._x11.close_display()
                self._x11 = None
            return None

    def close(self):
        self._closed = True
        if self._observer is not None:
            self._observer.close()
            self._observer = None
        if self._x11_timer is not None:
            self._x11_timer.stop()
            self._x11_timer = None
        if self._x11 is not None:
            self._x11.close_display()
            self._x11 = None
