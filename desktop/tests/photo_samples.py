"""Programmatic remote photos for the key-frame detector. No real photos are stored."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform


@dataclass(frozen=True)
class Key:
    """A key drawn on the face. Centre is relative to the face, size to its width."""

    cx: float
    cy: float
    w: float
    h: float
    color: str = "#d8dce2"
    shape: str = "round"        # round, rect, capsule, ring, strips
    text: str = ""
    text_color: str = "#20242a"
    split: bool = False         # a thin glare line across the key

    def box(self, aspect):
        """Normalized [x, y, w, h] of the key on a face with height = aspect × width."""
        w, h = self.w, self.h / aspect
        return [self.cx - w / 2, self.cy - h / 2, w, h]

    def frames(self, aspect):
        """Expected frames: a tall capsule is a rocker with an upper and a lower key."""
        x, y, w, h = self.box(aspect)
        if self.shape == "capsule" and self.h >= 1.9 * self.w:
            return [[x, y, w, h / 2], [x, y + h / 2, w, h / 2]]
        return [[x, y, w, h]]


def draw_face(keys, *, width=360, height=1000, panel="#2a2e35", labels=(), icons=(), shading=None,
              noise=2.0, seed=7):
    """Return a flat remote face as a QImage.

    ``labels`` are (cx, cy, text, size) printed directly on the panel and ``icons``
    are (cx, cy, diameter) small dots, both expressed like keys. ``shading`` is the
    brightness at the bottom-right corner when the top-left stays at 1.0.
    """
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(panel))
    with QPainter(image) as painter:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for key in keys:
            _draw_key(painter, key, width)
        painter.setPen(QColor("#e8e8e8"))
        for cx, cy, text, size in labels:
            font = QFont(painter.font())
            font.setPixelSize(round(size * width))
            painter.setFont(font)
            box = QRectF(cx * width - width * .2, cy * height - size * width, width * .4, size * width * 2)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#c9cdd3"))
        for cx, cy, diameter in icons:
            radius = diameter * width / 2
            painter.drawEllipse(QPointF(cx * width, cy * height), radius, radius)
    return _finish(image, shading, noise, seed)


def _draw_key(painter, key, width):
    height = painter.device().height()
    w, h = key.w * width, key.h * width
    rect = QRectF(key.cx * width - w / 2, key.cy * height - h / 2, w, h)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(key.color))
    if key.shape == "round":
        painter.drawEllipse(rect)
    elif key.shape == "rect":
        painter.drawRoundedRect(rect, w * .12, w * .12)
    elif key.shape == "capsule":
        radius = min(w, h) / 2
        painter.drawRoundedRect(rect, radius, radius)
    elif key.shape == "ring":
        path = QPainterPath()
        path.addEllipse(rect)
        inner = rect.adjusted(w * .3, h * .3, -w * .3, -h * .3)
        path.addEllipse(inner)
        path.setFillRule(Qt.FillRule.OddEvenFill)
        painter.drawPath(path)
        # Arrow marks on the ring, darker than the ring.
        painter.setBrush(QColor(key.text_color))
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            centre = QPointF(rect.center().x() + dx * w * .4, rect.center().y() + dy * h * .4)
            size = w * .04
            tip = QPointF(centre.x() + dx * size, centre.y() + dy * size)
            side = QPointF(-dy * size, dx * size)
            painter.drawPolygon(QPolygonF([tip, centre - QPointF(dx * size, dy * size) + side,
                                           centre - QPointF(dx * size, dy * size) - side]))
        ok = inner.adjusted(w * .07, h * .07, -w * .07, -h * .07)
        painter.setBrush(QColor(key.color))
        painter.drawEllipse(ok)
        return
    elif key.shape == "strips":
        strip = h / 11
        for i in range(0, 11, 2):
            painter.drawRect(QRectF(rect.left(), rect.top() + i * strip, w, strip))
        return
    if key.split:
        painter.setPen(QPen(painter.device().pixelColor(0, 0), max(1., width * .005)))
        painter.drawLine(QPointF(rect.left(), rect.center().y()), QPointF(rect.right(), rect.center().y()))
    if key.text:
        painter.setPen(QColor(key.text_color))
        font = QFont(painter.font())
        font.setPixelSize(round(min(w, h) * .45))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, key.text)


def _finish(image, shading, noise, seed):
    converted = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = converted.width(), converted.height()
    raw = np.frombuffer(converted.constBits(), np.uint8, count=converted.sizeInBytes())
    pixels = raw.reshape(height, converted.bytesPerLine())[:, :width * 3].reshape(height, width, 3).astype(np.float32)
    if shading is not None:
        ys, xs = np.mgrid[0:height, 0:width]
        weight = (xs / width + ys / height) / 2
        pixels *= (1 - (1 - shading) * weight)[..., None]
    if noise:
        pixels += np.random.default_rng(seed).normal(0, noise, pixels.shape)
    data = np.ascontiguousarray(np.clip(pixels, 0, 255).astype(np.uint8))
    return QImage(data.data, width, height, width * 3, QImage.Format.Format_RGB888).copy()


def photograph(face, *, corners=((120, 60), (430, 80), (460, 900), (90, 880)), size=(560, 980),
               background="#b9a485"):
    """Place a face on a table at an angle; returns (photo, corners as QPointF)."""
    points = [QPointF(x, y) for x, y in corners]
    flat = QPolygonF([QPointF(), QPointF(face.width(), 0), QPointF(face.width(), face.height()),
                      QPointF(0, face.height())])
    transform = QTransform()
    assert QTransform.quadToQuad(flat, QPolygonF(points), transform)
    photo = QImage(*size, QImage.Format.Format_RGB32)
    photo.fill(QColor(background))
    with QPainter(photo) as painter:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setTransform(transform)
        painter.drawImage(QPointF(), face)
    return photo, points


def iou(a, b):
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0., min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0., min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter)


def matches(expected_boxes, spots, threshold=.4):
    """For each expected box, the detected spots overlapping it well."""
    return [[spot for spot in spots if iou(box, spot.rect) >= threshold] for box in expected_boxes]


STANDARD_KEYS = (
    Key(.25, .08, .16, .16, "#c8322c", "round"),                     # power
    Key(.75, .08, .2, .1, "#d8dce2", "capsule"),
    Key(.2, .5, .18, .14, "#5a8fd8", "rect", "1", "#ffffff"),
    Key(.5, .5, .18, .14, "#5a8fd8", "rect", "2", "#ffffff"),
    Key(.8, .5, .18, .14, "#5a8fd8", "rect", "3", "#ffffff"),
    Key(.2, .62, .18, .14, "#e0b030", "round"),
    Key(.5, .62, .18, .14, "#3fae6a", "round"),
    Key(.8, .62, .18, .14, "#d8dce2", "round", "OK"),
    Key(.2, .8, .16, .34, "#d8dce2", "capsule", "+"),
    Key(.8, .8, .16, .34, "#d8dce2", "capsule", "-"),
    Key(.5, .9, .2, .12, "#9aa3ad", "rect"),
)

PAD = Key(.5, .28, .6, .6, "#d8dce2", "ring", text_color="#5b6470")
