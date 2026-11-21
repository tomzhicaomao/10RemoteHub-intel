from copy import deepcopy
import json
import plistlib

import pytest

from remotehub import chat_focus as cf
from remotehub.mac_chat_focus import MacFocusBackend

TARGET = cf.App("com.example.chat", 10, "Chat")
OTHER = cf.App("com.example.other", 20, "Other")


class Backend:
    def __init__(self):
        self.front = OTHER
        self.win = "window"
        self.current = "body"
        self.requests = []
        self.stamp = (1, 2)
        self.scan_result = "input"
        self.tree = {"window": {"AXRole": "AXWindow", "AXChildren": ["body", "input"]},
                     "body": {"AXRole": "AXStaticText", "AXParent": "window"},
                     "input": {"AXRole": "AXTextArea", "AXParent": "window", "AXEnabled": True},
                     "search": {"AXRole": "AXTextField", "AXParent": "window"}}

    def frontmost(self): return self.front
    def window(self, app): return self.win
    def focused(self, app): return self.current
    def input_stamp(self): return self.stamp
    def modal(self, window): return bool(self.tree[window].get("AXModal"))
    def settable(self, target): return target in self.tree
    def discover(self, app, window, cancelled): return self.scan_result
    def applications(self): return [TARGET]
    def prepare(self, app, cancelled): return None

    def read(self, element, name):
        assert name not in {"AXValue", "AXTitle", "AXSelectedText"}
        return self.tree.get(element, {}).get(name)

    def focus(self, target):
        self.requests.append(target)
        self.current = target
        self.tree[target]["AXFocused"] = True
        return True


def preferences(mode="auto", enabled=True):
    return {"enabled": enabled, "apps": {TARGET.bundle: {"name": TARGET.name, "mode": mode, "enabled": True}}}


def engine(backend, mode="auto"):
    result = cf.FocusEngine(backend)
    result.configure(preferences(mode))
    result.tick()
    return result


def test_default_off_and_whitelist_are_required():
    b = Backend()
    e = cf.FocusEngine(b)
    e.tick()
    b.front = TARGET
    e.tick()
    assert not b.requests
    e.configure({"enabled": True, "apps": {}})
    b.front = OTHER
    e.tick()
    b.front = TARGET
    e.tick()
    assert not b.requests


def test_one_attempt_per_activation_does_not_lock_user_focus():
    b = Backend()
    e = engine(b)
    b.front = TARGET
    e.tick()
    assert b.requests == ["input"]
    b.current = "body"
    for _ in range(5):
        e.tick()
    assert b.requests == ["input"]
    b.front = OTHER
    e.tick()
    b.front = TARGET
    e.tick()
    assert b.requests == ["input", "input"]


def test_ambiguous_automatic_detection_requests_manual_binding():
    b = Backend()
    b.scan_result = None
    e = engine(b)
    b.front = TARGET
    e.tick()
    assert e.states[TARGET.bundle] == "manual_needed"
    assert not b.requests


def test_search_editor_is_not_overridden():
    b = Backend()
    e = engine(b)
    b.front, b.current = TARGET, "search"
    e.tick()
    assert not b.requests and e.states[TARGET.bundle] == "editing"


def test_unknown_focus_or_modal_is_not_overridden():
    b = Backend()
    binding = cf.Binding(TARGET, "window", "input")
    b.front, b.current = TARGET, None
    assert cf.restore(b, binding, b.stamp, lambda: False) == "unavailable"
    b.current = "body"
    b.tree["window"]["AXModal"] = True
    assert cf.restore(b, binding, b.stamp, lambda: False) == "blocked"
    assert not b.requests


def test_disable_during_job_prevents_write():
    b = Backend()
    b.front = TARGET
    checks = iter([False, True])
    assert cf.restore(b, cf.Binding(TARGET, "window", "input"), b.stamp, lambda: next(checks)) == "changed"
    assert not b.requests


def test_user_input_during_discovery_prevents_write():
    b = Backend()
    e = engine(b)

    def discover(*args):
        b.stamp = (2, 2)
        return "input"

    b.discover = discover
    b.front = TARGET
    e.tick()
    assert not b.requests and e.states[TARGET.bundle] == "changed"


def test_manual_binding_requires_confirmation_and_does_not_focus_during_selection():
    b = Backend()
    e = engine(b, "manual")
    e.begin(TARGET.bundle)
    b.front, b.current = TARGET, "input"
    e.tick()
    assert e.draft is not None and not e.bindings and not b.requests
    e.confirm(TARGET.bundle)
    assert e.bindings[TARGET.bundle].element == "input"
    b.front, b.current = OTHER, "body"
    e.tick()
    b.front = TARGET
    e.tick()
    assert b.requests == ["input"]


def test_manual_capture_requests_accessibility_before_reading_hidden_composer():
    b = Backend()
    b.front, b.current = TARGET, None
    calls = []
    def prepare(app, cancelled):
        calls.append(app)
        b.current = "input"
    b.prepare = prepare
    e = cf.FocusEngine(b)
    e.configure(preferences(enabled=False))
    e.begin(TARGET.bundle)
    e.tick()
    assert calls == [TARGET] and e.draft is not None
    assert not b.requests  # Explicit selection only reads/captures; no focus write.


def test_accessibility_request_does_not_run_for_unselected_apps_or_disabled_service():
    b = Backend()
    calls = []
    b.prepare = lambda *args: calls.append(args)
    e = cf.FocusEngine(b)
    e.configure(preferences(enabled=False))
    b.front = TARGET
    e.tick()
    e.configure(preferences())
    b.front = OTHER
    e.tick()
    assert not calls


def test_automatic_discovery_also_prepares_accessibility():
    b = Backend()
    b.scan_result = None
    b.prepare = lambda *args: setattr(b, "scan_result", "input")
    e = engine(b)
    b.front = TARGET
    e.tick()
    assert b.requests == ["input"]


class AccessibilityPreparation(MacFocusBackend):
    def __init__(self):
        from types import SimpleNamespace
        self._enhanced_requests = {}
        self.front, self.value, self.error, self.requests = TARGET, False, 0, []
        self.ax = SimpleNamespace(
            kAXErrorAttributeUnsupported=-25205,
            AXUIElementCopyAttributeValue=lambda *args: (self.error, self.value),
            AXUIElementIsAttributeSettable=lambda *args: (0, True),
            AXUIElementSetAttributeValue=self.set_attribute)

    def frontmost(self): return self.front
    def root(self, app): return app.pid
    def set_attribute(self, root, attribute, value):
        assert attribute == "AXEnhancedUserInterface" and value is True
        self.requests.append(root)
        self.value = True
        return -25208  # Real GPT returns this even after exposing the tree.


def test_native_accessibility_negotiation_is_once_per_process_even_on_odd_return_code():
    b = AccessibilityPreparation()
    b.prepare(TARGET, lambda: False)
    b.prepare(TARGET, lambda: False)
    assert b.value and b.requests == [TARGET.pid]
    b.front = cf.App(TARGET.bundle, 99, TARGET.name)
    b.value = False
    b.prepare(b.front, lambda: False)
    assert b.requests == [TARGET.pid, 99]


def test_native_accessibility_does_not_write_if_ready_unsupported_or_foreground_changed():
    b = AccessibilityPreparation()
    b.value = True
    b.prepare(TARGET, lambda: False)
    assert not b.requests
    b = AccessibilityPreparation()
    b.error = -25205
    b.prepare(TARGET, lambda: False)
    assert not b.requests
    b = AccessibilityPreparation()
    b.front = OTHER
    b.prepare(TARGET, lambda: False)
    assert not b.requests
    b.front = TARGET
    b.prepare(TARGET, lambda: True)
    assert not b.requests
    b.ax.AXUIElementIsAttributeSettable = lambda *args: (setattr(b, "front", OTHER) or 0, True)
    b.prepare(TARGET, lambda: False)
    assert not b.requests


def test_manual_binding_is_invalidated_when_app_restarts():
    b = Backend()
    b.scan_result = None
    e = engine(b, "manual")
    e.bindings[TARGET.bundle] = cf.Binding(TARGET, "window", "input")
    b.front = cf.App(TARGET.bundle, 99, TARGET.name)
    e.tick()
    assert e.states[TARGET.bundle] == "needs_binding" and not b.requests


def test_manual_binding_is_invalidated_when_window_changes():
    b = Backend()
    b.scan_result = None
    e = engine(b, "manual")
    e.bindings[TARGET.bundle] = cf.Binding(TARGET, "old", "input")
    b.front = TARGET
    e.tick()
    assert e.states[TARGET.bundle] == "needs_binding" and not b.requests


def test_removing_app_revokes_live_binding():
    b = Backend()
    e = engine(b, "manual")
    e.bindings[TARGET.bundle] = cf.Binding(TARGET, "window", "input")
    e.configure({"enabled": True, "apps": {}})
    b.front = TARGET
    e.tick()
    assert not e.bindings and not b.requests


def test_manual_selection_expires_without_binding_automatically():
    b = Backend()
    now = [0]
    e = cf.FocusEngine(b, clock=lambda: now[0])
    e.configure(preferences())
    e.begin(TARGET.bundle)
    now[0] = 31
    b.front, b.current = TARGET, "input"
    e.tick()
    assert e.draft is None and not e.bindings


class Discovery(MacFocusBackend, Backend):
    def __init__(self): Backend.__init__(self)
    def read(self, element, name): return Backend.read(self, element, name)
    def settable(self, element): return True
    def visible(self, element, window): return True
    def children(self, element): return self.tree[element].get("AXChildren", [])


def test_automatic_detection_needs_chat_semantics_not_just_unique_textarea():
    b = Discovery()
    assert b.discover(TARGET, "window", lambda: False) is None
    b.tree["input"]["AXIdentifier"] = "prompt-textarea"
    assert b.discover(TARGET, "window", lambda: False) == "input"


def test_multiple_chat_candidates_are_rejected():
    b = Discovery()
    b.tree["input"]["AXPlaceholderValue"] = "输入消息"
    b.tree["other"] = dict(b.tree["input"])
    b.tree["window"]["AXChildren"].append("other")
    assert b.discover(TARGET, "window", lambda: False) is None


def test_incomplete_or_cancelled_scan_does_not_prove_uniqueness():
    b = Discovery()
    b.tree["input"]["AXIdentifier"] = "prompt-textarea"
    b.tree["window"]["AXChildren"].append("unreadable")
    assert b.discover(TARGET, "window", lambda: False) is None
    assert b.discover(TARGET, "window", lambda: True) is None


def test_auto_hints_reject_search_and_rename_fields():
    assert not cf.chat_metadata("prompt-textarea", "Search messages")
    assert not cf.chat_metadata("rename", "输入消息")
    assert not cf.chat_metadata(None, None)


def test_preferences_default_off_and_roundtrip(tmp_path):
    store = cf.Preferences(tmp_path)
    assert store.data == {"enabled": False, "apps": {}}
    store.save(preferences("manual"))
    assert cf.Preferences(tmp_path).data == preferences("manual")
    assert "AX" not in store.path.read_text()  # Native handles/content never persist.


def test_invalid_preferences_do_not_overwrite_user_file(tmp_path):
    path = tmp_path / "chat-focus.json"
    path.write_text("broken")
    store = cf.Preferences(tmp_path)
    assert not store.data["enabled"] and store.error
    with pytest.raises(ValueError): store.save(preferences())
    assert path.read_text() == "broken"


def test_save_failure_preserves_previous_settings(tmp_path, monkeypatch):
    store = cf.Preferences(tmp_path)
    before = deepcopy(store.data)
    monkeypatch.setattr(cf, "atomic_write", lambda *args: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError): store.save(preferences())
    assert store.data == before


def test_browser_cannot_be_whitelisted_wholesale():
    data = preferences()
    data["apps"]["com.google.Chrome"] = data["apps"].pop(TARGET.bundle)
    with pytest.raises(ValueError, match="浏览器"): cf.validate_preferences(data)


def test_newly_launched_app_waits_for_its_window_without_another_switch():
    b = Backend()
    e = engine(b)
    b.front, b.win = TARGET, None
    e.tick()
    assert e.states[TARGET.bundle] == "loading" and not b.requests
    b.win = "window"
    e.tick()
    assert b.requests == ["input"]


def test_first_selected_foreground_after_enable_is_not_missed():
    b = Backend()
    b.front = TARGET
    e = cf.FocusEngine(b)
    e.configure(preferences())
    e.tick()
    assert b.requests == ["input"]
    b.current = "body"
    e.tick()
    assert b.requests == ["input"]


def test_delayed_composer_can_be_detected_during_same_activation():
    b = Backend()
    e = engine(b)
    b.front, b.scan_result = TARGET, None
    e.tick()
    b.scan_result = "input"
    e.tick()
    assert b.requests == ["input"]


def test_user_typing_cancels_delayed_startup_focus():
    b = Backend()
    e = engine(b)
    b.front, b.win = TARGET, None
    e.tick()
    b.win, b.stamp = "window", (9, 2)
    e.tick()
    assert not b.requests and e.pending is None


def test_discovery_retries_are_bounded_and_never_poll_forever():
    b = Backend()
    e = engine(b)
    b.front, b.scan_result = TARGET, None
    for _ in range(4): e.tick()
    assert e.pending is None and not b.requests
    b.scan_result = "input"
    e.tick()
    assert not b.requests


def test_installed_app_can_be_selected_before_it_is_running(tmp_path):
    app = tmp_path / "Chat.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": TARGET.bundle, "CFBundleName": TARGET.name}))
    assert cf.app_from_path(app) == cf.App(TARGET.bundle, 0, TARGET.name)


def test_browser_url_handler_is_not_enabled_as_a_whole(tmp_path):
    app = tmp_path / "Browser.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.example.browser",
        "CFBundleURLTypes": [{"CFBundleURLSchemes": ["https"]}]}))
    with pytest.raises(ValueError, match="浏览器"):
        cf.app_from_path(app)


@pytest.mark.parametrize("route", ["app_switcher", "mapped_application"])
@pytest.mark.parametrize("enabled", [True, False])
def test_existing_remote_actions_share_foreground_focus_policy(qapp, tmp_path, monkeypatch, route, enabled):
    from remotehub.actions import ActionRunner
    from remotehub.model import Action
    b = Backend()
    e = cf.FocusEngine(b)
    e.configure(preferences(enabled=enabled))
    e.tick()
    runner = ActionRunner()
    # Simulate the existing system app-switch action without sending any keys.
    monkeypatch.setattr(runner.keys, "shortcut", lambda *args: setattr(b, "front", TARGET))
    monkeypatch.setattr(runner.windows, "toggle", lambda *args: setattr(b, "front", TARGET))
    app = tmp_path / "Chat.app"
    app.mkdir()
    action = Action("common", target="switch_app") if route == "app_switcher" else Action("toggle_app", target=str(app))
    runner.run(action)
    assert b.front == TARGET
    e.tick()
    assert b.requests == (["input"] if enabled else [])
