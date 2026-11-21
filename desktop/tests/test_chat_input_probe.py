"""The diagnostic must remain bounded and must not inspect user text."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest


spec = importlib.util.spec_from_file_location(
    "chat_input_probe", Path(__file__).parents[1] / "scripts" / "probe_chat_inputs.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_collects_metadata_without_reading_text_or_titles():
    reads = []
    tree = {"root": {"AXRole": "AXWindow", "AXChildren": ["input"]},
            "input": {"AXRole": "AXTextArea", "AXIdentifier": "composer",
                      "AXPlaceholderValue": "Message", "AXFocused": False,
                      "AXValue": "private draft", "AXTitle": "private conversation"}}

    def read(element, attr):
        reads.append(attr)
        return tree[element].get(attr)

    result = probe.inspect_tree("root", read, lambda *_: True)
    assert result["visited"] == 2
    assert not result["incomplete"]
    assert result["input_candidates"][0]["identifier"] == "composer"
    assert "AXValue" not in reads and "AXTitle" not in reads


def test_cycle_is_bounded_and_marked_incomplete():
    result = probe.inspect_tree(
        "root", lambda node, attr: [node] if attr == "AXChildren" else "AXGroup",
        lambda *_: False, limit=10)
    assert result["visited"] == 10
    assert result["incomplete"]


def test_large_tree_caps_queued_work():
    result = probe.inspect_tree(
        "root", lambda node, attr: list(range(100)) if attr == "AXChildren" else "AXGroup",
        lambda *_: False, limit=5)
    assert result["visited"] == 5 and result["incomplete"]


def test_deadline_does_not_report_no_inputs_as_complete():
    ticks = iter([0, 5])
    result = probe.inspect_tree("root", lambda *_: None, lambda *_: False,
                                seconds=4, clock=lambda: next(ticks))
    assert result["visited"] == 0 and result["incomplete"]


def test_missing_attributes_do_not_mean_supported():
    result = probe.inspect_tree("root", lambda *_: None, lambda *_: None)
    assert result["input_candidates"] == []
    assert result["roles"] == {"unknown": 1}


def test_untrusted_process_does_not_inspect_or_request_permission(monkeypatch, capsys):
    class App:
        def __init__(self, bundle):
            self.bundle = bundle

        def bundleIdentifier(self): return self.bundle
        def localizedName(self): return "Test app"
        def processIdentifier(self): return 123

    workspace = SimpleNamespace(runningApplications=lambda: [
        App("com.kimi.code.desktop"), App("unselected.app")])
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    # No mutation, authorization prompt, or tree-reading APIs are available.
    monkeypatch.setitem(probe.sys.modules, "ApplicationServices",
                        SimpleNamespace(AXIsProcessTrusted=lambda: False))
    monkeypatch.setitem(probe.sys.modules, "AppKit", SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)))
    assert probe.main([]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "permission_required"
    assert [a["bundle"] for a in report["apps"]] == ["com.kimi.code.desktop"]
    assert report["apps"][0]["status"] == "not_inspected"


def test_unsupported_platform_exits_without_native_imports(monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "platform", "linux")
    assert probe.main([]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unsupported_platform"


def test_windows_omitted_from_children_are_probed_separately():
    tree = {"app": {"AXRole": "AXApplication", "AXWindows": ["win"],
                    "AXFocusedWindow": "win", "AXFocusedUIElement": "input"},
            "win": {"AXRole": "AXWindow", "AXChildren": ["input"]},
            "input": {"AXRole": "AXTextArea"}}
    result = probe.inspect_application("app", lambda node, attr: tree[node].get(attr),
                                       lambda *_: True)
    assert result["input_candidates"] == []
    assert len(result["window_probe"]["input_candidates"]) == 1
    assert result["window_state"]["windows_attribute_count"] == 1
    assert result["window_state"]["focused_element_role"] == "AXTextArea"


def test_multiple_unfocused_windows_are_not_arbitrarily_selected():
    tree = {"app": {"AXRole": "AXApplication", "AXWindows": ["one", "two"]}}
    result = probe.inspect_application("app", lambda node, attr: tree[node].get(attr),
                                       lambda *_: True)
    assert "window_probe" not in result
    assert result["window_state"]["windows_attribute_count"] == 2


def test_no_windows_is_reported_without_inventing_candidates():
    result = probe.inspect_application("app", lambda node, attr: [] if attr == "AXWindows" else None,
                                       lambda *_: False)
    assert result["window_state"]["windows_attribute_count"] == 0
    assert not result["input_candidates"] and "window_probe" not in result


def test_frontmost_guard_skips_background_app_without_ax_reads(monkeypatch, capsys):
    app = SimpleNamespace(bundleIdentifier=lambda: "com.openai.codex",
                          localizedName=lambda: "ChatGPT", processIdentifier=lambda: 123,
                          isHidden=lambda: False)
    workspace = SimpleNamespace(runningApplications=lambda: [app],
                                frontmostApplication=lambda: None)
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setattr(probe, "live_frontmost_pid", lambda ax, **kwargs: None)
    monkeypatch.setitem(probe.sys.modules, "ApplicationServices",
                        SimpleNamespace(AXIsProcessTrusted=lambda: True))
    monkeypatch.setitem(probe.sys.modules, "AppKit", SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)))
    assert probe.main(["--bundle", "com.openai.codex", "--require-frontmost"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["apps"][0]["status"] == "foreground_unavailable"


def test_truncated_single_candidate_is_not_reported_as_complete():
    item = {"incomplete": True, "input_candidates": [{"role": "AXTextArea"}]}
    assert probe.candidate_status(item) == "partial_candidates_only"
    item["input_candidates"] = []
    assert probe.candidate_status(item) == "incomplete_no_candidates_observed"
    item["window_probe"] = {"incomplete": False, "input_candidates": [{"role": "AXTextArea"}]}
    assert probe.candidate_status(item) == "partial_candidates_only"


def test_direct_focus_capture_ignores_whole_app_node_limit_and_private_text():
    tree = {"app": {"AXFocusedUIElement": "composer"},
            "composer": {"AXRole": "AXTextArea", "AXParent": "group",
                         "AXValue": "private draft"},
            "group": {"AXRole": "AXGroup", "AXChildren": ["other", "composer"],
                      "AXParent": "win"},
            "win": {"AXRole": "AXWindow", "AXChildren": ["group"]}}
    calls = []

    def read(node, attr):
        calls.append((node, attr))
        assert attr not in {"AXValue", "AXTitle", "AXDescription", "AXSelectedText"}
        assert (node, attr) != ("app", "AXChildren")
        return tree[node].get(attr)

    result = probe.inspect_focused_input("app", read, lambda *_: True)
    assert result["status"] == "focused_text_input_sampled"
    assert result["focused_sample"]["role"] == "AXTextArea"
    assert result["ancestor_chain"][0]["index_in_parent"] == 1
    assert result["focus_unchanged"] and not result["trace_incomplete"]


def test_direct_focus_capture_detects_focus_change():
    focus = iter(["first", "second"])

    def read(node, attr):
        if attr == "AXFocusedUIElement":
            return next(focus)
        return "AXTextArea" if attr == "AXRole" else None

    result = probe.inspect_focused_input("app", read, lambda *_: True)
    assert result["status"] == "focus_changed_during_probe"
    assert result["trace_incomplete"]


def test_direct_focus_capture_bounds_parent_cycles():
    def read(node, attr):
        return {"AXFocusedUIElement": "loop", "AXRole": "AXTextArea",
                "AXParent": "loop", "AXChildren": ["loop"]}.get(attr)

    result = probe.inspect_focused_input("app", read, lambda *_: True, limit=3)
    assert len(result["ancestor_chain"]) == 3 and result["trace_incomplete"]


def test_direct_focus_capture_handles_absent_focus():
    result = probe.inspect_focused_input("app", lambda *_: None, lambda *_: False)
    assert result["status"] == "no_focused_element"


@pytest.mark.parametrize("capture", [False, True, "watch", "watch-fallback", "watch-unknown"])
def test_trusted_probe_uses_only_read_apis_and_never_verifies_chat_identity(monkeypatch, capsys, capture):
    app = SimpleNamespace(bundleIdentifier=lambda: "com.openai.codex",
                          localizedName=lambda: "ChatGPT", processIdentifier=lambda: 123,
                          isHidden=lambda: False)
    workspace = SimpleNamespace(runningApplications=lambda: [app],
                                frontmostApplication=lambda: app)
    tree = {"system": {"AXFocusedApplication": "app"},
            "app": {"AXRole": "AXApplication", "AXChildren": ["win"],
                    "AXWindows": ["win"], "AXFocusedWindow": "win",
                    "AXFocusedUIElement": "edit"},
            "win": {"AXRole": "AXWindow", "AXChildren": ["edit"]},
            # Same role and focus capabilities can describe a rename editor.
            "edit": {"AXRole": "AXTextArea", "AXFocused": True, "AXEnabled": True}}
    reads = []

    def read(node, attr, unused):
        reads.append(attr)
        if node == "system" and capture in {"watch-fallback", "watch-unknown"}:
            return -25204, None
        return 0, tree[node].get(attr)

    # Deliberately provide no action, set-attribute, keyboard, or mouse API.
    ax = SimpleNamespace(AXIsProcessTrusted=lambda: True,
                         AXUIElementCreateSystemWide=lambda: "system",
                         AXUIElementGetPid=lambda *args: (0, 123),
                         AXUIElementCreateApplication=lambda pid: "app",
                         AXUIElementSetMessagingTimeout=lambda *args: 0,
                         AXUIElementCopyAttributeValue=read,
                         AXUIElementIsAttributeSettable=lambda *args: (0, True))
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setitem(probe.sys.modules, "ApplicationServices", ax)
    monkeypatch.setitem(probe.sys.modules, "AppKit", SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)))
    args = ["--bundle", "com.openai.codex", "--require-frontmost"]
    watching = capture in {"watch", "watch-fallback", "watch-unknown"}
    if watching:
        current = [0.0]
        native_loop = SimpleNamespace(runUntilDate_=lambda delay: current.__setitem__(0, current[0] + delay))
        monkeypatch.setitem(probe.sys.modules, "Foundation", SimpleNamespace(
            NSDate=SimpleNamespace(dateWithTimeIntervalSinceNow_=lambda seconds: seconds),
            NSRunLoop=SimpleNamespace(currentRunLoop=lambda: native_loop)))
        original_watch = probe.watch_focused
        monkeypatch.setattr(probe, "watch_focused", lambda sample, wait, seconds:
                            original_watch(sample, wait, seconds=seconds, clock=lambda: current[0]))
        args.extend(["--watch-focused", "2"])
        if capture == "watch-unknown":
            workspace.frontmostApplication = lambda: None
    elif capture:
        args.append("--capture-focused")
    assert probe.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["diagnostic_only"] and not report["focus_action_performed"]
    assert not report["apps"][0]["chat_input_verified"]
    expected = ("no_focused_input_sampled" if capture == "watch-unknown" else "focused_samples_collected" if watching
                else "focused_text_input_sampled" if capture else "candidates_only")
    assert report["apps"][0]["status"] == expected
    if watching and capture != "watch-unknown":
        assert len(report["apps"][0]["focused_samples"]) == 1
        assert report["apps"][0]["observed_foreground_apps"][0]["bundle"] == "com.openai.codex"
    if capture == "watch-unknown":
        assert "foreground_unavailable" in report["apps"][0]["observation_states"]
        assert "skipped_not_frontmost" not in report["apps"][0]["observation_states"]
    if capture == "watch-fallback":
        assert report["apps"][0]["foreground_diagnostics"]["AXFocusedApplication:-25204"] > 0
        assert report["apps"][0]["foreground_diagnostics"]["workspace_fallback_used"] > 0
    assert "AXValue" not in reads and "AXTitle" not in reads


def test_live_foreground_requeries_ax_instead_of_appkit_cache():
    pids = iter([100, 200])
    ax = SimpleNamespace(AXUIElementCreateSystemWide=lambda: "system",
                         AXUIElementSetMessagingTimeout=lambda *args: 0,
                         AXUIElementCopyAttributeValue=lambda *args: (0, "app"),
                         AXUIElementGetPid=lambda *args: (0, next(pids)))
    assert probe.live_frontmost_pid(ax) == 100
    assert probe.live_frontmost_pid(ax) == 200


def test_live_foreground_failure_remains_unknown():
    ax = SimpleNamespace(AXUIElementCreateSystemWide=lambda: "system",
                         AXUIElementSetMessagingTimeout=lambda *args: 0,
                         AXUIElementCopyAttributeValue=lambda *args: (-25204, None))
    assert probe.live_frontmost_pid(ax) is None


def test_foreground_failure_retains_error_and_uses_refreshed_fallback():
    errors = probe.Counter()
    ax = SimpleNamespace(AXUIElementCreateSystemWide=lambda: "system",
                         AXUIElementSetMessagingTimeout=lambda *args: 0,
                         AXUIElementCopyAttributeValue=lambda *args: (-25204, None))
    assert probe.live_frontmost_pid(ax, diagnostics=errors, fallback=lambda: 321) == 321
    assert errors["AXFocusedApplication:-25204"] == 1
    assert errors["workspace_fallback_used"] == 1


def test_foreground_fallback_still_unknown_does_not_invent_pid():
    errors = probe.Counter()
    ax = SimpleNamespace(AXUIElementCreateSystemWide=lambda: "system",
                         AXUIElementSetMessagingTimeout=lambda *args: 0,
                         AXUIElementCopyAttributeValue=lambda *args: (-25212, None))
    assert probe.live_frontmost_pid(ax, diagnostics=errors, fallback=lambda: None) is None
    assert errors["workspace_foreground_unavailable"] == 1


def test_watch_captures_later_focus_and_preserves_it_after_switching_back():
    current = [0.0]
    captured = {"status": "focused_text_input_sampled", "focused_sample": {"role": "AXTextArea"}}
    results = iter([{"status": "skipped_not_frontmost"}, captured, captured,
                    {"status": "skipped_not_frontmost"}])
    result = probe.watch_focused(lambda: next(results),
                                 lambda delay: current.__setitem__(0, current[0] + delay),
                                 seconds=2, clock=lambda: current[0])
    assert result["status"] == "focused_samples_collected"
    assert len(result["focused_samples"]) == 1
    assert result["focused_samples"][0]["observations"] == 2
    assert not result["chat_input_verified"]


def test_watch_excludes_focus_changes_and_keeps_partial_results_on_interrupt():
    current = [0.0]
    results = iter([{"status": "focus_changed_during_probe"},
                    {"status": "focused_text_input_sampled", "focused_sample": {}}])

    def sample():
        try:
            return next(results)
        except StopIteration:
            raise KeyboardInterrupt

    result = probe.watch_focused(sample, lambda delay: current.__setitem__(0, current[0] + delay),
                                 seconds=10, clock=lambda: current[0])
    assert result["interrupted"] and len(result["focused_samples"]) == 1
    assert result["observation_states"]["focus_changed_during_probe"] == 1
