"""Route the macOS quit request (Dock「退出」, Cmd+Q from other apps, logout) to the app.

Qt refuses to quit while a modal dialog is open next to the main window: the
Dock only beeps. Handling the quit Apple event ourselves lets the window close
its dialogs (asking about unsaved changes) and then quit.
"""
from __future__ import annotations

import struct
import sys

_handler = None


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


def install(callback) -> bool:
    """Call ``callback()`` on the quit Apple event. Only on the real Cocoa platform.

    Call it once the event loop runs: AppKit installs its own handler at launch
    and would replace one installed earlier."""
    global _handler
    if sys.platform != "darwin":
        return False
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        import objc
        from Foundation import NSAppleEventManager, NSObject
    except ImportError:
        return False

    if _handler is None:
        class RemoteHubQuitHandler(NSObject):
            @objc.typedSelector(b"v@:@@")
            def handleQuit_withReply_(self, _event, _reply):
                self.callback()

        _handler = RemoteHubQuitHandler.alloc().init()
    _handler.callback = callback
    NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(
        _handler, b"handleQuit:withReply:", _fourcc("aevt"), _fourcc("quit"))
    return True
