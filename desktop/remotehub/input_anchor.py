"""Read-only screen geometry for the active desktop input target."""
from __future__ import annotations

from dataclasses import dataclass
import sys

Bounds = tuple[float, float, float, float]
EDITABLE_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}


@dataclass(frozen=True)
class InputAnchor:
    field: Bounds | None = None
    caret: Bounds | None = None
    window: Bounds | None = None


def _mac_anchor() -> InputAnchor:
    from objc import autorelease_pool
    from .mac_chat_focus import MacFocusBackend

    with autorelease_pool():
        backend = MacFocusBackend()
        app = backend.frontmost()
        if app is None or not backend.ax.AXIsProcessTrusted():
            return InputAnchor()
        root = backend.root(app)
        backend.ax.AXUIElementSetMessagingTimeout(root, 0.12)
        window = backend.rect(backend.read(root, "AXFocusedWindow"))
        element = backend.read(root, "AXFocusedUIElement")
        if (backend.read(element, "AXRole") not in EDITABLE_ROLES
                or backend.read(element, "AXSubrole") == "AXSecureTextField"):
            return InputAnchor(window=window)
        field = backend.rect(element)
        selected = backend.read(element, "AXSelectedTextRange")
        if selected is None:
            return InputAnchor(field=field, window=window)
        ax = backend.ax
        error, value = ax.AXUIElementCopyParameterizedAttributeValue(
            element, "AXBoundsForRange", selected, None)
        if not error and value is not None:
            ok, rect = ax.AXValueGetValue(value, ax.kAXValueCGRectType, None)
            if ok and rect.size.height > 0:
                caret = (rect.origin.x, rect.origin.y, max(1, rect.size.width), rect.size.height)
                return InputAnchor(field, caret, window)
        return InputAnchor(field=field, window=window)


def _windows_anchor() -> InputAnchor:
    import ctypes as c
    from ctypes import wintypes as w
    from .win_uia import WinUIAFocusBackend, _release_com

    class GuiThreadInfo(c.Structure):
        _fields_ = [
            ("size", w.DWORD), ("flags", w.DWORD), ("active", w.HWND),
            ("focus", w.HWND), ("capture", w.HWND), ("menu_owner", w.HWND),
            ("move_size", w.HWND), ("caret", w.HWND), ("caret_rect", w.RECT),
        ]

    user = c.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = w.HWND
    user.GetWindowRect.argtypes = (w.HWND, c.POINTER(w.RECT))
    user.GetGUIThreadInfo.argtypes = (w.DWORD, c.POINTER(GuiThreadInfo))
    user.ClientToScreen.argtypes = (w.HWND, c.POINTER(w.POINT))
    window_rect = w.RECT()
    window = None
    if user.GetWindowRect(user.GetForegroundWindow(), c.byref(window_rect)):
        window = (window_rect.left, window_rect.top,
                  window_rect.right - window_rect.left, window_rect.bottom - window_rect.top)
    info = GuiThreadInfo()
    info.size = c.sizeof(info)
    caret = None
    if user.GetGUIThreadInfo(0, c.byref(info)) and info.caret:
        rect = info.caret_rect
        point = w.POINT(rect.left, rect.top)
        if rect.bottom > rect.top and user.ClientToScreen(info.caret, c.byref(point)):
            caret = (point.x, point.y, max(1, rect.right - rect.left), rect.bottom - rect.top)
    backend = WinUIAFocusBackend()
    try:
        app = backend.frontmost()
        focused = backend.focused(app) if app is not None else None
        if backend.read(focused, "AXSubrole") == "AXSecureTextField":
            return InputAnchor(window=window)
        field = backend.rect(focused) if backend.read(focused, "AXRole") in EDITABLE_ROLES else None
        return InputAnchor(field, caret, window)
    finally:
        _release_com(backend._walker)
        _release_com(backend._uia)


def _x11_anchor() -> InputAnchor:
    from .linux_desktop import is_wayland_session
    if is_wayland_session():
        return InputAnchor()
    from .windows import X11Windows
    backend = X11Windows()
    try:
        ident = backend.active_window_id()
        if not ident:
            return InputAnchor()
        window = backend.d.create_resource_object("window", ident)
        bounds = window.get_geometry()
        position = backend.root.translate_coords(window, 0, 0)
        return InputAnchor(window=(position.x, position.y, bounds.width, bounds.height))
    finally:
        backend.close_display()


def focused_input_anchor() -> InputAnchor:
    try:
        if sys.platform == "darwin":
            return _mac_anchor()
        if sys.platform == "win32":
            return _windows_anchor()
        if sys.platform.startswith("linux"):
            return _x11_anchor()
    except Exception:
        # Access to another application's UI is optional and may race a window closing.
        pass
    return InputAnchor()
