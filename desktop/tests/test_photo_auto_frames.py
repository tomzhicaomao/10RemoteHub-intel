"""Photo learning: clean manual guided Zone -> Role flow, crop hover cursor, and legacy helper compatibility."""
from __future__ import annotations

import base64

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QHoverEvent, QImage
from PySide6.QtWidgets import QApplication

from photo_samples import PAD, STANDARD_KEYS, draw_face, photograph
from remotehub.device_probe import DeviceProbeDialog
from remotehub.key_roles import ZONE_FUNCTION, ZONE_KEYPAD, ZONE_NAVIGATION, ZONE_VOICE
from remotehub.probe_device import ProbeDevice
from remotehub.ui.photo_crop import PhotoCropDialog
from remotehub.ui.probe_photo import ALL_FRAMES_LEARNED, SILENT_KEY_TIP, ProbePhotoPane

DEVICE = ProbeDevice(("usb", 1), "通用遥控器", "USB", 0x1234, 0x5678, "a" * 64, (1,))
FACE_KEYS = (PAD, *STANDARD_KEYS[2:8])


class SharedReader:
    def __init__(self, device=DEVICE):
        self.device = device
        self.on_key = None

    def enumerate(self):
        return [self.device]

    def start(self, _identity, on_key, _on_removed, *, expected=None):
        self.on_key = on_key
        return 1, 1

    def tap(self, trigger):
        self.on_key((1, 0), trigger, True)
        self.on_key((1, 0), trigger, False)

    def stop(self):
        self.on_key = None

    close = stop


@pytest.fixture(autouse=True)
def discard_on_close(monkeypatch):
    """Closing a dialog with unsaved frames asks first; never block a test on it."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Discard)


@pytest.fixture
def straighten(monkeypatch, qtbot):
    """Answer the four-corner dialog: click the corners given, preview and use the crop."""
    chosen = {}

    def choose(dialog):
        dialog.show()
        corners = chosen.get("corners")
        if corners is None:
            dialog.original_button.click()
            return dialog.result()
        for corner in corners:
            qtbot.mouseClick(dialog.canvas, Qt.MouseButton.LeftButton,
                             pos=dialog.canvas.display_point(corner).toPoint())
        dialog.crop_button.click()
        dialog.crop_button.click()
        return dialog.result()

    monkeypatch.setattr(PhotoCropDialog, "exec", choose)
    return chosen


def save(tmp_path, image, name="remote.png"):
    path = tmp_path / name
    assert image.save(str(path))
    return path


def photo_dialog(qtbot, reader=None, install=None):
    reader = reader or SharedReader()
    dialog = DeviceProbeDialog(reader, device=reader.device, install=install or (lambda *_args: True))
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog, reader


def import_face(tmp_path, straighten, dialog, keys=FACE_KEYS):
    photo, corners = photograph(draw_face(keys))
    straighten["corners"] = corners
    dialog.photo.load_photo(save(tmp_path, photo))


def test_import_leaves_photo_clean_without_auto_frames(qtbot, tmp_path, straighten):
    dialog, _ = photo_dialog(qtbot)
    import_face(tmp_path, straighten, dialog)
    photo = dialog.photo

    straight = QImage.fromData(base64.b64decode(photo.profile.image))
    assert straight.height() > straight.width()
    assert not photo.profile.buttons
    assert not photo.auto_ids
    assert not dialog.active
    assert photo.count.text() == "已学习 0 键"
    assert photo.tips.isVisibleTo(dialog)
    assert photo.found_tip.text().startswith("点照片上按键的位置")
    assert photo.silent_tip.text() == SILENT_KEY_TIP
    assert photo.help_button.isVisibleTo(dialog)
    photo.mark_clean()


def test_manual_two_step_zone_then_role_before_pressing_physical_key(qtbot, tmp_path, straighten):
    dialog, reader = photo_dialog(qtbot)
    import_face(tmp_path, straighten, dialog)
    photo = dialog.photo

    # 1. Click photo -> pick 方向区 -> 上 -> press a non-standard vendor code.
    dialog.learn_position([0.35, 0.2, 0.14, 0.08])
    assert dialog.active and photo.pending is not None
    assert not dialog.capture_timer.isActive()  # never 12s-timeout while user picks Zone/Role
    assert photo.key_bar.isVisibleTo(dialog)
    assert not photo.key_name_input.isEnabled()

    photo.zone_buttons[ZONE_NAVIGATION].click()
    photo.role_buttons["up"].click()
    assert photo.pending.role == "up" and photo.pending.name == "上"
    assert "「上」" in dialog.feedback.text()

    reader.tap("hid:12:513")
    qtbot.waitUntil(lambda: not dialog.active and len(photo.profile.buttons) == 1)
    up_btn = photo.profile.buttons[0]
    assert up_btn.role == "up" and up_btn.name == "上" and up_btn.trigger == "hid:12:513"

    # 2. Click photo -> pick 数字区 -> 5 -> press non-standard code.
    dialog.learn_position([0.4, 0.5, 0.14, 0.08])
    photo.zone_buttons[ZONE_KEYPAD].click()
    photo.role_buttons["5"].click()
    reader.tap("hid:12:514")
    qtbot.waitUntil(lambda: not dialog.active and len(photo.profile.buttons) == 2)
    five_btn = photo.profile.buttons[1]
    assert five_btn.role == "5" and five_btn.name == "5"

    # 3. Click photo -> pick 语音区 -> press key.
    dialog.learn_position([0.4, 0.35, 0.14, 0.08])
    photo.zone_buttons[ZONE_VOICE].click()
    assert photo.pending.role == "voice" and photo.pending.name == "语音"
    reader.tap("hid:7:62")
    qtbot.waitUntil(lambda: not dialog.active and len(photo.profile.buttons) == 3)
    voice_btn = photo.profile.buttons[2]
    assert voice_btn.role == "voice" and voice_btn.name == "语音"

    # 4. Click photo -> explicitly pick 功能区 -> even a standard code (Up) stays Function Zone,
    #    and user can immediately customize the name.
    dialog.learn_position([0.2, 0.12, 0.14, 0.08])
    photo.zone_buttons[ZONE_FUNCTION].click()
    reader.tap("hid:7:82")
    qtbot.waitUntil(lambda: not dialog.active and len(photo.profile.buttons) == 4)
    func_btn = photo.profile.buttons[3]
    assert func_btn.role == ""
    assert photo.key_name_input.isEnabled()
    photo.key_name_input.setText("信号源")
    photo.rename_button.click()
    assert func_btn.name == "信号源" and func_btn.role == ""

    # Already-taken roles show a checkmark marker and reassigning maintains uniqueness.
    photo.select(func_btn.id)
    photo.zone_buttons[ZONE_NAVIGATION].click()
    assert "✓" in photo.role_buttons["up"].text()
    photo.role_buttons["up"].click()
    assert func_btn.role == "up" and up_btn.role == ""
    photo.mark_clean()


def test_manual_press_first_auto_fills_zone_role_and_friendly_name(qtbot, tmp_path, straighten):
    dialog, reader = photo_dialog(qtbot)
    import_face(tmp_path, straighten, dialog)
    photo = dialog.photo

    # Standard navigation key auto-selects 方向区 + 上.
    dialog.learn_position([0.4, 0.2, 0.14, 0.08])
    reader.tap("hid:7:82")
    qtbot.waitUntil(lambda: not dialog.active and len(photo.profile.buttons) == 1)
    assert photo.profile.buttons[0].role == "up"
    assert photo.profile.buttons[0].name == "上"
    assert photo.zone_buttons[ZONE_NAVIGATION].isChecked()
    assert photo.role_buttons["up"].isChecked()

    # Standard media volume key auto-fills friendly name in 功能区 without forcing a role.
    dialog.learn_position([0.2, 0.4, 0.14, 0.08])
    reader.tap("hid:12:233")
    qtbot.waitUntil(lambda: not dialog.active and len(photo.profile.buttons) == 2)
    assert photo.profile.buttons[1].role == ""
    assert photo.profile.buttons[1].name == "音量 +"
    assert photo.zone_buttons[ZONE_FUNCTION].isChecked()
    photo.mark_clean()


def test_manual_learning_missed_release_recovery_ai_help_and_direct_save(qtbot, tmp_path, straighten):
    saved = []
    dialog, reader = photo_dialog(qtbot, install=lambda remote, device: saved.append(remote) or True)
    import_face(tmp_path, straighten, dialog)
    photo = dialog.photo

    # Start learning a spot and select a role; missed release recovers cleanly without cancelling.
    dialog.learn_position([0.4, 0.2, 0.14, 0.08])
    photo.zone_buttons[ZONE_NAVIGATION].click()
    photo.role_buttons["ok"].click()
    assert not dialog.capture_timer.isActive()

    # AI help names the pending key even in manual mode.
    photo.help_button.click()
    clip = QApplication.clipboard().text()
    assert "「确定」" in clip and "没有收到任何按键信号" in clip

    # Down without release arms 4s guard and recovers non-fatally on timeout.
    reader.on_key((1, 0), "hid:7:40", True)
    qtbot.waitUntil(lambda: bool(dialog.held))
    assert dialog.capture_timer.isActive()
    dialog.timeout()
    assert dialog.active and not dialog.held and photo.pending is not None

    # Pressing again binds cleanly.
    reader.tap("hid:7:40")
    qtbot.waitUntil(lambda: not dialog.active and len(photo.profile.buttons) == 1)

    # With 1 key already learned, clicking a second spot keeps Save enabled and saving stops capture.
    dialog.learn_position([0.4, 0.4, 0.14, 0.08])
    assert dialog.active and dialog.enrollment.save_button.isEnabled()
    dialog.enrollment.save_button.click()
    assert not dialog.active and len(saved) == 1
    assert [b.trigger for b in saved[0].active_profile.buttons] == ["hid:7:40"]


def test_zone_role_and_edit_controls_never_clip_at_minimum_dialog_size(qtbot, tmp_path, straighten):
    dialog, _ = photo_dialog(qtbot, install=lambda remote, device: True)
    dialog.resize(dialog.minimumSize())
    import_face(tmp_path, straighten, dialog)
    dialog.learn_position([0.4, 0.3, 0.14, 0.08])
    photo = dialog.photo
    dialog.layout().activate()
    photo.layout().activate()

    controls = [
        photo.photo_button, photo.help_button, photo.key_name_input, photo.rename_button, photo.delete_button,
        *photo.zone_buttons.values(),
        *(photo.role_buttons[r] for r in ("up", "down", "left", "right", "ok", "back")),
    ]
    for control in controls:
        assert control.isVisibleTo(dialog)
        top_left = control.mapTo(dialog, QPoint(0, 0))
        bottom_right = control.mapTo(dialog, QPoint(control.width() - 1, control.height() - 1))
        assert top_left.x() >= 0 and bottom_right.x() < dialog.width()
        assert top_left.y() >= 0 and bottom_right.y() < dialog.height()

    # Also verify 2x6 keypad grid does not clip.
    photo.zone_buttons[ZONE_KEYPAD].click()
    dialog.layout().activate()
    for role in ("1", "6", "7", "hash"):
        btn = photo.role_buttons[role]
        assert btn.isVisibleTo(dialog)
        top_left = btn.mapTo(dialog, QPoint(0, 0))
        bottom_right = btn.mapTo(dialog, QPoint(btn.width() - 1, btn.height() - 1))
        assert top_left.x() >= 0 and bottom_right.x() < dialog.width()
        assert top_left.y() >= 0 and bottom_right.y() < dialog.height()
    photo.mark_clean()


def test_photo_crop_canvas_updates_hand_cursor_on_hover_events(qtbot):
    parent = ProbePhotoPane()
    qtbot.addWidget(parent)
    photo = draw_face(FACE_KEYS)
    dialog = PhotoCropDialog(photo, parent)
    qtbot.addWidget(dialog)
    dialog.show()
    canvas = dialog.canvas

    assert canvas.testAttribute(Qt.WidgetAttribute.WA_Hover)
    inside = canvas.image_rect().center()
    outside = QPointF(2.0, 2.0)

    # Hover inside image before 4 corners -> PointingHandCursor.
    QApplication.sendEvent(canvas, QHoverEvent(QEvent.Type.HoverMove, inside, inside, outside))
    assert canvas.cursor().shape() == Qt.CursorShape.PointingHandCursor

    # Hover outside image -> ArrowCursor.
    QApplication.sendEvent(canvas, QHoverEvent(QEvent.Type.HoverMove, outside, outside, inside))
    assert canvas.cursor().shape() == Qt.CursorShape.ArrowCursor

    # Click a corner, then hover directly on that corner -> OpenHandCursor.
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=inside.toPoint())
    assert len(canvas.corners) == 1
    corner_pos = canvas.display_point(canvas.corners[0])
    QApplication.sendEvent(canvas, QHoverEvent(QEvent.Type.HoverMove, corner_pos, corner_pos, outside))
    assert canvas.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_explicit_place_detected_keys_helper_keeps_hid_open_and_finishes_cleanly(qtbot, tmp_path, straighten):
    """Compatibility coverage for the explicit place_detected_keys() helper."""
    dialog, reader = photo_dialog(qtbot, install=lambda remote, device: True)
    starts = 0
    original_start = reader.start

    def counting_start(*args, **kwargs):
        nonlocal starts
        starts += 1
        return original_start(*args, **kwargs)

    reader.start = counting_start
    face = draw_face(STANDARD_KEYS[2:5])
    straighten["corners"] = None
    dialog.photo.load_photo(save(tmp_path, face))
    photo = dialog.photo
    assert photo.place_detected_keys(face) == 3
    qtbot.waitUntil(lambda: dialog.active)
    assert starts == 1

    photo.skip_button.click()
    assert dialog.active and starts == 1
    for code in ("hid:7:30", "hid:7:31", "hid:7:32"):
        reader.tap(code)
        qtbot.waitUntil(lambda code=code: any(k.trigger == code for k in photo.profile.buttons))
    qtbot.waitUntil(lambda: dialog.feedback.text() == ALL_FRAMES_LEARNED)
    assert not dialog.active and starts == 1
    photo.mark_clean()
