"""Rectify a rectangular photo crop from four ordered source-image points."""
from __future__ import annotations

from collections.abc import Sequence
from math import hypot

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QImage, QPainter, QPolygonF, QTransform


MAX_PHOTO_EDGE = 1600


def photo_crop_size(image: QImage, corners: Sequence[QPointF]) -> QSize:
    """Return the output size or an invalid QSize for a malformed selection."""
    if image.isNull() or len(corners) != 4:
        return QSize()
    if not all(0 <= point.x() <= image.width() and 0 <= point.y() <= image.height() for point in corners):
        return QSize()

    edges = [corners[(i + 1) % 4] - point for i, point in enumerate(corners)]
    turns = [
        edge.x() * following.y() - edge.y() * following.x()
        for edge, following in zip(edges, edges[1:] + edges[:1])
    ]
    # Clockwise in image coordinates, with each corner on a convex outline.
    if not all(turn > 1 for turn in turns):
        return QSize()
    top, right, bottom, left = (hypot(edge.x(), edge.y()) for edge in edges)
    if min(top, right, bottom, left) < 2:
        return QSize()
    width, height = (top + bottom) / 2, (left + right) / 2
    scale = min(1, MAX_PHOTO_EDGE / max(width, height))
    return QSize(round(width * scale), round(height * scale))


def rectify_photo(image: QImage, corners: Sequence[QPointF]) -> QImage | None:
    """Map top-left, top-right, bottom-right and bottom-left to a rectangle."""
    size = photo_crop_size(image, corners)
    if size.isEmpty():
        return None
    width, height = size.width(), size.height()
    target = QPolygonF([QPointF(0, 0), QPointF(width, 0), QPointF(width, height), QPointF(0, height)])
    transform = QTransform()
    if not QTransform.quadToQuad(QPolygonF(list(corners)), target, transform):
        return None

    result = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    if result.isNull():
        return None
    result.fill(Qt.GlobalColor.transparent)
    result.setColorSpace(image.colorSpace())
    with QPainter(result) as painter:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setTransform(transform)
        painter.drawImage(QRectF(image.rect()), image)
    result.setDevicePixelRatio(image.devicePixelRatio())
    return result
