"""After adding a remote the app lists what is still needed: required first, microphone optional."""
import pytest
from PySide6.QtWidgets import QPushButton

from remotehub import mac_device_helper, permissions
from remotehub.controller import AppController
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import remote_for_device
from remotehub.ui import setup_checklist as checklist_module
from remotehub.ui.window import MainWindow
from test_remotes import learned_profile


def _mac(monkeypatch, *, granted=False, installed=False):
    monkeypatch.setattr(checklist_module.sys, "platform", "darwin")
    state = "已授权" if granted else "未授权"
    monkeypatch.setattr(permissions, "permission_status", lambda: {"accessibility": state, "input": state})
    monkeypatch.setattr(mac_device_helper, "installation_status",
                        lambda: (0, "已安装") if installed else (1, "尚未安装"))


def _mic(monkeypatch, supported):
    monkeypatch.setattr(AppController, "supports_remote_mic", property(lambda self: supported))


def _window(qtbot, tmp_path):
    w = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(w)
    return w


def _add_usb_remote(w):
    device = ProbeDevice(("usb", 3), "2.4G", "USB", 0x1234, 0x5678)
    assert w.controller.add_remote(remote_for_device("书桌", device, learned_profile("书桌")))


@pytest.mark.fresh_config
def test_adding_a_remote_opens_the_checklist_with_required_and_optional_items(qtbot, tmp_path, monkeypatch):
    _mac(monkeypatch)
    _mic(monkeypatch, True)
    w = _window(qtbot, tmp_path)
    c = w.controller
    monkeypatch.setattr(c.virtual_mic, "installed", lambda: False)   # whatever this machine has
    monkeypatch.setattr(c.audio, "outputs", lambda: [])
    c.refresh_outputs()
    w._adding_new = True
    _add_usb_remote(w)
    w._enrollment_closed(1)
    qtbot.waitUntil(lambda: w.setup_dialog is not None)
    items = w.setup_dialog.checklist.items
    assert [(i.key, i.required) for i in items] == [
        ("input", True), ("accessibility", True), ("microphone", False)]
    assert "还有 2 项必须完成" in w.setup_dialog.summary.text()
    texts = [b.text() for b in w.setup_dialog.checklist.findChildren(QPushButton)]
    assert texts.count("去授权") == 2 and "安装" in texts
    w.shutdown()


@pytest.mark.fresh_config
def test_nothing_left_means_no_dialog_and_learning_more_keys_never_asks(qtbot, tmp_path, monkeypatch):
    _mac(monkeypatch, granted=True)
    _mic(monkeypatch, False)
    w = _window(qtbot, tmp_path)
    _add_usb_remote(w)
    assert w.open_setup() is None                       # every item is done
    _mac(monkeypatch, granted=False)
    w._adding_new = False                               # "学习更多按键" edits an existing remote
    w._enrollment_closed(1)
    qtbot.wait(20)
    assert w.setup_dialog is None
    w.shutdown()


def test_receiver_needs_the_reader_component_and_the_sidebar_leads_there(qtbot, tmp_path, monkeypatch):
    _mac(monkeypatch)
    w = _window(qtbot, tmp_path)                        # upgraded 0.2.0 receiver remote
    assert w.controller.uses_receiver
    w.sidebar.refresh_device()
    assert not w.sidebar.setup_button.isHidden()
    assert w.sidebar.setup_button.text() == "完成设置（还差 2 项）"
    w.sidebar.setup_button.click()
    assert w.setup_dialog is not None
    assert [i.key for i in w.setup_dialog.checklist.items if i.required] == ["component", "accessibility"]
    w.setup_dialog.reject()
    _mac(monkeypatch, granted=True, installed=True)
    w.sidebar.refresh_device()
    assert w.sidebar.setup_button.isHidden()
    w.shutdown()


def test_microphone_item_follows_the_install_state(qtbot, tmp_path, monkeypatch):
    _mac(monkeypatch, granted=True, installed=True)
    _mic(monkeypatch, True)
    w = _window(qtbot, tmp_path)
    c = w.controller
    c.voice.output_id = b""

    def mic():
        return next(i for i in checklist_module.setup_items(c, lambda _p: None) if i.key == "microphone")

    c.microphone_installed = False
    assert mic().action == "安装" and not mic().required
    c.microphone_installed, c.microphone_installed_at_start = True, False
    assert mic().action == "重新打开 App"
    c.voice.output_id = b"ready"
    assert mic().done and mic().action == ""
    w.shutdown()


def test_items_that_point_elsewhere_close_the_dialog_and_open_that_page(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(checklist_module.sys, "platform", "win32")
    _mic(monkeypatch, True)
    w = _window(qtbot, tmp_path)
    w.controller.voice.output_id = b""
    dialog = w.open_setup(force=True)
    next(b for b in dialog.checklist.findChildren(QPushButton) if b.text() == "前往语音设置").click()
    assert w.setup_dialog is None
    assert w.stack.currentIndex() == w.pages.index(w.voice_page)
    w.shutdown()


def test_windows_checklist_and_general_takeover_run_directly_without_looping_to_add_dialog(qtbot, tmp_path, monkeypatch):
    """Clicking takeover in Windows setup checklist runs WinUSB bind in place (no ADD loop)."""
    from remotehub import win_usb_driver as drv
    from remotehub.ui import general as general_module

    monkeypatch.setattr(checklist_module.sys, "platform", "win32")
    monkeypatch.setattr(general_module.sys, "platform", "win32")
    _mic(monkeypatch, False)

    state = {"bound": False}

    def fake_list():
        svc = "WinUSB" if state["bound"] else "HidUsb"
        return [], [
            drv.ManagedUSBDevice(r"USB\VID_0416&PID_0602&MI_03\7&1&0&0003", 0x0416, 0x0602, "receiver_0416", svc, "Voice", interface=3),
            drv.ManagedUSBDevice(r"USB\VID_0416&PID_0602&MI_04\7&1&0&0004", 0x0416, 0x0602, "receiver_0416", svc, "Keys", interface=4),
        ]

    monkeypatch.setattr(drv, "list_managed_devices", fake_list)
    launched = []
    monkeypatch.setattr(drv, "launch_elevated_action",
                        lambda action, ids, **kw: launched.append((action, list(ids), kw)) or (42, tmp_path / "res.json"))
    poll_done = {"done": False}
    monkeypatch.setattr(drv, "poll_elevated_process", lambda _h: (poll_done["done"], 0))
    monkeypatch.setattr(drv, "read_action_result", lambda _p, _c=0: {"ok": True, "message": "已由 App 接管这只接收器。"})

    w = _window(qtbot, tmp_path)
    # 1. GeneralPage shows direct takeover button for current unbound 0416:0602 receiver.
    w.general.refresh_takeovers()
    assert [b.text() for b in w.general.takeover_buttons] == ["让 App 接管这个接收器"]

    # 2. SetupDialog checklist executes takeover in place without ever navigating to ADD.
    dialog = w.open_setup(force=True)
    navigated = []
    dialog.navigate.connect(navigated.append)
    btn = next(b for b in dialog.checklist.findChildren(QPushButton) if b.text() == "让 App 接管这个接收器")
    btn.click()
    assert navigated == []
    assert w.add_dialog is None
    assert launched == [(
        "bind",
        [r"USB\VID_0416&PID_0602&MI_03\7&1&0&0003", r"USB\VID_0416&PID_0602&MI_04\7&1&0&0004"],
        {"confirm": False},
    )]
    assert dialog.checklist.pills["takeover"].text() == "正在接管…"

    # 3. Once elevated helper completes, checklist updates to 已接管 and GeneralPage switches to 恢复系统驱动.
    state["bound"] = True
    poll_done["done"] = True
    dialog.checklist._poll_windows_takeover()
    assert dialog.checklist.pills["takeover"].text() == "已接管"
    assert not any(b.text() == "让 App 接管这个接收器" for b in dialog.checklist.findChildren(QPushButton))
    w.general.refresh_takeovers()
    assert [b.text() for b in w.general.takeover_buttons] == ["恢复系统驱动"]
    w.shutdown()
