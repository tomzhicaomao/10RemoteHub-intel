"""Remote library, 0.2.0 migration and the unified add/read flow in the main window."""
from dataclasses import asdict, replace
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from remotehub import models
from remotehub.controller import AppController
from remotehub.learned_remotes import LearnedRemote, LearnedRemoteStore
from remotehub.model import Action, Button, Profile, default_profiles
from remotehub.photo_template import KEYMAP, television_profile
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import (
    Connection, Remote, RemoteLibraryStore, receiver_connection, remote_for_device, sync_layout,
)
from remotehub.ui import MainWindow


class Reader:
    """Shared-HID reader double with the enumerate/start/stop contract."""

    def __init__(self, *devices):
        self.devices = list(devices)
        self.expected = self.on_key = self.on_removed = None
        self.scope = ""

    def enumerate(self):
        self.stop()
        return list(self.devices)

    def start(self, identity, on_key, on_removed, *, expected=None):
        assert expected is None or (identity == expected.id and expected in self.devices)
        self.expected = expected or next(d for d in self.devices if d.id == identity)
        self.on_key, self.on_removed = on_key, on_removed
        return 1, 1

    def edge(self, device, trigger, down):
        if device == self.expected and self.on_key is not None:
            self.on_key((1, 0), trigger, down)

    def tap(self, device, trigger):
        self.edge(device, trigger, True)
        self.edge(device, trigger, False)

    def unplug(self, device):
        self.devices.remove(device)
        if device == self.expected and self.on_removed is not None:
            removed = self.on_removed
            self.stop()
            removed()

    def stop(self):
        self.expected = self.on_key = self.on_removed = None

    close = stop


RECEIVER_A = ProbeDevice(("usb", 1, 0x0416, 0x0602), "接收器 A", "USB", 0x0416, 0x0602, "a" * 64, (1,))
RECEIVER_B = ProbeDevice(("usb", 2, 0x0416, 0x0602), "接收器 B", "USB", 0x0416, 0x0602, "b" * 64, (2,))
DONGLE_A = ProbeDevice(("usb", 3, 0x1234, 0x5678), "2.4G 接收器", "USB", 0x1234, 0x5678, "c" * 64, (3,))
DONGLE_B = ProbeDevice(("usb", 4, 0x1234, 0x5678), "2.4G 接收器", "USB", 0x1234, 0x5678, "d" * 64, (4,))
BT_KEYBOARD = ProbeDevice(("hid", 11), "蓝牙键盘 620A:0407", "蓝牙", 0x620A, 0x0407, connection=(11,))
UP, OK = "hid:7:82", "hid:7:40"


def learned_profile(name="书桌遥控器", vendor=0x1234, product=0x5678):
    return Profile(name=name, vendor=vendor, product=product, image=television_profile().image, buttons=[
        Button(id="up", name="上", trigger=UP, rect=[0.1, 0.1, 0.2, 0.1]),
        Button(id="ok", name="确定", trigger=OK, rect=[0.1, 0.3, 0.2, 0.1]),
    ])


def write_legacy(tmp_path, settings=None, profiles=None):
    data = {"version": 1, "profiles": [asdict(p) for p in (profiles or default_profiles())],
            "settings": settings or {}}
    text = json.dumps(data, ensure_ascii=False)
    (tmp_path / "profiles.json").write_text(text, encoding="utf-8")
    return text


# -- library -------------------------------------------------------------------

def test_connection_accepts_every_unit_unless_restricted():
    any_unit = Connection("USB", 0x1234, 0x5678)
    assert any_unit.accepts(DONGLE_A) and any_unit.accepts(DONGLE_B)
    assert not any_unit.accepts(BT_KEYBOARD) and not any_unit.accepts(RECEIVER_A)
    only_a = Connection("USB", 0x1234, 0x5678, ["c" * 64])
    assert only_a.accepts(DONGLE_A) and not only_a.accepts(DONGLE_B)
    assert receiver_connection().receiver and not any_unit.receiver


@pytest.mark.fresh_config
def test_many_to_many_remotes_and_connections_roundtrip(tmp_path):
    store = RemoteLibraryStore(tmp_path)
    store.load()
    assert store.remotes == [] and store.active == "" and not store.load_error
    desk = remote_for_device("书桌遥控器", DONGLE_A, learned_profile())
    # The same handset also works over Bluetooth.
    desk.connections.append(Connection("蓝牙", BT_KEYBOARD.vendor, BT_KEYBOARD.product))
    store.add(desk)
    # A second handset shares the desk remote's receiver type.
    sofa = remote_for_device("沙发遥控器", DONGLE_B, learned_profile("沙发遥控器"))
    store.add(sofa)
    assert store.active == sofa.id
    assert [r.name for r in store.remotes if r.accepts(DONGLE_A)] == ["书桌遥控器", "沙发遥控器"]
    assert desk.accepts(BT_KEYBOARD) and not sofa.accepts(BT_KEYBOARD)

    reopened = RemoteLibraryStore(tmp_path)
    reopened.load()
    assert [r.to_json() for r in reopened.remotes] == [r.to_json() for r in store.remotes]
    assert reopened.active == sofa.id


@pytest.mark.fresh_config
def test_invalid_remote_is_rejected_and_file_kept(tmp_path):
    store = RemoteLibraryStore(tmp_path)
    store.load()
    store.add(remote_for_device("书桌遥控器", DONGLE_A, learned_profile()))
    before = store.path.read_bytes()
    broken = remote_for_device("坏", DONGLE_A, learned_profile("坏"))
    broken.profiles[0].buttons[0].trigger = "unsupported"
    with pytest.raises(ValueError, match="键码"):
        store.add(broken)
    assert store.path.read_bytes() == before and len(store.remotes) == 1
    with pytest.raises(ValueError, match="麦克风"):
        store.add(replace(remote_for_device("x", BT_KEYBOARD, learned_profile("x", BT_KEYBOARD.vendor, BT_KEYBOARD.product)),
                          voice={"voice_source": "remote"}))


@pytest.mark.fresh_config
def test_voice_settings_follow_the_active_remote(tmp_path):
    store = RemoteLibraryStore(tmp_path)
    store.load()
    first = remote_for_device("书桌", DONGLE_A, learned_profile("书桌"))
    second = remote_for_device("沙发", BT_KEYBOARD, learned_profile("沙发", BT_KEYBOARD.vendor, BT_KEYBOARD.product))
    store.add(first)
    store.settings.update(voice_source="trigger", voice_keys="ctrl+space", onboarded="1")
    store.add(second)
    assert store.settings.get("voice_keys", "alt") == "alt" and store.settings["onboarded"] == "1"
    store.activate(first.id)
    assert store.settings["voice_keys"] == "ctrl+space"
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["settings"] == {"onboarded": "1"}
    assert saved["remotes"][0]["voice"] == {"voice_source": "trigger", "voice_keys": "ctrl+space"}


def test_sync_layout_keeps_actions_and_adds_new_keys():
    remote = remote_for_device("书桌", DONGLE_A, learned_profile())
    other = Profile.loads(remote.profiles[0].dumps())
    other.id = "other"
    other.buttons[0].action = Action("text", text="hi")
    remote.profiles.append(other)
    source = remote.profiles[0]
    source.buttons[0].rect = [0.2, 0.2, 0.2, 0.1]
    source.buttons.append(Button(id="back", name="返回", trigger="hid:7:41", rect=[0.5, 0.5, 0.2, 0.1],
                                 action=Action("text", text="not copied")))
    sync_layout(remote, source)
    assert [b.id for b in other.buttons] == ["up", "ok", "back"]
    assert other.buttons[0].action.kind == "text" and other.buttons[0].rect == [0.2, 0.2, 0.2, 0.1]
    assert other.buttons[2].action.kind == "none"


# -- migration -----------------------------------------------------------------

@pytest.mark.fresh_config
def test_fresh_install_has_no_remote(tmp_path):
    c = AppController(tmp_path, auto_mapping=False)
    assert not c.has_remote and c.input_path() == ""
    assert c.remote_view().title == "未添加遥控器"
    assert not c.create_profile("x")
    assert not (tmp_path / "remotes.json").exists()


@pytest.mark.fresh_config
def test_upgrade_from_020_keeps_profiles_and_voice_and_old_file(tmp_path):
    first, second = default_profiles()[0], default_profiles()[0]
    second.id, second.name = "work", "工作"
    second.buttons[1].action = Action("text", text="hello")
    original = write_legacy(tmp_path, {
        "voice_source": "remote", "voice_keys": "alt", "input_routing": "automatic", "onboarded": "1",
        "active_profile_id": "work", "input_overlays": {},
    }, [first, second])
    c = AppController(tmp_path, auto_mapping=False)
    remote = c.remote
    assert remote.name == "遥控器 0416:0602" and remote.uses_receiver
    assert [p.id for p in remote.profiles] == [first.id, "work"] and c.profile.id == "work"
    assert c.profile.buttons[1].action.text == "hello"
    assert c.settings["voice_source"] == "remote" and c.settings["onboarded"] == "1"
    assert c.save()
    saved = json.loads((tmp_path / "remotes.json").read_text(encoding="utf-8"))
    assert saved["remotes"][0]["connections"] == [{"transport": "USB", "vendor": 0x0416, "product": 0x0602, "units": []}]
    assert saved["remotes"][0]["voice"]["voice_source"] == "remote"
    assert "voice_source" not in saved["settings"]
    # 0.2.0 can still be reinstalled.
    assert (tmp_path / "profiles.json").read_text(encoding="utf-8") == original


@pytest.mark.fresh_config
def test_development_remotes_are_imported_for_every_unit(tmp_path):
    write_legacy(tmp_path)
    bt_keyboard = learned_profile("BT_KEYBOARD", BT_KEYBOARD.vendor, BT_KEYBOARD.product)
    usb = television_profile()
    usb.id = "dev-usb"
    LearnedRemoteStore(tmp_path / "learned-remotes.json").save([
        LearnedRemote(bt_keyboard, "蓝牙", "", ["ctrl", "space"]),
        LearnedRemote(usb, "USB", "a" * 64),
    ], bt_keyboard.id)
    store = RemoteLibraryStore(tmp_path)
    store.load()
    receiver, imported = store.remotes
    assert receiver.uses_receiver and len(receiver.profiles) == 2
    assert imported.name == "BT_KEYBOARD" and imported.connections == [Connection("蓝牙", BT_KEYBOARD.vendor, BT_KEYBOARD.product)]
    assert imported.voice["voice_keys"] == "ctrl+space"
    assert store.migrated_from == ["profiles.json", "learned-remotes.json"]


# -- unified window --------------------------------------------------------------

def make_window(qtbot, tmp_path, reader):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.hid_reader = reader
    return window, c


@pytest.mark.fresh_config
def test_add_unknown_remote_learn_keys_and_read_from_any_receiver(qtbot, tmp_path, monkeypatch):
    photo = tmp_path / "photo.png"
    image = QImage(80, 200, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.gray)
    assert image.save(str(photo))
    reader = Reader(DONGLE_A, BT_KEYBOARD)
    window, c = make_window(qtbot, tmp_path, reader)
    window.show()
    assert window.stack.currentIndex() == window.pages.index(window.remotes_page)
    assert not window.sidebar.nav.button(window.pages.index(window.mapping)).isEnabled()

    monkeypatch.setattr(window, "safe", False)
    probe = window.add_remote()
    assert c.remote_input_paused
    probe.picker.setCurrentIndex(0)
    probe.start_button.click()
    reader.tap(DONGLE_A, UP)
    qtbot.waitUntil(probe.photo_link.isEnabled)
    assert not hasattr(probe, "candidate_cards")
    probe.photo_link.click()
    editor = probe.learning_dialog
    editor.photo.load_photo(photo)
    editor.learn_position([0.1, 0.1, 0.2, 0.1])
    reader.tap(DONGLE_A, UP)
    qtbot.waitUntil(editor.enrollment.save_button.isEnabled)
    editor.learn_position([0.1, 0.4, 0.2, 0.1])
    reader.tap(DONGLE_A, OK)
    qtbot.waitUntil(lambda: len(editor.photo.profile.buttons) == 2 and not editor.active)
    editor.enrollment.name.setText("书桌遥控器")
    editor.enrollment.save_button.click()
    qtbot.waitUntil(lambda: window.add_dialog is None)

    remote = c.remote
    assert remote.name == "书桌遥控器" and remote.connections == [Connection("USB", 0x1234, 0x5678)]
    assert not c.remote_input_paused and c.input_path() == "hid"
    assert window.stack.currentIndex() == window.pages.index(window.mapping)
    assert c.hid_device == DONGLE_A and c.remote_view().tone == "ok"

    pressed = []
    c.physical_key.connect(lambda trigger, down: pressed.append((trigger, down)))
    reader.tap(DONGLE_A, UP)
    qtbot.waitUntil(lambda: bool(pressed))
    assert pressed[0] == (UP, True)

    # A spare receiver of the same type takes over without confirmation.
    reader.devices.append(DONGLE_B)
    reader.unplug(DONGLE_A)
    qtbot.waitUntil(lambda: c.hid_device is None)
    c.scan_hid()
    assert c.hid_device == DONGLE_B

    saved = json.loads((tmp_path / "remotes.json").read_text(encoding="utf-8"))
    assert saved["remotes"][0]["connections"][0]["units"] == []


def pass_required_keys(qtbot, dialog, reader, device):
    """Press every required key of a verification dialog, holding voice keys."""
    now = [0.0]
    dialog.verification.clock = lambda: now[0]
    while not dialog.verification.required_done:
        step = dialog.verification.current
        passed = dialog.verification.counts()["passed"]
        reader.edge(device, step.trigger, True)
        qtbot.waitUntil(lambda: step.state == "held")
        now[0] += 1.0
        reader.edge(device, step.trigger, False)
        qtbot.waitUntil(lambda: dialog.verification.counts()["passed"] == passed + 1)


@pytest.mark.fresh_config
def test_plugged_catalog_receiver_is_offered_until_added_or_ignored(qtbot, tmp_path):
    from remotehub.inventory import USBDevice

    window, c = make_window(qtbot, tmp_path, Reader())
    window.show()
    c.monitor.apply([USBDevice(0x0416, 0x0602, "S", "接收器", "1")])
    window.check_detected_receiver()
    assert window.detected_banner.isVisible() and "0416:0602" in window.detected_text.text()
    window.ignore_detected()
    assert not window.detected_banner.isVisible()
    window.dismissed_models.clear()
    assert c.add_remote(remote_for_device("0416:0602", RECEIVER_A, television_profile(), "usb-0416-0602"))
    window.check_detected_receiver()
    assert not window.detected_banner.isVisible()


@pytest.mark.fresh_config
def test_switching_remotes_stops_shared_input_and_keeps_each_voice_setting(qtbot, tmp_path):
    reader = Reader(DONGLE_A, BT_KEYBOARD)
    window, c = make_window(qtbot, tmp_path, reader)
    desk = remote_for_device("书桌", DONGLE_A, learned_profile("书桌"))
    sofa = remote_for_device("沙发", BT_KEYBOARD, learned_profile("沙发", BT_KEYBOARD.vendor, BT_KEYBOARD.product))
    assert c.add_remote(desk, DONGLE_A)
    assert c.update_voice(voice_source="trigger", voice_keys="ctrl+space")
    assert not c.update_voice(voice_source="remote")
    assert c.add_remote(sofa, BT_KEYBOARD)
    assert c.hid_device == BT_KEYBOARD and reader.expected == BT_KEYBOARD
    assert c.voice_settings()["voice_keys"] == "alt"
    assert c.select_remote(desk.id)
    assert c.hid_device == DONGLE_A and c.voice_settings()["voice_keys"] == "ctrl+space"
    assert window.sidebar.remote_box.isVisibleTo(window) and window.sidebar.remote_box.count() == 2

    names = window._connection_users(DONGLE_B)
    assert names == ["书桌"]
    assert c.remove_remote(desk.id)
    assert c.remote.id == sofa.id and c.hid_device == BT_KEYBOARD


@pytest.mark.fresh_config
def test_learn_more_keys_updates_every_profile_and_accepts_new_receiver(qtbot, tmp_path, monkeypatch):
    reader = Reader(DONGLE_A, BT_KEYBOARD)
    window, c = make_window(qtbot, tmp_path, reader)
    assert c.add_remote(remote_for_device("书桌", DONGLE_A, learned_profile("书桌")), DONGLE_A)
    assert c.create_profile("工作")
    assert [b.action.kind for b in c.profile.buttons] == ["none", "none"]
    monkeypatch.setattr(window, "safe", False)
    editor = window.learn_more_keys()
    assert editor is not None and len(editor.photo.profile.buttons) == 2
    editor.learn_position([0.5, 0.6, 0.2, 0.1])
    reader.tap(DONGLE_A, "hid:7:41")
    qtbot.waitUntil(lambda: len(editor.photo.profile.buttons) == 3 and not editor.active)
    editor.enrollment.save_button.click()
    qtbot.waitUntil(lambda: window.add_dialog is None)
    assert all(len(p.buttons) == 3 for p in c.remote.profiles)
    assert c.hid_device == DONGLE_A


BT_SIG = ProbeDevice(("hid", 21), "蓝牙遥控器", "蓝牙", 0x620A, 0x0407, connection=(21,), vendor_id_source=1)
BT_USBIF = ProbeDevice(("hid", 22), "另一个蓝牙设备", "蓝牙", 0x620A, 0x0407, connection=(22,), vendor_id_source=2)


def test_bluetooth_connection_keeps_its_vendor_id_source():
    remote = remote_for_device("蓝牙", BT_SIG, learned_profile("蓝牙", 0x620A, 0x0407))
    connection = remote.connections[0]
    assert connection.vendor_id_source == 1
    assert remote.accepts(BT_SIG) and not remote.accepts(BT_USBIF)
    assert remote.to_json()["connections"][0]["vendor_id_source"] == 1
    usb = remote_for_device("usb", DONGLE_A, learned_profile())
    assert "vendor_id_source" not in usb.to_json()["connections"][0]
    assert Remote.from_json(remote.to_json()).connections == remote.connections
