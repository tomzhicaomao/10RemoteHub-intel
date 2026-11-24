"""Pick four corners on a remote photo and confirm the perspective crop."""
from __future__ import annotations

from math import hypot

from PySide6.QtCore import QEvent, QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout, QWidget

from ..photo_cutout import photo_crop_size, rectify_photo
from ..theme import qcolor
from .widgets import label, set_role


_CORNER_NAMES = ("左上角", "右上角", "右下角", "左下角")
_HANDLE_RADIUS = 10
_ARROWS = {
    Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
    Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1),
}


class PhotoCropCanvas(QWidget):
    corners_changed = Signal()

    def __init__(self, photo: QImage, parent: QWidget):
        super().__init__(parent)
        self.photo = photo
        self.preview: QImage | None = None
        self.corners: list[QPointF] = []
        self._selected: int | None = None
        self._dragging = False
        self.setMinimumSize(300, 260)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)   # the cursor shows what a click will do
        self.set_preview(None)

    def set_preview(self, image: QImage | None):
        self.preview = image
        self._dragging = False
        if image is None:
            self.setAccessibleName("遥控器原图，点选四角")
        else:
            self.setAccessibleName("透视拉平后的照片")
        self._update_cursor(None)
        self.update()

    def reset(self):
        self.corners.clear()
        self._selected = None
        self._dragging = False
        self.corners_changed.emit()
        self.update()

    def image_rect(self) -> QRectF:
        image = self.preview if self.preview is not None else self.photo
        inset = _HANDLE_RADIUS + 4
        area = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        fitted = QSizeF(image.size()).scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
        rect = QRectF(QPointF(), fitted)
        rect.moveCenter(area.center())
        return rect

    def display_point(self, point: QPointF) -> QPointF:
        rect = self.image_rect()
        return QPointF(
            rect.left() + point.x() * rect.width() / self.photo.width(),
            rect.top() + point.y() * rect.height() / self.photo.height(),
        )

    def _source_point(self, point: QPointF) -> QPointF:
        rect = self.image_rect()
        return QPointF(
            (point.x() - rect.left()) * self.photo.width() / rect.width(),
            (point.y() - rect.top()) * self.photo.height() / rect.height(),
        )

    def _corner_at(self, position: QPointF) -> int | None:
        hit_radius = _HANDLE_RADIUS + 2 if len(self.corners) == 4 else 6
        for i, point in enumerate(self.corners):
            delta = self.display_point(point) - position
            if hypot(delta.x(), delta.y()) <= hit_radius:
                return i
        return None

    def cursor_shape_at(self, position: QPointF | None) -> Qt.CursorShape:
        """Pointing hand where a click places or moves a corner, open hand on a
        draggable corner, closed hand while dragging, arrow elsewhere."""
        if self.preview is not None:
            return Qt.CursorShape.ArrowCursor
        if self._dragging:
            return Qt.CursorShape.ClosedHandCursor
        if position is None:
            return Qt.CursorShape.PointingHandCursor
        if self._corner_at(position) is not None:
            return Qt.CursorShape.OpenHandCursor
        if self.image_rect().contains(position) and (len(self.corners) < 4 or self._selected is not None):
            return Qt.CursorShape.PointingHandCursor
        return Qt.CursorShape.ArrowCursor

    def _update_cursor(self, position: QPointF | None):
        self.setCursor(self.cursor_shape_at(position))

    def _move_corner(self, point: QPointF):
        self.corners[self._selected] = QPointF(
            max(0, min(point.x(), self.photo.width())),
            max(0, min(point.y(), self.photo.height())),
        )
        self.corners_changed.emit()
        self.update()

    def paintEvent(self, event):
        image = self.preview if self.preview is not None else self.photo
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(self.image_rect(), image, QRectF(image.rect()))
            if self.preview is not None or not self.corners:
                return

            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            points = [self.display_point(corner) for corner in self.corners]
            outline = QPolygonF(points + points[:1] if len(points) == 4 else points)
            for color, width in ((qcolor("INK"), 3), (qcolor("PANEL"), 1.5)):
                painter.setPen(QPen(color, width))
                painter.drawPolyline(outline)
            for i, point in enumerate(points):
                painter.setPen(QPen(qcolor("PANEL"), 2))
                painter.setBrush(qcolor("ACCENT_DEEP" if i == self._selected else "INK"))
                painter.drawEllipse(point, _HANDLE_RADIUS, _HANDLE_RADIUS)
                bounds = QRectF(point.x() - _HANDLE_RADIUS, point.y() - _HANDLE_RADIUS,
                                2 * _HANDLE_RADIUS, 2 * _HANDLE_RADIUS)
                painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, str(i + 1))

    def mousePressEvent(self, event):
        if self.preview is not None or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        position = event.position()
        self.setFocus()
        hit = self._corner_at(position)
        if hit is not None:
            self._selected = hit
            self._dragging = True
            self._update_cursor(position)
            self.update()
            return
        if self.image_rect().contains(position):
            if len(self.corners) < 4:
                self._selected = len(self.corners)
                self.corners.append(self._source_point(position))
                self.corners_changed.emit()
                self.update()
            elif self._selected is not None:
                self._move_corner(self._source_point(position))
            self._update_cursor(position)

    def event(self, event):
        if event.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverMove):
            self._update_cursor(event.position())
        elif event.type() == QEvent.Type.HoverLeave:
            self._update_cursor(None)
        return super().event(event)

    def enterEvent(self, event):
        super().enterEvent(event)
        position = event.position() if hasattr(event, "position") else None
        self._update_cursor(position)

    def showEvent(self, event):
        super().showEvent(event)
        self._update_cursor(None)

    def mouseMoveEvent(self, event):
        if self._dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self._move_corner(self._source_point(event.position()))
        else:
            super().mouseMoveEvent(event)
        self._update_cursor(event.position())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
        super().mouseReleaseEvent(event)
        self._update_cursor(event.position())

    def keyPressEvent(self, event):
        direction = _ARROWS.get(event.key())
        if self.preview is None and self._selected is not None and direction is not None:
            step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            self._move_corner(self.corners[self._selected] + QPointF(direction[0] * step, direction[1] * step))
            event.accept()
        else:
            super().keyPressEvent(event)


class PhotoCropDialog(QDialog):
    preview_ready = Signal(int, int)

    def __init__(self, photo: QImage, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("四角裁剪")
        self.resize(760, 650)
        self.selected_image: QImage | None = None
        self.canvas = PhotoCropCanvas(photo, self)
        self.hint = label("", "value")   # the step to do now, e.g. which corner is next

        choices = QDialogButtonBox(self)
        self.adjust_button = choices.addButton("重选四角", QDialogButtonBox.ButtonRole.ResetRole)
        self.crop_button = choices.addButton("预览裁剪", QDialogButtonBox.ButtonRole.AcceptRole)
        self.original_button = choices.addButton("使用原图", QDialogButtonBox.ButtonRole.ActionRole)
        self.crop_button.setDefault(True)
        self.original_button.setAutoDefault(False)
        self.adjust_button.setAutoDefault(False)
        set_role(self.crop_button, "primary")

        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(choices)

        self.canvas.corners_changed.connect(self._refresh)
        self.adjust_button.clicked.connect(self._adjust)
        self.crop_button.clicked.connect(self._crop)
        self.original_button.clicked.connect(self._use_original)
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.activateWindow()
        self.canvas.setFocus()

    def _refresh(self):
        previewing = self.canvas.preview is not None
        count = len(self.canvas.corners)
        valid = not photo_crop_size(self.canvas.photo, self.canvas.corners).isEmpty()
        self.adjust_button.setEnabled(count > 0)
        self.adjust_button.setText("调整四角" if previewing else "重选四角")
        self.crop_button.setText("使用裁剪" if previewing else "预览裁剪")
        self.crop_button.setEnabled(previewing or valid)
        if previewing:
            message = "确认拉平后的照片。"
        elif count < 4:
            message = f"以遥控器自身朝向为准，点选外框的{_CORNER_NAMES[count]}（{count + 1}/4）。"
        elif valid:
            message = "四角已选定。拖动圆点、点选圆点后点新位置，或按方向键微调。"
        else:
            message = "四角需依次围成一个凸四边形。请调整圆点或重选。"
        self.hint.setText(message)

    def _adjust(self):
        if self.canvas.preview is None:
            self.canvas.reset()
        else:
            self.canvas.set_preview(None)
            self._refresh()

    def _crop(self):
        if self.canvas.preview is not None:
            self.selected_image = self.canvas.preview
            self.accept()
            return
        preview = rectify_photo(self.canvas.photo, self.canvas.corners)
        if preview is None:
            self.hint.setText("无法拉平这组四角，请调整圆点或使用原图。")
            return
        self.canvas.set_preview(preview)
        self._refresh()
        self.preview_ready.emit(preview.width(), preview.height())

    def _use_original(self):
        self.selected_image = self.canvas.photo
        self.accept()
