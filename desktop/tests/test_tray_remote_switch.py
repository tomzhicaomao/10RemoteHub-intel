"""Tests for tray remote switcher submenu (「切换遥控器」)."""
import pytest
from PySide6.QtWidgets import QMessageBox

from remotehub.model import Action, Button, Profile
from remotehub.photo_template import television_profile
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import remote_for_device
from remotehub.ui import MainWindow


def _learned_remote(name, device, trigger, profile_name):
    profile = Profile(
        name=profile_name,
        vendor=device.vendor,
        product=device.product,
        image=television_profile().image,
        buttons=[Button(id="btn-ok", name="确定", trigger=trigger, rect=[0.1, 0.1, 0.2, 0.1])],
    )
    return remote_for_device(name, device, profile)


@pytest.mark.fresh_config
def test_tray_menu_shows_and_switches_remotes(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller

    assert window.tray_remote_menu.title() == "切换遥控器"
    empty_actions = window.tray_remote_menu.actions()
    assert len(empty_actions) == 1
    assert empty_actions[0].text() == "未添加遥控器"
    assert not empty_actions[0].isEnabled()

    usb_dev = ProbeDevice(("usb", 1), "USB 遥控器", "USB", 0x1234, 0x5678, "a" * 64, (1,))
    bt_dev = ProbeDevice(("hid", 2), "蓝牙遥控器", "蓝牙", 0x620A, 0x0407, "", (2,), vendor_id_source=1)
    desk = _learned_remote("书桌遥控器", usb_dev, "hid:7:82", "书桌方案")
    sofa = _learned_remote("客厅遥控器", bt_dev, "hid:7:40", "客厅方案")
    assert c.add_remote(desk, usb_dev)
    assert c.add_remote(sofa, bt_dev)

    actions = window.tray_remote_menu.actions()
    assert [a.text() for a in actions] == ["书桌遥控器", "客厅遥控器"]
    assert [a.isChecked() for a in actions] == [False, True]

    actions[0].trigger()
    assert c.remote.id == desk.id
    refreshed = window.tray_remote_menu.actions()
    assert [a.isChecked() for a in refreshed] == [True, False]
    assert [a.text() for a in window.tray_profile_menu.actions()] == ["书桌方案"]
    assert window.sidebar.remote_box.currentData() == desk.id

    # Visible window with unsaved text refuses tray remote switch when user cancels.
    window.show()
    window.show_mapping()
    window.inspector.choose_kind("text")
    window.inspector.text_input.setPlainText("unsaved draft")
    assert window.inspector.text_dirty
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Cancel)
    window.tray_remote_menu.actions()[1].trigger()
    assert c.remote.id == desk.id
    assert [a.isChecked() for a in window.tray_remote_menu.actions()] == [True, False]

    # When remote input is paused, tray remote switching actions are disabled.
    assert c.pause_remote_input()
    assert all(not a.isEnabled() for a in window.tray_remote_menu.actions())
    c.resume_remote_input()
    window.shutdown()
