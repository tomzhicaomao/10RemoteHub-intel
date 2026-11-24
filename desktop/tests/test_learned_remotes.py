from dataclasses import asdict, replace
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from remotehub import models
from remotehub.actions import launch_command
from remotehub.launch import LaunchRequest
from remotehub.learned_remotes import LearnedRemote, LearnedRemoteStore
from remotehub.mac_hid_probe import ProbeDevice
from remotehub.model import Action, Button, Profile
from remotehub.nine_key import CONTROL_ROLES, KEYPAD_LABELS, KEYPAD_ROLES
from remotehub.photo_template import KEYMAP, television_profile
from remotehub.remote_library import remote_for_device
from remotehub.ui import MainWindow
from remotehub.ui.mapping import MappingPage


class Reader:
    def __init__(self, *devices):
        self.devices = list(devices)
        self.expected = self.on_key = self.on_removed = None

    def enumerate(self):
        self.stop()
        return self.devices

    def start(self, identity, on_key, on_removed, *, expected):
        assert identity == expected.id and expected in self.devices
        self.expected, self.on_key, self.on_removed = expected, on_key, on_removed
        return 1, 1

    def edge(self, device, trigger, down, *, channel=(10, 0)):
        if device == self.expected and self.on_key is not None:
            self.on_key(channel, trigger, down)

    def tap(self, device, trigger):
        self.edge(device, trigger, True)
        self.edge(device, trigger, False)

    def stop(self):
        self.expected = self.on_key = self.on_removed = None

    close = stop


class Keys:
    def __init__(self):
        self.edges = []

    def press(self, keys):
        self.edges.append((True, keys))

    def release(self):
        self.edges.append((False,))


USB = ProbeDevice(("usb", 8, 0x0416, 0x0602), "0416:0602", "USB", 0x0416, 0x0602, "a" * 64, (10,))
BT_KEYBOARD = ProbeDevice(("hid", 11), "蓝牙键盘 620A:0407", "蓝牙", 0x620A, 0x0407, connection=(11,))
KEYBOARD = ProbeDevice(("hid", 12), "外接键盘", "USB", 0x0101, 0x0102, connection=(12,))
APP, VOICE = "hid:7:82", "hid:12:207"


def mapping(device, app):
    return LearnedRemote(Profile(
        name=device.name, vendor=device.vendor, product=device.product, image=television_profile().image,
        buttons=[Button(trigger=APP, action=Action("app", target=str(app))), Button(trigger=VOICE, action=Action("voice"))],
    ), device.transport, device.unit_key, ["ctrl", "space"])


def record_launch(controller, monkeypatch):
    commands = []
    run = controller.runner.run

    def action_runner(action, down):
        if action.kind == "app":
            if down:
                return LaunchRequest(launch_command(action, platform="darwin"))
            return None
        return run(action, down)

    monkeypatch.setattr(controller.runner, "run", action_runner)
    monkeypatch.setattr(controller.launcher, "start", lambda request: commands.append(request.command))
    return commands


def open_window(qtbot, tmp_path, reader, *remotes):
    """Main window with shared-HID remotes; the first one is active and connected."""
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.hid_reader = reader
    for remote, device in reversed(remotes):
        assert c.add_remote(remote, device)
    return window, c


def test_roundtrip_multiple_schemes_and_failed_save_keeps_previous_file(tmp_path):
    app = tmp_path / "Example.app"
    usb = mapping(USB, app)
    bt_keyboard = mapping(BT_KEYBOARD, app)
    store = LearnedRemoteStore(tmp_path / "learned-remotes.json")
    assert store.load() == ([], "")
    store.save([usb, bt_keyboard], bt_keyboard.profile.id)
    assert store.load() == ([usb, bt_keyboard], bt_keyboard.profile.id)
    old = store.path.read_bytes()
    usb.profile.buttons[0].trigger = "unsupported trigger"
    with pytest.raises(ValueError, match="键码"):
        store.save([usb, bt_keyboard], usb.profile.id)
    assert store.path.read_bytes() == old


@pytest.mark.fresh_config
def test_enrollment_diagnosis_distinguishes_unknown_device_and_missing_release(qtbot, tmp_path, monkeypatch):
    reader = Reader(USB)
    window, _ = open_window(qtbot, tmp_path, reader)
    window.show()
    monkeypatch.setattr(window, "safe", False)
    setup = window.add_remote()
    setup.not_listed.click()
    setup.system_connection.setChecked(True)
    setup.copy_check.click()
    info = json.loads(QApplication.clipboard().text())
    assert info["step"] == "not_visible"
    assert info["observations"]["system_connection_confirmed_by_user"]
    assert "device" not in info

    setup.picker.setCurrentIndex(0)
    setup.start_button.click()
    reader.edge(USB, APP, True)
    qtbot.waitUntil(lambda: setup.input_step == "down_only")
    setup.timeout()
    setup.copy_check.click()
    text = QApplication.clipboard().text()
    info = json.loads(text)
    assert info["step"] == "missing_release" and not setup.photo_link.isEnabled()
    assert info["device"] == {"transport": "USB", "vendor": "0416", "product": "0602", "stable_identity_available": True}
    assert info["tested_key"] == APP and info["observations"]["held_signals"] == 1
    assert USB.unit_key not in text and USB.name not in text


@pytest.mark.fresh_config
def test_voice_guide_resume_and_explicit_dictation_confirmation(qtbot, tmp_path):
    saved = mapping(USB, tmp_path / "Example.app")
    remote = remote_for_device("书桌", BT_KEYBOARD, replace(saved.profile, vendor=BT_KEYBOARD.vendor, product=BT_KEYBOARD.product))
    reader = Reader(BT_KEYBOARD)
    window, c = open_window(qtbot, tmp_path, reader, (remote, BT_KEYBOARD))
    window.show()
    keys = Keys()
    c.voice.keys = keys
    guide = window.voice_guide
    window.show_page(window.pages.index(window.voice_page))
    # The microphone is chosen only in step 1 「语音来源」; the trial card follows 「仅触发识别」.
    assert not c.voice_enabled and not guide.isVisible()
    page = window.voice_page
    page.source.setCurrentIndex(page.source.findData("trigger"))
    page.source_chosen()
    assert c.voice_settings()["voice_source"] == "trigger" and guide.isVisible()
    guide.copy.click()
    info = json.loads(QApplication.clipboard().text())
    assert info["step"] == "voice_waiting_for_key"
    assert info["observations"]["remote_mic_capture_available"] is False
    assert not guide.confirm.isEnabled()
    assert c.update_voice(voice_keys="ctrl+space")
    reader.edge(BT_KEYBOARD, VOICE, True)
    qtbot.waitUntil(lambda: len(keys.edges) == 1)
    assert not guide.confirm.isEnabled()
    reader.edge(BT_KEYBOARD, VOICE, False)
    qtbot.waitUntil(guide.confirm.isEnabled)
    assert guide.evidence()[0] == "shortcut_sent"
    guide.confirm.click()
    assert guide.evidence()[0] == "dictation_confirmed"
    stored = json.loads((tmp_path / "remotes.json").read_text(encoding="utf-8"))
    assert stored["remotes"][0]["voice"]["guide_verified_voice"]
    assert c.update_voice(voice_keys="shift")
    assert guide.evidence()[0] == "voice_waiting_for_key"


@pytest.mark.fresh_config
def test_nine_key_binding_uses_the_learned_photo_and_device_events(qtbot, tmp_path, monkeypatch):
    # Use the physical 2 as the opener as well as the numeric 2,
    # so a device with just the keypad and navigation buttons can still exit.
    buttons = {
        role: Button(name=KEYPAD_LABELS.get(role, f"导航 {role}"), trigger=f"hid:7:{index}")
        for index, role in enumerate(KEYPAD_ROLES + CONTROL_ROLES, 60)
    }
    opener = buttons["2"]
    profile = Profile(name=BT_KEYBOARD.name, vendor=BT_KEYBOARD.vendor, product=BT_KEYBOARD.product,
                      image=television_profile().image, buttons=list(buttons.values()))
    reader = Reader(BT_KEYBOARD, KEYBOARD)
    window, c = open_window(qtbot, tmp_path, reader, (remote_for_device(BT_KEYBOARD.name, BT_KEYBOARD, profile), BT_KEYBOARD))
    assert c.hid_device == BT_KEYBOARD
    c.select(opener.id)
    inspector = window.mapping.inspector
    inspector.choose_kind("nine_key")
    inspector.nine_key_setup.click()
    editor = inspector.nine_key_editor
    bindings = {role: button.id for role, button in buttons.items()}
    for role, identity in bindings.items():
        editor.slots[role].click()
        window.mapping.canvas.callouts[identity].click()
    assert c.selected_id == opener.id
    editor.save.click()
    stored = json.loads((tmp_path / "remotes.json").read_text(encoding="utf-8"))
    assert stored["remotes"][0]["profiles"][0]["buttons"][1]["action"]["spec"] == {"bindings": bindings}

    pasted = []
    monkeypatch.setattr(c.keys, "input_text", pasted.append)
    reader.tap(KEYBOARD, opener.trigger)   # not the connected device
    assert not c.overlays.nine_key_open
    for control in (opener, buttons["mode"], buttons["mode"], buttons["1"], opener, buttons["confirm"]):
        reader.tap(BT_KEYBOARD, control.trigger)
    qtbot.waitUntil(lambda: bool(pasted))
    assert pasted == ["12"]
    assert window.nine_key_osd.isVisible()
    c.overlays.nine_key_exit_hold.setInterval(5)
    reader.edge(BT_KEYBOARD, opener.trigger, True)
    qtbot.waitUntil(lambda: not window.nine_key_osd.isVisible())
    reader.edge(BT_KEYBOARD, opener.trigger, False)
