"""Photo workspace for learning the keys of a selected probe device."""
from __future__ import annotations

import base64
from dataclasses import replace
import json
import logging
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QTimer, Signal
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLineEdit, QScrollArea, QSizePolicy,
    QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from ..canvas import RemoteCanvas
from ..key_roles import (
    KEYPAD_ROLES, NAVIGATION_ROLES, ROLE_NAMES, ZONE_FUNCTION, ZONE_KEYPAD, ZONE_NAVIGATION, ZONE_VOICE,
    friendly_name_from_trigger, infer_role_from_trigger, zone_for_role,
)
from ..model import MAX_EXTRA_TRIGGERS, Button, Profile
from ..photo_template import television_profile
from .widgets import button, label, row


_LOGGER = logging.getLogger(__name__)
NO_KEYS_FOUND = "没能自动找到按键，请手动框选：点击照片上的按键位置，再按一下实体键。"
SILENT_KEY_TIP = ("点照片上你想用的键即可（电源键、空中鼠标等不发普通按键信号的键无需学习）；"
                  "如常用键按了也没反应，可点「复制给 AI 的求助」排查。")
ALL_FRAMES_LEARNED = "自动找到的按键框都已学完。漏掉的键可以点照片补框，然后保存遥控器。"
MAX_KEYS = 128


def _log_photo_import(event, **fields):
    if _LOGGER.isEnabledFor(logging.INFO):
        _LOGGER.info("%s", json.dumps({"event": event, **fields}))


def _overlaps(rect, other):
    x, y, w, h = rect
    cx, cy = x + w / 2, y + h / 2
    ox, oy, ow, oh = other
    return ox <= cx <= ox + ow and oy <= cy <= oy + oh


def _frame_at(x, y, width, height):
    return [
        max(0., min(x - width / 2, 1 - width)),
        max(0., min(y - height / 2, 1 - height)),
        width, height,
    ]


class LearningCanvas(RemoteCanvas):
    position_picked = Signal(list)
    geometry_edited = Signal(str, list)

    def __init__(self):
        super().__init__()
        self.drag = None
        self.dragging = False
        self.size_controls = QWidget(self)
        controls = QVBoxLayout(self.size_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(4)
        self.enlarge_button = self._size_button("＋", "放大高亮框", 1.2)
        self.shrink_button = self._size_button("−", "缩小高亮框", 1 / 1.2)
        controls.addWidget(self.enlarge_button)
        controls.addWidget(self.shrink_button)
        self.size_controls.hide()

    def _size_button(self, text, name, factor):
        control = QToolButton(self.size_controls)
        control.setText(text)
        control.setToolTip(name)
        control.setAccessibleName(name)
        control.setFixedSize(28, 28)
        control.clicked.connect(lambda: self.resize_selected(factor))
        return control

    def _button_highlighted(self, button):
        return bool(button.all_triggers)

    def _hotspot_at(self, point):
        return next((key for key in reversed(self.profile.buttons) if self.hotspot_rect(key).contains(point)), None)

    def _selected_key(self):
        if self.profile is not None:
            return next((key for key in self.profile.buttons if key.id == self.selection), None)
        return None

    def layout_callouts(self):
        super().layout_callouts()
        key = self._selected_key()
        if key is None or self.pixmap.isNull():
            self.size_controls.hide()
            return
        area = self.image_rect()
        hotspot = self.hotspot_rect(key)
        callout = self.callouts[key.id]
        self.size_controls.adjustSize()
        if callout.geometry().center().x() < area.center().x():
            x = area.left() - self.size_controls.width() - 6
        else:
            x = area.right() + 6
        y = max(4, min(round(hotspot.center().y() - self.size_controls.height() / 2),
                       self.height() - self.size_controls.height() - 4))
        self.size_controls.move(round(x), y)
        self.size_controls.show()
        self.size_controls.raise_()

    def resize_selected(self, factor):
        key = self._selected_key()
        if key is None:
            return
        left, top, width, height = key.rect
        scale = min(max(factor, .01 / min(width, height)), 1. / max(width, height))
        self.geometry_edited.emit(key.id, _frame_at(
            left + width / 2, top + height / 2, width * scale, height * scale
        ))

    def mousePressEvent(self, event):
        self.drag = None
        self.dragging = False
        area = self.image_rect()
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self.pixmap.isNull()
            and area.contains(event.position())
        ):
            key = self._hotspot_at(event.position())
            if key is None:
                x, y = self._normalized(event.position())
                self.position_picked.emit(_frame_at(x, y, min(.25, 22 / area.width()), min(.25, 22 / area.height())))
                self.setFocus()
            else:
                self.selected.emit(key.id)
                if key.id == self.selection:
                    self.drag = (key.id, event.position(), tuple(key.rect))
                    self.setFocus()
            event.accept()
            return
        super().mousePressEvent(event)

    def drag_to(self, point):
        if self.drag is None:
            return False
        identity, origin, (left, top, width, height) = self.drag
        delta = point - origin
        if not self.dragging and delta.manhattanLength() < QApplication.startDragDistance():
            return False
        self.dragging = True
        area = self.image_rect()
        self.geometry_edited.emit(identity, _frame_at(
            left + width / 2 + delta.x() / area.width(),
            top + height / 2 + delta.y() / area.height(),
            width, height,
        ))
        return True

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self.drag_to(event.position()):
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mouseMoveEvent(event)
        if not self.pixmap.isNull() and self.image_rect().contains(event.position()):
            cursor = Qt.CursorShape.OpenHandCursor if self._hotspot_at(event.position()) else Qt.CursorShape.CrossCursor
            self.setCursor(cursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.drag is not None:
            self.drag_to(event.position())
            self.drag = None
            self.dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ProbePhotoPane(QWidget):
    image_changed = Signal()
    failed = Signal(str)
    selection_changed = Signal()
    # Ask the dialog to learn this existing frame; an empty id stops learning.
    learn_requested = Signal(str)
    help_requested = Signal()       # the dialog copies an AI help request

    def __init__(self, device=None, *, preview=False, profile=None, embed_controls: bool = True):
        super().__init__()
        self._embed_controls = bool(embed_controls)
        self.device = device
        self.profile = profile if profile is not None else Profile(
            name=(device.default_remote_name if device is not None else "遥控器"),
            vendor=device.vendor if device is not None else 0,
            product=device.product if device is not None else 0,
        )
        self.pending: Button | None = None
        self._pending_role_locked = False
        self.selected = ""
        self._active_zone = ZONE_FUNCTION
        self.dirty = False
        self._shown_key_id = ""
        self.has_microphone: bool | None = None
        self.marking_voice_key: bool = False
        self.mic_access = None  # () -> plain-language microphone level, set by the add dialog
        # Optional frames placed by automatic detection (kept for compatibility).
        self.auto_ids: set[str] = set()
        self._queued = None
        self._queue = QTimer(self)
        self._queue.setSingleShot(True)
        self._queue.setInterval(0)
        self._queue.timeout.connect(self._send_queued)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.photo_button = button("导入照片", self.choose_photo)
        self.photo_button.setAutoDefault(False)
        self.example_button = button("使用示例照片", self.use_example) if preview else None
        photo_controls = [self.photo_button]
        if self.example_button is not None:
            self.example_button.setAutoDefault(False)
            photo_controls.append(self.example_button)
        self.count = label("", "muted", wrap=False)
        layout.addWidget(row(label("遥控器照片", "section"), self.count, None, *photo_controls))

        # Step 1 after importing a new photo: ask whether the remote has a physical microphone/voice key.
        self.mic_step_card = QFrame()
        self.mic_step_card.setObjectName("deviceCard")
        mic_layout = QVBoxLayout(self.mic_step_card)
        mic_layout.setContentsMargins(10, 8, 10, 8)
        mic_layout.setSpacing(6)
        self.mic_step_label = label("第 1 步：这只遥控器物理上带有麦克风（语音键）吗？", "value")
        self.mic_yes_button = button("有，先标语音键位置", self.start_voice_mark, "primary")
        self.mic_no_button = button("没有，直接学按键", self.skip_voice_mark)
        for ctrl in (self.mic_yes_button, self.mic_no_button):
            ctrl.setAutoDefault(False)
        mic_layout.addWidget(self.mic_step_label)
        mic_layout.addWidget(row(self.mic_yes_button, self.mic_no_button, None))
        self.mic_step_card.hide()
        if self._embed_controls:
            layout.addWidget(self.mic_step_card)

        self.placeholder = label("导入一张遥控器正面照片，开始学习按键。", "muted")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.canvas = LearningCanvas()
        self.canvas.setAccessibleName("遥控器照片。点击学习实体按键，拖动高亮框可以调整位置。")
        self.canvas.selected.connect(self.select)
        self.canvas.geometry_edited.connect(self.set_rect)
        self.workbench = QScrollArea()
        self.workbench.setFrameShape(QFrame.Shape.NoFrame)
        self.workbench.setWidgetResizable(True)
        self.workbench.setWidget(self.canvas)
        self.stage = QStackedWidget()
        self.stage.setMinimumSize(420, 520)
        self.stage.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.stage.addWidget(self.placeholder)
        self.stage.addWidget(self.workbench)
        layout.addWidget(self.stage, 1)

        # Always-on tips while learning: nothing can tell that a key sent nothing.
        self.found_tip = label("", "caption")
        self.silent_tip = label(SILENT_KEY_TIP, "caption")
        self.help_button = button("复制给 AI 的求助", self.help_requested.emit, "link")
        self.help_button.setAutoDefault(False)
        self.help_button.hide()           # shown by a dialog that can build the request
        self.tips = QWidget()
        tips = QVBoxLayout(self.tips)
        tips.setContentsMargins(0, 0, 0, 0)
        tips.setSpacing(2)
        tips.addWidget(self.found_tip)
        silent = QHBoxLayout()
        silent.setSpacing(8)
        silent.addWidget(self.silent_tip, 1)
        silent.addWidget(self.help_button, 0, Qt.AlignmentFlag.AlignBottom)
        tips.addLayout(silent)
        self.tips.hide()
        if self._embed_controls:
            layout.addWidget(self.tips)
        self.detected_count = 0

        self.key_name_input = QLineEdit()
        self.key_name_input.setMaxLength(100)
        self.key_name_input.setMinimumWidth(90)
        self.key_name_input.setPlaceholderText("自定义按键名称")
        self.key_name_input.setAccessibleName("选中按键名称")
        self.key_name_input.returnPressed.connect(self._commit_rename)
        self.rename_button = button("改名", self._commit_rename)
        self.rename_button.setAutoDefault(False)
        self.delete_button = button("删除此键", self.delete_selected, "danger")
        self.delete_button.setAutoDefault(False)
        self.skip_button = button("暂时跳过", self.skip_pending, "primary")
        self.skip_button.setAutoDefault(False)
        self.skip_button.hide()

        # Two-step physical classification card: 1) Zone (with examples) -> 2) Key role or custom name.
        self.key_bar = QFrame()
        self.key_bar.setObjectName("deviceCard")
        key_bar_layout = QVBoxLayout(self.key_bar)
        key_bar_layout.setContentsMargins(10, 8, 10, 8)
        key_bar_layout.setSpacing(6)

        zone_grid = QGridLayout()
        zone_grid.setContentsMargins(0, 0, 0, 0)
        zone_grid.setSpacing(6)
        self.zone_buttons: dict[str, object] = {}
        zone_specs = (
            (ZONE_NAVIGATION, "方向区\n上下左右/确定/返回"),
            (ZONE_KEYPAD, "数字区\n0–9 / * / #"),
            (ZONE_VOICE, "语音区\n语音键"),
            (ZONE_FUNCTION, "功能区\n音量/主页/自定义"),
        )
        for idx, (zone_id, text) in enumerate(zone_specs):
            btn = button(text, lambda z=zone_id: self.select_zone(z), "chip")
            btn.setCheckable(True)
            btn.setAutoDefault(False)
            btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            btn.setMinimumHeight(40)
            self.zone_buttons[zone_id] = btn
            if self._embed_controls:
                zone_grid.addWidget(btn, 0, idx)
                zone_grid.setColumnStretch(idx, 1)
            else:
                zone_grid.addWidget(btn, idx // 2, idx % 2)
                zone_grid.setColumnStretch(idx % 2, 1)
        key_bar_layout.addLayout(zone_grid)

        self.role_buttons: dict[str, object] = {}
        self.role_stack = QStackedWidget()
        self._zone_page_index: dict[str, int] = {}

        # Page 0: Navigation roles (6 keys)
        nav_page = QWidget()
        nav_row = QHBoxLayout(nav_page)
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(6)
        for role_id in NAVIGATION_ROLES:
            btn = button(ROLE_NAMES[role_id], lambda r=role_id: self.set_role_selected(r), "chip")
            btn.setCheckable(True)
            btn.setAutoDefault(False)
            self.role_buttons[role_id] = btn
            nav_row.addWidget(btn, 1)
        self._zone_page_index[ZONE_NAVIGATION] = self.role_stack.addWidget(nav_page)

        # Page 1: Keypad roles (2 compact rows x 6 keys)
        pad_page = QWidget()
        pad_grid = QGridLayout(pad_page)
        pad_grid.setContentsMargins(0, 0, 0, 0)
        pad_grid.setSpacing(4)
        for idx, role_id in enumerate(KEYPAD_ROLES):
            btn = button(ROLE_NAMES[role_id], lambda r=role_id: self.set_role_selected(r), "chip")
            btn.setCheckable(True)
            btn.setAutoDefault(False)
            self.role_buttons[role_id] = btn
            pad_grid.addWidget(btn, idx // 6, idx % 6)
        self._zone_page_index[ZONE_KEYPAD] = self.role_stack.addWidget(pad_page)

        # Page 2: Voice zone
        voice_page = QWidget()
        voice_layout = QHBoxLayout(voice_page)
        voice_layout.setContentsMargins(0, 0, 0, 0)
        voice_layout.addWidget(label("已标记为「语音」键（保存后可在「语音设置」里测试语音功能）。", "caption"))
        self._zone_page_index[ZONE_VOICE] = self.role_stack.addWidget(voice_page)

        # Page 3: Function zone (custom name)
        func_page = QWidget()
        func_layout = QHBoxLayout(func_page)
        func_layout.setContentsMargins(0, 0, 0, 0)
        func_layout.addWidget(label("功能键不限类型：按下实体键后，可直接在下方输入自定义名称（常见媒体键也会自动预填）。", "caption"))
        self._zone_page_index[ZONE_FUNCTION] = self.role_stack.addWidget(func_page)
        key_bar_layout.addWidget(self.role_stack)

        edit_row = QHBoxLayout()
        edit_row.setContentsMargins(0, 0, 0, 0)
        edit_row.setSpacing(6)
        edit_row.addWidget(label("键名", "caption", wrap=False))
        edit_row.addWidget(self.key_name_input, 1)
        edit_row.addWidget(self.rename_button)
        edit_row.addWidget(self.skip_button)
        edit_row.addWidget(self.delete_button)
        key_bar_layout.addLayout(edit_row)

        self.key_bar.hide()
        if self._embed_controls:
            layout.addWidget(self.key_bar)
        self.render()

    def choose_photo(self):
        from PySide6.QtWidgets import QMessageBox

        tips = QMessageBox(self)
        tips.setWindowTitle("拍摄建议")
        tips.setText("把遥控器正面朝上平放，手机与桌面平行，从正上方拍摄。尽量让遥控器顶部朝向画面上方，四周留一点空间。")
        proceed = tips.addButton("选择照片", QMessageBox.ButtonRole.AcceptRole)
        tips.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        tips.setDefaultButton(proceed)
        _log_photo_import("shooting_tips_opened")
        tips.exec()
        select_file = tips.clickedButton() is proceed
        tips.deleteLater()
        _log_photo_import("shooting_tips_closed", proceed=select_file)
        if not select_file:
            return
        _log_photo_import("file_picker_opened")
        path, _ = QFileDialog.getOpenFileName(self, "选择遥控器照片", "", "图片 (*.png *.jpg *.jpeg *.webp)")
        _log_photo_import("file_picker_closed", selected=bool(path))
        if path:
            if self.load_photo(path) and not self.profile.buttons and self.has_microphone is None:
                self.ask_microphone_dialog()

    def load_photo(self, path):
        from ..photo_cutout import MAX_PHOTO_EDGE
        from .photo_crop import PhotoCropDialog

        started = perf_counter()
        stage = "file_stat"
        _log_photo_import("import_started")
        try:
            file_bytes = Path(path).stat().st_size
            _log_photo_import("file_checked", file_bytes=file_bytes)
            if file_bytes > 20_000_000:
                stage = "file_too_large"
                raise ValueError("请选择小于 20 MB 的照片。")
            stage = "decode"
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            image = reader.read()
            if image.isNull():
                _log_photo_import("decode_failed", reader_error=reader.error().name)
                raise ValueError("照片无法读取，请换一张 PNG、JPG 或 WebP 照片。")
            _log_photo_import(
                "image_decoded", width=image.width(), height=image.height(), has_alpha=image.hasAlphaChannel(),
            )
            if max(image.width(), image.height()) > MAX_PHOTO_EDGE:
                stage = "resize"
                image = image.scaled(
                    MAX_PHOTO_EDGE, MAX_PHOTO_EDGE,
                    Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
                )
            stage = "corner_selection"
            editor = PhotoCropDialog(image, self)
            editor.preview_ready.connect(
                lambda width, height: _log_photo_import("crop_preview_ready", width=width, height=height)
            )
            _log_photo_import("crop_dialog_opened", width=image.width(), height=image.height())
            try:
                accepted = editor.exec() == editor.DialogCode.Accepted
                selected = editor.selected_image if accepted else None
                corners = [[round(point.x(), 1), round(point.y(), 1)] for point in editor.canvas.corners]
            finally:
                editor.deleteLater()
            if selected is None:
                _log_photo_import("crop_dialog_closed", choice="cancelled", corners=corners)
                return
            source = "original" if selected is image else "manual_crop"
            _log_photo_import("crop_dialog_closed", choice=source, corners=corners)
            image = selected
            stage = "encode"
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer, "PNG"):
                raise ValueError("无法载入这张照片。")
        except Exception as exc:
            _log_photo_import("import_failed", stage=stage, error_type=type(exc).__name__)
            if not isinstance(exc, (OSError, ValueError)):
                raise
            self.failed.emit(str(exc))
            return False
        self.detected_count = 0
        self.set_image(base64.b64encode(bytes(data)).decode("ascii"))
        _log_photo_import(
            "import_completed", source=source, width=image.width(), height=image.height(), png_bytes=data.size(),
            elapsed_ms=round((perf_counter() - started) * 1000),
        )
        return True

    def ask_microphone_dialog(self):
        """Prominent modal step asking whether the physical remote has a microphone/voice key."""
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setWindowTitle("这只遥控器有麦克风吗？")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("请确认你手里的这只遥控器物理上是否带有麦克风（语音键）：")
        box.setInformativeText(
            "• 有麦克风：接下来先在左侧照片上点一下「语音键」的位置完成标记，保存后即可在「语音设置」里配置麦克风。\n"
            "• 没有麦克风：跳过语音键标记，直接开始学习普通按键。"
        )
        yes_btn = box.addButton("有，先标记语音键", QMessageBox.ButtonRole.AcceptRole)
        no_btn = box.addButton("没有麦克风", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes_btn)
        box.exec()
        clicked = box.clickedButton()
        box.deleteLater()
        if clicked is yes_btn:
            self.start_voice_mark()
        elif clicked is no_btn:
            self.skip_voice_mark()

    def use_example(self):
        self.set_image(television_profile().image)

    def place_detected_keys(self, image):
        """Frame the keys found on a newly imported photo, then start learning them.

        Frames left from an earlier automatic pass that were never learned belong to
        the old photo and are replaced. New frames are not placed over existing keys.
        Without usable frames the user frames keys by hand as before.
        """
        from ..photo_hotspots import detect_hotspots

        stale = {key.id for key in self.profile.buttons if key.id in self.auto_ids and not key.trigger}
        self.profile.buttons = [key for key in self.profile.buttons if key.id not in stale]
        self.auto_ids -= stale
        if self.selected in stale:
            self.selected = ""
        started = perf_counter()
        try:
            spots = detect_hotspots(image)
        except (ValueError, MemoryError) as exc:
            _log_photo_import("hotspots_failed", error_type=type(exc).__name__)
            spots = []
        _log_photo_import("hotspots_detected", count=len(spots), elapsed_ms=round((perf_counter() - started) * 1000))
        added = []
        numbered = sum(1 for key in self.profile.buttons if key.id in self.auto_ids and key.name.startswith("按键 "))
        for spot in spots:
            if len(self.profile.buttons) >= MAX_KEYS:
                break
            if any(_overlaps(spot.rect, key.rect) or _overlaps(key.rect, spot.rect) for key in self.profile.buttons):
                continue
            if spot.name:
                name = spot.name
            else:
                numbered += 1
                name = f"按键 {numbered}"
            key = Button(name=name, rect=[round(value, 4) for value in spot.rect], role=spot.role)
            self.profile.buttons.append(key)
            self.auto_ids.add(key.id)
            added.append(key)
        self.detected_count = len(added)
        if stale or added:
            self.dirty = True
        if added:
            self.selected = added[0].id
        self.render()
        if added:
            self.learn_requested.emit(added[0].id)
        elif not self.profile.buttons:
            self.failed.emit(NO_KEYS_FOUND)
        return len(added)

    def unlearned_frames(self):
        return [key for key in self.profile.buttons if key.id in self.auto_ids and not key.trigger]

    def next_unlearned(self, after=None):
        """The next frame still waiting for a key code, in photo order after ``after``."""
        waiting = self.unlearned_frames()
        if after is None or not waiting:
            return waiting[0].id if waiting else ""
        order = [key.id for key in self.profile.buttons]
        start = order.index(after) if after in order else -1
        later = [key for key in waiting if order.index(key.id) > start and key.id != after]
        candidates = later or [key for key in waiting if key.id != after]
        return candidates[0].id if candidates else ""

    def learning_frame(self):
        """True while an existing (automatically placed) frame waits for its key."""
        return self.pending is not None and any(key is self.pending for key in self.profile.buttons)

    def capture_prompt(self):
        if self.learning_frame() and self.pending.role == "voice":
            return ("请按一下遥控器上的「语音」键再松开，记下它的键码，保存后在「语音」页用它触发识别。"
                    "按了没有反应就点「停止学习」跳过，之后也能在「语音」页另选触发键。")
        if self.learning_frame():
            left = len(self.unlearned_frames())
            return (f"按这颗键：「{self.pending.name}」。按一下实体键再松开（待学 {left} 个框）。"
                    "框的位置不对可以拖动或缩放；不是按键就删除，暂时不学就跳过。")
        if self.pending is not None and self.pending.role:
            return f"已选好位置（「{self.pending.name}」）。请按一下对应的实体键再松开。"
        return "已选好位置。请按一下对应的实体键再松开（也可先在下方选好分区和键位再按键）。"

    def saved_profile(self):
        """Copy of the profile to save: automatic frames never learned are left out."""
        from copy import deepcopy

        waiting = {key.id for key in self.unlearned_frames()}
        profile = deepcopy(self.profile)
        profile.buttons = [key for key in profile.buttons if key.id not in waiting]
        return profile

    def skip_pending(self):
        if not self.learning_frame():
            return
        self.learn_requested.emit(self.next_unlearned(after=self.pending.id))

    def _queue_learning(self, identity):
        self._queued = identity
        self._queue.start()

    def _send_queued(self):
        identity, self._queued = self._queued, None
        if identity:
            self.learn_requested.emit(identity)
        elif identity == "":
            self.failed.emit(ALL_FRAMES_LEARNED)

    def start_voice_mark(self):
        """User confirmed the physical remote has a microphone: mark the voice button on the photo first."""
        self.has_microphone = True
        self.marking_voice_key = True
        text = "请直接点击照片上「语音键（麦克风）」的位置（点一下即可，无需按遥控器）。"
        access = self.mic_access() if callable(self.mic_access) else ""
        self.mic_step_label.setText(f"{text}\n{access}" if access else text)
        self.mic_yes_button.hide()
        self.mic_no_button.setText("先跳过，直接学按键")
        self.mic_step_card.show()

    def skip_voice_mark(self):
        if self.has_microphone is None:
            self.has_microphone = False
        self.marking_voice_key = False
        self.mic_step_card.hide()

    def place_voice_marker(self, rect) -> Button:
        """Mark the physical voice key's rect on the photo without requiring a HID trigger."""
        self.has_microphone = True
        self.marking_voice_key = False
        self.mic_step_card.hide()
        existing = next((b for b in self.profile.buttons if b.role == "voice"), None)
        if existing is not None:
            existing.rect = [round(float(v), 4) for v in rect]
            target = existing
        else:
            target = Button(name="语音", rect=[round(float(v), 4) for v in rect], role="voice")
            self.profile.buttons.append(target)
        self.selected = target.id
        self._active_zone = ZONE_VOICE
        self.dirty = True
        self.render()
        return target

    def set_image(self, image):
        self.profile.image = image
        self.dirty = True
        self.marking_voice_key = False
        self.mic_step_card.hide()
        self.image_changed.emit()
        self.render()

    def set_capturing(self, active):
        self.photo_button.setEnabled(not active)
        if self.example_button is not None:
            self.example_button.setEnabled(not active)
        self.key_name_input.setEnabled(not active)
        self.rename_button.setEnabled(not active)
        # Zone & role buttons stay clickable even while waiting for a physical key,
        # so users can pick Zone -> Key either before or after pressing the remote.
        allow_classify = self._editable_button() is not None
        for btn in self.zone_buttons.values():
            btn.setEnabled(allow_classify)
        for btn in self.role_buttons.values():
            btn.setEnabled(allow_classify)
        # Deleting a wrongly placed position never requires stopping learning first.
        self.delete_button.setEnabled(self._editable_button() is not None)

    def mark_clean(self):
        self.dirty = False

    def selected_button(self):
        return next((key for key in self.profile.buttons if key.id == self.selected), None)

    def _editable_button(self):
        return self.pending if self.pending is not None else self.selected_button()

    def _commit_rename(self):
        self.rename_selected(self.key_name_input.text())

    def rename_selected(self, name):
        target = self._editable_button()
        if target is None:
            return False
        cleaned = name.strip() if isinstance(name, str) else ""
        if not cleaned or len(cleaned) > 100:
            self.failed.emit("按键名称应为 1–100 字。")
            self.key_name_input.setText(target.name)
            return False
        if cleaned == target.name:
            return True
        target.name = cleaned
        self.dirty = True
        self.render()
        return True

    def select_zone(self, zone: str) -> None:
        target = self._editable_button()
        if target is None or zone not in self._zone_page_index:
            return
        self._active_zone = zone
        if zone == ZONE_VOICE:
            self.set_role_selected("voice")
            return
        if zone == ZONE_FUNCTION:
            if target.role:
                self.set_role_selected("")
            elif target is self.pending:
                self._pending_role_locked = True
                self.render()
            else:
                self.render()
            if self.key_name_input.isEnabled():
                self.key_name_input.setFocus()
                self.key_name_input.selectAll()
            return
        self.render()

    def set_role_selected(self, role: str) -> bool:
        target = self._editable_button()
        if target is None:
            return False
        changed = role != target.role
        if role:
            for other in self.profile.buttons:
                if other is not target and other.role == role:
                    other.role = ""
        old_role_name = ROLE_NAMES.get(target.role, "")
        target.role = role
        if role and (
            target.name in ("等待按键", "")
            or target.name.startswith("按键 ")
            or (old_role_name and target.name == old_role_name)
        ):
            target.name = ROLE_NAMES[role]
        self._active_zone = zone_for_role(role)
        if target is self.pending:
            self._pending_role_locked = True
        else:
            if changed:
                self.dirty = True
        self.render()
        return changed or target is self.pending

    def delete_selected(self):
        if self.pending is not None and not self.learning_frame():
            # A new position still waiting for its key: dropping it ends this wait.
            self.pending = None
            self._pending_role_locked = False
            self.render()
            self.learn_requested.emit("")
            return True
        target = self._editable_button()
        idx = next((i for i, key in enumerate(self.profile.buttons) if target is not None and key.id == target.id), None)
        if idx is None:
            return False
        self.selected = target.id
        was_learning = self.learning_frame() and self.pending.id == self.selected
        following = self.next_unlearned(after=self.selected) if was_learning else ""
        removed = self.profile.buttons.pop(idx)
        self.auto_ids.discard(removed.id)
        for layer in getattr(self.profile, "app_layers", ()):
            layer.overrides.pop(removed.id, None)
        if was_learning:
            self.pending = None
        if self.profile.buttons:
            self.selected = self.profile.buttons[min(idx, len(self.profile.buttons) - 1)].id
        else:
            self.selected = ""
        self.dirty = True
        self.render()
        if was_learning:
            self.learn_requested.emit(following)
        return True

    def select(self, identity):
        key = next((key for key in self.profile.buttons if key.id == identity), None)
        if key is not None and key.id in self.auto_ids and not key.trigger:
            # Clicking a frame that still waits for its key learns that frame.
            if self.pending is None:
                self.selected = identity
                self.render()
            if self.pending is None or self.pending.id != identity:
                self.learn_requested.emit(identity)
            return
        if self.pending is None:
            self.selected = identity
            if key is not None:
                self._active_zone = zone_for_role(key.role)
            self.render()

    def set_rect(self, identity, rect):
        if self.pending is not None and self.pending.id == identity:
            target = self.pending
        else:
            target = next((key for key in self.profile.buttons if key.id == identity), None)
        if target is not None and target.rect != rect:
            target.rect = rect
            if target is not self.pending or self.learning_frame():
                self.dirty = True
            self.canvas.layout_callouts()
            self.canvas.update()

    def begin(self, rect):
        """Wait for a key: on a new frame at ``rect``, or on an existing frame by its id."""
        self._pending_role_locked = False
        if isinstance(rect, str):
            target = next((key for key in self.profile.buttons if key.id == rect), None)
            if target is None or target.trigger:
                return False
            # The 「语音」 mark keeps its role whatever key code the voice key sends.
            self._pending_role_locked = target.role == "voice"
            self.pending = target
            self.selected = target.id
            self._active_zone = zone_for_role(target.role)
            self.render()
            return True
        if len(self.profile.buttons) >= MAX_KEYS:
            self.failed.emit("每台遥控器最多学习 128 个按键。")
            return False
        self.pending = Button(name="等待按键", rect=rect)
        self._active_zone = ZONE_NAVIGATION
        self.render()
        return True

    def cancel(self):
        if self.pending is not None:
            self.pending = None
            self._pending_role_locked = False
            sel = self.selected_button()
            if sel is not None:
                self._active_zone = zone_for_role(sel.role)
            self.render()

    def _apply_learned_identity(self, bound: Button, trigger: str, *, auto_frame: bool):
        inferred_role = infer_role_from_trigger(trigger)
        friendly = friendly_name_from_trigger(trigger)
        if self._pending_role_locked:
            # User explicitly chose a zone/role before pressing the physical key.
            if bound.role:
                for other in self.profile.buttons:
                    if other is not bound and other.role == bound.role:
                        other.role = ""
                if bound.name in ("等待按键", "") or bound.name.startswith("按键 "):
                    bound.name = ROLE_NAMES[bound.role]
            elif bound.name in ("等待按键", "") or bound.name.startswith("按键 "):
                if friendly:
                    bound.name = friendly
                else:
                    bound.name = f"按键 {len(self.profile.buttons) + 1} · {trigger}"[:100]
            self._active_zone = zone_for_role(bound.role)
            return
        if inferred_role:
            for other in self.profile.buttons:
                if other is not bound and other.role == inferred_role:
                    other.role = ""
            previous_role_name = ROLE_NAMES.get(bound.role, "")
            bound.role = inferred_role
            if (
                bound.name == "等待按键"
                or bound.name.startswith("按键 ")
                or (auto_frame and previous_role_name and bound.name == previous_role_name)
            ):
                bound.name = ROLE_NAMES[inferred_role]
        elif bound.name == "等待按键" or bound.name.startswith("按键 "):
            if friendly:
                bound.name = friendly
            elif auto_frame:
                bound.name = f"{bound.name} · {trigger}"[:100]
            else:
                bound.name = f"按键 {len(self.profile.buttons) + 1} · {trigger}"[:100]
        self._active_zone = zone_for_role(bound.role)

    def bind(self, trigger):
        existing = next((button for button in self.profile.buttons if button.matches_trigger(trigger)), None)
        bound = None
        if existing is not None:
            self.selected = existing.id
            self._active_zone = zone_for_role(existing.role)
        elif self.learning_frame():
            bound = self.pending
            self._apply_learned_identity(bound, trigger, auto_frame=True)
            bound.trigger = trigger
        else:
            bound = self.pending
            self._apply_learned_identity(bound, trigger, auto_frame=False)
            bound.trigger = trigger
            self.profile.buttons.append(bound)
        if bound is not None:
            self.selected = bound.id
            self.dirty = True
        was_function_locked = self._pending_role_locked and bound is not None and not bound.role
        self.pending = None
        self._pending_role_locked = False
        self.render()
        if was_function_locked and self.key_name_input.isEnabled() and not self.auto_ids:
            self.key_name_input.setFocus()
            self.key_name_input.selectAll()
        if bound is not None and self.auto_ids:
            # Highlight the next frame still waiting, once the dialog has finished this sample.
            self._queue_learning(self.next_unlearned(after=bound.id))
        return existing is None

    def add_extra_trigger(self, button_id: str, trigger: str) -> bool:
        """Bind an additional hardware trigger code to an existing learned button."""
        target = next((button for button in self.profile.buttons if button.id == button_id), None)
        if target is None or not trigger:
            return False
        existing = next((button for button in self.profile.buttons if button.matches_trigger(trigger)), None)
        if existing is not None:
            self.selected = existing.id
            self.render()
            return existing.id == target.id
        if not target.trigger:
            target.trigger = trigger
        else:
            if len(target.extra_triggers) >= MAX_EXTRA_TRIGGERS:
                self.failed.emit(f"单颗按键最多记录 {MAX_EXTRA_TRIGGERS + 1} 个键码。")
                return False
            target.extra_triggers.append(trigger)
        self.selected = target.id
        self.dirty = True
        self.render()
        return True

    def render(self):
        has_image = bool(self.profile.image)
        self.stage.setCurrentWidget(self.workbench if has_image else self.placeholder)
        self.photo_button.setText("更换照片" if has_image else "导入照片")
        waiting = len(self.unlearned_frames())
        learned = len(self.profile.buttons) - waiting
        self.count.setText(f"已学习 {learned} 键" + (f" · 待学 {waiting} 框" if waiting else ""))
        learning_frame = self.learning_frame()
        self.skip_button.setVisible(learning_frame)
        self.tips.setVisible(has_image)
        self.found_tip.setText(
            f"自动找到 {self.detected_count} 个按键。如果有按键没被框出来，点照片上那个按键的位置就能继续添加。"
            if self.detected_count else "点照片上按键的位置，选好分区与键位（或直接按实体键自动识别），就能添加这颗键。")
        selected_btn = self._editable_button()
        self.key_bar.setVisible(selected_btn is not None)
        if selected_btn is not None:
            if selected_btn.id != self._shown_key_id or not self.key_name_input.hasFocus():
                shown_name = "" if selected_btn.name == "等待按键" else selected_btn.name
                self.key_name_input.setText(shown_name)
            self._shown_key_id = selected_btn.id
            taken_roles = {
                b.role for b in self.profile.buttons
                if b is not selected_btn and b.role
            }
            has_voice_marker = any(b.role == "voice" for b in self.profile.buttons)
            # A placed voice key shows only its status; a new position may still change its zone.
            is_voice_key = selected_btn.role == "voice" and any(b is selected_btn for b in self.profile.buttons)
            # The voice key is marked in the first step; the zone stays only as a fallback for
            # remotes whose microphone question was never answered or was skipped.
            hide_voice_zone = self.has_microphone is False or has_voice_marker
            for zone_id, zbtn in self.zone_buttons.items():
                zbtn.setVisible(not is_voice_key and not (zone_id == ZONE_VOICE and hide_voice_zone))
                zbtn.setChecked(zone_id == self._active_zone)
                zbtn.setEnabled(True)
            zone = ZONE_VOICE if is_voice_key else self._active_zone
            self.role_stack.setCurrentIndex(self._zone_page_index[zone])
            for role_id, rbtn in self.role_buttons.items():
                rbtn.setChecked(selected_btn.role == role_id)
                rbtn.setText(f"{ROLE_NAMES[role_id]} ✓" if role_id in taken_roles else ROLE_NAMES[role_id])
                rbtn.setEnabled(True)
        else:
            self._shown_key_id = ""
            self.key_name_input.clear()
        profile = self.profile
        selection = self.selected
        if self.pending is not None:
            if not learning_frame:
                profile = replace(profile, buttons=[*profile.buttons, self.pending])
            selection = self.pending.id
        self.canvas.set_profile(profile, selection)
        self.selection_changed.emit()
