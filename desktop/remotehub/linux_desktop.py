"""Linux XDG Desktop Entry catalog, terminal detection, and X11 window/layer helpers."""
from __future__ import annotations

import configparser
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil

from .model import SELF_APP_IDS, normalize_app_layer_id, validate_app_layer_id

LINUX_TERMINALS = frozenset({
    "gnome-terminal",
    "gnome-terminal-server",
    "kgx",
    "ptyxis",
    "konsole",
    "xfce4-terminal",
    "mate-terminal",
    "tilix",
    "terminator",
    "alacritty",
    "kitty",
    "wezterm",
    "wezterm-gui",
    "foot",
    "xterm",
    "uxterm",
    "urxvt",
    "lxterminal",
    "qterminal",
    "guake",
    "yakuake",
    "sakura",
    "hyper",
    "warp-terminal",
})


@dataclass(frozen=True)
class DesktopAppEntry:
    app_id: str
    name: str
    desktop_path: str
    exec_name: str
    wm_class: str


def is_wayland_session(env=None) -> bool:
    env = os.environ if env is None else env
    if env.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    return bool(env.get("WAYLAND_DISPLAY")) and not env.get("DISPLAY")


def xdg_application_dirs(home: Path | None = None, env=None) -> list[Path]:
    home = Path(home) if home is not None else Path.home()
    env = os.environ if env is None else env
    data_home_raw = env.get("XDG_DATA_HOME", "")
    data_home = Path(data_home_raw) if data_home_raw and Path(data_home_raw).is_absolute() else home / ".local/share"
    raw_dirs = env.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    dirs = [data_home / "applications"]
    for part in raw_dirs.split(":"):
        if part.strip():
            dirs.append(Path(part.strip()) / "applications")
    for extra in (
        home / ".local/share/flatpak/exports/share/applications",
        Path("/var/lib/flatpak/exports/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
    ):
        if extra not in dirs:
            dirs.append(extra)
    return dirs


def parse_desktop_file(path: Path) -> DesktopAppEntry | None:
    """Parse an XDG `.desktop` file into a privacy-safe DesktopAppEntry."""
    try:
        if not path.is_file() or path.stat().st_size > 256_000:
            return None
        cfg = configparser.ConfigParser(interpolation=None, strict=False)
        cfg.read(path, encoding="utf-8")
        if "Desktop Entry" not in cfg:
            return None
        sec = cfg["Desktop Entry"]
        if sec.get("Type", "").strip() != "Application":
            return None
        if sec.get("NoDisplay", "").strip().lower() == "true" or sec.get("Hidden", "").strip().lower() == "true":
            return None
        name = (
            sec.get("Name[zh_CN]")
            or sec.get("Name[zh_TW]")
            or sec.get("Name[zh]")
            or sec.get("Name")
            or path.stem
        ).strip()
        exec_raw = sec.get("Exec", "").strip()
        if not exec_raw:
            return None
        try:
            tokens = shlex.split(exec_raw)
        except ValueError:
            tokens = exec_raw.split()
        # Strip leading `env KEY=VAL` wrappers if present
        idx = 0
        if tokens and Path(tokens[0]).name == "env":
            idx = 1
            while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
                idx += 1
        cmd_token = tokens[idx] if idx < len(tokens) else (tokens[0] if tokens else "")
        exec_name = Path(cmd_token).name.strip() if cmd_token else ""
        wm_class = sec.get("StartupWMClass", "").strip()
        stem = path.stem.strip()
        candidate_id = stem if len(stem) >= 2 else (f"exe:{exec_name}" if exec_name else "")
        app_id = normalize_app_layer_id(candidate_id)
        try:
            app_id = validate_app_layer_id(app_id)
        except ValueError:
            if exec_name:
                app_id = validate_app_layer_id(normalize_app_layer_id("exe:" + exec_name))
            else:
                return None
        return DesktopAppEntry(
            app_id=app_id,
            name=(name or stem)[:100],
            desktop_path=str(path),
            exec_name=exec_name.casefold(),
            wm_class=wm_class.casefold(),
        )
    except Exception:
        return None


def scan_desktop_entries(dirs=None) -> list[DesktopAppEntry]:
    """Return deduplicated XDG desktop applications ordered by localized name."""
    search_dirs = list(dirs) if dirs is not None else xdg_application_dirs()
    seen_ids: set[str] = set()
    entries: list[DesktopAppEntry] = []
    for folder in search_dirs:
        if not Path(folder).is_dir():
            continue
        try:
            files = sorted(Path(folder).rglob("*.desktop"))[:600]
        except OSError:
            continue
        for file in files:
            entry = parse_desktop_file(file)
            if entry is None:
                continue
            key = entry.app_id.casefold()
            if key in seen_ids or key in SELF_APP_IDS:
                continue
            seen_ids.add(key)
            entries.append(entry)
    return sorted(entries, key=lambda item: (item.name.casefold(), item.app_id))


def match_x11_window_to_layer_app(executable: str, wm_classes: tuple[str, ...], catalog: list[DesktopAppEntry] | None = None) -> tuple[str, str] | None:
    """Resolve an X11 window's `/proc/<pid>/exe` + `WM_CLASS` to `(app_id, display_name)`."""
    exe_name = Path(executable).name.strip() if executable else ""
    classes_lower = [c.strip().casefold() for c in wm_classes if c and c.strip()]
    entries = catalog if catalog is not None else scan_desktop_entries()

    for cls in classes_lower:
        for entry in entries:
            if (entry.wm_class and entry.wm_class == cls) or entry.app_id.casefold() == cls or entry.app_id.casefold().endswith("." + cls):
                return entry.app_id, entry.name
    if exe_name:
        exe_low = exe_name.casefold()
        for entry in entries:
            if entry.exec_name and entry.exec_name == exe_low:
                return entry.app_id, entry.name
        raw_id = normalize_app_layer_id("exe:" + exe_name)
        if raw_id.casefold() not in SELF_APP_IDS:
            try:
                validate_app_layer_id(raw_id)
                display = wm_classes[-1].strip() if wm_classes and wm_classes[-1].strip() else exe_name
                return raw_id, display[:100]
            except ValueError:
                pass
    for cls in reversed(classes_lower):
        raw_id = normalize_app_layer_id("exe:" + cls)
        if raw_id.casefold() not in SELF_APP_IDS:
            try:
                validate_app_layer_id(raw_id)
                return raw_id, cls[:100]
            except ValueError:
                continue
    return None


def is_x11_foreground_terminal() -> bool:
    """Return True if the active X11 window is a terminal emulator requiring Ctrl+Shift+V."""
    if is_wayland_session() or not os.environ.get("DISPLAY"):
        return False
    try:
        from .windows import X11Windows
        backend = X11Windows()
        try:
            fg = backend.foreground()
            if fg is None:
                return False
            exe_name = Path(fg.executable).name.casefold() if fg.executable else ""
            if exe_name in LINUX_TERMINALS:
                return True
            wm_class = fg.handle.get_wm_class() or ()
            return any(str(c).strip().casefold() in LINUX_TERMINALS for c in wm_class)
        finally:
            backend.close_display()
    except Exception:
        return False


def desktop_entry_display_name(path_str: str) -> str:
    p = Path(path_str)
    if p.suffix.casefold() == ".desktop":
        parsed = parse_desktop_file(p)
        if parsed is not None and parsed.name:
            return parsed.name
        return p.stem
    return p.name


def launch_desktop_entry_command(desktop_path: Path) -> list[str]:
    """Build a shell-free argv to launch an XDG `.desktop` file."""
    if shutil.which("gio"):
        return ["gio", "launch", str(desktop_path)]
    if shutil.which("gtk-launch"):
        return ["gtk-launch", desktop_path.name]
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    cfg.read(desktop_path, encoding="utf-8")
    exec_line = cfg.get("Desktop Entry", "Exec", fallback="").strip()
    if exec_line:
        parts = [tok for tok in shlex.split(exec_line) if not tok.startswith("%")]
        if parts:
            return parts
    return ["xdg-open", str(desktop_path)]
