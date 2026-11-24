"""Synthetic rectangular subjects exercise perspective correction and photo import."""
import base64
import json
import logging

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF, QTransform
from PySide6.QtWidgets import QFileDialog, QMessageBox

from remotehub.photo_cutout import MAX_PHOTO_EDGE, photo_crop_size, rectify_photo
from remotehub.ui.photo_crop import PhotoCropDialog
from remotehub.ui.probe_photo import ProbePhotoPane


PHOTO_LOGGER = "remotehub.ui.probe_photo"
TRAPEZOID = ((133, 40), (242, 69), (263, 328), (98, 310))
MARKERS = (
    (0.23, 0.22, "#d03028"), (0.77, 0.22, "#168b55"),
    (0.77, 0.78, "#295dc4"), (0.23, 0.78, "#d59322"),
)


def import_events(caplog):
    events = (json.loads(record.getMessage()) for record in caplog.records if record.name == PHOTO_LOGGER)
    return {event["event"]: event for event in events}


def make_photo(outline=TRAPEZOID):
    corners = [QPointF(x, y) for x, y in outline]
    face = QImage(120, 280, QImage.Format.Format_RGB32)
    face.fill(QColor("#263141"))
    with QPainter(face) as painter:
        for x, y, color in MARKERS:
            painter.fillRect(QRectF(x * face.width() - 12, y * face.height() - 12, 24, 24), QColor(color))
    flat = QPolygonF([QPointF(), QPointF(120, 0), QPointF(120, 280), QPointF(0, 280)])
    transform = QTransform()
    assert QTransform.quadToQuad(flat, QPolygonF(corners), transform)
    photo = QImage(420, 380, QImage.Format.Format_RGB32)
    photo.fill(QColor("#b9a485"))
    with QPainter(photo) as painter:
        painter.setTransform(transform)
        painter.drawImage(QPointF(), face)
    return photo, corners


@pytest.mark.parametrize("outline", [
    TRAPEZOID,
    ((335, 52), (354, 161), (84, 182), (93, 45)),
    ((160, 30), (178, 30), (385, 281), (20, 280)),
])
def test_perspective_crop_keeps_corner_order_and_flattens_markers(outline):
    photo, corners = make_photo(outline)
    before = photo.copy()

    result = rectify_photo(photo, corners)

    assert photo == before
    assert result is not None
    assert result.size() == photo_crop_size(photo, corners)
    for x, y, color in MARKERS:
        actual = result.pixelColor(round(x * result.width()), round(y * result.height())).getRgb()
        expected = QColor(color).getRgb()
        # At steep angles, the camera image only has a few pixels for each
        # marker near the far edge. The second sampling pass can blend them.
        assert max(abs(a - b) for a, b in zip(actual, expected)) <= 16


@pytest.mark.parametrize("outline", [
    TRAPEZOID[:3],
    (TRAPEZOID[0], TRAPEZOID[2], TRAPEZOID[1], TRAPEZOID[3]),
    (TRAPEZOID[0], TRAPEZOID[1], TRAPEZOID[2], TRAPEZOID[2]),
    (TRAPEZOID[0], TRAPEZOID[1], TRAPEZOID[2], (175, 116)),
    ((-1, 5), *TRAPEZOID[1:]),
])
def test_perspective_crop_rejects_incomplete_or_invalid_corners(outline):
    photo, _ = make_photo()
    corners = [QPointF(x, y) for x, y in outline]
    assert photo_crop_size(photo, corners).isEmpty()
    assert rectify_photo(photo, corners) is None


def test_full_image_edges_and_size_limit_are_supported():
    photo = QImage(2200, 1100, QImage.Format.Format_RGB32)
    photo.fill(QColor("steelblue"))
    corners = [QPointF(0, 0), QPointF(2200, 0), QPointF(2200, 1100), QPointF(0, 1100)]

    result = rectify_photo(photo, corners)

    assert result is not None
    assert result.width() == MAX_PHOTO_EDGE and result.height() == MAX_PHOTO_EDGE // 2
    assert result.pixelColor(0, 0) == QColor("steelblue")
    assert result.pixelColor(result.width() - 1, result.height() - 1) == QColor("steelblue")


def pick_corners(qtbot, dialog, corners):
    for corner in corners:
        qtbot.mouseClick(
            dialog.canvas, Qt.MouseButton.LeftButton, pos=dialog.canvas.display_point(corner).toPoint()
        )


def test_editor_adjusts_four_corners_and_returns_from_preview(qtbot):
    parent = ProbePhotoPane()
    qtbot.addWidget(parent)
    photo, corners = make_photo()
    dialog = PhotoCropDialog(photo, parent)
    dialog.show()
    assert not dialog.crop_button.isEnabled()

    pick_corners(qtbot, dialog, corners)
    canvas = dialog.canvas
    assert dialog.crop_button.isEnabled()
    start = canvas.corners[0]
    destination = start + QPointF(9, 7)
    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=canvas.display_point(start).toPoint())
    qtbot.mouseMove(canvas, pos=canvas.display_point(destination).toPoint())
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=canvas.display_point(destination).toPoint())
    assert canvas.corners[0].x() == pytest.approx(destination.x(), abs=1)
    assert canvas.corners[0].y() == pytest.approx(destination.y(), abs=1)
    x = canvas.corners[0].x()
    qtbot.keyClick(canvas, Qt.Key.Key_Right)
    assert canvas.corners[0].x() == pytest.approx(x + 1)

    dialog.crop_button.click()
    assert canvas.preview is not None
    assert dialog.crop_button.text() == "使用裁剪"
    dialog.adjust_button.click()
    assert canvas.preview is None and len(canvas.corners) == 4

    canvas_position = canvas.display_point(QPointF(160, 64)).toPoint()
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas_position)
    assert canvas.corners[0].x() == pytest.approx(160, abs=1)
    dialog.adjust_button.click()
    assert not canvas.corners and not dialog.crop_button.isEnabled()


def test_editor_blocks_crossed_corners(qtbot):
    parent = ProbePhotoPane()
    qtbot.addWidget(parent)
    photo, corners = make_photo()
    dialog = PhotoCropDialog(photo, parent)
    dialog.show()
    pick_corners(qtbot, dialog, [corners[i] for i in (0, 2, 1, 3)])
    assert len(dialog.canvas.corners) == 4
    assert not dialog.crop_button.isEnabled()
    assert "凸四边形" in dialog.hint.text()


@pytest.mark.parametrize("choice", ["manual_crop", "original", "cancelled"])
def test_import_uses_rectified_crop_or_original_and_logs_decision(qtbot, tmp_path, monkeypatch, caplog, choice):
    caplog.set_level(logging.INFO, logger=PHOTO_LOGGER)
    pane = ProbePhotoPane()
    qtbot.addWidget(pane)
    original, corners = make_photo()
    path = tmp_path / "private-photo.png"
    assert original.save(str(path))

    def choose(dialog):
        dialog.show()
        if choice == "manual_crop":
            pick_corners(qtbot, dialog, corners)
            dialog.crop_button.click()
            assert dialog.canvas.preview is not None
            dialog.crop_button.click()
        elif choice == "original":
            dialog.original_button.click()
        else:
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        return dialog.result()

    monkeypatch.setattr(PhotoCropDialog, "exec", choose)
    if choice == "cancelled":
        pane.load_photo(path)
        assert not pane.profile.image
    else:
        with qtbot.waitSignal(pane.image_changed):
            pane.load_photo(path)
        imported = QImage.fromData(base64.b64decode(pane.profile.image))
        if choice == "original":
            assert imported.convertToFormat(original.format()) == original
        else:
            assert imported.height() > imported.width()
            x, y, color = MARKERS[0]
            assert imported.pixelColor(round(x * imported.width()), round(y * imported.height())) == QColor(color)

    events = import_events(caplog)
    assert "crop_dialog_opened" in events
    assert events["crop_dialog_closed"]["choice"] == choice
    assert len(events["crop_dialog_closed"]["corners"]) == (4 if choice == "manual_crop" else 0)
    if choice == "manual_crop":
        preview = events["crop_preview_ready"]
        assert (preview["width"], preview["height"]) == (imported.width(), imported.height())
    if choice == "cancelled":
        assert "import_completed" not in events
    else:
        assert events["import_completed"]["source"] == choice
    assert str(tmp_path) not in caplog.text and path.name not in caplog.text


def test_import_logs_decode_failure_without_the_file_name(qtbot, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=PHOTO_LOGGER)
    pane = ProbePhotoPane()
    qtbot.addWidget(pane)
    path = tmp_path / "private-photo.png"
    path.write_bytes(b"invalid image")

    with qtbot.waitSignal(pane.failed):
        pane.load_photo(path)
    events = import_events(caplog)
    assert events["decode_failed"]["reader_error"]
    assert events["import_failed"]["stage"] == "decode"
    assert "crop_dialog_opened" not in events
    assert str(tmp_path) not in caplog.text and path.name not in caplog.text


@pytest.mark.parametrize("choice", ["选择照片", "取消"])
def test_overhead_shooting_tips_come_before_the_picker(qtbot, monkeypatch, caplog, choice):
    caplog.set_level(logging.INFO, logger=PHOTO_LOGGER)
    pane = ProbePhotoPane()
    qtbot.addWidget(pane)
    calls = []

    def answer_tips(message):
        calls.append("tips")
        assert "手机与桌面平行，从正上方拍摄" in message.text()
        next(button for button in message.buttons() if button.text() == choice).click()
        return message.result()

    def pick(*_):
        calls.append("picker")
        return "remote.png", ""

    monkeypatch.setattr(QMessageBox, "exec", answer_tips)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", pick)
    monkeypatch.setattr(pane, "load_photo", calls.append)
    pane.photo_button.click()
    assert calls == (["tips", "picker", "remote.png"] if choice == "选择照片" else ["tips"])
    events = import_events(caplog)
    assert events["shooting_tips_closed"]["proceed"] is (choice == "选择照片")
    if choice == "选择照片":
        assert events["file_picker_closed"]["selected"] is True
    else:
        assert "file_picker_opened" not in events
