"""macOS AX adapter: negotiate AX exposure and focus; never read text or titles."""
from collections import deque
import os
import time

from .chat_focus import App, BROWSERS, BLOCKED_ROLES, chat_metadata


def _app_from_running(app):
    if app is None or not app.bundleIdentifier():
        return None
    return App(str(app.bundleIdentifier()), int(app.processIdentifier()), str(app.localizedName() or ""))


def running_apps():
    from AppKit import NSWorkspace
    return sorted((App(str(a.bundleIdentifier()), int(a.processIdentifier()), str(a.localizedName()))
                   for a in NSWorkspace.sharedWorkspace().runningApplications()
                   if a.activationPolicy() == 0 and a.bundleIdentifier()
                   and int(a.processIdentifier()) != os.getpid() and str(a.bundleIdentifier()) not in BROWSERS),
                  key=lambda a: a.name.casefold())


class MacWorkspaceObserver:
    """Main-thread NSWorkspace activation observer; caches immutable App state."""

    def __init__(self, on_activate, *, center=None, workspace=None):
        from AppKit import NSWorkspace, NSWorkspaceApplicationKey, NSWorkspaceDidActivateApplicationNotification
        self._workspace = workspace or NSWorkspace.sharedWorkspace()
        self._center = center or self._workspace.notificationCenter()
        self._app_key = NSWorkspaceApplicationKey
        self._on_activate = on_activate
        self.current_app = _app_from_running(self._workspace.frontmostApplication())
        self._token = self._center.addObserverForName_object_queue_usingBlock_(
            NSWorkspaceDidActivateApplicationNotification, None, None, self._handle
        )

    def _handle(self, notification):
        info = notification.userInfo() if notification is not None else None
        running = info.get(self._app_key) if info is not None else None
        self.current_app = _app_from_running(running or self._workspace.frontmostApplication())
        self._on_activate()

    def refresh(self):
        self.current_app = _app_from_running(self._workspace.frontmostApplication())
        return self.current_app

    def close(self):
        if self._token is not None:
            self._center.removeObserver_(self._token)
            self._token = None


class MacFocusBackend:
    notifies_activation = False

    @staticmethod
    def applications():
        return running_apps()

    def __init__(self, frontmost_provider=None):
        import ApplicationServices as ax
        import Quartz
        from AppKit import NSWorkspace
        self.ax, self.q = ax, Quartz
        self.workspace = NSWorkspace.sharedWorkspace()
        self._frontmost_provider = frontmost_provider
        self.notifies_activation = frontmost_provider is not None
        self._enhanced_requests = {}
        ax.AXUIElementSetMessagingTimeout(ax.AXUIElementCreateSystemWide(), 0.08)

    def prepare(self, app, cancelled):
        """Ask a selected foreground app to expose its web accessibility tree.

        AppKit/WebKit can advertise windows while withholding all web controls.
        Only request the supported app attribute, at most once per process.
        Never alter macOS permissions, keyboard navigation, or input focus here.
        """
        if cancelled() or self._enhanced_requests.get(app.bundle) == app.pid or self.frontmost() != app:
            return
        root = self.root(app)
        attribute = "AXEnhancedUserInterface"
        error, value = self.ax.AXUIElementCopyAttributeValue(root, attribute, None)
        if error:
            if error == self.ax.kAXErrorAttributeUnsupported:
                self._enhanced_requests[app.bundle] = app.pid
            return
        if value:
            self._enhanced_requests[app.bundle] = app.pid
            return
        error, settable = self.ax.AXUIElementIsAttributeSettable(root, attribute, None)
        if error or not settable or cancelled() or self.frontmost() != app:
            return
        self._enhanced_requests[app.bundle] = app.pid
        # The observed GPT returns -25208 even though readback becomes true and
        # its full tree appears. Do not abort capture based on this return code.
        self.ax.AXUIElementSetAttributeValue(root, attribute, True)

    def frontmost(self):
        if self._frontmost_provider is not None:
            return self._frontmost_provider()
        # AXFocusedApplication fails on the observed Mac. This refreshed AppKit
        # path was verified there, and Qt also keeps the main Cocoa loop running.
        from Foundation import NSDate, NSRunLoop
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.005))
        return _app_from_running(self.workspace.frontmostApplication())

    def root(self, app):
        if not self.ax.AXIsProcessTrusted():
            raise RuntimeError("请在辅助功能中授权本应用")
        return self.ax.AXUIElementCreateApplication(app.pid)

    def read(self, element, attr):
        if element is None:
            return None
        error, value = self.ax.AXUIElementCopyAttributeValue(element, attr, None)
        return None if error else value

    def window(self, app):
        return self.read(self.root(app), "AXFocusedWindow")

    def focused(self, app):
        return self.read(self.root(app), "AXFocusedUIElement")

    def children(self, element):
        error, value = self.ax.AXUIElementCopyAttributeValue(element, "AXChildren", None)
        if error in (self.ax.kAXErrorNoValue, self.ax.kAXErrorAttributeUnsupported):
            return []
        if error:
            raise RuntimeError("无法完整读取输入框结构")
        return value or []

    def settable(self, element):
        error, value = self.ax.AXUIElementIsAttributeSettable(element, "AXFocused", None)
        return not error and bool(value)

    def modal(self, window):
        return bool(self.read(window, "AXSheets") or self.read(window, "AXModal")
                    or self.read(window, "AXSubrole") in {"AXDialog", "AXSystemDialog"})

    def input_stamp(self):
        q = self.q
        return tuple(int(q.CGEventSourceCounterForEventType(q.kCGEventSourceStateCombinedSessionState, kind))
                     for kind in (q.kCGEventKeyDown, q.kCGEventLeftMouseDown, q.kCGEventRightMouseDown,
                                  q.kCGEventOtherMouseDown, q.kCGEventScrollWheel))

    def rect(self, element):
        position, size = self.read(element, "AXPosition"), self.read(element, "AXSize")
        if position is None or size is None:
            return None
        try:
            ok_p, point = self.ax.AXValueGetValue(position, self.ax.kAXValueCGPointType, None)
            ok_s, dimensions = self.ax.AXValueGetValue(size, self.ax.kAXValueCGSizeType, None)
            return (point.x, point.y, dimensions.width, dimensions.height) if ok_p and ok_s else None
        except (TypeError, ValueError, AttributeError):
            return None

    def visible(self, element, window):
        if self.read(element, "AXHidden"):
            return False
        rect, outer = self.rect(element), self.rect(window)
        if rect is None or outer is None:
            return False
        x, y, w, h = rect
        left, top, width, height = outer
        return w > 0 and h > 0 and left <= x and top <= y and x + w <= left + width and y + h <= top + height

    def discover(self, app, window, cancelled):
        if self.modal(window):
            return None
        pending = deque([(window, 0)])
        candidates = []
        deadline = time.monotonic() + 0.6
        count = 0
        while pending and count < 700 and time.monotonic() < deadline and not cancelled():
            node, depth = pending.pop()
            count += 1
            role = self.read(node, "AXRole")
            if role is None:
                return None  # Incomplete evidence must not prove uniqueness.
            if role in BLOCKED_ROLES or self.read(node, "AXModal"):
                return None
            if role == "AXTextArea":
                if (chat_metadata(self.read(node, "AXIdentifier"), self.read(node, "AXPlaceholderValue"))
                        and self.read(node, "AXEnabled") is True and self.settable(node)
                        and self.visible(node, window)):
                    candidates.append(node)
                    if len(candidates) > 1:
                        return None
                continue
            if role in {"AXStaticText", "AXImage", "AXMenuBar"}:
                continue
            children = self.children(node)
            if children and (depth >= 64 or len(pending) + count + len(children) > 700):
                return None
            pending.extend((child, depth + 1) for child in children)
        if pending or cancelled() or len(candidates) != 1:
            return None
        return candidates[0]

    def focus(self, target):
        self.last_focus_error = int(self.ax.AXUIElementSetAttributeValue(target, "AXFocused", True))
        return self.last_focus_error == 0

    def app_build(self, app):
        from AppKit import NSRunningApplication
        from Foundation import NSBundle
        from .chat_focus_locator import digest
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(app.pid)
        if running is None or not running.bundleURL(): return None
        bundle = NSBundle.bundleWithURL_(running.bundleURL())
        if bundle is None: return None
        version = bundle.objectForInfoDictionaryKey_("CFBundleVersion")
        short = bundle.objectForInfoDictionaryKey_("CFBundleShortVersionString")
        return digest([app.bundle, str(version), str(short)]) if version or short else None

    def describe(self, app, window, target):
        from .chat_focus_locator import describe
        return describe(self, window, target, self.app_build(app))

    def resolve(self, app, window, locator, cancelled):
        from .chat_focus_locator import resolve
        return resolve(self, window, locator, self.app_build(app), cancelled)
