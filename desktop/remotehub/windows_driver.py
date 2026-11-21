"""Windows USB interface driver state and guided Zadig binding entry.

Interface 3 (vendor voice) and Interface 4 (keyboard / '#' key) should both be
bound to WinUSB for complete exclusive capture. Interface 4 without WinUSB
falls back to Raw Input (missing '#').
"""
from __future__ import annotations

from pathlib import Path
import sys

ENUM_KEY = r"SYSTEM\CurrentControlSet\Enum\USB\VID_0416&PID_0602&MI_03"
ENUM_KEY_KEYBOARD = r"SYSTEM\CurrentControlSet\Enum\USB\VID_0416&PID_0602&MI_04"


def _service_for_enum_key(enum_key: str, *, registry=None) -> str:
    """Return 'winusb' | 'other' | 'missing' for one composite USB interface."""
    if registry is None:
        if sys.platform != "win32":
            return "other"
        try:
            import winreg as registry
        except ImportError:
            return "other"
    try:
        with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, enum_key) as key:
            count = registry.QueryInfoKey(key)[0]
            for index in range(count):
                instance = registry.EnumKey(key, index)
                with registry.OpenKey(key, instance) as sub:
                    try:
                        service = str(registry.QueryValueEx(sub, "Service")[0]).strip()
                    except FileNotFoundError:
                        continue
                    if service:
                        return "winusb" if service.lower() == "winusb" else "other"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "other"
    return "missing"


def binding_detail(*, registry=None) -> dict[str, str]:
    """Return detailed WinUSB status across Interface 3 (voice) and Interface 4 (keys)."""
    voice = _service_for_enum_key(ENUM_KEY, registry=registry)
    keyboard = _service_for_enum_key(ENUM_KEY_KEYBOARD, registry=registry)
    if voice == "winusb" and keyboard == "winusb":
        overall = "winusb"
    elif voice == "winusb":
        overall = "voice_only"
    elif keyboard == "winusb":
        overall = "keys_only"
    elif voice == "missing" and keyboard == "missing":
        overall = "missing"
    else:
        overall = "other"
    return {"state": overall, "voice": voice, "keyboard": keyboard}


def binding_state(*, registry=None) -> str:
    """'winusb' | 'other' | 'missing' for the receiver's vendor voice interface."""
    return _service_for_enum_key(ENUM_KEY, registry=registry)


def helper_path():
    """Bundled Zadig next to the app (local debug setups only; never redistributed)."""
    for base in (Path(sys.executable).parent, Path(__file__).resolve().parent.parent):
        candidate = base / "zadig-2.9.exe"
        if candidate.is_file():
            return candidate
    return None


def launch_helper():
    """Open the binding helper, or the download page when it is not bundled.

    Zadig is GPLv3 and is never redistributed with this project, so users
    download it from zadig.akeo.ie and bind Interface 3 & Interface 4 once.
    """
    path = helper_path()
    if path is None:
        import webbrowser
        webbrowser.open("https://zadig.akeo.ie/")
        return (
            "已打开 Zadig 下载页。装好后勾选 Options → List All Devices，"
            "把 Interface 3 和 Interface 4 都绑定到 WinUSB；详细步骤见 INSTALL-Windows.md。"
        )
    import ctypes
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(path), None, None, 1)
    if result <= 32:
        raise RuntimeError("无法以管理员身份启动绑定工具")
    return "已打开绑定工具：请勾选 Options → List All Devices，把 Interface 3 和 Interface 4 分别绑定到 WinUSB。"
