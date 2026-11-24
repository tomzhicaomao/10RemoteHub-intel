"""A virtual microphone installed while the app runs needs a new process on macOS."""
import pytest

from remotehub import relaunch
import remotehub.controller as controller_module
from remotehub.controller import AppController
from remotehub.ui import MainWindow
from remotehub.ui import window as window_module


def test_schedule_relaunch_waits_for_this_process_then_runs_the_launch_command(monkeypatch):
    calls = []
    monkeypatch.setattr(relaunch.QProcess, "startDetached", lambda *a: calls.append(a) or (True, 0))
    monkeypatch.setattr(relaunch.sys, "executable", "/Applications/10RemoteHub.app/Contents/MacOS/10RemoteHub")
    monkeypatch.setattr(relaunch.os, "getpid", lambda: 4321)
    assert relaunch.schedule_relaunch()
    program, args, _cwd = calls[0]
    assert program == "/bin/sh" and args[2:] == ["sh", "4321", "/usr/bin/open", "/Applications/10RemoteHub.app"]


def test_launch_command_reopens_the_bundle_or_the_same_interpreter_call():
    assert relaunch.launch_command("/Applications/10RemoteHub.app/Contents/MacOS/10RemoteHub", ["x"]) == \
        ["/usr/bin/open", "/Applications/10RemoteHub.app"]
    assert relaunch.launch_command("/repo/.venv/bin/python", ["/repo/.venv/bin/python", "-m", "remotehub"]) == \
        ["/repo/.venv/bin/python", "-m", "remotehub"]


@pytest.fixture
def new_driver(tmp_path, monkeypatch):
    """Controller alone: no window is listening, so a relaunch request cannot quit or reopen anything."""
    c = AppController(tmp_path, auto_mapping=False)
    monkeypatch.setattr(controller_module.sys, "platform", "darwin")
    # The driver appeared after start, but this process's device list never shows it.
    c.microphone_installed_at_start = False
    monkeypatch.setattr(c.virtual_mic, "installed", lambda: True)
    monkeypatch.setattr(c.audio, "outputs", lambda: [])
    c.refresh_outputs()
    yield c
    c.shutdown()


def test_mac_microphone_installed_while_running_offers_to_reopen_the_app(new_driver):
    c = new_driver
    assert c.microphone_needs_relaunch
    view = c.microphone_view()
    assert view.title == "已安装，重新打开 App 后生效" and "重新打开 App" in view.detail
    requested = []
    c.relaunch_requested.connect(lambda: requested.append(True))
    c.setup_virtual_mic()               # the voice page button: no second installer run
    assert requested == [True]


def test_mac_microphone_present_at_start_keeps_the_old_advice(new_driver):
    c = new_driver
    c.microphone_installed_at_start = True
    assert not c.microphone_needs_relaunch
    assert c.microphone_view().title == "已安装，尚未生效"


def test_relaunch_schedules_the_waiter_only_when_the_app_really_quits(qtbot, tmp_path, monkeypatch):
    w = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(w)
    scheduled, quits, spawned = [], [], []
    # Never start a real waiter from a test: it would reopen this test run after it exits.
    monkeypatch.setattr(relaunch.QProcess, "startDetached", lambda *a: spawned.append(a) or (True, 0))
    monkeypatch.setattr(relaunch, "schedule_relaunch", lambda: scheduled.append(True) or True)
    monkeypatch.setattr(window_module.QApplication, "quit", lambda: quits.append(True))
    monkeypatch.setattr(w, "shutdown", lambda: None)
    monkeypatch.setattr(w, "request_quit", lambda: False)   # e.g. unsaved keys, user cancelled
    w.relaunch()
    assert not w._relaunch_pending and scheduled == []
    monkeypatch.setattr(w, "request_quit", lambda: (w.quit_app(), True)[1])
    w.relaunch()
    assert scheduled == [True] and quits == [True]
    assert not w._relaunch_pending      # a later quit (e.g. closing the window) must not reopen again
    w.quit_app()
    assert scheduled == [True] and spawned == []
    w.controller.shutdown()
