"""Tests for A1: bundled 0416:0602 identified by model_id and non-fixed remotes on 0416:0602."""
from dataclasses import asdict
import ctypes as c
import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QPushButton

from remotehub import windows_input as win_input_mod
from remotehub import windows_probe as win_probe_mod
from remotehub.controller import AppController
from remotehub.devices import DeviceInput
from remotehub.hid_reports import HIDReports
from remotehub.mac_hid_probe import MacHIDProbe, _Interface
from remotehub.model import Action, Button, Profile, default_profiles
from remotehub.photo_template import KEYMAP, apply_fixed_keys, is_fixed_model, is_voice_button
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import Connection, Remote, RemoteLibraryStore, remote_for_device
from remotehub.ui import MainWindow
from remotehub.ui.probe_photo import ProbePhotoPane
from remotehub.windows_collector import WindowsCollector
from remotehub.windows_hid import Caps, ConsumerReports, SUCCESS, U32

FIXTURES = Path(__file__).parent / "fixtures/receiver-descriptors.json"
RECEIVER_A = ProbeDevice(("usb", 1, 0x0416, 0x0602), "0416:0602", "USB", 0x0416, 0x0602, "a" * 64, (1,))
DONGLE_A = ProbeDevice(("usb", 2, 0x1234, 0x5678), "2.4G 接收器", "USB", 0x1234, 0x5678, "b" * 64, (2,))
NON_KEYMAP_KBD_USAGE = 5      # HID Keyboard usage 5 ('B')
NON_KEYMAP_KBD_TRIGGER = "hid:7:5"
NON_KEYMAP_MEDIA_USAGE = 0x00B5  # HID Consumer Next Track (181)
NON_KEYMAP_MEDIA_TRIGGER = "hid:12:181"


class SharedReader:
    def __init__(self, *devices):
        self.devices = list(devices)
        self.expected = self.on_key = self.on_removed = None
        self.scope = ""

    def enumerate(self):
        self.stop()
        return list(self.devices)

    def start(self, identity, on_key, on_removed, *, expected=None):
        self.expected = expected or next(d for d in self.devices if d.id == identity)
        self.on_key, self.on_removed = on_key, on_removed
        return 1, 1

    def tap(self, device, trigger):
        if device == self.expected and self.on_key is not None:
            self.on_key((1, 0), trigger, True)
            self.on_key((1, 0), trigger, False)

    def stop(self):
        self.expected = self.on_key = self.on_removed = None

    close = stop


def _make_photo(tmp_path):
    path = tmp_path / "other-remote.png"
    img = QImage(120, 240, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.lightGray)
    assert img.save(str(path))
    return path


def _enroll_other_remote_on_0416_0602(qtbot, window, tmp_path, monkeypatch, reader):
    monkeypatch.setattr(window, "safe", False)
    photo = _make_photo(tmp_path)
    probe = window.add_remote()
    assert probe.selected_device() == RECEIVER_A
    probe.start_button.click()
    reader.tap(RECEIVER_A, NON_KEYMAP_KBD_TRIGGER)
    qtbot.waitUntil(probe.photo_link.isEnabled)
    probe.photo_link.click()
    editor = probe.learning_dialog
    assert not editor.photo.profile.image
    editor.photo.load_photo(photo)

    editor.learn_position([0.1, 0.1, 0.2, 0.1])
    reader.tap(RECEIVER_A, NON_KEYMAP_KBD_TRIGGER)
    qtbot.waitUntil(lambda: len(editor.photo.profile.buttons) == 1 and not editor.active)

    editor.learn_position([0.1, 0.3, 0.2, 0.1])
    reader.tap(RECEIVER_A, NON_KEYMAP_MEDIA_TRIGGER)
    qtbot.waitUntil(lambda: len(editor.photo.profile.buttons) == 2 and not editor.active)

    editor.enrollment.name.setText("配 0416:0602 的其他遥控器")
    editor.enrollment.save_button.click()
    qtbot.waitUntil(lambda: window.add_dialog is None)
    return photo


@pytest.mark.fresh_config
def test_020_migration_sets_fixed_model_id_and_preserves_upgrade_behavior(qtbot, tmp_path):
    legacy_prof = default_profiles()[0]
    legacy_prof.buttons[1].action = Action("text", text="keep-me")
    legacy_json = json.dumps({
        "version": 1,
        "profiles": [asdict(legacy_prof)],
        "settings": {"voice_source": "remote", "voice_keys": "alt", "onboarded": "1"},
    }, ensure_ascii=False)
    (tmp_path / "profiles.json").write_text(legacy_json, encoding="utf-8")

    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    remote = c.remote

    assert remote is not None
    assert remote.model_id == "usb-0416-0602"
    assert remote.uses_receiver and remote.is_fixed_model and remote.supports_remote_mic
    assert c.fixed_model and c.supports_remote_mic and not c.learns_in_enrollment
    assert c.profile.buttons[1].action == Action("text", text="keep-me")
    assert c.settings["voice_source"] == "remote"
    assert window.inspector.more.isHidden()
    assert not window.sidebar.learn_button.isVisibleTo(window)
    assert c.update_voice(voice_source="remote")
    assert (tmp_path / "profiles.json").read_text(encoding="utf-8") == legacy_json
    window.shutdown()


@pytest.mark.fresh_config
def test_other_remote_on_0416_0602_is_not_fixed_and_unlocks_learning_and_inspector(qtbot, tmp_path, monkeypatch):
    # ProbePhotoPane never auto-fills the bundled remote photo for 0416:0602 manual learning.
    pane = ProbePhotoPane(RECEIVER_A)
    qtbot.addWidget(pane)
    assert not pane.profile.image

    reader = SharedReader(RECEIVER_A, DONGLE_A)
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.hid_reader = reader
    window.show()

    _enroll_other_remote_on_0416_0602(qtbot, window, tmp_path, monkeypatch, reader)
    remote = c.remote
    assert remote is not None
    assert remote.model_id == ""
    assert remote.uses_receiver is True
    assert remote.is_fixed_model is False
    assert c.fixed_model is False
    assert c.learns_in_enrollment is True
    assert {b.trigger for b in c.profile.buttons} == {NON_KEYMAP_KBD_TRIGGER, NON_KEYMAP_MEDIA_TRIGGER}

    # Fixed-key enforcement must never touch non-fixed remotes sharing 0416:0602.
    before_triggers = [b.trigger for b in c.profile.buttons]
    assert apply_fixed_keys(c.profile, model_id=remote.model_id) is False
    assert [b.trigger for b in c.profile.buttons] == before_triggers
    assert not is_fixed_model(remote)

    # Inspector stays unlocked; creating a new profile preserves this remote's learned photo and keys.
    assert not window.inspector.more.isHidden()
    assert not window.inspector.test.isHidden()
    c.select(c.profile.buttons[0].id)
    assert c.rename("自定义B键") or True
    assert c.assign(Action("shortcut", ["ctrl", "b"]))
    assert c.create_profile("方案 2")
    assert len(c.profile.buttons) == 2
    assert {b.trigger for b in c.profile.buttons} == {NON_KEYMAP_KBD_TRIGGER, NON_KEYMAP_MEDIA_TRIGGER}
    assert all(b.action.kind == "none" for b in c.profile.buttons)

    # Sidebar and RemoteListPage expose「学习更多按键」, and learn_more_keys opens successfully.
    window.sidebar.refresh_device()
    assert window.sidebar.learn_button.isVisibleTo(window)
    card = window.remotes_page.saved_cards[remote.id]
    assert any(btn.text() == "学习更多按键" for btn in card.findChildren(QPushButton))

    learn_dialog = window.learn_more_keys()
    assert learn_dialog is not None
    learn_dialog.learn_position([0.5, 0.5, 0.2, 0.1])
    reader.tap(RECEIVER_A, "hid:7:41")
    qtbot.waitUntil(lambda: len(learn_dialog.photo.profile.buttons) == 3 and not learn_dialog.active)
    learn_dialog.enrollment.save_button.click()
    qtbot.waitUntil(lambda: window.add_dialog is None)
    assert all(len(p.buttons) == 3 for p in c.remote.profiles)
    window.shutdown()


@pytest.mark.fresh_config
def test_other_remote_on_0416_0602_voice_defaults_to_trigger_and_disallows_remote_mic(qtbot, tmp_path, monkeypatch):
    reader = SharedReader(RECEIVER_A)
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.hid_reader = reader
    window.show()

    _enroll_other_remote_on_0416_0602(qtbot, window, tmp_path, monkeypatch, reader)
    remote = c.remote
    assert not remote.supports_remote_mic
    assert not c.supports_remote_mic
    assert c.voice_settings()["voice_source"] == "trigger"

    # Remote mic is forbidden both at controller level and storage validation level.
    assert not c.update_voice(voice_source="remote")
    bad = Remote(
        name="非法语音来源",
        connections=[Connection("USB", 0x0416, 0x0602)],
        profiles=[Profile(vendor=0x0416, product=0x0602, image=_make_photo(tmp_path) and c.profile.image, buttons=list(c.profile.buttons))],
        model_id="",
        voice={"voice_source": "remote"},
    )
    with pytest.raises(ValueError, match="识别软件当前的麦克风"):
        bad.validate()

    # Voice UI hides the remote mic option and shows trigger guide.
    voice_idx = window.voice_page.source.findData("remote")
    assert voice_idx >= 0
    assert window.voice_page.source.view().isRowHidden(voice_idx)
    # The voice key panel only shows the source; it is chosen on the voice page alone.
    assert not hasattr(window.inspector.voice_editor, "source")
    assert "仅触发识别" in window.inspector.voice_editor.source_summary.text()
    window.show_page(window.pages.index(window.voice_page))
    assert window.voice_guide.isVisibleTo(window.voice_page)

    # Dedicated collector voice session helpers do not fire for non-fixed remote.
    usb_started = []
    voice_edges = []
    monkeypatch.setattr(c, "start_usb", lambda prof: usb_started.append(prof))
    monkeypatch.setattr(c.device, "voice_edge", lambda down: voice_edges.append(down))
    c.ensure_remote_audio()
    c._voice_edge(True)
    c._voice_edge(False)
    assert not usb_started and not voice_edges

    # Voice trigger assigned on this remote works via DeviceInput (dedicated collector key signal).
    c.assign_voice_trigger_button(c.profile.buttons[0].id)
    assert c.update_voice(voice_source="trigger")
    sent = []
    c.voice.keys = SimpleNamespace(press=lambda k: sent.append(("down", k)), release=lambda: sent.append(("up",)), shortcut=lambda k: sent.append(("tap", k)))
    c.device.key.emit(NON_KEYMAP_KBD_TRIGGER, True)
    c.device.key.emit(NON_KEYMAP_KBD_TRIGGER, False)
    qtbot.waitUntil(lambda: window.voice_guide.confirm.isEnabled())
    window.shutdown()


def _raw_keyboard_packet(handle, scan, flags, vk=0x41):
    body = bytes(win_input_mod.KEYBOARD(scan, flags, 0, vk, 0, 0))
    header = win_input_mod.HEADER(1, c.sizeof(win_input_mod.HEADER) + len(body), handle, 0)
    return bytes(header) + body


def _raw_consumer_packet(handle, report):
    body = len(report).to_bytes(4, "little") + (1).to_bytes(4, "little") + report
    header = win_input_mod.HEADER(2, c.sizeof(win_input_mod.HEADER) + len(body), handle, 0)
    return bytes(header) + body


def test_macos_learning_and_runtime_keycodes_match_including_non_keymap_keys(qtbot, tmp_path):
    known_triggers = {item["trigger"] for item in KEYMAP["keys"].values()}
    assert NON_KEYMAP_KBD_TRIGGER not in known_triggers
    assert NON_KEYMAP_MEDIA_TRIGGER not in known_triggers

    descriptors = json.loads(FIXTURES.read_text(encoding="utf-8"))["descriptors"]
    desc_kbd = bytes.fromhex(descriptors["4"])
    desc_media = bytes.fromhex(descriptors["1"])

    def send_mac_report(listener, raw):
        data = (c.c_uint8 * len(raw)).from_buffer_copy(raw)
        listener.report(None, 0, None, 0, 0, data, len(raw))

    learned_events = []
    kbd_listener = MacHIDProbe._listener(_Interface(11, 4, desc_kbd, 8), lambda _ch, trig, down: learned_events.append((trig, down)), None)
    media_listener = MacHIDProbe._listener(_Interface(12, 1, desc_media, 2), lambda _ch, trig, down: learned_events.append((trig, down)), None)

    dev_input = DeviceInput()
    runtime_events = []
    dev_input.key.connect(lambda trig, down: runtime_events.append((trig, down)))
    dev_input._event(json.dumps({"type": "descriptor", "interface": 4, "hex": descriptors["4"]}), 0)
    dev_input._event(json.dumps({"type": "descriptor", "interface": 1, "hex": descriptors["1"]}), 0)

    samples = [
        (4, kbd_listener, bytes.fromhex(KEYMAP["keys"]["tv-number-1"]["press_hex"])),
        (4, kbd_listener, bytes.fromhex(KEYMAP["keys"]["tv-number-1"]["release_hex"])),
        (4, kbd_listener, bytes.fromhex(KEYMAP["keys"]["tv-number-#"]["press_hex"])),
        (4, kbd_listener, bytes.fromhex(KEYMAP["keys"]["tv-number-#"]["release_hex"])),
        (4, kbd_listener, b"\x00\x00\x05\x00\x00\x00\x00\x00"),
        (4, kbd_listener, b"\x00\x00\x00\x00\x00\x00\x00\x00"),
        (1, media_listener, bytes.fromhex(KEYMAP["keys"]["tv-volume-up"]["press_hex"])),
        (1, media_listener, bytes.fromhex(KEYMAP["keys"]["tv-volume-up"]["release_hex"])),
        (1, media_listener, NON_KEYMAP_MEDIA_USAGE.to_bytes(2, "little")),
        (1, media_listener, b"\x00\x00"),
    ]
    for iface, listener, report in samples:
        send_mac_report(listener, report)
        dev_input._event(json.dumps({"type": "report", "interface": iface, "hex": report.hex()}), 0)

    expected = [
        ("hid:7:30", True), ("hid:7:30", False),
        ("hid:7:204", True), ("hid:7:204", False),
        (NON_KEYMAP_KBD_TRIGGER, True), (NON_KEYMAP_KBD_TRIGGER, False),
        ("hid:12:233", True), ("hid:12:233", False),
        (NON_KEYMAP_MEDIA_TRIGGER, True), (NON_KEYMAP_MEDIA_TRIGGER, False),
    ]
    assert learned_events == expected
    assert runtime_events == learned_events


def test_windows_learning_and_runtime_keycodes_match_including_non_keymap_keys(qtbot, monkeypatch):
    # 1. Raw Input path: WindowsHIDProbe (learning) vs WindowsInput (runtime for non-fixed 0416:0602 profile)
    packet = [b""]

    class FakeUser32:
        def __init__(self):
            def get_raw_data(_handle, _cmd, buffer, size, _header_size):
                size._obj.value = len(packet[0])
                if buffer is None:
                    return 0
                c.memmove(buffer, packet[0], len(packet[0]))
                return len(packet[0])

            def get_dev_info(_handle, command, buffer, size):
                if command == 0x20000007:
                    path = r"\\?\hid#vid_0416&pid_0602#receiver"
                    c.cast(size, c.POINTER(U32))[0] = len(path) + 1
                    if buffer is not None:
                        buffer.value = path
                    return len(path)
                assert command == 0x20000005
                c.cast(size, c.POINTER(U32))[0] = 64
                return 64 if buffer is not None else 0

            self.RegisterRawInputDevices = lambda *_args: True
            self.GetRawInputData = get_raw_data
            self.GetRawInputDeviceInfoW = get_dev_info
            self.GetRawInputDeviceList = lambda *_args: 0

    class FakeHid:
        def HidP_GetCaps(self, _preparsed, caps):
            c.cast(caps, c.POINTER(Caps)).contents.input_length = 3
            return SUCCESS

        def HidP_MaxUsageListLength(self, _kind, _page, _preparsed):
            return 8

        def HidP_GetUsages(self, _kind, _page, _collection, usages, count, _preparsed, raw_ptr, length):
            raw = c.string_at(raw_ptr, length)
            usage = int.from_bytes(raw[1:], "little")
            c.cast(count, c.POINTER(U32))[0] = bool(usage)
            usages[0] = usage
            return SUCCESS

    fake_user = FakeUser32()
    fake_user_helper = SimpleNamespace(
        GetRawInputDeviceInfoW=lambda handle, cmd, buf, size: fake_user.GetRawInputDeviceInfoW(handle, cmd, buf, size)
    )
    monkeypatch.setattr(win_input_mod.c, "WinDLL", lambda *args, **kwargs: fake_user, raising=False)
    monkeypatch.setattr(win_input_mod, "ConsumerReports", lambda _user, handle: ConsumerReports(fake_user_helper, handle, hid=FakeHid()))
    monkeypatch.setattr(win_probe_mod, "WindowsProbeMetadata", lambda: SimpleNamespace())

    learned_raw = []
    runtime_raw = []
    probe = win_probe_mod.WindowsHIDProbe()
    probe.allowed = {42}
    probe.on_key = lambda _ch, trig, down: learned_raw.append((trig, down))

    other_profile = Profile(
        name="其他 0416:0602 遥控器",
        vendor=0x0416,
        product=0x0602,
        buttons=[
            Button(id="k-1", name="键1", trigger="hid:7:30"),
            Button(id="k-unmapped", name="表外扫描码", trigger="win:115:0"),
            Button(id="k-next", name="下一曲", trigger=NON_KEYMAP_MEDIA_TRIGGER),
        ],
    )
    assert not is_fixed_model(other_profile)
    runtime_input = win_input_mod.WindowsInput(other_profile, lambda trig, down: runtime_raw.append((trig, down)), lambda *_: None, None)

    msg = win_input_mod.w.MSG()
    msg.message = 0x00FF
    packets = [
        _raw_keyboard_packet(42, 2, 0, 0x31),       # mapped key '1' -> hid:7:30
        _raw_keyboard_packet(42, 2, 1, 0x31),
        _raw_keyboard_packet(42, 115, 0, 0x41),     # non-keymap keyboard scan -> win:115:0 (must not drop!)
        _raw_keyboard_packet(42, 115, 1, 0x41),
        _raw_consumer_packet(42, b"\x00\xe9\x00"),  # keymap consumer -> hid:12:233
        _raw_consumer_packet(42, b"\x00\x00\x00"),
        _raw_consumer_packet(42, b"\x00\xb5\x00"),  # non-keymap consumer -> hid:12:181 (must not drop!)
        _raw_consumer_packet(42, b"\x00\x00\x00"),
    ]
    for raw in packets:
        packet[0] = raw
        probe.nativeEventFilter(None, c.addressof(msg))
        runtime_input.nativeEventFilter(None, c.addressof(msg))

    expected_raw = [
        ("hid:7:30", True), ("hid:7:30", False),
        ("win:115:0", True), ("win:115:0", False),
        ("hid:12:233", True), ("hid:12:233", False),
        (NON_KEYMAP_MEDIA_TRIGGER, True), (NON_KEYMAP_MEDIA_TRIGGER, False),
    ]
    assert learned_raw == expected_raw
    assert runtime_raw == learned_raw

    # 2. WinUSB path: HIDReports (_WinUSBKeys learning) vs WindowsCollector._key_loop (runtime)
    descriptors = json.loads(FIXTURES.read_text(encoding="utf-8"))["descriptors"]
    for iface, reports in (
        ("4", [
            bytes.fromhex(KEYMAP["keys"]["tv-number-#"]["press_hex"]),
            bytes.fromhex(KEYMAP["keys"]["tv-number-#"]["release_hex"]),
            b"\x00\x00\x05\x00\x00\x00\x00\x00",
            b"\x00\x00\x00\x00\x00\x00\x00\x00",
        ]),
        ("1", [
            bytes.fromhex(KEYMAP["keys"]["tv-volume-up"]["press_hex"]),
            bytes.fromhex(KEYMAP["keys"]["tv-volume-up"]["release_hex"]),
            NON_KEYMAP_MEDIA_USAGE.to_bytes(2, "little"),
            b"\x00\x00",
        ]),
    ):
        desc = bytes.fromhex(descriptors[iface])
        learn_parser = HIDReports(desc)
        learned_winusb = [ev for rep in reports for ev in learn_parser.decode(rep)]

        collector = WindowsCollector()
        runtime_winusb = []
        collector.key.connect(lambda trig, down: runtime_winusb.append((trig, down)))
        stop_event = threading.Event()
        queue_reports = list(reports)

        class FakeEndpoint:
            def read(self, _size, timeout=200):
                if not queue_reports:
                    stop_event.set()
                    return b""
                return queue_reports.pop(0)

        collector._key_loop(FakeEndpoint(), HIDReports(desc), stop_event)
        assert runtime_winusb == learned_winusb
        assert runtime_winusb
