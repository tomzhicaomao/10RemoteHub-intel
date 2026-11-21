"""Read-only permission checks; settings links open only on a user click."""
import os
from pathlib import Path
import subprocess
import sys


MAC_PANES = {
    "accessibility": "Privacy_Accessibility",
    "input": "Privacy_ListenEvent",
}

LINUX_UDEV_PATHS = (
    Path("/etc/udev/rules.d/70-10remotehub.rules"),
    Path("/lib/udev/rules.d/70-10remotehub.rules"),
    Path("/usr/lib/udev/rules.d/70-10remotehub.rules"),
)


def settings_window():
    """Return visible Settings bounds and whether the guide may be shown.

    Read public window metadata only: no titles, screen capture, Accessibility
    automation, permission request or changes to another application's window.
    Coordinates are logical screen points, with the main screen's top-left origin.
    """
    if sys.platform != "darwin":
        return None
    from AppKit import NSRunningApplication, NSWorkspace
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly,
    )
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.systempreferences")
    pids = {int(app.processIdentifier()) for app in apps}
    if not pids:
        return None
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    front_pid = int(front.processIdentifier()) if front else None
    foreground = "settings" if front_pid in pids else "self" if front_pid == os.getpid() else "other"
    windows = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    ) or []
    for window in windows:  # Front-to-back order; ignore sheets/menus/tiny panels.
        if window.get("kCGWindowOwnerPID") not in pids or window.get("kCGWindowLayer") != 0:
            continue
        bounds = window.get("kCGWindowBounds", {})
        if bounds.get("Width", 0) >= 480 and bounds.get("Height", 0) >= 320:
            rect = tuple(round(bounds[k]) for k in ("X", "Y", "Width", "Height"))
            return rect, foreground
    return None


def running_app_bundle():
    """Use this process's bundle, never a name search that could pick an old build."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    for path in executable.parents:
        if path.suffix.lower() == ".app" and (path / "Contents/Info.plist").is_file():
            return path
    return None


def reveal_running_app():
    bundle = running_app_bundle()
    if bundle is None:
        raise RuntimeError("无法定位当前应用，请从打包后的 App 打开此功能")
    subprocess.run(["/usr/bin/open", "-R", str(bundle)], check=True, timeout=10)


def bundled_udev_rule_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for rel in ("native/linux-udev/70-10remotehub.rules", "udev/70-10remotehub.rules"):
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return base / "native/linux-udev/70-10remotehub.rules"


def linux_access_status(
    *,
    sysfs_root: Path = Path("/sys/bus/usb/devices"),
    dev_bus_root: Path = Path("/dev/bus/usb"),
    uinput_path: Path = Path("/dev/uinput"),
    rule_paths=LINUX_UDEV_PATHS,
) -> dict[str, str]:
    """Read-only check of Linux udev rule, receiver USB node access, and Wayland uinput."""
    rule_installed = any(Path(p).is_file() for p in rule_paths)
    receiver_rw = None
    if Path(sysfs_root).is_dir():
        for entry in Path(sysfs_root).iterdir():
            try:
                vid = (entry / "idVendor").read_text(encoding="utf-8").strip().lower()
                pid = (entry / "idProduct").read_text(encoding="utf-8").strip().lower()
                if (vid, pid) != ("0416", "0602"):
                    continue
                busnum = int((entry / "busnum").read_text(encoding="utf-8").strip())
                devnum = int((entry / "devnum").read_text(encoding="utf-8").strip())
                node = Path(dev_bus_root) / f"{busnum:03d}" / f"{devnum:03d}"
                receiver_rw = bool(node.exists() and os.access(node, os.R_OK | os.W_OK))
                break
            except (OSError, ValueError):
                continue
    if receiver_rw is True:
        usb_state = "已授权"
    elif receiver_rw is False:
        usb_state = "未授权（需配置 udev）"
    else:
        usb_state = "已安装规则" if rule_installed else "尚未配置 udev"

    is_wayland = os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or (
        bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("DISPLAY")
    )
    if Path(uinput_path).exists() and os.access(uinput_path, os.W_OK):
        uinput_state = "已授权"
    elif is_wayland:
        uinput_state = "未授权（Wayland 发键需 uinput）"
    else:
        uinput_state = "X11 免配置"
    return {
        "udev_rule": "已安装" if rule_installed else "未安装",
        "usb": usb_state,
        "uinput": uinput_state,
    }


def permission_status():
    if sys.platform == "darwin":
        result = {}
        for name in MAC_PANES:
            try:
                if name == "accessibility":
                    from ApplicationServices import AXIsProcessTrusted
                    allowed = AXIsProcessTrusted()
                else:
                    from Quartz import CGPreflightListenEventAccess
                    allowed = CGPreflightListenEventAccess()
                result[name] = "已授权" if allowed else "未授权"
            except Exception:
                result[name] = "无法检测，请在系统设置中查看"
        return result
    if sys.platform.startswith("linux"):
        return linux_access_status()
    return {}


def open_permission_settings(name):
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    if sys.platform != "darwin" or name not in MAC_PANES:
        raise ValueError("此平台没有对应的 macOS 授权页面")
    url = "x-apple.systempreferences:com.apple.preference.security?" + MAC_PANES[name]
    if not QDesktopServices.openUrl(QUrl(url)):
        raise RuntimeError("无法打开系统设置，请手动前往“隐私与安全性”")
