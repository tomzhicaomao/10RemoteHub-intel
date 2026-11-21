"""Read-only macOS chat input inventory; never requests permission or sets focus.

Run with the project's macOS Python. No text values, conversation titles, URLs,
clipboard contents, screenshots, or keystrokes are collected. Identifiers and
placeholders are UI metadata; review the report before sharing it.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import json
import sys
import time


# Observed bundle IDs, not an enabled product whitelist or compatibility claim.
OBSERVED_APPS = (
    "com.deepseek.dsh",
    "com.kimi.code.desktop",
    "com.google.antigravity",
    "com.openai.codex",
)
EDIT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}


def refreshed_frontmost_pid(workspace):
    """Let AppKit refresh its app-state cache before querying the foreground."""
    from Foundation import NSDate, NSRunLoop
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
    front = workspace.frontmostApplication()
    return int(front.processIdentifier()) if front is not None else None


def live_frontmost_pid(ax, *, diagnostics=None, fallback=None):
    """Retain AX errors and allow an explicitly refreshed workspace fallback."""
    diagnostics = diagnostics if diagnostics is not None else Counter()

    def failed(stage, error):
        diagnostics[f"{stage}:{int(error)}"] += 1
        pid = fallback() if fallback is not None else None
        diagnostics["workspace_fallback_used" if pid is not None else "workspace_foreground_unavailable"] += 1
        return pid

    system = ax.AXUIElementCreateSystemWide()
    error = ax.AXUIElementSetMessagingTimeout(system, 0.15)
    if error:
        return failed("system_timeout", error)
    error, app = ax.AXUIElementCopyAttributeValue(system, "AXFocusedApplication", None)
    if error or app is None:
        return failed("AXFocusedApplication", error)
    error, pid = ax.AXUIElementGetPid(app, None)
    if error or not pid or pid < 0:
        return failed("AXUIElementGetPid", error)
    return int(pid)


def watch_focused(sample, wait, *, seconds, clock=time.monotonic):
    """Keep bounded, distinct metadata samples across a relaxed capture window."""
    start = clock()
    captures = []
    states = Counter()
    interrupted = False
    truncated = False
    try:
        while clock() - start < seconds:
            result = sample()
            states[result["status"]] += 1
            if result["status"] == "focused_text_input_sampled":
                # These are metadata snapshots, not proof of control identity.
                previous = next((x for x in captures if x["sample"] == result), None)
                elapsed = round(clock() - start, 1)
                if previous is not None:
                    previous["last_seen_seconds"] = elapsed
                    previous["observations"] += 1
                elif len(captures) < 12:
                    captures.append({"first_seen_seconds": elapsed,
                                     "last_seen_seconds": elapsed,
                                     "observations": 1, "sample": result})
                else:
                    truncated = True
            remaining = seconds - (clock() - start)
            if remaining > 0:
                wait(min(0.5, remaining))
    except KeyboardInterrupt:
        interrupted = True
    return {"status": "focused_samples_collected" if captures else "no_focused_input_sampled",
            "focused_samples": captures, "observation_states": dict(states),
            "interrupted": interrupted, "samples_truncated": truncated,
            "chat_input_verified": False}


def candidate_status(item):
    """A partial inventory cannot establish uniqueness or chat semantics."""
    scans = [item]
    if "window_probe" in item:
        scans.append(item["window_probe"])
    has_candidates = any(scan.get("input_candidates") for scan in scans)
    incomplete = any(scan.get("incomplete") for scan in scans)
    if incomplete:
        return "partial_candidates_only" if has_candidates else "incomplete_no_candidates_observed"
    return "candidates_only" if has_candidates else "no_candidates_observed"


def inspect_focused_input(root, read, settable, *, limit=40, seconds=4.0,
                          clock=time.monotonic):
    """Sample the user's chosen focus directly, without a whole-app search.

    Ancestor indexes describe this snapshot only; they are not a safe reusable
    selector. No text values, accessible titles, or descriptions are collected.
    """
    focused = read(root, "AXFocusedUIElement")
    if focused is None:
        return {"status": "no_focused_element", "focused_sample": None}
    role = read(focused, "AXRole")
    sample = {"role": role, "subrole": read(focused, "AXSubrole"),
              "identifier": read(focused, "AXIdentifier"),
              "placeholder": read(focused, "AXPlaceholderValue"),
              "enabled": read(focused, "AXEnabled"),
              "focused": read(focused, "AXFocused"),
              "focus_settable": settable(focused, "AXFocused")}
    current = focused
    chain = []
    deadline = clock() + seconds
    reached_window = False
    while current is not None and len(chain) < limit and clock() < deadline:
        node = {"role": read(current, "AXRole"),
                "identifier": read(current, "AXIdentifier")}
        chain.append(node)
        if node["role"] in {"AXWindow", "AXApplication"}:
            reached_window = True
            break
        parent = read(current, "AXParent")
        if parent is None:
            break
        children = read(parent, "AXChildren")
        node["index_in_parent"] = next(
            (i for i, child in enumerate(children or []) if child == current), None)
        node["sibling_count"] = len(children) if children is not None else None
        current = parent
    unchanged = read(root, "AXFocusedUIElement") == focused
    status = "focused_text_input_sampled" if role in EDIT_ROLES else "focused_nontext_element"
    if not unchanged:
        status = "focus_changed_during_probe"
    return {"status": status, "focused_sample": sample, "ancestor_chain": chain,
            "trace_incomplete": not reached_window, "focus_unchanged": unchanged}


def inspect_application(root, read, settable):
    """Read explicit window attributes too: AXChildren may omit windows."""
    windows = read(root, "AXWindows")
    focused_window = read(root, "AXFocusedWindow")
    focused_element = read(root, "AXFocusedUIElement")
    result = inspect_tree(root, read, settable)
    result["window_state"] = {
        "windows_attribute_count": len(windows) if windows is not None else None,
        "focused_window_present": focused_window is not None,
        "focused_element_role": read(focused_element, "AXRole") if focused_element is not None else None,
    }
    # Keep this separate: it is evidence from an additional traversal, not extra
    # unique candidates to add to the application-level candidate count.
    if result["roles"].get("AXWindow", 0) == 0:
        target = focused_window
        if target is None and windows is not None and len(windows) == 1:
            target = windows[0]
        if target is not None:
            result["window_probe"] = inspect_tree(target, read, settable)
    return result


def inspect_tree(root, read, settable, *, limit=1200, seconds=4.0,
                 clock=time.monotonic):
    """Bound traversal and report candidates without asserting chat semantics."""
    deadline = clock() + seconds
    pending = deque([(root, 0)])
    roles = Counter()
    candidates = []
    visited = 0
    depth_limited = False
    while pending and visited < limit and clock() < deadline:
        element, depth = pending.popleft()
        visited += 1
        role = read(element, "AXRole")
        roles[str(role or "unknown")] += 1
        if role in EDIT_ROLES:
            candidates.append({
                "role": role,
                "subrole": read(element, "AXSubrole"),
                "identifier": read(element, "AXIdentifier"),
                "placeholder": read(element, "AXPlaceholderValue"),
                "enabled": read(element, "AXEnabled"),
                "focused": read(element, "AXFocused"),
                "focus_settable": settable(element, "AXFocused"),
            })
        children = read(element, "AXChildren") or []
        if depth >= 40:
            depth_limited |= bool(children)
            continue
        # AX trees may be malformed or huge. Cap queued work as well as visits.
        capacity = max(0, limit - visited - len(pending))
        if len(children) > capacity:
            depth_limited = True
        pending.extend((child, depth + 1) for child in children[:capacity])
    return {"visited": visited, "incomplete": bool(pending) or depth_limited,
            "roles": dict(roles), "input_candidates": candidates}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", action="append", help="Exact bundle ID; repeatable")
    parser.add_argument("--delay", type=int, default=0, choices=range(31),
                        metavar="0..30", help="Allow time to manually switch apps before reading")
    parser.add_argument("--require-frontmost", action="store_true",
                        help="Only inspect the selected application if it is frontmost")
    parser.add_argument("--capture-focused", action="store_true",
                        help="Read the manually selected input directly; requires the app in front")
    parser.add_argument("--watch-focused", type=int, default=0, choices=range(1, 31),
                        metavar="1..30", help="Observe focus metadata for this many seconds; one --bundle required")
    args = parser.parse_args(argv)
    if args.watch_focused and (not args.bundle or len(args.bundle) != 1):
        parser.error("--watch-focused requires exactly one --bundle")
    if sys.platform != "darwin":
        print(json.dumps({"status": "unsupported_platform"}))
        return 2

    import ApplicationServices as ax
    from AppKit import NSWorkspace

    if args.delay:
        instruction = "切到目标应用，并点一下真正的聊天输入框" if args.capture_focused else "切到目标应用并保持窗口打开"
        print(f"请在 {args.delay} 秒内{instruction}；只读取控件，不改变焦点。",
              file=sys.stderr, flush=True)
        time.sleep(args.delay)
    selected = set(args.bundle or OBSERVED_APPS)
    workspace = NSWorkspace.sharedWorkspace()
    running_apps = list(workspace.runningApplications())
    apps = [app for app in running_apps
            if str(app.bundleIdentifier()) in selected]
    trusted = bool(ax.AXIsProcessTrusted())
    report = {"status": "inspected" if trusted else "permission_required",
              "diagnostic_only": True, "focus_action_performed": False,
              "trusted": trusted, "apps": [],
              "not_running": sorted(selected - {str(a.bundleIdentifier()) for a in apps})}
    for app in apps:
        item = {"name": str(app.localizedName()), "bundle": str(app.bundleIdentifier()),
                "pid": int(app.processIdentifier())}
        report["apps"].append(item)
        if not trusted:
            item["status"] = "not_inspected"
            continue
        foreground_diagnostics = Counter()
        item["foreground_diagnostics"] = foreground_diagnostics

        def foreground_pid():
            return live_frontmost_pid(ax, diagnostics=foreground_diagnostics,
                                      fallback=lambda: refreshed_frontmost_pid(workspace))

        before = foreground_pid()
        item["frontmost_before"] = None if before is None else before == item["pid"]
        item["frontmost_source"] = "AXFocusedApplication_or_refreshed_NSWorkspace"
        if not args.watch_focused and (args.require_frontmost or args.capture_focused) and not item["frontmost_before"]:
            item["status"] = "foreground_unavailable" if before is None else "skipped_not_frontmost"
            continue
        errors = Counter()

        def read(element, name):
            error, value = ax.AXUIElementCopyAttributeValue(element, name, None)
            if error:
                errors[f"{name}:{int(error)}"] += 1
                return None
            return value

        def settable(element, name):
            error, value = ax.AXUIElementIsAttributeSettable(element, name, None)
            if error:
                errors[f"settable:{name}:{int(error)}"] += 1
                return None
            return bool(value)

        root = ax.AXUIElementCreateApplication(item["pid"])
        error = ax.AXUIElementSetMessagingTimeout(root, 0.15)
        if error:
            item["status"] = "timeout_setup_failed"
            item["error"] = int(error)
            continue
        if args.watch_focused:
            from Foundation import NSDate, NSRunLoop
            app_names = {int(a.processIdentifier()): (str(a.localizedName()), str(a.bundleIdentifier()))
                         for a in running_apps}
            foreground_apps = Counter()

            def sample():
                foreground = foreground_pid()
                foreground_apps[app_names.get(foreground, ("unknown", "unknown"))] += 1
                if foreground is None:
                    return {"status": "foreground_unavailable"}
                if foreground != item["pid"]:
                    return {"status": "skipped_not_frontmost"}
                result = inspect_focused_input(root, read, settable)
                after = foreground_pid()
                if after is None:
                    return {"status": "foreground_unavailable"}
                if after != item["pid"]:
                    return {"status": "foreground_changed_during_probe"}
                return result

            def wait(seconds):
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))

            print(f"开始连续观察 {args.watch_focused} 秒。现在切到 GPT，点击真正的聊天输入框并停留几秒；无需打字。",
                  file=sys.stderr, flush=True)
            item.update(watch_focused(sample, wait, seconds=args.watch_focused))
            item["observed_foreground_apps"] = [
                {"name": name, "bundle": bundle, "observations": count}
                for (name, bundle), count in foreground_apps.items()]
            print(f"观察结束，记录到 {len(item['focused_samples'])} 组文本框结构样本。", file=sys.stderr, flush=True)
        elif args.capture_focused:
            item.update(inspect_focused_input(root, read, settable))
        else:
            item.update(inspect_application(root, read, settable))
            item["status"] = candidate_status(item)
        after = foreground_pid()
        item["frontmost_after"] = None if after is None else after == item["pid"]
        item["read_errors"] = dict(errors)
        item["chat_input_verified"] = False
        if not args.watch_focused and (args.require_frontmost or args.capture_focused) and not item["frontmost_after"]:
            item["status"] = "foreground_unavailable" if after is None else "foreground_changed_during_probe"
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if trusted else 3


if __name__ == "__main__":
    raise SystemExit(main())
