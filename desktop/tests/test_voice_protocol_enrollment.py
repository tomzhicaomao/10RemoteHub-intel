"""Protocol-first voice: the photo 「语音」 mark, the saved voice protocol, and the voice page agree."""
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from remotehub.model import Action
from remotehub.photo_template import KEYMAP
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import Remote
from remotehub.ui import MainWindow
from remotehub.key_roles import ZONE_NAVIGATION as ZONE_NAV_ID, ZONE_VOICE as ZONE_VOICE_ID

RECEIVER = ProbeDevice(("usb", 1, 0x0416, 0x0602), "0416:0602", "USB", 0x0416, 0x0602, "a" * 64, (1,))
DONGLE = ProbeDevice(("usb", 2, 0x1234, 0x5678), "2.4G 接收器", "USB", 0x1234, 0x5678, "b" * 64, (2,))
RECEIVER_VOICE = KEYMAP["keys"]["tv-voice"]["trigger"]


class Reader:
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


def _window(qtbot, tmp_path, monkeypatch, *devices):
    reader = Reader(*devices)
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    window.controller.hid_reader = reader
    monkeypatch.setattr(window, "safe", False)
    window.show()
    return window, reader


def _photo(tmp_path):
    path = tmp_path / "remote.png"
    image = QImage(120, 240, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.lightGray)
    assert image.save(str(path))
    return path


def _open_editor(qtbot, window, reader, device, tmp_path):
    probe = window.add_remote()
    index = next(i for i, d in enumerate(probe.devices) if d == device)
    probe.picker.setCurrentIndex(index)
    probe.start_button.click()
    reader.tap(device, "hid:7:5")
    qtbot.waitUntil(probe.photo_link.isEnabled)
    probe.photo_link.click()
    editor = probe.learning_dialog
    editor.photo.load_photo(_photo(tmp_path))
    return editor


def _learn(qtbot, editor, reader, device, rect, trigger):
    count = len(editor.photo.profile.buttons)
    editor.learn_position(rect)
    reader.tap(device, trigger)
    qtbot.waitUntil(lambda: len(editor.photo.profile.buttons) == count + 1 and not editor.active)


def _save(qtbot, window, editor, name):
    editor.enrollment.name.setText(name)
    editor.enrollment.save_button.click()
    qtbot.waitUntil(lambda: window.add_dialog is None)


@pytest.mark.fresh_config
def test_receiver_photo_remote_with_marked_voice_key_uses_remote_microphone(qtbot, tmp_path, monkeypatch):
    window, reader = _window(qtbot, tmp_path, monkeypatch, RECEIVER)
    c = window.controller
    editor = _open_editor(qtbot, window, reader, RECEIVER, tmp_path)

    editor.photo.start_voice_mark()
    # Saying it has a microphone tells the user plainly what the microphone can do.
    assert "遥控器麦克风直接能用" in editor.photo.mic_step_label.text()
    editor.learn_position([0.4, 0.1, 0.2, 0.1])
    # A built-in voice protocol needs no voice key press (shared HID would re-enumerate).
    assert not editor.active
    assert "不用按语音键" in editor.feedback.text()
    assert not editor.enrollment.save_button.isEnabled()      # a voice mark alone is no learned key
    _learn(qtbot, editor, reader, RECEIVER, [0.1, 0.4, 0.2, 0.1], "hid:7:5")
    _save(qtbot, window, editor, "我的 0416:0602")

    remote = c.remote
    assert remote.model_id == "" and not remote.is_fixed_model
    assert remote.voice_protocol == "usb-0416-0602"
    assert remote.supports_remote_mic and c.supports_remote_mic
    voice = next(b for b in c.profile.buttons if b.role == "voice")
    assert voice.trigger == RECEIVER_VOICE and voice.action == Action("voice")
    # The marked key is reserved for the microphone like the bundled model's voice key.
    assert c._is_voice_button(voice)
    c.select(voice.id)
    assert c.selected_voice_key
    assert c.update_voice(voice_source="remote")
    # Same receiver voice key as the bundled model: it must still send the recognition shortcut.
    assert c.receiver_voice_key
    assert not c.update_voice(voice_mode="manual")
    # Learning still works: the remote is not treated as the bundled fixed model.
    assert c.learns_in_enrollment
    assert json.loads((tmp_path / "remotes.json").read_text(encoding="utf-8"))["remotes"][0]["voice_protocol"] \
        == "usb-0416-0602"
    window.shutdown()


@pytest.mark.fresh_config
def test_generic_remote_voice_mark_learns_trigger_for_recognition(qtbot, tmp_path, monkeypatch):
    window, reader = _window(qtbot, tmp_path, monkeypatch, DONGLE)
    c = window.controller
    editor = _open_editor(qtbot, window, reader, DONGLE, tmp_path)

    editor.photo.start_voice_mark()
    assert "遥控器麦克风目前用不了" in editor.photo.mic_step_label.text()
    editor.learn_position([0.4, 0.1, 0.2, 0.1])
    # No built-in voice protocol: the dialog waits for the physical voice key on the mark itself.
    assert editor.active and editor.photo.pending is not None and editor.photo.pending.role == "voice"
    assert "「语音」键" in editor.feedback.text()
    reader.tap(DONGLE, "hid:12:207")
    qtbot.waitUntil(lambda: not editor.active)
    voice = next(b for b in editor.photo.profile.buttons if b.role == "voice")
    assert voice.trigger == "hid:12:207" and voice.name == "语音"
    _save(qtbot, window, editor, "普通遥控器")

    remote = c.remote
    assert remote.voice_protocol == "" and not remote.supports_remote_mic
    saved = next(b for b in c.profile.buttons if b.role == "voice")
    assert saved.action == Action("voice")
    assert not c._is_voice_button(saved)            # no microphone protocol: the key stays remappable
    guide = window.voice_guide
    assert c.update_voice(voice_source="trigger")
    assert guide.key_picker.findData(saved.id) >= 0
    assert guide.key_picker.currentData() == saved.id
    window.shutdown()


@pytest.mark.fresh_config
def test_receiver_remote_without_voice_mark_keeps_computer_microphone(qtbot, tmp_path, monkeypatch):
    window, reader = _window(qtbot, tmp_path, monkeypatch, RECEIVER)
    c = window.controller
    editor = _open_editor(qtbot, window, reader, RECEIVER, tmp_path)
    editor.photo.skip_voice_mark()
    _learn(qtbot, editor, reader, RECEIVER, [0.1, 0.4, 0.2, 0.1], "hid:7:5")
    _save(qtbot, window, editor, "没有麦克风")
    assert c.remote.voice_protocol == ""
    assert not c.supports_remote_mic
    assert c.voice_settings()["voice_source"] == "trigger"
    window.shutdown()


def test_earlier_030_receiver_remote_with_voice_mark_migrates_to_voice_protocol():
    def raw(role):
        return {
            "id": "r1", "name": "旧照片遥控器", "model_id": "",
            "connections": [{"transport": "USB", "vendor": 0x0416, "product": 0x0602, "units": []}],
            "voice": {"voice_source": "trigger"}, "active_profile_id": "p1",
            "profiles": [{"id": "p1", "name": "默认", "vendor": 0x0416, "product": 0x0602, "buttons": [
                {"id": "b1", "name": "语音", "rect": [0.1, 0.1, 0.1, 0.1], "role": role},
            ]}],
        }

    assert Remote.from_json(raw("voice")).voice_protocol == "usb-0416-0602"
    assert Remote.from_json(raw("")).voice_protocol == ""
    assert "voice_protocol" not in Remote.from_json(raw("")).to_json()


@pytest.mark.fresh_config
def test_photo_voice_mark_hides_zones_and_positions_delete_while_waiting(qtbot, tmp_path, monkeypatch):
    window, reader = _window(qtbot, tmp_path, monkeypatch, RECEIVER)
    editor = _open_editor(qtbot, window, reader, RECEIVER, tmp_path)
    photo = editor.photo
    photo.start_voice_mark()
    editor.learn_position([0.4, 0.1, 0.2, 0.1])
    # Selecting the voice mark shows only its status, never the ordinary zone buttons.
    assert all(not b.isVisibleTo(editor) for b in photo.zone_buttons.values())

    # A misplaced new position can be deleted while it still waits for its key.
    editor.learn_position([0.1, 0.6, 0.2, 0.1])
    assert editor.active and photo.pending is not None
    assert photo.delete_button.isEnabled()
    photo.delete_button.click()
    qtbot.waitUntil(lambda: not editor.active)
    assert photo.pending is None
    assert [b.role for b in photo.profile.buttons] == ["voice"]
    # Ordinary keys: the voice zone is gone once the voice key is marked.
    _learn(qtbot, editor, reader, RECEIVER, [0.1, 0.4, 0.2, 0.1], "hid:7:5")
    assert not photo.zone_buttons[ZONE_VOICE_ID].isVisibleTo(editor)
    assert photo.zone_buttons[ZONE_NAV_ID].isVisibleTo(editor)
    window.shutdown()
