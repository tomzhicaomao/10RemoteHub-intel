from threading import Event

from remotehub.chat_focus_service import ChatFocusService
from remotehub.ui.chat_focus import ChatFocusCard
from test_chat_focus import Backend, TARGET, OTHER


def test_default_off_does_not_start_native_work(qapp, tmp_path):
    service = ChatFocusService(tmp_path, backend=Backend())
    service._drain()
    assert service._executor is None and service._engine is None
    service.shutdown()


def test_safe_mode_refuses_external_capture(qapp, tmp_path):
    import pytest
    service = ChatFocusService(tmp_path, live=False, backend=Backend())
    service.add(TARGET)
    with pytest.raises(ValueError, match="预览模式"):
        service.begin_binding(TARGET.bundle)
    service.enable(True)
    service._drain()
    assert service._executor is None
    service.shutdown()


def test_manual_confirm_and_return_flow_without_real_app(qtbot, tmp_path):
    backend = Backend()
    service = ChatFocusService(tmp_path, backend=backend)
    service.add(TARGET)
    service.begin_binding(TARGET.bundle)
    qtbot.waitUntil(lambda: service.state["selecting"] == TARGET.bundle)
    backend.front, backend.current = TARGET, "input"
    qtbot.waitUntil(lambda: service.state["candidate_ready"])
    assert not backend.requests
    backend.front, backend.current = OTHER, "body"
    service.confirm_binding()
    qtbot.waitUntil(lambda: not service.state["selecting"])
    assert service.data["apps"][TARGET.bundle]["mode"] == "manual"
    service.enable(True)
    qtbot.waitUntil(lambda: service._engine.preferences["enabled"] and service._engine.seeded)
    backend.front = TARGET
    qtbot.waitUntil(lambda: len(backend.requests) == 1)
    service.shutdown()


def test_pause_cancels_inflight_discovery_before_write(qtbot, tmp_path):
    backend = Backend()
    entered, release = Event(), Event()

    def discover(*args):
        entered.set()
        release.wait(2)
        return "input"

    backend.discover = discover
    service = ChatFocusService(tmp_path, backend=backend)
    service.add(TARGET)
    service.enable(True)
    qtbot.waitUntil(lambda: service._engine is not None and service._engine.seeded)
    backend.front = TARGET
    qtbot.waitUntil(entered.is_set)
    service.enable(False)
    release.set()
    qtbot.waitUntil(lambda: not service._busy)
    assert not backend.requests and not service.data["enabled"]
    service.shutdown()


def test_ui_selects_multiple_apps_and_refresh_keeps_selections(qtbot, tmp_path):
    backend = Backend()
    backend.applications = lambda: [TARGET, OTHER]
    service = ChatFocusService(tmp_path, backend=backend)
    card = ChatFocusCard(service)
    qtbot.addWidget(card)
    first = card.checkboxes[TARGET.bundle]
    first.click()
    card.checkboxes[OTHER.bundle].click()
    assert set(service.data["apps"]) == {TARGET.bundle, OTHER.bundle}
    card.refresh_apps_button.click()
    assert card.checkboxes[TARGET.bundle] is first
    assert all(box.isChecked() for box in card.checkboxes.values())
    assert "2" in card.selection_count.text()
    assert not service.data["enabled"]
    assert card.rows[TARGET.bundle][0].text().startswith("已就绪")
    assert card.enabled.text() == "自动聚焦聊天输入框"
    from remotehub.ui.widgets import ScrollSafeComboBox
    assert not card.findChildren(ScrollSafeComboBox)
    card.enabled.click()
    assert service.data["enabled"]
    card.enabled.click()
    assert not service.data["enabled"]
    assert "暂停" in card.status.text()
    card.checkboxes[TARGET.bundle].click()
    assert TARGET.bundle not in service.data["apps"]
    assert service.data["apps"][OTHER.bundle]["enabled"]
    from remotehub.chat_focus import Preferences
    assert set(Preferences(tmp_path).data["apps"]) == {OTHER.bundle}
    backend.applications = lambda: []
    card.refresh_apps_button.click()
    assert card.checkboxes[OTHER.bundle].isChecked()  # Closed apps remain selected.
    service.shutdown()


def test_failed_selection_save_reverts_checkbox_without_clearing_other_apps(qtbot, tmp_path, monkeypatch):
    backend = Backend()
    backend.applications = lambda: [TARGET, OTHER]
    service = ChatFocusService(tmp_path, backend=backend)
    service.add(TARGET)
    card = ChatFocusCard(service)
    qtbot.addWidget(card)
    monkeypatch.setattr(service.preferences, "save", lambda data: (_ for _ in ()).throw(OSError("保存失败")))
    card.checkboxes[OTHER.bundle].click()
    assert not card.checkboxes[OTHER.bundle].isChecked()
    assert card.checkboxes[TARGET.bundle].isChecked()
    assert "保存失败" in card.status.text()
    service.shutdown()


def test_confirmation_controls_only_enable_for_a_draft(qtbot, tmp_path):
    service = ChatFocusService(tmp_path, backend=Backend())
    card = ChatFocusCard(service)
    qtbot.addWidget(card)
    assert not card.confirm.isEnabled()
    service.state.update(selecting=TARGET.bundle, seconds_left=20, candidate_ready=False)
    service.updated.emit()
    assert not card.confirm.isEnabled() and "20 秒" in card.binding_hint.text()
    service.state["candidate_ready"] = True
    service.updated.emit()
    assert card.confirm.isEnabled() and "确认" in card.binding_hint.text()
    service.shutdown()


def test_confirm_persists_locator_and_new_service_recovers(qtbot, tmp_path):
    locator = {"version": 1, "build": "a" * 64, "signature": "b" * 64}
    backend = Backend()
    backend.describe = lambda *args: locator
    service = ChatFocusService(tmp_path, backend=backend)
    service.add(TARGET)
    service.begin_binding(TARGET.bundle)
    qtbot.waitUntil(lambda: service.state["selecting"] == TARGET.bundle)
    backend.front, backend.current = TARGET, "input"
    qtbot.waitUntil(lambda: service.state["candidate_ready"])
    backend.front, backend.current = OTHER, "body"
    service.confirm_binding()
    qtbot.waitUntil(lambda: "locator" in service.data["apps"][TARGET.bundle])
    assert "已保存" in service.message
    service.shutdown()
    backend.scan_result = None
    backend.resolve = lambda app, window, saved, cancelled: "input" if saved == locator else None
    restarted = ChatFocusService(tmp_path, backend=backend)
    card = ChatFocusCard(restarted)
    qtbot.addWidget(card)
    assert card.rows[TARGET.bundle][0].text().startswith("已就绪")
    restarted.enable(True)
    qtbot.waitUntil(lambda: restarted._engine is not None and restarted._engine.seeded)
    backend.front = TARGET
    qtbot.waitUntil(lambda: backend.requests == ["input"])
    restarted.shutdown()


def test_failed_locator_save_is_not_reported_as_persisted(qapp, tmp_path, monkeypatch):
    service = ChatFocusService(tmp_path, backend=Backend())
    service.add(TARGET)
    monkeypatch.setattr(service.preferences, "save", lambda data: (_ for _ in ()).throw(OSError("full")))
    state = dict(service.state, learned={"bundle": TARGET.bundle, "locator":
                 {"version": 1, "build": "a" * 64, "signature": "b" * 64}})
    service._finished((service._cancel, (state, "")))
    assert "保存失败" in service.message and "locator" not in service.data["apps"][TARGET.bundle]
    service.shutdown()


def test_event_driven_backend_stops_idle_timer_and_wakes_on_activation(qtbot, tmp_path):
    backend = Backend()
    backend.notifies_activation = True
    backend.front = OTHER
    service = ChatFocusService(tmp_path, backend=backend)
    assert not service.timer.isActive()
    service.add(TARGET)
    service.enable(True)
    qtbot.waitUntil(lambda: service._engine is not None and service._engine.seeded and not service._busy)
    assert not service.timer.isActive()

    updates = []
    service.updated.connect(lambda: updates.append(True))
    service._drain()
    qtbot.waitUntil(lambda: not service._busy)
    assert not updates  # Unchanged snapshot must not emit redundant UI refreshes.

    backend.front, backend.current = TARGET, "body"
    service._activated.emit()
    qtbot.waitUntil(lambda: backend.requests == ["input"] and not service._busy)
    assert updates
    assert not service.timer.isActive()
    service.shutdown()


def test_saved_app_keeps_rebind_button_visible_and_rebinds_cleanly(qtbot, tmp_path):
    first_locator = {"version": 1, "build": "a" * 64, "signature": "1" * 64}
    second_locator = {"version": 1, "build": "a" * 64, "signature": "2" * 64}
    current_locator = [first_locator]
    backend = Backend()
    backend.applications = lambda: [TARGET]
    backend.describe = lambda *args: current_locator[0]
    service = ChatFocusService(tmp_path, backend=backend)
    service.add(TARGET)
    card = ChatFocusCard(service)
    qtbot.addWidget(card)
    card.show()

    _, bind = card.rows[TARGET.bundle]
    assert not bind.isHidden() and bind.text() == "指定聊天输入框"
    bind.click()
    qtbot.waitUntil(lambda: service.state["selecting"] == TARGET.bundle)
    backend.front, backend.current = TARGET, "input"
    qtbot.waitUntil(lambda: service.state["candidate_ready"])
    backend.front, backend.current = OTHER, "body"
    card.confirm.click()
    qtbot.waitUntil(lambda: service.data["apps"][TARGET.bundle].get("locator") == first_locator)

    assert not bind.isHidden() and bind.isEnabled()
    assert bind.text() == "重新指定"

    current_locator[0] = second_locator
    bind.click()
    qtbot.waitUntil(lambda: service.state["selecting"] == TARGET.bundle)
    backend.front, backend.current = TARGET, "input"
    qtbot.waitUntil(lambda: service.state["candidate_ready"])
    backend.front, backend.current = OTHER, "body"
    card.confirm.click()
    qtbot.waitUntil(lambda: service.data["apps"][TARGET.bundle].get("locator") == second_locator)
    service.shutdown()


def test_mac_workspace_observer_tracks_notification_and_unregisters():
    import sys
    import pytest
    if sys.platform != "darwin":
        pytest.skip("macOS NSWorkspace observer test")
    from types import SimpleNamespace
    from Foundation import NSNotificationCenter
    from AppKit import NSWorkspaceApplicationKey, NSWorkspaceDidActivateApplicationNotification
    from remotehub.mac_chat_focus import MacWorkspaceObserver

    class FakeRunningApp:
        def __init__(self, bundle, pid, name):
            self._bundle, self._pid, self._name = bundle, pid, name
        def bundleIdentifier(self): return self._bundle
        def processIdentifier(self): return self._pid
        def localizedName(self): return self._name

    initial = FakeRunningApp("org.example.first", 101, "First")
    switched = FakeRunningApp("org.example.second", 202, "Second")
    after_close = FakeRunningApp("org.example.third", 303, "Third")
    workspace = SimpleNamespace(frontmostApplication=lambda: initial)
    center = NSNotificationCenter.alloc().init()
    events = []
    observer = MacWorkspaceObserver(lambda: events.append(True), center=center, workspace=workspace)
    assert observer.current_app.bundle == "org.example.first"

    center.postNotificationName_object_userInfo_(
        NSWorkspaceDidActivateApplicationNotification, None, {NSWorkspaceApplicationKey: switched}
    )
    assert events == [True]
    assert observer.current_app.bundle == "org.example.second" and observer.current_app.pid == 202

    observer.close()
    center.postNotificationName_object_userInfo_(
        NSWorkspaceDidActivateApplicationNotification, None, {NSWorkspaceApplicationKey: after_close}
    )
    assert events == [True]
    assert observer.current_app.bundle == "org.example.second"


def test_row_status_only_surfaces_states_that_need_user_action():
    from remotehub.ui.chat_focus import row_status
    transient = ["restored", "already", "editing", "changed", "blocked", "confirming", "unconfirmed", "waiting", "loading", "saved"]
    texts = {row_status(code)[0] for code in transient}
    assert len(texts) == 1 and all(not row_status(code)[1] for code in transient)
    assert "最近一次" in row_status("changed")[2]
    for code in ("rebind", "needs_binding", "manual_needed", "unavailable"):
        assert row_status(code)[1]
