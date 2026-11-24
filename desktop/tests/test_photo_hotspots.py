"""Automatic key frames on a straightened remote photo (photo_hotspots)."""
from __future__ import annotations

import base64

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen

from photo_samples import PAD, STANDARD_KEYS, Key, draw_face, iou, matches, photograph
from remotehub.photo_cutout import rectify_photo
from remotehub.photo_hotspots import MIN_KEYS, Hotspot, detect_hotspots, label

ASPECT = 1000 / 360


@pytest.fixture(autouse=True)
def _fonts(qapp):
    """Painting text on the sample photos needs a GUI application."""
    return qapp


def frames(keys, aspect=ASPECT):
    return [frame for key in keys for frame in key.frames(aspect)]


def assert_one_spot_per_frame(keys, spots, aspect=ASPECT, threshold=.4):
    expected = frames(keys, aspect)
    for frame, found in zip(expected, matches(expected, spots, threshold)):
        assert len(found) == 1, (frame, found)
    return expected


def test_finds_keys_of_different_shapes_and_colours():
    spots = detect_hotspots(draw_face(STANDARD_KEYS))

    expected = assert_one_spot_per_frame(STANDARD_KEYS, spots)
    assert len(spots) == len(expected)
    assert all(isinstance(spot, Hotspot) and 0 < spot.confidence <= 1 for spot in spots)
    assert all(0 <= spot.x and spot.x + spot.width <= 1 and 0 <= spot.y and spot.y + spot.height <= 1
               for spot in spots)
    by_key = {key: matches([key.box(ASPECT)], spots, .3)[0] for key in STANDARD_KEYS}
    assert by_key[STANDARD_KEYS[1]][0].shape == "capsule"     # wide capsule
    assert by_key[STANDARD_KEYS[0]][0].shape == "round"
    assert by_key[STANDARD_KEYS[2]][0].shape == "rect"


def test_a_tall_capsule_is_split_into_two_rocker_keys():
    rocker = STANDARD_KEYS[8]
    spots = detect_hotspots(draw_face(STANDARD_KEYS))
    halves = [spot for spot in spots if iou(spot.rect, rocker.box(ASPECT)) > .3]
    assert len(halves) == 2
    upper, lower = sorted(halves, key=lambda spot: spot.y)
    assert upper.y + upper.height <= lower.y + 1e-6


def test_frames_come_in_reading_order():
    spots = detect_hotspots(draw_face(STANDARD_KEYS))
    centres = [spot.y + spot.height / 2 for spot in spots]
    assert all(later >= earlier - .03 for earlier, later in zip(centres, centres[1:]))
    first_row = [spot for spot in spots if spot.y < .12]
    assert len(first_row) == 2 and first_row[0].x < first_row[1].x


def test_text_and_icons_inside_a_key_stay_part_of_that_key():
    keys = (
        Key(.25, .2, .22, .16, "#d8dce2", "rect", "MENU"),
        Key(.75, .2, .22, .16, "#d8dce2", "round", "OK"),
        Key(.25, .5, .22, .16, "#3a3f47", "rect", "5", "#ffffff"),
        Key(.75, .5, .26, .16, "#d8dce2", "capsule", "◀▶"),
    )
    spots = detect_hotspots(draw_face(keys))
    assert len(spots) == len(assert_one_spot_per_frame(keys, spots))


def test_printed_text_and_small_icons_on_the_panel_are_ignored():
    keys = STANDARD_KEYS[2:8]
    labels = [(.5, .44, "INPUT  SOURCE", .045), (.5, .7, "VOL      CH", .04), (.5, .97, "remote", .035)]
    icons = [(.1, .3, .025), (.9, .3, .02), (.5, .35, .03)]
    spots = detect_hotspots(draw_face(keys, labels=labels, icons=icons))
    assert len(spots) == len(assert_one_spot_per_frame(keys, spots))


def test_fragments_of_one_key_are_merged():
    keys = (
        Key(.25, .2, .24, .2, "#d8dce2", "strips"),             # a grille of separate strips
        Key(.75, .2, .24, .16, "#d8dce2", "rect", split=True),  # crossed by a thin glare line
        Key(.25, .5, .2, .16, "#5a8fd8", "round"),
        Key(.75, .5, .2, .16, "#5a8fd8", "round"),
    )
    spots = detect_hotspots(draw_face(keys))
    assert len(spots) == len(assert_one_spot_per_frame(keys, spots))
    strips = matches([keys[0].box(ASPECT)], spots)[0][0]
    assert iou(strips.rect, keys[0].box(ASPECT)) > .7


def test_direction_ring_becomes_five_frames():
    keys = (PAD, *STANDARD_KEYS[2:5])
    spots = detect_hotspots(draw_face(keys))
    pad = {spot.role: spot for spot in spots if spot.role}
    assert set(pad) == {"up", "down", "left", "right", "ok"}
    assert [pad[role].name for role in ("up", "down", "left", "right", "ok")] == ["上", "下", "左", "右", "确定"]

    def centre(spot):
        return spot.x + spot.width / 2, spot.y + spot.height / 2

    ok_x, ok_y = centre(pad["ok"])
    assert ok_x == pytest.approx(PAD.cx, abs=.03) and ok_y == pytest.approx(PAD.cy, abs=.015)
    assert centre(pad["up"])[1] < ok_y < centre(pad["down"])[1]
    assert centre(pad["left"])[0] < ok_x < centre(pad["right"])[0]
    for role in ("up", "down"):
        assert centre(pad[role])[0] == pytest.approx(ok_x, abs=.03)
    for role in ("left", "right"):
        assert centre(pad[role])[1] == pytest.approx(ok_y, abs=.015)
    # No frame overlaps another, and the ring itself is not one more giant key.
    ring = PAD.box(ASPECT)
    assert all(iou(ring, spot.rect) < .4 for spot in spots)
    assert all(iou(a.rect, b.rect) == 0 for i, a in enumerate(spots) for b in spots[i + 1:])
    assert_one_spot_per_frame(STANDARD_KEYS[2:5], spots)
    assert len(spots) == 5 + 3


def test_separate_cross_keys_are_labelled_but_a_number_grid_is_not():
    cross = (
        Key(.5, .2, .16, .16, "#d8dce2", "rect"), Key(.5, .36, .16, .16, "#d8dce2", "rect"),
        Key(.25, .28, .16, .16, "#d8dce2", "rect"), Key(.75, .28, .16, .16, "#d8dce2", "rect"),
        Key(.5, .28, .16, .16, "#c8322c", "round"),
    )
    spots = detect_hotspots(draw_face(cross))
    assert sorted(spot.role for spot in spots) == ["down", "left", "ok", "right", "up"]
    ok = next(spot for spot in spots if spot.role == "ok")
    assert iou(ok.rect, cross[4].box(ASPECT)) > .4
    up = next(spot for spot in spots if spot.role == "up")
    assert iou(up.rect, cross[0].box(ASPECT)) > .4

    grid = tuple(Key(x, y, .18, .14, "#d8dce2", "rect", str(n))
                 for n, (y, x) in enumerate(((y, x) for y in (.3, .4, .5) for x in (.2, .5, .8)), start=1))
    spots = detect_hotspots(draw_face(grid))
    assert len(spots) == 9
    assert not any(spot.role for spot in spots)


def test_low_contrast_keys_are_found():
    keys = tuple(Key(key.cx, key.cy, key.w, key.h, "#9a9a9a", key.shape) for key in STANDARD_KEYS)
    spots = detect_hotspots(draw_face(keys, panel="#808080", noise=3.0))
    assert len(spots) == len(assert_one_spot_per_frame(keys, spots))


def test_keys_the_colour_of_the_panel_are_found_by_their_rim():
    keys = tuple(Key(key.cx, key.cy, key.w, key.h, "#2a2e35", key.shape) for key in STANDARD_KEYS)
    face = draw_face(keys, noise=1.5)
    # Only a dark rim marks each key, as moulded keys often show in a photo.
    with QPainter(face) as painter:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#121418"), 2.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for key in keys:
            x, y, w, h = key.box(ASPECT)
            rect = QRectF(x * face.width(), y * face.height(), w * face.width(), h * face.height())
            radius = min(rect.width(), rect.height()) / 2 if key.shape != "rect" else rect.width() * .12
            painter.drawRoundedRect(rect, radius, radius)
    spots = detect_hotspots(face)
    assert len(spots) == len(assert_one_spot_per_frame(keys, spots))


def test_panel_shadow_does_not_hide_keys_or_add_frames():
    keys = tuple(key for key in STANDARD_KEYS if key.color != "#d8dce2")  # light keys vanish into a light panel
    spots = detect_hotspots(draw_face(STANDARD_KEYS, panel="#c4c8ce", shading=.5))
    assert_one_spot_per_frame(keys, spots)
    assert len(spots) <= len(frames(STANDARD_KEYS))


def test_dark_panel_shadow():
    spots = detect_hotspots(draw_face(STANDARD_KEYS, shading=.55))
    assert len(spots) == len(assert_one_spot_per_frame(STANDARD_KEYS, spots))


def test_slight_perspective_residue_at_the_edges_is_ignored():
    face = draw_face(STANDARD_KEYS)
    photo, corners = photograph(face)
    # The user clicked a little outside each corner: table colour shows along the edges.
    nudged = [corners[0] + QPointF(-8, -6), corners[1] + QPointF(9, -4),
              corners[2] + QPointF(6, 9), corners[3] + QPointF(-7, 5)]
    straight = rectify_photo(photo, nudged)
    assert straight is not None
    spots = detect_hotspots(straight)
    # Each key still gets exactly one frame near where it is drawn; tall
    # capsules may come out as one or two frames depending on the stretch.
    for key in STANDARD_KEYS:
        hits = matches([key.box(ASPECT)], spots, .2)[0]
        assert 1 <= len(hits) <= len(key.frames(ASPECT)), key
    assert len(frames(STANDARD_KEYS)) - 2 <= len(spots) <= len(frames(STANDARD_KEYS))
    for spot in spots:
        assert .01 < spot.x and spot.x + spot.width < .99


def test_bundled_example_photo_yields_its_thirty_keys():
    from remotehub.photo_template import television_profile

    profile = television_profile()
    image = QImage.fromData(base64.b64decode(profile.image))
    spots = detect_hotspots(image)
    assert len(spots) == 30
    assert sorted(spot.role for spot in spots if spot.role) == sorted([
        "up", "down", "left", "right", "ok",
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "star", "0", "hash",
    ])
    # Most keys of the bundled layout are covered by exactly one detected frame.
    found = matches([button.rect for button in profile.buttons], spots, .25)
    assert sum(len(hits) == 1 for hits in found) >= len(profile.buttons) - 3


@pytest.mark.parametrize("image", [
    lambda: draw_face(()),
    lambda: draw_face(STANDARD_KEYS[:MIN_KEYS - 1]),
    lambda: QImage(),
    lambda: draw_face((), width=30, height=40),
], ids=["blank", "two-keys", "null", "tiny"])
def test_too_few_keys_or_unusable_photo_returns_nothing(image):
    assert detect_hotspots(image()) == []


def test_transparent_corners_are_not_keys():
    face = draw_face(STANDARD_KEYS).convertToFormat(QImage.Format.Format_ARGB32)
    for x in range(40):
        for y in range(40 - x):
            face.setPixelColor(x, y, QColor(0, 0, 0, 0))
    spots = detect_hotspots(face)
    assert len(spots) == len(frames(STANDARD_KEYS))


def test_large_photos_are_scaled_down_with_the_same_result():
    small = detect_hotspots(draw_face(STANDARD_KEYS))
    large = detect_hotspots(draw_face(STANDARD_KEYS, width=720, height=2000))
    assert len(small) == len(large)
    for a, b in zip(small, large):
        assert iou(a.rect, b.rect) > .7


def test_component_labelling():
    mask = np.array([
        [1, 1, 0, 0, 1],
        [0, 1, 0, 1, 1],
        [0, 0, 0, 0, 0],
        [1, 0, 1, 0, 0],
        [0, 1, 0, 0, 1],
    ], bool)
    labels, count = label(mask)
    assert count == 4          # diagonal neighbours join
    assert labels[3, 0] == labels[4, 1] == labels[3, 2]
    _, count = label(mask, diagonal=False)
    assert count == 6


ICON_KEYS = tuple(Key(x, y, .3, .3, "#d8dce2", "ring", text_color="#5b6470")
                  for x, y in ((.27, .55), (.73, .55), (.27, .7), (.73, .7)))


def test_round_icon_keys_beside_the_pad_stay_single_keys():
    # Real photo: home/back/menu keys are round with a centred icon; each was
    # taken for a small direction pad and split into five frames.
    spots = detect_hotspots(draw_face((PAD, *ICON_KEYS)))
    roles = [spot.role for spot in spots if spot.role]
    assert sorted(roles) == ["down", "left", "ok", "right", "up"]
    assert len(spots) == 5 + len(ICON_KEYS)
    assert_one_spot_per_frame(ICON_KEYS, spots)


def test_without_a_large_pad_no_round_key_becomes_a_pad():
    spots = detect_hotspots(draw_face(ICON_KEYS + STANDARD_KEYS[:1]))
    assert not [spot for spot in spots if spot.role]
    assert len(spots) == len(ICON_KEYS) + 1


def test_a_rocker_longer_than_the_remote_is_wide_is_found():
    rocker = Key(.72, .76, .26, .75, "#d8dce2", "capsule", "+")
    keys = (rocker, *STANDARD_KEYS[2:5])
    spots = detect_hotspots(draw_face(keys))
    assert_one_spot_per_frame(keys, spots)
