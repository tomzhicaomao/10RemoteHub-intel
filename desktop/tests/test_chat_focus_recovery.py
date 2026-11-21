from copy import deepcopy

from remotehub import chat_focus as cf
from test_chat_focus import Backend, TARGET, OTHER, preferences
from test_chat_focus import Discovery
from remotehub import chat_focus_locator as locator_module


def learned_tree():
    b = Discovery()
    b.tree["composer"] = {"AXRole": "AXGroup", "AXIdentifier": "compose",
                          "AXParent": "window", "AXChildren": ["input", "send"]}
    b.tree["input"]["AXParent"] = "composer"
    b.tree["send"] = {"AXRole": "AXButton", "AXParent": "composer"}
    b.tree["window"]["AXChildren"] = ["body", "composer"]
    return b


def test_locator_reacquires_new_objects_without_text_or_indices():
    b = learned_tree()
    saved = locator_module.describe(b, "window", "input", "a" * 64)
    assert locator_module.valid(saved)
    b.tree["replacement"] = b.tree.pop("input")
    b.tree["composer"]["AXChildren"][0] = "replacement"
    # An unrelated sibling before the composer does not supply the target index.
    b.tree["window"]["AXChildren"].insert(0, "search")
    assert locator_module.resolve(b, "window", saved, "a" * 64) == "replacement"


def test_locator_rejects_duplicate_incomplete_changed_build_or_structure():
    original = learned_tree()
    saved = locator_module.describe(original, "window", "input", "a" * 64)
    assert saved
    assert locator_module.resolve(original, "window", saved, "c" * 64) is None
    assert locator_module.resolve(original, "window", saved, "a" * 64, lambda: True) is None
    for kind in ("duplicate", "incomplete", "changed", "hidden", "modal"):
        b = learned_tree()
        if kind == "duplicate":
            b.tree["copy"] = dict(b.tree["input"])
            b.tree["second"] = dict(b.tree["composer"], AXChildren=["copy", "send"])
            b.tree["copy"]["AXParent"] = "second"
            b.tree["window"]["AXChildren"].append("second")
        elif kind == "incomplete": b.tree["window"]["AXChildren"].append("missing")
        elif kind == "changed": b.tree["composer"]["AXIdentifier"] = "rename"
        elif kind == "hidden": b.visible = lambda *args: False
        else: b.tree["window"]["AXModal"] = True
        assert locator_module.resolve(b, "window", saved, "a" * 64) is None


def test_invalid_saved_locator_is_not_loaded(tmp_path):
    import json
    data = preferences("manual")
    data["apps"][TARGET.bundle]["locator"] = {"signature": "old path"}
    (tmp_path / "chat-focus.json").write_text(json.dumps(data))
    assert cf.Preferences(tmp_path).error


def test_every_activation_has_a_result_in_content_free_log(tmp_path):
    import json
    from remotehub.chat_focus_log import FocusLog
    sink = FocusLog(tmp_path)
    b = Backend()
    e = cf.FocusEngine(b, log=sink.write)
    e.configure(preferences())
    for _ in range(2):
        b.front, b.current = OTHER, "body"
        e.tick()
        b.front = TARGET
        e.tick()
    sink.write("worker_error", message="private chat text", value="secret", error="secret")
    sink.close()
    text = (tmp_path / "chat-focus.log").read_text()
    rows = [json.loads(line[line.index("{"):]) for line in text.splitlines()]
    results = [row for row in rows if row["event"] == "result"]
    assert [row["attempt"] for row in results] == [1, 2]
    assert all(row["status"] == "restored" for row in results)
    assert "secret" not in text and "private" not in text


def test_native_error_does_not_override_observed_focus():
    b = Backend()
    actual_focus = b.focus
    b.focus = lambda target: (actual_focus(target) and False)
    b.front = TARGET
    assert cf.restore(b, cf.Binding(TARGET, "window", "input"), b.stamp, lambda: False) == "restored"


def test_readback_exception_does_not_repeat_the_focus_write():
    import pytest
    b = Backend()
    original = b.read
    def read(node, name):
        if name == "AXFocused" and b.requests:
            raise RuntimeError("readback interrupted")
        return original(node, name)
    b.read = read
    e = cf.FocusEngine(b)
    e.configure(preferences())
    b.front = TARGET
    with pytest.raises(RuntimeError): e.tick()
    b.current = "body"
    e.tick()
    assert b.requests == ["input"]


def test_saved_target_does_not_override_other_editor():
    b = Backend()
    b.resolve = lambda *args: "input"
    data = preferences("manual")
    data["apps"][TARGET.bundle]["locator"] = {"version": 1, "build": "a" * 64, "signature": "b" * 64}
    e = cf.FocusEngine(b)
    e.configure(data)
    b.front, b.current = TARGET, "search"
    e.tick()
    assert e.states[TARGET.bundle] == "editing" and not b.requests


def test_disable_during_confirmation_never_requests_again():
    b = Backend()
    b.focus = lambda target: (b.requests.append(target) or True)
    e = cf.FocusEngine(b)
    e.configure(preferences())
    b.front = TARGET
    e.tick()
    e.configure(preferences(enabled=False))
    for _ in range(3): e.tick()
    assert e.confirmation is None and b.requests == ["input"]


def test_delayed_focus_is_confirmed_without_a_second_focus_request():
    b = Backend()
    now = [0]
    b.focus = lambda target: (b.requests.append(target) or True)
    e = cf.FocusEngine(b, clock=lambda: now[0])
    e.configure(preferences())
    e.tick()
    b.front = TARGET
    e.tick()
    assert e.states[TARGET.bundle] == "confirming"
    b.current = "input"
    b.tree["input"]["AXFocused"] = True
    now[0] = 0.4
    e.tick()
    assert e.states[TARGET.bundle] == "restored" and b.requests == ["input"]


def test_confirmation_timeout_is_unknown_not_a_second_attempt():
    b = Backend()
    now = [0]
    b.focus = lambda target: (b.requests.append(target) or False)
    e = cf.FocusEngine(b, clock=lambda: now[0])
    e.configure(preferences())
    e.tick()
    b.front = TARGET
    e.tick()
    now[0] = 2
    e.tick()
    e.tick()
    assert e.states[TARGET.bundle] == "unconfirmed" and b.requests == ["input"]


def test_confirmation_stops_on_user_action_or_switch():
    for switch in (True, False):
        b = Backend()
        b.focus = lambda target: (b.requests.append(target) or True)
        e = cf.FocusEngine(b)
        e.configure(preferences())
        e.tick()
        b.front = TARGET
        e.tick()
        if switch: b.front = OTHER
        else: b.stamp = (9, 9)
        e.tick()
        assert e.states[TARGET.bundle] == "changed" and b.requests == ["input"]


def test_saved_locator_is_used_after_engine_restart():
    b = Backend()
    b.describe = lambda *args: {"version": 1, "build": "a" * 64, "signature": "b" * 64}
    e = cf.FocusEngine(b)
    e.configure(preferences("manual"))
    e.begin(TARGET.bundle)
    b.front, b.current = TARGET, "input"
    e.tick()
    locator = e.confirm(TARGET.bundle)
    data = preferences("manual")
    data["apps"][TARGET.bundle]["locator"] = locator
    b.resolve = lambda app, window, saved, cancelled: "input" if saved == locator else None
    b.scan_result = None
    b.front, b.current = OTHER, "body"
    restarted = cf.FocusEngine(b)
    restarted.configure(deepcopy(data))
    restarted.tick()
    b.front = TARGET
    restarted.tick()
    assert b.requests == ["input"]


def test_saved_locator_mismatch_never_falls_back_to_another_chat_field():
    b = Backend()
    b.resolve = lambda *args: None
    data = preferences("manual")
    data["apps"][TARGET.bundle]["locator"] = {"version": 1, "build": "a" * 64, "signature": "b" * 64}
    e = cf.FocusEngine(b)
    e.configure(data)
    e.tick()
    b.front = TARGET
    for _ in range(3): e.tick()
    assert not b.requests and e.states[TARGET.bundle] == "rebind"
