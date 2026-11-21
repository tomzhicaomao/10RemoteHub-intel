"""One-shot GPT focus trial, explicitly enabled with --focus-once.

Uses a manually sampled structure only to validate initial capture. Restoration
uses the same live AX element and window, never a saved index as a focus target.
No keyboard/mouse events, text reads, text writes, or background installation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

from probe_chat_inputs import EDIT_ROLES, inspect_focused_input, live_frontmost_pid, refreshed_frontmost_pid

BUNDLE = "com.openai.codex"
DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "artifacts/chat-input-probe/chatgpt-watch.json"
BLOCKED_ROLES = {"AXSheet", "AXDialog", "AXMenu", "AXMenuItem", "AXMenuBar"}


def load_profile(path):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    matches = [app for app in report.get("apps", []) if app.get("bundle") == BUNDLE]
    if len(matches) != 1:
        raise ValueError("需要一份明确对应 GPT 的手动输入框样本")
    app = matches[0]
    samples = app.get("focused_samples", [])
    if len(samples) != 1 or app.get("samples_truncated"):
        raise ValueError("样本不唯一或被截断，不能选择聚焦目标")
    sample = samples[0]["sample"]
    chain = sample.get("ancestor_chain", [])
    if (sample.get("trace_incomplete") or not sample.get("focus_unchanged")
            or sample.get("focused_sample", {}).get("role") != "AXTextArea"
            or not chain or chain[-1].get("role") != "AXWindow"):
        raise ValueError("需要完整且稳定的聊天文本框样本")
    return int(app["pid"]), chain


def capture_target(backend, chain):
    """Bind only a currently focused live object matching the user's sample."""
    if backend.frontmost_pid() != backend.pid:
        return None, "waiting_for_gpt"
    focused = backend.read(backend.root, "AXFocusedUIElement")
    window = backend.read(backend.root, "AXFocusedWindow")
    if focused is None or window is None:
        return None, "focus_or_window_unavailable"
    sample = inspect_focused_input(backend.root, backend.read, backend.settable)
    if (sample["status"] != "focused_text_input_sampled" or sample.get("trace_incomplete")
            or sample.get("ancestor_chain") != chain):
        return None, "waiting_for_matching_chat_input"
    if (backend.frontmost_pid() != backend.pid
            or backend.read(backend.root, "AXFocusedUIElement") != focused
            or backend.read(backend.root, "AXFocusedWindow") != window):
        return None, "capture_changed"
    return (focused, window), "captured"


def focus_context(backend, focused, target, window):
    """A different editable control, menu, or unresolved focus wins over us."""
    for _ in range(40):
        if focused is None:
            return "focus_context_unknown"
        role = backend.read(focused, "AXRole")
        if role is None:
            return "focus_context_unknown"
        subrole = backend.read(focused, "AXSubrole")
        if role in BLOCKED_ROLES or subrole in {"AXDialog", "AXSystemDialog", "AXSecureTextField"}:
            return "dialog_or_menu_active"
        if focused == target:
            return "already_focused"
        if role in EDIT_ROLES:
            return "another_editor_active"
        if focused == window:
            return "clear"
        focused = backend.read(focused, "AXParent")
    return "focus_context_unknown"


def restore_once(backend, target, window):
    """At most one focus request, with foreground/window/input guards."""
    stamp = backend.input_stamp()
    if backend.frontmost_pid() != backend.pid:
        return "foreground_changed"
    if backend.read(backend.root, "AXFocusedWindow") != window:
        return "window_changed"
    if backend.read(window, "AXSheets"):
        return "dialog_or_menu_active"
    if backend.read(window, "AXModal"):
        return "dialog_or_menu_active"
    if (backend.read(target, "AXRole") != "AXTextArea"
            or backend.read(target, "AXEnabled") is not True
            or backend.settable(target, "AXFocused") is not True):
        return "target_unavailable"
    # Check that the retained handle still belongs to the exact captured window.
    ancestor = target
    for _ in range(40):
        if ancestor == window:
            break
        ancestor = backend.read(ancestor, "AXParent") if ancestor is not None else None
    else:
        return "target_detached"
    focused = backend.read(backend.root, "AXFocusedUIElement")
    context = focus_context(backend, focused, target, window)
    if context != "clear":
        return context
    # Recheck after all IPC reads. Never retry if the user interacted meanwhile.
    if (backend.frontmost_pid() != backend.pid
            or backend.read(backend.root, "AXFocusedWindow") != window):
        return "foreground_or_window_changed"
    if backend.read(backend.root, "AXFocusedUIElement") != focused:
        return "focus_changed"
    if backend.input_stamp() != stamp:
        return "user_interacted"
    error = backend.focus(target)
    if error:
        return "focus_request_failed"
    # A successful AX return code alone is not proof that focus actually moved.
    backend.wait(0.1)
    if (backend.frontmost_pid() == backend.pid
            and backend.read(backend.root, "AXFocusedWindow") == window
            and backend.read(backend.root, "AXFocusedUIElement") == target
            and backend.read(target, "AXFocused") is True):
        return "focus_restored"
    return "focus_not_confirmed"


def run_trial(backend, chain, *, seconds=60, clock=time.monotonic, tell=lambda text: None):
    deadline = clock() + seconds
    captured = None
    left = False
    last = "waiting_for_gpt"
    while clock() < deadline:
        if captured is None:
            captured, last = capture_target(backend, chain)
            if captured is not None:
                tell("已记住聊天输入框。现在点一下聊天正文，切到其他应用，再切回 GPT；回来后先不要点击或打字。")
        else:
            front = backend.frontmost_pid()
            if front is not None and front != backend.pid:
                left = True
                last = "waiting_for_return"
            elif left and front == backend.pid:
                return restore_once(backend, *captured)
        backend.wait(0.1)
    return "timeout:" + last


class MacBackend:
    def __init__(self, pid):
        import ApplicationServices as ax
        import Quartz
        from AppKit import NSWorkspace
        self.ax, self.quartz, self.pid = ax, Quartz, pid
        self.workspace = NSWorkspace.sharedWorkspace()
        self.diagnostics = Counter()
        self.focus_requests = 0
        if not ax.AXIsProcessTrusted():
            raise RuntimeError("请从已授权辅助功能的本机终端运行")
        apps = [app for app in self.workspace.runningApplications()
                if str(app.bundleIdentifier()) == BUNDLE and int(app.processIdentifier()) == pid]
        if len(apps) != 1:
            raise RuntimeError("GPT 进程已变化；旧样本不能用于这次验证")
        self.root = ax.AXUIElementCreateApplication(pid)
        if ax.AXUIElementSetMessagingTimeout(self.root, 0.15):
            raise RuntimeError("无法设置辅助功能调用超时")

    def frontmost_pid(self):
        return live_frontmost_pid(self.ax, diagnostics=self.diagnostics,
                                  fallback=lambda: refreshed_frontmost_pid(self.workspace))

    def read(self, element, attr):
        error, value = self.ax.AXUIElementCopyAttributeValue(element, attr, None)
        if error:
            self.diagnostics[f"{attr}:{int(error)}"] += 1
            return None
        return value

    def settable(self, element, attr):
        error, value = self.ax.AXUIElementIsAttributeSettable(element, attr, None)
        return bool(value) if not error else None

    def input_stamp(self):
        q = self.quartz
        return tuple(int(q.CGEventSourceCounterForEventType(q.kCGEventSourceStateCombinedSessionState, kind))
                     for kind in (q.kCGEventKeyDown, q.kCGEventLeftMouseDown, q.kCGEventRightMouseDown,
                                  q.kCGEventOtherMouseDown, q.kCGEventScrollWheel))

    def focus(self, target):
        self.focus_requests += 1
        error = self.ax.AXUIElementSetAttributeValue(target, "AXFocused", True)
        if error:
            self.diagnostics[f"set_focus:{int(error)}"] += 1
        return error

    @staticmethod
    def wait(seconds):
        from Foundation import NSDate, NSRunLoop
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--focus-once", action="store_true", help="Enable this one-shot foreground focus trial")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    args = parser.parse_args(argv)
    if not args.focus_once:
        parser.error("实际聚焦验证需显式传入 --focus-once；此工具不会自动后台启用")
    if sys.platform != "darwin":
        parser.error("此次验证仅适用于 macOS")
    backend = None
    try:
        pid, chain = load_profile(args.profile)
        backend = MacBackend(pid)
        tell = lambda message: print(message, file=sys.stderr, flush=True)
        tell("验证限时 60 秒：先切到 GPT，点聊天输入框并停留 3 秒；再点聊天正文，切到其他应用，再切回 GPT。只尝试恢复一次焦点。")
        status = run_trial(backend, chain, tell=tell)
    except KeyboardInterrupt:
        status = "cancelled"
    except Exception as exc:
        status = "error:" + str(exc)
    result = {"bundle": BUNDLE, "status": status,
              "focus_requests": backend.focus_requests if backend else 0,
              "diagnostics": dict(backend.diagnostics) if backend else {}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("验证结束：" + status, file=sys.stderr, flush=True)
    return 0 if status in {"focus_restored", "already_focused"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
