"""First-run guide: add a remote, prepare what that remote needs, then verify a key."""

import pytest
from remotehub import mac_device_helper, permissions
from remotehub.ui import onboarding as onboarding_module
from remotehub.ui import setup_checklist as checklist_module
from remotehub.ui.window import MainWindow


def window(qtbot, tmp_path, monkeypatch, installed=False):
    monkeypatch.setattr(mac_device_helper, 'installation_status',
                        lambda: (0 if installed else 1, '已安装' if installed else '尚未安装'))
    w = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(w)
    return w


def test_first_run_marks_profile_and_opens_mapping(qtbot, tmp_path, monkeypatch):
    w = window(qtbot, tmp_path, monkeypatch)
    c = w.controller
    assert c.onboarding_needed()
    dialog = onboarding_module.OnboardingDialog(c)
    qtbot.addWidget(dialog)
    assert dialog.index == 0
    dialog.next_page()
    assert dialog.index == 1
    dialog.next_page()
    assert dialog.index == 2
    dialog.next_page()
    assert dialog.index == 3
    opened = []
    dialog.open_mapping.connect(lambda: opened.append(True))
    dialog.next_page()
    assert opened == [True]
    assert not c.onboarding_needed()
    assert c.settings.get('onboarded') == '1'
    w.shutdown()


def _mac(monkeypatch, granted=False):
    monkeypatch.setattr(checklist_module.sys, "platform", "darwin")
    state = "已授权" if granted else "未授权"
    monkeypatch.setattr(permissions, "permission_status", lambda: {"accessibility": state, "input": state})


def _item_button(dialog, text):
    from PySide6.QtWidgets import QPushButton
    return next(b for b in dialog.checklist.findChildren(QPushButton) if b.text() == text)


def test_install_button_opens_installer_and_refreshes(qtbot, tmp_path, monkeypatch):
    w = window(qtbot, tmp_path, monkeypatch)
    _mac(monkeypatch)
    calls = []
    monkeypatch.setattr(w.controller, 'install_collector', lambda remove=False: calls.append(remove))
    dialog = onboarding_module.OnboardingDialog(w.controller)
    qtbot.addWidget(dialog)
    dialog.show_page(2)
    # The bundled receiver needs the reader component first, then accessibility.
    assert [i.key for i in dialog.checklist.items if i.required] == ["component", "accessibility"]
    assert dialog.checklist.pills["component"].text() == "未安装"
    _item_button(dialog, "安装").click()
    assert calls == [False]
    w.shutdown()


def test_component_status_is_read_only(qtbot, tmp_path, monkeypatch):
    w = window(qtbot, tmp_path, monkeypatch, installed=True)
    _mac(monkeypatch, granted=True)
    dialog = onboarding_module.OnboardingDialog(w.controller)
    qtbot.addWidget(dialog)
    dialog.refresh()
    assert dialog.checklist.pills["component"].text() == '已安装'
    assert all(i.done for i in dialog.checklist.items if i.required)
    w.shutdown()


def test_show_onboarding_once_respects_the_saved_flag(qtbot, tmp_path, monkeypatch):
    from types import SimpleNamespace
    w = window(qtbot, tmp_path, monkeypatch)
    created = []

    class Fake:
        def __init__(self, controller, parent=None):
            created.append(controller)
            self.open_mapping = SimpleNamespace(connect=lambda slot: None)
            self.open_voice = SimpleNamespace(connect=lambda slot: None)
            self.open_settings = SimpleNamespace(connect=lambda slot: None)
            self.add_remote = SimpleNamespace(connect=lambda slot: None)

        def exec(self):
            pass

    monkeypatch.setattr(onboarding_module, 'OnboardingDialog', Fake)
    w.show_onboarding_once()
    assert len(created) == 1
    w.controller.finish_onboarding()
    w.show_onboarding_once()
    assert len(created) == 1
    w.shutdown()


def test_windows_prepare_page_names_both_components_and_links_out(qtbot, tmp_path, monkeypatch):
    """Windows: the one-click takeover is optional, the microphone links to the voice page."""
    from PySide6.QtWidgets import QLabel
    from remotehub import windows_driver

    w = window(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(checklist_module.sys, "platform", "win32")
    monkeypatch.setattr(windows_driver, "binding_detail", lambda: {"keyboard": "winusb", "voice": "other"})
    w.controller.voice.output_id = b""
    dialog = onboarding_module.OnboardingDialog(w.controller)
    qtbot.addWidget(dialog)
    dialog.show_page(2)

    text = " ".join(label.text() for label in dialog.checklist.findChildren(QLabel))
    # Ordinary users take the receiver over in one click; no driver tools or interface numbers.
    assert "让 App 接管这个接收器" in text
    assert "Zadig" not in text and "Interface" not in text
    assert not any(i.required for i in dialog.checklist.items)
    assert dialog.checklist.pills["takeover"].text() == "已接管"
    if w.controller.supports_remote_mic:
        _item_button(dialog, "前往语音设置")
    w.shutdown()


def test_real_number_key_completes_first_run_verification(qtbot, tmp_path, monkeypatch):
    from remotehub.photo_template import KEYMAP

    w = window(qtbot, tmp_path, monkeypatch)
    dialog = onboarding_module.OnboardingDialog(w.controller)
    qtbot.addWidget(dialog)
    dialog.show_page(3)
    trigger = KEYMAP['keys']['tv-number-1']['trigger']
    w.controller.on_key(trigger, True)
    assert dialog.verified
    assert dialog.verify_pill.text() == '按键已验证'
    w.controller.on_key(trigger, False)
    w.shutdown()


@pytest.mark.fresh_config
def test_add_step_opens_add_dialog_and_prepare_follows_the_remote(qtbot, tmp_path, monkeypatch):
    from remotehub.probe_device import ProbeDevice
    from remotehub.remote_library import remote_for_device
    from test_remotes import learned_profile

    w = window(qtbot, tmp_path, monkeypatch)
    c = w.controller
    dialog = onboarding_module.OnboardingDialog(c)
    qtbot.addWidget(dialog)
    requested = []
    dialog.add_remote.connect(lambda: requested.append(True))
    dialog.show_page(1)
    assert dialog.remote_pill.text() == "未添加"
    from PySide6.QtWidgets import QPushButton
    next(b for b in dialog.findChildren(QPushButton) if b.text() == "添加遥控器").click()
    assert requested == [True]
    _mac(monkeypatch)
    device = ProbeDevice(("usb", 3), "2.4G", "USB", 0x1234, 0x5678)
    assert c.add_remote(remote_for_device("书桌", device, learned_profile("书桌")))
    assert dialog.remote_pill.text() == "已添加"
    # Adding a remote moves straight on to what it still needs.
    qtbot.waitUntil(lambda: dialog.index == 2)
    keys = [i.key for i in dialog.checklist.items]
    assert "component" not in keys and keys[:2] == ["input", "accessibility"]
    w.shutdown()
