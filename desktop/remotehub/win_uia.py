"""Windows UI Automation (UIA), Win32 HMENU, UWP, PE version, and .lnk adapters.

Pure Python + stdlib `ctypes` implementation — requires zero external COM or
Win32 pip packages, keeping the Windows dependency lockfile minimal and auditable.
"""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import struct
import threading
import time
from typing import NamedTuple

from .chat_focus import App, chat_metadata
from .chat_focus_locator import describe as locator_describe, resolve as locator_resolve
from .model import SELF_APP_IDS, normalize_app_layer_id, validate_app_layer_id

# COM CLSIDs & IIDs (Windows SDK UIAutomationClient.h)
CLSID_CUIAutomation = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
CLSID_CUIAutomation8 = "{E22AD333-B25F-460C-83D0-0581107395C9}"
IID_IUIAutomation = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"
CLSID_ShellLink = "{00021401-0000-0000-C000-000000000046}"
IID_IShellLinkW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPersistFile = "{0000010B-0000-0000-C000-000000000046}"

CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2
COINIT_MULTITHREADED = 0x0

# UIA Pattern IDs
UIA_InvokePatternId = 10000
UIA_ValuePatternId = 10002
UIA_ExpandCollapsePatternId = 10005
UIA_SelectionItemPatternId = 10010
UIA_TextPatternId = 10014
UIA_TogglePatternId = 10015
UIA_LegacyIAccessiblePatternId = 10018

# UIA Property IDs
UIA_AriaPropertiesPropertyId = 30102

# UIA ControlType IDs -> canonical AX* roles used by chat_focus & ax_actions
UIA_CONTROL_ROLES = {
    50000: "AXButton",          # Button
    50002: "AXCheckBox",        # CheckBox
    50003: "AXComboBox",        # ComboBox
    50004: "AXTextField",       # Edit (promoted to AXTextArea when multiline/composer)
    50005: "AXButton",          # Hyperlink
    50006: "AXImage",           # Image
    50007: "AXGroup",           # ListItem
    50008: "AXList",            # List
    50009: "AXMenu",            # Menu
    50010: "AXMenuBar",         # MenuBar
    50011: "AXMenuItem",        # MenuItem
    50012: "AXValueIndicator",  # ProgressBar
    50013: "AXRadioButton",     # RadioButton
    50014: "AXScrollBar",       # ScrollBar
    50015: "AXValueIndicator",  # Slider
    50016: "AXTextField",       # Spinner
    50018: "AXGroup",           # Tab
    50019: "AXTab",             # TabItem
    50020: "AXStaticText",      # Text
    50021: "AXGroup",           # ToolBar
    50023: "AXOutline",         # Tree
    50024: "AXGroup",           # TreeItem
    50025: "AXGroup",           # Custom
    50026: "AXGroup",           # Group
    50027: "AXValueIndicator",  # Thumb
    50028: "AXTable",           # DataGrid
    50030: "AXTextArea",        # Document (RichEdit / Chromium / Electron / WebView2 composer)
    50031: "AXMenuButton",      # SplitButton
    50032: "AXWindow",          # Window
    50033: "AXScrollArea",      # Pane
    50036: "AXTable",           # Table
}


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", w.DWORD),
        ("Data2", w.WORD),
        ("Data3", w.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class VARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("val", ctypes.c_uint64),
        ("pad", ctypes.c_uint64),
    ]


def _guid(text: str) -> GUID:
    value = GUID()
    if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(value)) != 0:
        raise ValueError(f"Invalid GUID: {text}")
    return value


def _vmethod(pointer, index: int, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])


_THREAD_COM = threading.local()
_UIA_DLL_HANDLE = None


def ensure_thread_com(mode: int = COINIT_MULTITHREADED) -> None:
    """Keep COM initialized on the current thread and pin UIAutomationCore.dll."""
    global _UIA_DLL_HANDLE
    if os.name != "nt":
        return
    if _UIA_DLL_HANDLE is None:
        try:
            _UIA_DLL_HANDLE = ctypes.WinDLL("UIAutomationCore")
        except Exception:
            pass
    if getattr(_THREAD_COM, "initialized", False):
        return
    ole32 = getattr(getattr(ctypes, "windll", None), "ole32", None)
    if ole32 is not None:
        hr = int(ole32.CoInitializeEx(None, mode)) & 0xFFFFFFFF
        # S_OK (0), S_FALSE (1), or RPC_E_CHANGED_MODE (0x80010106: Qt STA already active)
        if hr in (0, 1, 0x80010106):
            _THREAD_COM.initialized = True


@contextlib.contextmanager
def com_apartment(mode: int = COINIT_MULTITHREADED):
    ole32 = getattr(getattr(ctypes, "windll", None), "ole32", None)
    hr = ole32.CoInitializeEx(None, mode) if ole32 is not None else -1
    try:
        yield
    finally:
        if ole32 is not None and hr in (0, 1):
            ole32.CoUninitialize()


def _release_com(ptr):
    if ptr and getattr(ptr, "value", ptr):
        try:
            _vmethod(ptr, 2)(ptr)
        except Exception:
            pass


def _free_bstr(bstr_val):
    raw = ctypes.cast(bstr_val, ctypes.c_void_p).value if isinstance(bstr_val, ctypes.c_wchar_p) else int(bstr_val or 0)
    if raw:
        try:
            ctypes.windll.oleaut32.SysFreeString(ctypes.c_void_p(raw))
        except Exception:
            pass


# -- Pure-Python + COM .lnk resolver & PE version helper ----------------------


def parse_lnk_bytes(data: bytes) -> str:
    """Extract target .exe path from MS-SHLLINK (.lnk) binary data without COM."""
    if len(data) < 76 or data[:4] != b"\x4c\x00\x00\x00":
        return ""
    flags = struct.unpack_from("<I", data, 20)[0]
    offset = 76
    # HasLinkTargetIDList (bit 0)
    if flags & 0x01:
        if offset + 2 > len(data):
            return ""
        idlist_size = struct.unpack_from("<H", data, offset)[0]
        offset += 2 + idlist_size
    # HasLinkInfo (bit 1)
    if flags & 0x02:
        if offset + 28 > len(data):
            return ""
        info_size, info_hdr_size, info_flags = struct.unpack_from("<III", data, offset)
        if info_size < 28 or offset + info_size > len(data):
            return ""
        if info_flags & 0x01:  # VolumeIDAndLocalBasePath
            if info_hdr_size >= 0x24 and offset + 36 <= len(data):
                uni_off = struct.unpack_from("<I", data, offset + 28)[0]
                if uni_off and offset + uni_off < offset + info_size:
                    end = offset + uni_off
                    while end + 1 < offset + info_size and data[end : end + 2] != b"\x00\x00":
                        end += 2
                    target = data[offset + uni_off : end].decode("utf-16-le", errors="ignore").strip()
                    if target:
                        return target
            local_off = struct.unpack_from("<I", data, offset + 16)[0]
            if local_off and offset + local_off < offset + info_size:
                end = data.find(b"\x00", offset + local_off, offset + info_size)
                if end > offset + local_off:
                    target = data[offset + local_off : end].decode("mbcs" if os.name == "nt" else "utf-8", errors="ignore").strip()
                    if target:
                        return target
        offset += info_size
    # StringData: Name (0x04), RelativePath (0x08), WorkingDir (0x10), Arguments (0x20), IconLocation (0x40)
    is_unicode = bool(flags & 0x80)
    for bit in (0x04, 0x08):
        if flags & bit:
            if offset + 2 > len(data):
                return ""
            count = struct.unpack_from("<H", data, offset)[0]
            offset += 2
            byte_len = count * (2 if is_unicode else 1)
            if offset + byte_len > len(data):
                return ""
            raw_str = data[offset : offset + byte_len]
            offset += byte_len
            if bit == 0x08:
                rel = raw_str.decode("utf-16-le" if is_unicode else "utf-8", errors="ignore").strip()
                if rel.lower().endswith(".exe"):
                    return rel
    return ""


def resolve_windows_shortcut(path: str | Path) -> str:
    """Resolve a Windows `.lnk` file to its target path, or return `str(path)`."""
    raw_str = str(path)
    win_p = PureWindowsPath(raw_str)
    if win_p.suffix.casefold() != ".lnk":
        return raw_str
    p = Path(path)
    if os.name == "nt" and p.is_file():
        try:
            with com_apartment(COINIT_APARTMENTTHREADED):
                link = ctypes.c_void_p()
                hr = ctypes.windll.ole32.CoCreateInstance(
                    ctypes.byref(_guid(CLSID_ShellLink)),
                    None,
                    CLSCTX_INPROC_SERVER,
                    ctypes.byref(_guid(IID_IShellLinkW)),
                    ctypes.byref(link),
                )
                if hr == 0 and link.value:
                    try:
                        persist = ctypes.c_void_p()
                        if _vmethod(link, 0, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                            link, ctypes.byref(_guid(IID_IPersistFile)), ctypes.byref(persist)
                        ) == 0 and persist.value:
                            try:
                                # IPersistFile::Load (slot 5)
                                if _vmethod(persist, 5, w.LPCWSTR, w.DWORD)(persist, str(p), 0) == 0:
                                    buf = ctypes.create_unicode_buffer(32768)
                                    # IShellLinkW::GetPath (slot 3): (pszFile, cch, pfd, fFlags)
                                    if _vmethod(link, 3, w.LPWSTR, ctypes.c_int, ctypes.c_void_p, w.DWORD)(
                                        link, buf, 32768, None, 0
                                    ) == 0 and buf.value:
                                        return buf.value
                            finally:
                                _release_com(persist)
                    finally:
                        _release_com(link)
        except Exception:
            pass
    if p.is_file():
        try:
            parsed = parse_lnk_bytes(p.read_bytes())
            if parsed:
                if not PureWindowsPath(parsed).is_absolute():
                    candidate = (p.parent / parsed).resolve()
                    if candidate.exists():
                        return str(candidate)
                return parsed
        except OSError:
            pass
    return raw_str


@lru_cache(maxsize=256)
def pe_friendly_name(exe_path: str) -> str:
    """Read FileDescription or ProductName from a Windows PE executable, cached."""
    win_path = PureWindowsPath(exe_path)
    fallback = (win_path.stem or win_path.name).strip()
    if os.name != "nt" or not exe_path:
        return fallback[:100]
    try:
        ver = ctypes.WinDLL("version", use_last_error=True)
        ver.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
        ver.GetFileVersionInfoSizeW.restype = w.DWORD
        ver.GetFileVersionInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p]
        ver.GetFileVersionInfoW.restype = w.BOOL
        ver.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            w.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(w.UINT),
        ]
        ver.VerQueryValueW.restype = w.BOOL

        dummy = w.DWORD(0)
        size = ver.GetFileVersionInfoSizeW(exe_path, ctypes.byref(dummy))
        if not size or size > 4_000_000:
            return fallback[:100]
        block = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(exe_path, 0, size, block):
            return fallback[:100]

        langs: list[str] = []
        ptr = ctypes.c_void_p()
        length = w.UINT(0)
        if ver.VerQueryValueW(block, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(length)) and length.value >= 4:
            count = length.value // 4
            pairs = (w.WORD * (count * 2)).from_address(ptr.value)
            for idx in range(count):
                langs.append(f"{pairs[idx * 2]:04x}{pairs[idx * 2 + 1]:04x}")
        for common in ("080404b0", "040904b0", "040904e4", "000004b0"):
            if common not in langs:
                langs.append(common)

        for lang_cp in langs:
            for field in ("FileDescription", "ProductName"):
                val_ptr = ctypes.c_void_p()
                val_len = w.UINT(0)
                sub = f"\\StringFileInfo\\{lang_cp}\\{field}"
                if ver.VerQueryValueW(block, sub, ctypes.byref(val_ptr), ctypes.byref(val_len)) and val_ptr.value and val_len.value > 1:
                    text = ctypes.wstring_at(val_ptr.value).strip()
                    if text and text.lower() not in {"application", "setup", "installer"}:
                        return text[:100]
    except Exception:
        pass
    return fallback[:100]


class InstalledWinApp(NamedTuple):
    app_id: str
    name: str
    path: str


_CHAT_AI_KEYWORDS = (
    "kimi", "claude", "chatgpt", "codex", "cursor", "antigravity", "windsurf",
    "doubao", "豆包", "weixin", "wechat", "微信", "wxwork", "企业微信",
    "feishu", "lark", "飞书", "dingtalk", "钉钉", "qq", "telegram", "discord",
    "slack", "windowsterminal", "cherry", "chatbox", "anythingllm", "lm studio",
    "obsidian", "trae", "zed", "vscode", "visual studio code",
)

_SKIP_EXE_STEMS = (
    "uninstall", "unins0", "卸载", "readme", "help", "帮助", "manual", "website",
    "update", "updater", "inno_updater", "elevate", "crashpad", "setup", "installer",
    "command-runner", "sandbox", "extension-host", "webbridge", "slides", "kimiim-cli",
    "sharetarget", "widget", "startfeed", "phoneexperience", "openconsole",
)


def _discover_msix_gui_apps() -> list[InstalledWinApp]:
    """Read user-installed MSIX/AppX GUI apps (e.g. official ChatGPT/Codex, Claude, Windows Terminal)."""
    if os.name != "nt":
        return []
    import winreg
    import xml.etree.ElementTree as ET

    results: list[InstalledWinApp] = []
    reg_path = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as root:
            count = winreg.QueryInfoKey(root)[0]
            for idx in range(min(count, 500)):
                try:
                    pkg_name = winreg.EnumKey(root, idx)
                    low_pkg = pkg_name.casefold()
                    if any(s in low_pkg for s in ("sharetarget", "widget", "runtime", "vclibs", "ui.xaml", "cbs", "startexperiences", "yourphone", "sechealth")):
                        continue
                    with winreg.OpenKey(root, pkg_name) as sub:
                        pkg_root, _ = winreg.QueryValueEx(sub, "PackageRootFolder")
                        disp, _ = winreg.QueryValueEx(sub, "DisplayName")
                    manifest = Path(str(pkg_root)) / "AppxManifest.xml"
                    if not manifest.is_file():
                        continue
                    tree = ET.parse(manifest)
                    for elem in tree.iter():
                        if not elem.tag.endswith("Application"):
                            continue
                        rel_exe = elem.attrib.get("Executable", "").strip()
                        if not rel_exe or not rel_exe.casefold().endswith(".exe"):
                            continue
                        full_exe = str(Path(str(pkg_root)) / rel_exe)
                        exe_name = PureWindowsPath(full_exe).name
                        if any(s in exe_name.casefold() for s in _SKIP_EXE_STEMS):
                            continue
                        app_id = validate_app_layer_id(normalize_app_layer_id("exe:" + exe_name))
                        friendly = str(disp or "").strip()
                        if not friendly or friendly.startswith("ms-resource:"):
                            if exe_name.casefold() == "windowsterminal.exe":
                                friendly = "Windows 终端"
                            else:
                                friendly = pe_friendly_name(full_exe) or PureWindowsPath(exe_name).stem
                        if "openai.codex" in low_pkg and "codex" not in friendly.casefold():
                            friendly = f"{friendly} (Codex)"
                        results.append(InstalledWinApp(app_id, friendly[:100], full_exe))
                except Exception:
                    continue
    except OSError:
        pass
    return results


def resolve_windows_app_alias(path: str | Path) -> str:
    """Resolve 0-byte WindowsApps execution aliases (e.g. chatgpt-desktop.exe) to real MSIX .exe."""
    raw = str(path)
    win_p = PureWindowsPath(raw)
    if os.name != "nt" or "windowsapps" not in raw.casefold():
        return raw
    stem_low = win_p.stem.casefold()
    for item in _discover_msix_gui_apps():
        target_stem = PureWindowsPath(item.path).stem.casefold()
        if stem_low == target_stem or (stem_low.startswith("chatgpt") and target_stem == "chatgpt"):
            return item.path
    return raw


def discover_start_menu_apps(roots=None) -> list[InstalledWinApp]:
    """Scan Start Menu, Desktop, %LOCALAPPDATA%\\Programs and MSIX apps -> [InstalledWinApp]."""
    custom_roots = roots is not None
    if roots is None:
        if os.name != "nt":
            return []
        roots = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("USERPROFILE", "")) / "Desktop",
        ]
        one_desk = Path(os.environ.get("OneDrive", "")) / "桌面"
        if one_desk.is_dir():
            roots.append(one_desk)
    from .chat_focus import BROWSERS

    browser_set = {b.casefold() for b in BROWSERS}
    seen: dict[str, InstalledWinApp] = {}

    def _register(exe_target: str, display_name: str):
        win_t = PureWindowsPath(exe_target)
        if win_t.suffix.casefold() != ".exe":
            return
        stem_low = win_t.stem.casefold()
        if any(w in stem_low or w in display_name.casefold() for w in _SKIP_EXE_STEMS):
            return
        try:
            app_id = validate_app_layer_id(normalize_app_layer_id("exe:" + win_t.name))
        except ValueError:
            return
        if app_id.casefold() in SELF_APP_IDS or app_id.casefold() in seen:
            return
        seen[app_id.casefold()] = InstalledWinApp(app_id, display_name.strip()[:100] or win_t.stem, exe_target)

    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        try:
            candidates = list(root_path.rglob("*.lnk"))[:500]
        except OSError:
            continue
        for lnk in candidates:
            stem = lnk.stem.strip()
            if not stem or any(word in stem.casefold() for word in _SKIP_EXE_STEMS):
                continue
            target = resolve_windows_shortcut(lnk)
            _register(target, stem)

    if not custom_roots and os.name == "nt":
        # Direct official-website user-level installers (%LOCALAPPDATA%\Programs\*/*.exe, Claude, Doubao)
        local_app = Path(os.environ.get("LOCALAPPDATA", ""))
        prog_dir = local_app / "Programs"
        if prog_dir.is_dir():
            try:
                for sub in prog_dir.iterdir():
                    if not sub.is_dir():
                        continue
                    for exe in list(sub.glob("*.exe"))[:8]:
                        _register(str(exe), pe_friendly_name(str(exe)))
            except OSError:
                pass
        for extra_exe in (
            local_app / "AnthropicClaude" / "claude.exe",
            local_app / "Claude" / "Claude.exe",
            local_app / "Doubao" / "Application" / "Doubao.exe",
        ):
            if extra_exe.is_file():
                _register(str(extra_exe), pe_friendly_name(str(extra_exe)))
        for msix_item in _discover_msix_gui_apps():
            if msix_item.app_id.casefold() not in browser_set and msix_item.app_id.casefold() not in seen:
                seen[msix_item.app_id.casefold()] = msix_item

    return sorted(seen.values(), key=lambda row: (row.name.casefold(), row.app_id))


def resolve_uwp_child_hwnd(user32, hwnd: int, host_pid: int) -> tuple[int, int]:
    """Resolve real UWP/MSIX CoreWindow child HWND & PID inside ApplicationFrameHost.exe."""
    found = [hwnd, host_pid]
    enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    @enum_proc
    def _each_child(child_hwnd, _lparam):
        child_pid = w.DWORD(0)
        user32.GetWindowThreadProcessId(child_hwnd, ctypes.byref(child_pid))
        if child_pid.value and int(child_pid.value) != host_pid:
            found[0] = int(child_hwnd)
            found[1] = int(child_pid.value)
            return False
        return True

    try:
        user32.EnumChildWindows(hwnd, _each_child, 0)
    except Exception:
        pass
    return found[0], found[1]


def is_process_elevated(pid: int) -> bool:
    """Return True if target Windows process runs elevated while current process does not."""
    if os.name != "nt" or pid <= 0:
        return False
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        advapi.OpenProcessToken.restype = w.BOOL
        advapi.GetTokenInformation.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
        ]
        advapi.GetTokenInformation.restype = w.BOOL

        def _elevated_handle(proc_handle) -> bool:
            tok = w.HANDLE()
            if not advapi.OpenProcessToken(proc_handle, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
                return False
            try:
                val = w.DWORD(0)
                ret_len = w.DWORD(0)
                # TokenElevation == 20
                if advapi.GetTokenInformation(tok, 20, ctypes.byref(val), ctypes.sizeof(val), ctypes.byref(ret_len)):
                    return bool(val.value)
                return False
            finally:
                kernel.CloseHandle(tok)

        if _elevated_handle(kernel.GetCurrentProcess()):
            return False  # Both elevated -> UIPI will not block
        proc = kernel.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not proc:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED implies higher integrity/elevation
        try:
            return _elevated_handle(proc)
        finally:
            kernel.CloseHandle(proc)
    except Exception:
        return False


def foreground_uipi_blocked() -> bool:
    """Return True if the active foreground window belongs to an elevated process."""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = w.HWND
        user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = w.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return is_process_elevated(int(pid.value))
    except Exception:
        return False


# -- Event-driven Foreground Activation Hook ----------------------------------


class WinEventObserver:
    """Zero-polling foreground window switch hook on the Qt GUI thread."""

    EVENT_SYSTEM_FOREGROUND = 0x0003
    EVENT_SYSTEM_MINIMIZEEND = 0x0017
    WINEVENT_OUTOFCONTEXT = 0x0000
    WINEVENT_SKIPOWNPROCESS = 0x0002

    def __init__(self, callback):
        self._callback = callback
        self._hooks = []
        self._proc = None
        self.current_app: App | None = None
        if os.name != "nt":
            return
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        hook_proto = ctypes.WINFUNCTYPE(
            None,
            w.HANDLE,
            w.DWORD,
            w.HWND,
            w.LONG,
            w.LONG,
            w.DWORD,
            w.DWORD,
        )
        self._user.SetWinEventHook.argtypes = [
            w.DWORD,
            w.DWORD,
            w.HMODULE,
            hook_proto,
            w.DWORD,
            w.DWORD,
            w.DWORD,
        ]
        self._user.SetWinEventHook.restype = w.HANDLE
        self._user.UnhookWinEvent.argtypes = [w.HANDLE]
        self._user.UnhookWinEvent.restype = w.BOOL

        def _on_event(_hook, _event, _hwnd, id_obj, id_child, _thread, _time):
            if id_obj != 0 or id_child != 0:
                return
            self.refresh()
            try:
                self._callback()
            except Exception:
                pass

        self._proc = hook_proto(_on_event)
        flags = self.WINEVENT_OUTOFCONTEXT | self.WINEVENT_SKIPOWNPROCESS
        for ev in (self.EVENT_SYSTEM_FOREGROUND, self.EVENT_SYSTEM_MINIMIZEEND):
            handle = self._user.SetWinEventHook(ev, ev, None, self._proc, 0, 0, flags)
            if handle:
                self._hooks.append(handle)
        self.refresh()

    def refresh(self) -> App | None:
        self.current_app = win_frontmost_app()
        return self.current_app

    def close(self):
        if os.name == "nt" and getattr(self, "_user", None) is not None:
            for handle in self._hooks:
                try:
                    self._user.UnhookWinEvent(handle)
                except Exception:
                    pass
        self._hooks.clear()
        self._proc = None


def _is_desktop_shell_hwnd(user32, hwnd: int) -> bool:
    """Exclude Windows wallpaper/desktop shell roots (Progman/WorkerW) while keeping File Explorer windows."""
    if not hwnd:
        return False
    buf = ctypes.create_unicode_buffer(64)
    if user32.GetClassNameW(hwnd, buf, 64) > 0:
        return buf.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")
    return False


def win_frontmost_app() -> App | None:
    if os.name != "nt":
        return None
    try:
        from .app_layers import WINDOWS_SYSTEM_HOSTS
        from .windows import WinWindows

        helper = WinWindows()
        helper.user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        helper.user.GetClassNameW.restype = ctypes.c_int
        hwnd = int(helper.user.GetForegroundWindow() or 0)
        if not hwnd or _is_desktop_shell_hwnd(helper.user, hwnd):
            return None
        info = helper._info(hwnd)
        if info is None or info.pid == os.getpid() or not info.executable:
            return None
        win_p = PureWindowsPath(info.executable)
        if win_p.name.casefold() in WINDOWS_SYSTEM_HOSTS:
            return None
        app_id = normalize_app_layer_id("exe:" + win_p.name)
        if not app_id or app_id.casefold() in SELF_APP_IDS:
            return None
        return App(app_id, int(info.pid), pe_friendly_name(info.executable))
    except Exception:
        return None


def win_running_apps(*, include_browsers: bool = False) -> list[App]:
    if os.name != "nt":
        return []
    from .app_layers import WINDOWS_SYSTEM_HOSTS
    from .chat_focus import BROWSERS
    from .windows import WinWindows

    helper = WinWindows()
    helper.user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    helper.user.GetClassNameW.restype = ctypes.c_int
    found: dict[str, App] = {}
    own_pid = os.getpid()
    for win in helper._all():
        if win.pid == own_pid or not win.executable:
            continue
        if _is_desktop_shell_hwnd(helper.user, int(win.handle)):
            continue
        win_p = PureWindowsPath(win.executable)
        exe_name = win_p.name
        if not exe_name or exe_name.casefold() in WINDOWS_SYSTEM_HOSTS:
            continue
        app_id = normalize_app_layer_id("exe:" + exe_name)
        if not app_id or app_id.casefold() in SELF_APP_IDS:
            continue
        if not include_browsers and app_id.casefold() in {b.casefold() for b in BROWSERS}:
            continue
        try:
            validate_app_layer_id(app_id)
        except ValueError:
            continue
        found.setdefault(app_id.casefold(), App(app_id, int(win.pid), pe_friendly_name(win.executable)))
    return sorted(found.values(), key=lambda item: (item.name.casefold(), item.bundle))


def win_chat_focus_candidates() -> list[App]:
    """Return running desktop apps plus installed AI/chat/coding/terminal .exe & MSIX apps."""
    if os.name != "nt":
        return []
    from .chat_focus import BROWSERS

    browser_set = {b.casefold() for b in BROWSERS}
    by_id: dict[str, App] = {}
    for running in win_running_apps(include_browsers=False):
        by_id[running.bundle.casefold()] = running
    for item in discover_start_menu_apps():
        key = item.app_id.casefold()
        if key in browser_set or key in SELF_APP_IDS:
            continue
        if key in by_id:
            # Prefer Start Menu / MSIX friendly display name (e.g. 'ChatGPT (Codex)' over 'Codex')
            cur = by_id[key]
            by_id[key] = App(cur.bundle, cur.pid, item.name or cur.name)
            continue
        haystack = f"{item.app_id} {item.name}".casefold()
        if any(kw in haystack for kw in _CHAT_AI_KEYWORDS):
            by_id[key] = App(item.app_id, 0, item.name)
    return sorted(by_id.values(), key=lambda item: (item.name.casefold(), item.bundle))


# -- UIA Element Wrapper & Win32 Menu Wrapper ---------------------------------


def strip_win32_menu_label(raw: str) -> str:
    """Clean Win32 menu strings like '&File\\tCtrl+O' or '文件(&F)...' -> 'File' / '文件…'."""
    if not raw:
        return ""
    head = raw.split("\t", 1)[0]
    # Remove CJK parenthesized accelerator e.g. '(&F)' or '（&F）'
    head = re.sub(r"\s*[\(（]&[A-Za-z0-9][\)）]", "", head)
    # Unescape '&&' -> placeholder, drop single '&', restore '&'
    head = head.replace("&&", "\x00").replace("&", "").replace("\x00", "&")
    head = " ".join(head.split()).strip()
    return head.replace("...", "…")


@dataclass(frozen=True)
class Win32MenuNode:
    """Synthetic AX-compatible node wrapping a classic Win32 HMENU hierarchy."""

    hwnd: int
    hmenu: int
    role: str
    title: str = ""
    enabled: bool = True
    cmd_id: int = 0
    parent: object = None


class WinUIAElement:
    """Ref-counted wrapper around `IUIAutomationElement*` with structural equality."""

    def __init__(self, uia_ptr, elem_ptr, *, is_root_window: bool = False):
        self._uia = uia_ptr
        self.ptr = elem_ptr
        self._is_root_window = is_root_window
        self._hash_cache: int | None = None

    def __del__(self):
        ptr, self.ptr = getattr(self, "ptr", None), None
        if ptr:
            _release_com(ptr)

    def _runtime_tuple(self) -> tuple[int, ...] | None:
        if not self.ptr:
            return None
        psa = ctypes.c_void_p()
        if _vmethod(self.ptr, 4, ctypes.POINTER(ctypes.c_void_p))(self.ptr, ctypes.byref(psa)) != 0 or not psa.value:
            return None
        try:
            oleaut = ctypes.windll.oleaut32
            lbound = ctypes.c_long(0)
            ubound = ctypes.c_long(-1)
            oleaut.SafeArrayGetLBound(psa, 1, ctypes.byref(lbound))
            oleaut.SafeArrayGetUBound(psa, 1, ctypes.byref(ubound))
            count = int(ubound.value - lbound.value + 1)
            if count <= 0 or count > 32:
                return None
            data_ptr = ctypes.POINTER(ctypes.c_int)()
            if oleaut.SafeArrayAccessData(psa, ctypes.byref(data_ptr)) != 0:
                return None
            try:
                return tuple(int(data_ptr[i]) for i in range(count))
            finally:
                oleaut.SafeArrayUnaccessData(psa)
        finally:
            ctypes.windll.oleaut32.SafeArrayDestroy(psa)

    def __hash__(self) -> int:
        if self._hash_cache is None:
            rt = self._runtime_tuple()
            self._hash_cache = hash(rt) if rt is not None else hash(int(ctypes.cast(self.ptr, ctypes.c_void_p).value or 0))
        return self._hash_cache

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if not isinstance(other, WinUIAElement) or not self.ptr or not other.ptr:
            return False
        same = w.BOOL(0)
        hr = _vmethod(self._uia, 3, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(w.BOOL))(
            self._uia, self.ptr, other.ptr, ctypes.byref(same)
        )
        return hr == 0 and bool(same.value)


# -- Windows UIA Chat-Focus & AX-Action Backends ------------------------------


class WinUIAFocusBackend:
    """Windows UI Automation backend implementing the FocusEngine & Locator protocol."""

    def __init__(self, *, frontmost_provider=None):
        self._frontmost_provider = frontmost_provider
        self._uia = None
        self._walker = None
        self._prepared_hwnds: set[int] = set()
        self._active_hwnd: int = 0
        self.last_focus_error = None
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._user.GetForegroundWindow.restype = w.HWND
        self._user.GetAncestor.argtypes = [w.HWND, w.UINT]
        self._user.GetAncestor.restype = w.HWND
        self._user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        self._user.GetClassNameW.restype = ctypes.c_int
        self._user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self._user.IsWindow.argtypes = [w.HWND]
        self._user.IsIconic.argtypes = [w.HWND]
        self._enum_child_cb = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self._user.EnumChildWindows.argtypes = [w.HWND, self._enum_child_cb, w.LPARAM]
        self._user.SendMessageTimeoutW.argtypes = [
            w.HWND,
            w.UINT,
            w.WPARAM,
            w.LPARAM,
            w.UINT,
            w.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]

    def _ensure_uia(self):
        ensure_thread_com(COINIT_MULTITHREADED)
        if self._uia is not None:
            return self._uia, self._walker
        iid = _guid(IID_IUIAutomation)
        ptr = ctypes.c_void_p()
        for clsid_str in (CLSID_CUIAutomation8, CLSID_CUIAutomation):
            hr = ctypes.windll.ole32.CoCreateInstance(
                ctypes.byref(_guid(clsid_str)),
                None,
                CLSCTX_INPROC_SERVER,
                ctypes.byref(iid),
                ctypes.byref(ptr),
            )
            if hr == 0 and ptr.value:
                break
        if not ptr.value:
            raise RuntimeError("无法初始化 Windows UI Automation 服务")
        walker = ctypes.c_void_p()
        # get_ControlViewWalker (slot 14)
        if _vmethod(ptr, 14, ctypes.POINTER(ctypes.c_void_p))(ptr, ctypes.byref(walker)) != 0 or not walker.value:
            _release_com(ptr)
            raise RuntimeError("无法创建 Windows UI Automation 控件视图遍历器")
        self._uia = ptr
        self._walker = walker
        return self._uia, self._walker

    def applications(self) -> list[App]:
        return win_chat_focus_candidates()

    def frontmost(self) -> App | None:
        if self._frontmost_provider is not None:
            app = self._frontmost_provider()
            if app is not None:
                return app
        return win_frontmost_app()

    def input_stamp(self) -> int:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if self._user.GetLastInputInfo(ctypes.byref(info)):
            return int(info.dwTime)
        return 0

    @staticmethod
    def _info_matches_app(info, app: App) -> bool:
        if info is None:
            return False
        if int(app.pid) > 0 and int(info.pid) == int(app.pid):
            return True
        if info.executable and app.bundle:
            win_id = normalize_app_layer_id("exe:" + PureWindowsPath(info.executable).name)
            return win_id.casefold() == normalize_app_layer_id(app.bundle).casefold()
        return False

    def _foreground_hwnd_for_app(self, app: App) -> int:
        from .windows import WinWindows

        helper = WinWindows()
        fg = int(helper.user.GetForegroundWindow() or 0)
        if fg and not _is_desktop_shell_hwnd(self._user, fg):
            info = helper._info(fg)
            if self._info_matches_app(info, app):
                return fg
        for item in helper._all():
            if not _is_desktop_shell_hwnd(self._user, int(item.handle)) and self._info_matches_app(item, app):
                return int(item.handle)
        return 0

    def prepare(self, app: App, cancelled):
        hwnd = self._foreground_hwnd_for_app(app)
        if cancelled() or not hwnd or hwnd in self._prepared_hwnds:
            return
        self._prepared_hwnds.add(hwnd)
        woke_render = [False]
        try:
            res = ctypes.c_size_t(0)
            self._user.SendMessageTimeoutW(hwnd, 0x003D, 0, -4, 0x0002, 120, ctypes.byref(res))

            @self._enum_child_cb
            def _wake_child(child_hwnd, _lparam):
                if cancelled():
                    return False
                cls_buf = ctypes.create_unicode_buffer(64)
                self._user.GetClassNameW(child_hwnd, cls_buf, 64)
                cls = cls_buf.value
                if "Chrome_RenderWidgetHostHWND" in cls or "WebView" in cls or "Intermediate D3D" in cls or cls:
                    r2 = ctypes.c_size_t(0)
                    self._user.SendMessageTimeoutW(child_hwnd, 0x003D, 0, -4, 0x0002, 80, ctypes.byref(r2))
                    if "Chrome_" in cls or "WebView" in cls:
                        woke_render[0] = True
                return True

            self._user.EnumChildWindows(hwnd, _wake_child, 0)
            if woke_render[0] and not cancelled():
                time.sleep(0.06)
        except Exception:
            pass

    def root(self, app: App):
        return self.window(app)

    def _native_hwnd(self, element: WinUIAElement) -> int:
        nhwnd = ctypes.c_void_p(0)
        # get_CurrentNativeWindowHandle (slot 36)
        if _vmethod(element.ptr, 36, ctypes.POINTER(ctypes.c_void_p))(element.ptr, ctypes.byref(nhwnd)) == 0 and nhwnd.value:
            return int(nhwnd.value)
        return 0

    def _belongs_to_window(self, element: WinUIAElement, app: App, target_hwnd: int) -> bool:
        """Accept multi-process Electron renderers and embedded WebView2 child HWNDs inside target_hwnd."""
        proc_id = self._get_int(element, 20)
        if int(app.pid) > 0 and proc_id == int(app.pid):
            return True
        if proc_id > 0 and app.bundle:
            from .windows import WinWindows
            exe = WinWindows()._exe_for_pid(proc_id)
            if exe:
                win_id = normalize_app_layer_id("exe:" + PureWindowsPath(exe).name)
                if win_id.casefold() == normalize_app_layer_id(app.bundle).casefold():
                    return True
        if target_hwnd:
            cur: WinUIAElement | None = element
            for _ in range(36):
                if cur is None:
                    break
                nh = self._native_hwnd(cur)
                if nh:
                    if nh == target_hwnd:
                        return True
                    ga_root = int(self._user.GetAncestor(nh, 2) or 0)  # GA_ROOT = 2
                    if ga_root == target_hwnd:
                        return True
                cur = self.read(cur, "AXParent")
        return False

    def window(self, app: App) -> WinUIAElement | None:
        hwnd = self._foreground_hwnd_for_app(app)
        if not hwnd or self._user.IsIconic(hwnd):
            return None
        self._active_hwnd = hwnd
        uia, _ = self._ensure_uia()
        elem = ctypes.c_void_p()
        # ElementFromHandle (slot 6)
        if _vmethod(uia, 6, w.HWND, ctypes.POINTER(ctypes.c_void_p))(uia, hwnd, ctypes.byref(elem)) == 0 and elem.value:
            return WinUIAElement(uia, elem, is_root_window=True)
        return None

    def focused(self, app: App) -> WinUIAElement | None:
        target_hwnd = self._foreground_hwnd_for_app(app)
        if target_hwnd:
            self._active_hwnd = target_hwnd
        uia, _ = self._ensure_uia()
        elem = ctypes.c_void_p()
        # GetFocusedElement (slot 8)
        if _vmethod(uia, 8, ctypes.POINTER(ctypes.c_void_p))(uia, ctypes.byref(elem)) != 0 or not elem.value:
            return None
        wrapped = WinUIAElement(uia, elem)
        if not self._belongs_to_window(wrapped, app, target_hwnd):
            return None
        return wrapped

    def _get_int(self, element: WinUIAElement, slot: int) -> int:
        val = ctypes.c_int(0)
        if _vmethod(element.ptr, slot, ctypes.POINTER(ctypes.c_int))(element.ptr, ctypes.byref(val)) == 0:
            return int(val.value)
        return 0

    def _get_bool(self, element: WinUIAElement, slot: int, default: bool = False) -> bool:
        val = w.BOOL(0)
        if _vmethod(element.ptr, slot, ctypes.POINTER(w.BOOL))(element.ptr, ctypes.byref(val)) == 0:
            return bool(val.value)
        return default

    def _get_bstr(self, element: WinUIAElement, slot: int) -> str:
        raw = ctypes.c_void_p()
        if _vmethod(element.ptr, slot, ctypes.POINTER(ctypes.c_void_p))(element.ptr, ctypes.byref(raw)) != 0 or not raw.value:
            return ""
        try:
            return ctypes.wstring_at(raw.value)
        finally:
            _free_bstr(raw.value)

    def _get_aria_placeholder(self, element: WinUIAElement) -> str:
        var = VARIANT()
        ctypes.windll.oleaut32.VariantInit(ctypes.byref(var))
        try:
            if _vmethod(element.ptr, 10, ctypes.c_int, ctypes.POINTER(VARIANT))(
                element.ptr, UIA_AriaPropertiesPropertyId, ctypes.byref(var)
            ) != 0:
                return ""
            if var.vt == 8 and var.val:  # VT_BSTR
                raw_aria = ctypes.wstring_at(int(var.val))
                for part in raw_aria.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if k.strip().casefold() == "placeholder" and v.strip():
                            return v.strip()
            return ""
        finally:
            ctypes.windll.oleaut32.VariantClear(ctypes.byref(var))

    def _has_pattern(self, element: WinUIAElement, pattern_id: int) -> ctypes.c_void_p | None:
        unk = ctypes.c_void_p()
        if _vmethod(element.ptr, 16, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            element.ptr, pattern_id, ctypes.byref(unk)
        ) == 0 and unk.value:
            return unk
        return None

    def _is_editable_text_control(self, element: WinUIAElement, ctrl_type: int) -> bool:
        if ctrl_type == 50004:  # Edit
            val_pat = self._has_pattern(element, UIA_ValuePatternId)
            if val_pat is not None:
                try:
                    ro = w.BOOL(0)
                    if _vmethod(val_pat, 5, ctypes.POINTER(w.BOOL))(val_pat, ctypes.byref(ro)) == 0 and ro.value:
                        return False
                finally:
                    _release_com(val_pat)
            return True
        if ctrl_type in (50030, 50026):  # Document / Group (Electron ProseMirror/Lexical/TermControl)
            cls_name = self._get_bstr(element, 30)
            if cls_name == "TermControl":
                return True
            aria_role = self._get_bstr(element, 41).strip().casefold()
            if aria_role in {"textbox", "searchbox", "combobox"}:
                return True
            val_pat = self._has_pattern(element, UIA_ValuePatternId)
            if val_pat is not None:
                try:
                    ro = w.BOOL(0)
                    if _vmethod(val_pat, 5, ctypes.POINTER(w.BOOL))(val_pat, ctypes.byref(ro)) == 0:
                        return not bool(ro.value)
                finally:
                    _release_com(val_pat)
            return False
        return False

    def _is_top_window_element(self, element: WinUIAElement, ctrl_type: int) -> bool:
        if getattr(element, "_is_root_window", False) or ctrl_type == 50032:
            return True
        if self._active_hwnd and ctrl_type == 50033:
            nh = self._native_hwnd(element)
            if nh and nh == self._active_hwnd:
                return True
        return False

    def _canonical_role(self, element: WinUIAElement) -> str | None:
        ctrl_type = self._get_int(element, 21)
        if not ctrl_type:
            return None
        if self._is_top_window_element(element, ctrl_type):
            cls_name = self._get_bstr(element, 30)
            if cls_name == "#32770":
                return "AXDialog"
            return "AXWindow"
        if ctrl_type in (50004, 50030, 50026):
            if ctrl_type == 50004 and self._get_bool(element, 35, False):  # CurrentIsPassword
                return "AXTextField"
            if self._is_editable_text_control(element, ctrl_type):
                return "AXTextArea"
            if ctrl_type == 50030:
                return "AXWebArea"
            if ctrl_type == 50004:
                return "AXStaticText"
        return UIA_CONTROL_ROLES.get(ctrl_type, "AXGroup")

    def read(self, element, attr: str):
        if element is None:
            return None
        if isinstance(element, Win32MenuNode):
            if attr == "AXRole":
                return element.role
            if attr == "AXTitle":
                return element.title
            if attr == "AXEnabled":
                return element.enabled
            if attr == "AXParent":
                return element.parent
            return None
        if not isinstance(element, WinUIAElement) or not element.ptr:
            return None
        if attr == "AXRole":
            return self._canonical_role(element)
        if attr == "AXSubrole":
            if self._get_bool(element, 35, False):
                return "AXSecureTextField"
            cls_name = self._get_bstr(element, 30)
            if cls_name == "#32770":
                return "AXDialog"
            return ""
        if attr == "AXIdentifier":
            return self._get_bstr(element, 29)
        if attr == "AXTitle":
            return self._get_bstr(element, 23)
        if attr == "AXDescription":
            return self._get_bstr(element, 37)  # ItemType / localized description
        if attr == "AXHelp":
            return self._get_bstr(element, 31)
        if attr == "AXPlaceholderValue":
            return self._get_aria_placeholder(element) or self._get_bstr(element, 31) or self._get_bstr(element, 23)
        if attr == "AXValue":
            val_pat = self._has_pattern(element, UIA_ValuePatternId)
            if val_pat is not None:
                try:
                    raw = ctypes.c_void_p()
                    if _vmethod(val_pat, 4, ctypes.POINTER(ctypes.c_void_p))(val_pat, ctypes.byref(raw)) == 0 and raw.value:
                        try:
                            return ctypes.wstring_at(raw.value)
                        finally:
                            _free_bstr(raw.value)
                finally:
                    _release_com(val_pat)
            return ""
        if attr == "AXEnabled":
            return self._get_bool(element, 28, True)
        if attr == "AXFocused":
            return self._get_bool(element, 26, False)
        if attr == "AXHidden":
            return self._get_bool(element, 38, False)
        if attr == "AXParent":
            uia, walker = self._ensure_uia()
            parent = ctypes.c_void_p()
            if _vmethod(walker, 3, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                walker, element.ptr, ctypes.byref(parent)
            ) == 0 and parent.value:
                return WinUIAElement(uia, parent)
            return None
        if attr == "AXWindow":
            node = element
            for _ in range(64):
                if node is None:
                    return None
                ct = self._get_int(node, 21)
                if self._is_top_window_element(node, ct):
                    return node
                node = self.read(node, "AXParent")
            return None
        return None

    def children(self, element) -> list:
        if element is None:
            return []
        if isinstance(element, Win32MenuNode):
            return []
        if not isinstance(element, WinUIAElement) or not element.ptr:
            return []
        uia, walker = self._ensure_uia()
        result: list[WinUIAElement] = []
        cur = ctypes.c_void_p()
        if _vmethod(walker, 4, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            walker, element.ptr, ctypes.byref(cur)
        ) != 0 or not cur.value:
            return result
        while cur.value and len(result) < 256:
            current_ptr = cur.value
            result.append(WinUIAElement(uia, ctypes.c_void_p(current_ptr)))
            nxt = ctypes.c_void_p()
            hr = _vmethod(walker, 6, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                walker, ctypes.c_void_p(current_ptr), ctypes.byref(nxt)
            )
            if hr != 0 or not nxt.value:
                break
            cur = nxt
        return result

    def settable(self, element) -> bool:
        if not isinstance(element, WinUIAElement) or not element.ptr:
            return False
        return self._get_bool(element, 27, False) and self._get_bool(element, 28, True)

    def modal(self, window) -> bool:
        if not isinstance(window, WinUIAElement) or not window.ptr:
            return False
        for child in self.children(window)[:24]:
            role = self.read(child, "AXRole")
            if role in {"AXDialog", "AXSheet", "AXMenu"} and not self.read(child, "AXHidden"):
                return True
        return False

    def rect(self, element) -> tuple[float, float, float, float] | None:
        if not isinstance(element, WinUIAElement) or not element.ptr:
            return None
        rc = w.RECT()
        if _vmethod(element.ptr, 43, ctypes.POINTER(w.RECT))(element.ptr, ctypes.byref(rc)) != 0:
            return None
        width = float(rc.right - rc.left)
        height = float(rc.bottom - rc.top)
        if width <= 0 or height <= 0:
            return None
        return float(rc.left), float(rc.top), width, height

    def visible(self, element, window) -> bool:
        if self.read(element, "AXHidden"):
            return False
        r_el = self.rect(element)
        r_win = self.rect(window)
        if r_el is None or r_win is None:
            return False
        x, y, width, height = r_el
        wx, wy, ww, wh = r_win
        tol = 4.0
        return width >= 8 and height >= 8 and x >= wx - tol and y >= wy - tol and x + width <= wx + ww + tol and y + height <= wy + wh + tol

    def focus(self, element) -> bool:
        if not isinstance(element, WinUIAElement) or not element.ptr:
            return False
        hr = _vmethod(element.ptr, 3)(element.ptr)
        self.last_focus_error = None if hr == 0 else int(hr)
        return hr == 0

    def _app_build_digest(self, app: App) -> str:
        from .windows import WinWindows

        helper = WinWindows()
        for item in helper._all():
            if int(item.pid) == int(app.pid) and item.executable:
                try:
                    st = Path(item.executable).stat()
                    raw = f"win32:{app.bundle.casefold()}:{st.st_size}:{st.st_mtime_ns}"
                    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
                except OSError:
                    break
        return hashlib.sha256(f"win32:{app.bundle.casefold()}".encode("utf-8")).hexdigest()

    def discover(self, app: App, window, cancelled):
        """Auto-discover a single unambiguous chat composer input inside window."""
        pending = [(window, 0)]
        matches: list[WinUIAElement] = []
        deadline = time.monotonic() + 1.4
        visited = 0
        while pending and visited < 2400 and time.monotonic() < deadline and not cancelled():
            node, depth = pending.pop()
            visited += 1
            role = self.read(node, "AXRole")
            if role in {"AXDialog", "AXSheet", "AXMenu", "AXMenuItem"}:
                return None
            if role == "AXTextArea":
                if (
                    self.read(node, "AXEnabled") is True
                    and self.settable(node)
                    and self.visible(node, window)
                    and chat_metadata(self.read(node, "AXIdentifier"), self.read(node, "AXPlaceholderValue"))
                ):
                    matches.append(node)
                    if len(matches) > 1:
                        return None
                continue
            if depth >= 48 or role in {"AXStaticText", "AXImage", "AXMenuBar"}:
                continue
            kids = self.children(node)
            if kids:
                pending.extend((child, depth + 1) for child in reversed(kids))
        return matches[0] if len(matches) == 1 and not cancelled() else None

    def describe(self, app: App, window, target):
        build = self._app_build_digest(app)
        return locator_describe(self, window, target, build)

    def resolve(self, app: App, window, locator, cancelled):
        build = self._app_build_digest(app)
        return locator_resolve(self, window, locator, build, cancelled)


class WinAXActionBackend:
    """Windows UIA + Win32 HMENU backend for `ui_element` and `app_menu` actions."""

    def __init__(self):
        self._focus = WinUIAFocusBackend()
        self._user = self._focus._user
        self._user.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
        self._user.GetCursorPos.restype = w.BOOL
        self._user.GetMenu.argtypes = [w.HWND]
        self._user.GetMenu.restype = w.HMENU
        self._user.GetMenuItemCount.argtypes = [w.HMENU]
        self._user.GetMenuItemCount.restype = ctypes.c_int
        self._user.GetSubMenu.argtypes = [w.HMENU, ctypes.c_int]
        self._user.GetSubMenu.restype = w.HMENU
        self._user.GetMenuItemID.argtypes = [w.HMENU, ctypes.c_int]
        self._user.GetMenuItemID.restype = w.UINT
        self._user.GetMenuState.argtypes = [w.HMENU, w.UINT, w.UINT]
        self._user.GetMenuState.restype = w.UINT
        self._user.GetMenuStringW.argtypes = [w.HMENU, w.UINT, w.LPWSTR, ctypes.c_int, w.UINT]
        self._user.GetMenuStringW.restype = ctypes.c_int
        self._user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        self._user.PostMessageW.restype = w.BOOL
        self._menu_children: dict[Win32MenuNode, list[Win32MenuNode]] = {}

    def applications(self, *, include_browsers: bool = True) -> list[App]:
        return win_running_apps(include_browsers=include_browsers)

    def frontmost(self) -> App | None:
        return self._focus.frontmost()

    def prepare(self, app: App, cancelled):
        self._focus.prepare(app, cancelled)

    def root(self, app: App):
        return self._focus.root(app)

    def window(self, app: App):
        return self._focus.window(app)

    def read(self, element, attr: str):
        return self._focus.read(element, attr)

    def _build_win32_menu_tree(self, hwnd: int, hmenu: int) -> Win32MenuNode | None:
        count = self._user.GetMenuItemCount(hmenu)
        if count <= 0:
            return None
        self._menu_children.clear()
        root = Win32MenuNode(hwnd=hwnd, hmenu=hmenu, role="AXMenuBar")

        def populate(menu_handle: int, container: Win32MenuNode, depth: int):
            if depth > 6:
                return
            n = self._user.GetMenuItemCount(menu_handle)
            items: list[Win32MenuNode] = []
            for idx in range(max(0, n)):
                state = self._user.GetMenuState(menu_handle, idx, 0x0400)  # MF_BYPOSITION
                if state == 0xFFFFFFFF or (state & 0x0800):  # MF_SEPARATOR
                    continue
                buf = ctypes.create_unicode_buffer(256)
                chars = self._user.GetMenuStringW(menu_handle, idx, buf, 256, 0x0400)
                title = strip_win32_menu_label(buf.value if chars > 0 else "")
                if not title:
                    continue
                enabled = not bool(state & (0x0001 | 0x0002))  # MF_GRAYED | MF_DISABLED
                sub_hmenu = self._user.GetSubMenu(menu_handle, idx)
                if sub_hmenu:
                    item_node = Win32MenuNode(
                        hwnd=hwnd,
                        hmenu=int(sub_hmenu),
                        role="AXMenuBarItem" if depth == 0 else "AXMenuItem",
                        title=title,
                        enabled=enabled,
                        parent=container,
                    )
                    sub_container = Win32MenuNode(
                        hwnd=hwnd,
                        hmenu=int(sub_hmenu),
                        role="AXMenu",
                        title=title,
                        enabled=enabled,
                        parent=item_node,
                    )
                    self._menu_children[item_node] = [sub_container]
                    populate(int(sub_hmenu), sub_container, depth + 1)
                    items.append(item_node)
                else:
                    cmd_id = int(self._user.GetMenuItemID(menu_handle, idx))
                    if cmd_id and cmd_id != 0xFFFFFFFF:
                        leaf = Win32MenuNode(
                            hwnd=hwnd,
                            hmenu=menu_handle,
                            role="AXMenuItem",
                            title=title,
                            enabled=enabled,
                            cmd_id=cmd_id,
                            parent=container,
                        )
                        items.append(leaf)
            self._menu_children[container] = items

        populate(hmenu, root, 0)
        return root if self._menu_children.get(root) else None

    def menubar(self, app: App):
        hwnd = self._focus._foreground_hwnd_for_app(app)
        if not hwnd:
            return None
        hmenu = self._user.GetMenu(hwnd)
        if hmenu:
            tree = self._build_win32_menu_tree(hwnd, int(hmenu))
            if tree is not None:
                return tree
        win_el = self.window(app)
        if win_el is None:
            return None
        # Search top 3 levels for modern UIA MenuBar (WPF / WinUI 3 / Qt / Electron)
        queue = [(win_el, 0)]
        visited = 0
        while queue and visited < 180:
            node, depth = queue.pop(0)
            visited += 1
            if self._focus._get_int(node, 21) == 50010:  # UIA_MenuBarControlTypeId
                if self._focus._get_bstr(node, 29) != "SystemMenuBar":
                    return node
            if depth < 3:
                queue.extend((c, depth + 1) for c in self._focus.children(node)[:24])
        return None

    def children(self, element) -> list:
        if isinstance(element, Win32MenuNode):
            return list(self._menu_children.get(element, ()))
        if isinstance(element, WinUIAElement):
            kids = self._focus.children(element)
            # If a UIA MenuItem has ExpandCollapsePattern and is collapsed with 0 children,
            # expand only when inspecting a menu item inside a MenuBar/Menu.
            if not kids and self._focus._get_int(element, 21) == 50011:
                exp = self._focus._has_pattern(element, UIA_ExpandCollapsePatternId)
                if exp is not None:
                    try:
                        state = ctypes.c_int(0)
                        if _vmethod(exp, 5, ctypes.POINTER(ctypes.c_int))(exp, ctypes.byref(state)) == 0 and state.value == 0:
                            _vmethod(exp, 3)(exp)  # Expand()
                            time.sleep(0.03)
                            kids = self._focus.children(element)
                    finally:
                        _release_com(exp)
            return kids
        return []

    def settable(self, element) -> bool:
        return self._focus.settable(element)

    def modal(self, window) -> bool:
        return self._focus.modal(window)

    def visible(self, element, window) -> bool:
        return self._focus.visible(element, window)

    def rect(self, element):
        return self._focus.rect(element)

    def actions(self, element) -> set[str]:
        if isinstance(element, Win32MenuNode):
            return {"AXPress"} if element.cmd_id and element.enabled else set()
        if not isinstance(element, WinUIAElement) or not element.ptr:
            return set()
        for pat_id in (
            UIA_InvokePatternId,
            UIA_TogglePatternId,
            UIA_SelectionItemPatternId,
            UIA_ExpandCollapsePatternId,
            UIA_LegacyIAccessiblePatternId,
        ):
            pat = self._focus._has_pattern(element, pat_id)
            if pat is not None:
                _release_com(pat)
                return {"AXPress"}
        return set()

    def press(self, element) -> bool:
        if isinstance(element, Win32MenuNode):
            if not element.enabled or not element.cmd_id or not element.hwnd:
                return False
            if is_process_elevated_from_hwnd(self._user, element.hwnd):
                raise PermissionError("目标窗口以管理员权限运行，普通权限程序无法调用其菜单命令")
            return bool(self._user.PostMessageW(element.hwnd, 0x0111, element.cmd_id, 0))
        if not isinstance(element, WinUIAElement) or not element.ptr:
            return False
        for pat_id, slot in (
            (UIA_InvokePatternId, 3),            # Invoke()
            (UIA_TogglePatternId, 3),            # Toggle()
            (UIA_SelectionItemPatternId, 3),     # Select()
            (UIA_LegacyIAccessiblePatternId, 4), # DoDefaultAction()
        ):
            pat = self._focus._has_pattern(element, pat_id)
            if pat is not None:
                try:
                    if _vmethod(pat, slot)(pat) == 0:
                        return True
                finally:
                    _release_com(pat)
        exp = self._focus._has_pattern(element, UIA_ExpandCollapsePatternId)
        if exp is not None:
            try:
                state = ctypes.c_int(0)
                _vmethod(exp, 5, ctypes.POINTER(ctypes.c_int))(exp, ctypes.byref(state))
                slot = 4 if state.value == 1 else 3
                if _vmethod(exp, slot)(exp) == 0:
                    return True
            finally:
                _release_com(exp)
        return False

    def focus(self, element) -> bool:
        return self._focus.focus(element)

    def mouse_point(self) -> tuple[float, float]:
        pt = w.POINT()
        if self._user.GetCursorPos(ctypes.byref(pt)):
            return float(pt.x), float(pt.y)
        return 0.0, 0.0

    def element_at_cursor(self, app: App):
        target_hwnd = self._focus._foreground_hwnd_for_app(app)
        if target_hwnd:
            self._focus._active_hwnd = target_hwnd
        uia, _ = self._focus._ensure_uia()
        pt = w.POINT()
        if not self._user.GetCursorPos(ctypes.byref(pt)):
            return None
        elem = ctypes.c_void_p()
        # ElementFromPoint (slot 7) — pass POINT by value
        if _vmethod(uia, 7, w.POINT, ctypes.POINTER(ctypes.c_void_p))(uia, pt, ctypes.byref(elem)) != 0 or not elem.value:
            return None
        wrapped = WinUIAElement(uia, elem)
        if self._focus._belongs_to_window(wrapped, app, target_hwnd):
            return wrapped
        return None


def is_process_elevated_from_hwnd(user32, hwnd: int) -> bool:
    pid = w.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return is_process_elevated(int(pid.value))
