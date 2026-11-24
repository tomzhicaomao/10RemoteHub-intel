"""Candidate and microphone displays that leave the active application focused."""
from __future__ import annotations

import math
import sys

from PySide6.QtCore import QPoint, QRect, QRunnable, QSignalBlocker, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QSpinBox, QVBoxLayout, QWidget,
)

from ..input_anchor import InputAnchor, focused_input_anchor
from ..input_overlay_preferences import CORNERS, MIN_OPACITY, POSITIONS
from ..nine_key import KEY_LABELS, MODE_LABELS
from ..theme import qcolor, repolish
from .osd import NonActivatingOverlay
from .voice import combo, form

EDGE_MARGIN = 8
CANDIDATES_PER_PAGE = 4

KEY_SIZE = (46, 40)


def overlay_position(available: QRect, width: int, height: int, position: str,
                     fallback: str, target: QRect | None = None, caret: QRect | None = None) -> QPoint:
    area = available.adjusted(EDGE_MARGIN, EDGE_MARGIN, -EDGE_MARGIN, -EDGE_MARGIN)
    right = max(area.left(), area.right() - width + 1)
    bottom = max(area.top(), area.bottom() - height + 1)
    if position == "input":
        reference = target if target is not None and target.height() <= 110 else caret or target
        if reference is not None and available.intersects(reference):
            x = min(max((caret or reference).left(), area.left()), right)
            below = reference.bottom() + EDGE_MARGIN
            above = reference.top() - EDGE_MARGIN - height
            if below <= bottom:
                return QPoint(x, max(area.top(), below))
            if above >= area.top():
                return QPoint(x, above)
        position = fallback
    return QPoint(right if position.endswith("right") else area.left(),
                  bottom if position.startswith("bottom") else area.top())


def _qt_bounds(bounds):
    if bounds is None or not all(math.isfinite(value) for value in bounds):
        return None
    x, y, width, height = bounds
    if width <= 0 or height <= 0:
        return None
    if sys.platform == "win32":
        # Qt scales the dimensions of Windows screens while keeping their
        # desktop origins. UI Automation reports physical screen pixels.
        for screen in QGuiApplication.screens():
            rect = screen.geometry()
            scale = screen.devicePixelRatio()
            if (rect.left() <= x + width / 2 < rect.left() + rect.width() * scale
                    and rect.top() <= y + height / 2 < rect.top() + rect.height() * scale):
                x = rect.left() + (x - rect.left()) / scale
                y = rect.top() + (y - rect.top()) / scale
                width, height = width / scale, height / scale
                break
    return QRect(round(x), round(y), max(1, round(width)), max(1, round(height)))


class _ReadInputAnchor(QRunnable):
    def __init__(self, ticket, completed):
        super().__init__()
        self.ticket = ticket
        self.completed = completed

    def run(self):
        anchor = focused_input_anchor()
        try:
            self.completed.emit(self.ticket, anchor)
        except RuntimeError:
            pass  # Overlay closed while a native accessibility call was pending.


def _hud_label(name, text=""):
    """Styled by the theme stylesheet (QLabel#hud*), like the other overlays."""
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _highlight(widget, selected):
    if widget.property("selected") != selected:
        widget.setProperty("selected", selected)
        repolish(widget)
        for child in widget.findChildren(QLabel):
            repolish(child)


def _keycap(digit, letters):
    """A remote key: the digit, and under it what the key types."""
    key = QFrame()
    key.setObjectName("nineKey")
    key.setFixedSize(*KEY_SIZE)
    box = QVBoxLayout(key)
    box.setContentsMargins(0, 3, 0, 3)
    box.setSpacing(0)
    for name, text in (("nineKeyDigit", digit), ("nineKeyLetters", letters)):
        part = _hud_label(name, text)
        part.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(part)
    key.setAccessibleName(f"{digit} {letters}")
    return key


def _header(title):
    """Accent bar, title and the mode pill, as on the other overlay cards."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    accent = QFrame()
    accent.setObjectName("osdAccent")
    heading = _hud_label("hudTitle", title)
    mode = _hud_label("hudMode")
    row.addWidget(accent, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(heading, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(mode, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch(1)
    return row, heading, mode


class InputStatusOverlay(NonActivatingOverlay):
    _anchor_ready = Signal(int, object)
    ANCHOR_DEADLINE_MS = 180
    minimum_width = 180
    minimum_height = 62

    def __init__(self, title, controller, *, animated):
        super().__init__(title, animated=animated, click_through=True)
        self.c = controller
        self.card.setObjectName("inputHudCard")
        self._ticket = 0
        self._anchor_busy = False
        self._waiting = QTimer(self)
        self._waiting.setSingleShot(True)
        self._waiting.timeout.connect(self._show_at_fallback)
        self._anchor_ready.connect(self._receive_anchor, Qt.ConnectionType.QueuedConnection)
        self._placement = None
        self._field = self._caret = None

    def _anchor_point(self, available, width, height):
        position, fallback = self._placement
        return overlay_position(available, width, height, position, fallback, self._field, self._caret)

    def _apply_opacity(self, opacity):
        panel, edge = qcolor("PANEL"), qcolor("LINE_STRONG")
        alpha = round(255 * opacity / 100)
        style = (f"QFrame#inputHudCard {{ background-color: rgba({panel.red()}, {panel.green()}, {panel.blue()}, {alpha});"
                 f" border: 1px solid {edge.name()}; border-radius: 16px; }}")
        if self.card.styleSheet() != style:
            self.card.setStyleSheet(style)

    def place(self, position, fallback):
        self._ticket += 1
        self._placement = (position, fallback)
        self._field = self._caret = None
        self._waiting.stop()
        if (self._anchor_busy or not self.c.live or self._is_wayland_platform()
                or QGuiApplication.platformName() == "offscreen"):
            self._show_at_fallback()
            return
        self._anchor_busy = QThreadPool.globalInstance().tryStart(_ReadInputAnchor(self._ticket, self._anchor_ready))
        if self._anchor_busy:
            self._waiting.start(self.ANCHOR_DEADLINE_MS)
        else:
            self._show_at_fallback()

    def _receive_anchor(self, ticket, anchor):
        self._anchor_busy = False
        if ticket == self._ticket and self._waiting.isActive():
            self._waiting.stop()
            self._show_at_anchor(anchor)

    def _show_at_fallback(self):
        self._show_at_anchor(InputAnchor())

    def _show_at_anchor(self, anchor):
        self._field = _qt_bounds(anchor.field)
        self._caret = _qt_bounds(anchor.caret)
        reference = self._caret or self._field or _qt_bounds(anchor.window)
        screen = QGuiApplication.screenAt(reference.center()) if reference is not None else None
        self.show_centered(screen=screen)

    def dismiss(self):
        self._ticket += 1
        self._waiting.stop()
        self._placement = None
        super().dismiss()

    def closeEvent(self, event):
        self._ticket += 1
        self._waiting.stop()
        super().closeEvent(event)


class NineKeyOverlay(InputStatusOverlay):
    FULL_SIZE = (580, 244)
    COMPACT_SIZE = (440, 210)
    target_width, target_height = FULL_SIZE

    def __init__(self, controller, *, animated=True):
        super().__init__("九键输入", controller, animated=animated)
        box = QHBoxLayout(self.card)
        box.setContentsMargins(16, 14, 18, 14)
        box.setSpacing(16)
        self.keypad = QWidget()
        keys = QGridLayout(self.keypad)
        keys.setContentsMargins(0, 0, 0, 0)
        keys.setSpacing(5)
        self.key_labels = []
        for index, letters in enumerate(KEY_LABELS):
            key = _keycap(str(index + 1), letters or "选词")
            keys.addWidget(key, index // 3, index % 3)
            self.key_labels.append(key)
        for index, (letter, title) in enumerate((("*", "标点"), ("0", "空格"), ("#", "切换"))):
            key = _keycap(letter, title)
            keys.addWidget(key, 3, index)
            if letter == "0":
                self.zero_key = key
        box.addWidget(self.keypad, 0, Qt.AlignmentFlag.AlignTop)
        right = QVBoxLayout()
        right.setSpacing(9)
        box.addLayout(right, 1)
        header, self.heading, self.mode = _header("九键输入")
        right.addLayout(header)
        field = QFrame()
        field.setObjectName("hudField")
        field_box = QHBoxLayout(field)
        field_box.setContentsMargins(12, 7, 12, 7)
        self.composition = _hud_label("hudComposition")
        self.composition.setMinimumHeight(26)
        field_box.addWidget(self.composition, 1)
        right.addWidget(field)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.candidates = []
        for _ in range(CANDIDATES_PER_PAGE):
            item = _hud_label("hudCandidate")
            item.setAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setMinimumWidth(46)
            policy = item.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            item.setSizePolicy(policy)
            row.addWidget(item, 1)
            self.candidates.append(item)
        right.addLayout(row)
        right.addStretch(1)
        self.hint = _hud_label("hudHint")
        right.addWidget(self.hint)
        self._compact = None
        self.c.overlays.nine_key_changed.connect(self.sync)
        self.c.input_overlays_changed.connect(self.sync)

    def sync(self):
        state = self.c.overlays
        if not state.nine_key_open:
            self.dismiss()
            return
        composition = state.nine_key_state
        settings = self.c.input_overlay_settings
        self._apply_opacity(settings["opacity"])
        compact = settings["nine_compact"]
        changed_size = self._compact != compact
        self._compact = compact
        self.keypad.setVisible(not compact)
        if changed_size:
            self.layout().activate()   # the window minimum must drop before it resizes
        self.target_width, self.target_height = self.COMPACT_SIZE if compact else self.FULL_SIZE
        for index, key in enumerate(self.key_labels, 1):
            _highlight(key, state.nine_key_highlight == str(index))
        _highlight(self.zero_key, state.nine_key_highlight == "0")
        picking_symbol = composition.symbol_index is not None
        self.mode.setText(MODE_LABELS[composition.mode])
        selected = composition.selected_candidate
        text = composition.text
        prompt = {"pinyin": "按数字键输入拼音", "abc": "重复按同键切换字母", "number": "输入数字"}
        preview = text or prompt[composition.mode]
        if picking_symbol:
            preview = "选择标点和符号"
        elif selected and selected.pinyin:
            preview = f"{text}  ·  {selected.pinyin}"
        placeholder = not text and not picking_symbol
        if self.composition.property("placeholder") != placeholder:
            self.composition.setProperty("placeholder", placeholder)
            repolish(self.composition)
        composition_metrics = QFontMetrics(self.composition.font())
        self.composition.setText(composition_metrics.elidedText(preview, Qt.TextElideMode.ElideLeft, 330))
        candidates, index = composition.display_candidates
        start = (index // CANDIDATES_PER_PAGE) * CANDIDATES_PER_PAGE
        shown = candidates[start:start + CANDIDATES_PER_PAGE]
        for offset, label in enumerate(self.candidates):
            text = shown[offset] if offset < len(shown) else ""
            metrics = QFontMetrics(label.font())
            label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, 66))
            label.setAccessibleName(text)
            label.setVisible(bool(text))
            _highlight(label, bool(text and start + offset == index))
        names = state.nine_key_names
        controls = []
        if candidates and "left" in names and "right" in names:
            controls.append(f'{names["left"][:5]}/{names["right"][:5]} 选择')
        controls.append(f'{names["confirm"][:6]} 上屏')
        if not picking_symbol and "mode" in names:
            controls.append(f'{names["mode"][:5]} 切换')
        if len(candidates) > CANDIDATES_PER_PAGE:
            controls.append(f"{index + 1}/{len(candidates)}")
        elif not candidates:
            exit_key = "长按唤起键" if state.nine_key_shared_opener else names.get("back", "唤起键")
            controls.append(f"{exit_key[:7]} 退格/退出")
        hint = composition.notice or "   ".join(controls)
        self.hint.setText(QFontMetrics(self.hint.font()).elidedText(hint, Qt.TextElideMode.ElideRight, 330))
        placement = (settings["nine_position"], settings["nine_fallback"])
        if changed_size or self._placement != placement:
            self.place(*placement)


class VoiceStatusOverlay(InputStatusOverlay):
    target_width = 260
    target_height = 82

    def __init__(self, controller, *, animated=True):
        super().__init__("语音输入状态", controller, animated=animated)
        row = QHBoxLayout(self.card)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(9)
        self.dot = _hud_label("hudDot")
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status = _hud_label("hudStatus")
        row.addWidget(self.status)
        row.addStretch(1)
        self.level = QProgressBar()
        self.level.setObjectName("hudLevel")
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.level.setFixedSize(52, 5)
        self.level.setAccessibleName("麦克风电平")
        row.addWidget(self.level)
        self._preparing = False
        self.c.voice_changed.connect(self.sync)
        self.c.input_overlays_changed.connect(self.sync)
        self.c.level.connect(self._show_level)

    def _show_level(self, amplitude):
        if self.level.isVisible():
            self.level.setValue(round(max(0.0, min(1.0, amplitude)) * 100))

    def sync(self):
        settings = self.c.input_overlay_settings
        voice = self.c.voice
        active = not voice.ending and (voice.owner in ("usb", "trigger") or self.c.recording_requested)
        if self.c.shutting_down or not settings["show_voice"] or not active:
            self._preparing = False
            self.level.setValue(0)
            self.dismiss()
            return
        preparing = voice.routing or voice.gated or voice.owner is None
        recording = not preparing and voice.owner == "usb"
        if preparing:
            status = "准备麦克风" if voice.owner != "trigger" else "准备语音输入"
        elif recording:
            status = "正在录音"
        else:
            status = "已触发识别" if voice.recognition_started else "语音输入"
        self.status.setText(status)
        if self.dot.property("recording") != recording:
            self.dot.setProperty("recording", recording)
            repolish(self.dot)
        self.level.setVisible(recording)
        self._apply_opacity(settings["opacity"])
        placement = (settings["voice_position"], settings["voice_fallback"])
        resolved = self._field is not None or self._caret is not None
        refresh = (self._preparing and not preparing and placement[0] == "input"
                   and not resolved and not self._waiting.isActive())
        self._preparing = preparing
        if self._placement != placement or refresh:
            self.place(*placement)


class InputOverlaySettings(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        fields = form()
        self.fields = {}
        for key, title, items in (
            ("nine_position", "九键浮层位置", POSITIONS),
            ("nine_fallback", "九键备用角落", CORNERS),
            ("nine_compact", "九键布局", ((False, "键位和候选"), (True, "精简候选条"))),
            ("voice_position", "语音浮层位置", POSITIONS),
            ("voice_fallback", "语音备用角落", CORNERS),
        ):
            control = combo(items, title)
            control.currentIndexChanged.connect(
                lambda _index, k=key, field=control: self.c.set_input_overlay_setting(k, field.currentData()))
            fields.addRow(title, control)
            self.fields[key] = control
        self.opacity = QSpinBox()
        self.opacity.setRange(MIN_OPACITY, 100)
        self.opacity.setSuffix("%")
        self.opacity.setAccessibleName("浮层底板不透明度")
        self.opacity.editingFinished.connect(
            lambda: self.c.set_input_overlay_setting("opacity", self.opacity.value()))
        fields.addRow("底板不透明度", self.opacity)
        box.addLayout(fields)
        self.show_voice = QCheckBox("显示语音状态浮层")
        self.show_voice.toggled.connect(lambda visible: self.c.set_input_overlay_setting("show_voice", visible))
        box.addWidget(self.show_voice)
        self.c.input_overlays_changed.connect(self.refresh)
        self.c.profiles_changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        settings = self.c.input_overlay_settings
        for key, control in self.fields.items():
            with QSignalBlocker(control):
                control.setCurrentIndex(control.findData(settings[key]))
        self.fields["nine_fallback"].setEnabled(settings["nine_position"] == "input")
        self.fields["voice_fallback"].setEnabled(settings["voice_position"] == "input")
        with QSignalBlocker(self.opacity):
            self.opacity.setValue(settings["opacity"])
        with QSignalBlocker(self.show_voice):
            self.show_voice.setChecked(settings["show_voice"])
