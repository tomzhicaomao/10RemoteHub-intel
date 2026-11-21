"""Exercise focus decisions with fake controls; never focus a desktop app."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
try:
    import restore_chatgpt_input as trial
finally:
    sys.path.pop(0)


class Backend:
    pid = 1
    root = "app"

    def __init__(self):
        self.tree = {
            "app": {"AXFocusedWindow": "win", "AXFocusedUIElement": "body"},
            "win": {"AXRole": "AXWindow", "AXChildren": ["group", "body"]},
            "group": {"AXRole": "AXGroup", "AXParent": "win", "AXChildren": ["input"]},
            "input": {"AXRole": "AXTextArea", "AXParent": "group", "AXEnabled": True, "AXFocused": False},
            "body": {"AXRole": "AXStaticText", "AXParent": "win"},
            "rename": {"AXRole": "AXTextField", "AXParent": "win"},
        }
        self.front = 1
        self.requests = []
        self.now = 0.0
        self.accept = True
        self.focus_error = 0

    def read(self, element, attr):
        assert attr not in {"AXValue", "AXTitle", "AXSelectedText"}
        return self.tree.get(element, {}).get(attr)

    def settable(self, element, attr): return True
    def frontmost_pid(self): return self.front
    def input_stamp(self): return (1, 2, 3)
    def wait(self, seconds): self.now += seconds

    def focus(self, element):
        self.requests.append(element)
        if self.accept and not self.focus_error:
            self.tree["app"]["AXFocusedUIElement"] = element
            self.tree[element]["AXFocused"] = True
        return self.focus_error


def test_restores_exact_live_target_once_and_checks_result():
    b = Backend()
    assert trial.restore_once(b, "input", "win") == "focus_restored"
    assert b.requests == ["input"]


@pytest.mark.parametrize("focused,expected", [("rename", "another_editor_active"),
                                               (None, "focus_context_unknown"),
                                               ("input", "already_focused")])
def test_existing_editor_or_unknown_focus_is_not_overridden(focused, expected):
    b = Backend()
    b.tree["app"]["AXFocusedUIElement"] = focused
    assert trial.restore_once(b, "input", "win") == expected
    assert not b.requests


def test_nested_child_of_rename_editor_is_also_protected():
    b = Backend()
    b.tree["body"]["AXParent"] = "rename"
    assert trial.restore_once(b, "input", "win") == "another_editor_active"
    assert not b.requests


@pytest.mark.parametrize("attr,value,expected", [("AXSheets", ["sheet"], "dialog_or_menu_active"),
                                                ("AXModal", True, "dialog_or_menu_active")])
def test_modal_window_is_not_touched(attr, value, expected):
    b = Backend()
    b.tree["win"][attr] = value
    assert trial.restore_once(b, "input", "win") == expected
    assert not b.requests


def test_stale_or_detached_target_never_falls_back_to_another_input():
    b = Backend()
    b.tree["input"]["AXParent"] = None
    assert trial.restore_once(b, "input", "win") == "target_detached"
    assert not b.requests
    del b.tree["input"]
    assert trial.restore_once(b, "input", "win") == "target_unavailable"


def test_new_window_or_other_foreground_is_not_activated():
    b = Backend()
    b.front = 2
    assert trial.restore_once(b, "input", "win") == "foreground_changed"
    b.front = 1
    b.tree["app"]["AXFocusedWindow"] = "new_window"
    assert trial.restore_once(b, "input", "win") == "window_changed"
    assert not b.requests


def test_user_interaction_during_checks_cancels_focus():
    b = Backend()
    stamps = iter([(1, 2), (1, 3)])
    b.input_stamp = lambda: next(stamps)
    assert trial.restore_once(b, "input", "win") == "user_interacted"
    assert not b.requests


def test_focus_change_during_checks_cancels_focus():
    b = Backend()
    fronts = []

    def foreground():
        fronts.append(1)
        if len(fronts) == 2:
            b.tree["app"]["AXFocusedUIElement"] = "rename"
        return 1

    b.frontmost_pid = foreground
    assert trial.restore_once(b, "input", "win") == "focus_changed"
    assert not b.requests


def test_api_success_without_actual_focus_is_not_reported_as_restored():
    b = Backend()
    b.accept = False
    assert trial.restore_once(b, "input", "win") == "focus_not_confirmed"
    assert b.requests == ["input"]


def test_failed_request_is_not_retried():
    b = Backend()
    b.focus_error = -25204
    assert trial.restore_once(b, "input", "win") == "focus_request_failed"
    assert b.requests == ["input"]


def test_capture_requires_same_manually_sampled_structure():
    b = Backend()
    b.tree["app"]["AXFocusedUIElement"] = "input"
    sample = trial.inspect_focused_input(b.root, b.read, b.settable)
    chain = sample["ancestor_chain"]
    assert trial.capture_target(b, chain)[0] == ("input", "win")
    b.tree["group"]["AXChildren"] = ["rename", "input"]
    assert trial.capture_target(b, chain)[0] is None
    assert not b.requests


def test_restoration_retains_object_even_if_sibling_indexes_change():
    b = Backend()
    b.tree["group"]["AXChildren"] = ["rename", "input"]
    assert trial.restore_once(b, "input", "win") == "focus_restored"
    assert b.requests == ["input"]


def test_trial_waits_for_departure_and_returns_after_single_attempt(monkeypatch):
    b = Backend()
    monkeypatch.setattr(trial, "capture_target", lambda *_: (("input", "win"), "captured"))
    fronts = iter([None, 2, 1])
    b.frontmost_pid = lambda: next(fronts, 1)
    assert trial.run_trial(b, [], clock=lambda: b.now) == "focus_restored"
    assert b.requests == ["input"]


def test_trial_times_out_without_departure(monkeypatch):
    b = Backend()
    monkeypatch.setattr(trial, "capture_target", lambda *_: (("input", "win"), "captured"))
    assert trial.run_trial(b, [], seconds=1, clock=lambda: b.now).startswith("timeout:")
    assert not b.requests


def test_profile_rejects_multiple_candidates(tmp_path):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps({"apps": [{"bundle": trial.BUNDLE, "pid": 1,
                                          "focused_samples": [{}, {}]}]}))
    with pytest.raises(ValueError, match="不唯一"):
        trial.load_profile(path)


def test_no_focus_action_without_explicit_flag():
    with pytest.raises(SystemExit) as error:
        trial.main([])
    assert error.value.code == 2
