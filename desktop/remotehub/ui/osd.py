"""Non-focus-stealing On-Screen Display (OSD) windows: Cheat Sheet & 3x3 Grid.

Neither window ever activates 10RemoteHub or steals keyboard focus, preserving
both foreground-app layer detection and direct text/shortcut delivery.
"""
from __future__ import annotations

from ctypes import c_void_p
import sys

from PySide6.QtCore import QEasingCurve, QPoint, Qt, QVariantAnimation
from PySide6.QtGui import QCursor, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QGridLayout,
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..branding import APP_NAME
from ..canvas import RemoteCanvas, callout_text
from ..controller import StatusView
from ..model import Action, GRID_SIZE, GridCell, effective_button
from ..photo_template import is_voice_button
from ..presentation import grid_cell_texts
from ..theme import qcolor, repolish, stylesheet
from .widgets import Pill, button, label

# Win32 extended window styles & SetWindowPos flags for non-activating HUDs
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def compute_win32_exstyle(existing: int, *, click_through: bool) -> int:
    style = int(existing) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
    if click_through:
        style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        style &= ~WS_EX_TRANSPARENT
    return style


def cheat_callout_text(profile, btn, layer=None) -> tuple[str, str]:
    """Effective runtime label and QSS state for a button on the Cheat Sheet."""
    if not btn.configurable:
        return btn.name, "blocked"
    if profile is not None and is_voice_button(profile, btn):
        return f"{btn.name}  语音输入", "assigned"
    if layer is not None and layer.overrides.get(btn.id):
        eff = effective_button(btn, layer)
        if all(a.kind == "none" for a in (eff.action, eff.double_action, eff.long_action)):
            return f"★ {btn.name}  无操作", "empty"
        base_text, _ = callout_text(eff)
        return f"★ {base_text}", "override"
    eff = effective_button(btn, layer)
    return callout_text(eff)


class NonActivatingOverlay(QWidget):
    """Floating frameless HUD card that never activates the host process."""

    target_width = 760
    target_height = 520
    OPEN_DURATION_MS = 165
    CLOSE_DURATION_MS = 130
    SLIDE_IN_PX = 12
    SLIDE_OUT_PX = 8

    def __init__(self, title_suffix: str, *, animated: bool = True, click_through: bool = False):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if click_through:
            flags |= Qt.WindowType.WindowTransparentForInput
        super().__init__(None, flags)
        self.animated = animated
        self._click_through = click_through
        self._presenting = False
        self._closing = False
        self._target_pos = QPoint(0, 0)
        self._close_start_pos = QPoint(0, 0)
        self._close_start_opacity = 1.0

        self.setObjectName("osdRoot")
        self.setWindowTitle(f"{APP_NAME} · {title_suffix}")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        if click_through:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowOpacity(1.0)
        self.setStyleSheet(stylesheet() + "\nQWidget#osdRoot { background: transparent; }")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 22)
        self.card = QFrame()
        self.card.setObjectName("osdCard")
        self.card.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 10)
        shadow.setColor(qcolor("INK", 76))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)

        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._on_anim_step)
        self._anim.finished.connect(self._on_anim_finished)

    def _is_wayland_platform(self) -> bool:
        return "wayland" in (QApplication.platformName() or "").lower()

    def _safe_move(self, x: int | QPoint, y: int | None = None):
        if self._is_wayland_platform():
            return
        if isinstance(x, QPoint):
            self.move(x)
        elif y is not None:
            self.move(x, y)

    def _build_header(self, title_text: str, close_callback, close_accessible: str) -> QFrame:
        bar = QFrame(self.card)
        bar.setObjectName("osdHeader")
        bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = QHBoxLayout(bar)
        header.setContentsMargins(20, 13, 18, 12)
        header.setSpacing(10)
        accent = QFrame(bar)
        accent.setObjectName("osdAccent")
        accent.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.heading = label(title_text, "heading", wrap=False)
        self.layer_pill = Pill("")
        self.layer_pill.setWordWrap(False)
        self.close_button = button("收起", close_callback, "osdClose")
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setAccessibleName(close_accessible)
        header.addWidget(accent, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.heading, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.layer_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        header.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return bar

    def _build_footer(self, badges: tuple[str, ...]) -> tuple[QFrame, QLabel]:
        bar = QFrame(self.card)
        bar.setObjectName("osdFooter")
        bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 9, 20, 10)
        row.setSpacing(6)
        self._footer_badges: list[QLabel] = []
        for kbd_text in badges:
            chip = QLabel(kbd_text, bar)
            chip.setProperty("role", "kbdHint")
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            row.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
            self._footer_badges.append(chip)
        if badges:
            row.addSpacing(4)
        text_label = QLabel("", bar)
        text_label.setObjectName("osdFooterText")
        text_label.setWordWrap(True)
        text_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row.addWidget(text_label, 1, Qt.AlignmentFlag.AlignVCenter)
        return bar, text_label

    def _native_ns_window(self):
        if QApplication.platformName() != "cocoa":
            return None
        try:
            import objc
            ptr = int(self.winId())
            if not ptr:
                return None
            ns_view = objc.objc_object(c_void_p=c_void_p(ptr))
            return ns_view.window()
        except Exception:
            return None

    def _apply_win32_overlay_hints(self):
        if sys.platform != "win32" or QApplication.platformName() == "offscreen":
            return
        try:
            import ctypes as c
            user32 = getattr(getattr(c, "windll", None), "user32", None)
            if user32 is None:
                return
            hwnd = int(self.winId())
            if not hwnd:
                return
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_long.argtypes = [c.c_void_p, c.c_int]
            get_long.restype = c.c_ssize_t
            set_long.argtypes = [c.c_void_p, c.c_int, c.c_ssize_t]
            set_long.restype = c.c_ssize_t
            cur = int(get_long(hwnd, GWL_EXSTYLE))
            target = compute_win32_exstyle(cur, click_through=bool(self._click_through or self._closing))
            if target != cur:
                set_long(hwnd, GWL_EXSTYLE, target)
        except Exception:
            pass

    def _apply_native_overlay_hints(self):
        if sys.platform == "win32":
            self._apply_win32_overlay_hints()
            return
        ns_window = self._native_ns_window()
        if ns_window is None:
            return
        try:
            # NSWindowStyleMaskNonactivatingPanel (1 << 7) keeps clicks & show()
            # from activating 10RemoteHub or bringing MainWindow forward.
            if ns_window.respondsToSelector_("styleMask") and ns_window.respondsToSelector_("setStyleMask:"):
                ns_window.setStyleMask_(ns_window.styleMask() | (1 << 7))
            if ns_window.respondsToSelector_("setFloatingPanel:"):
                ns_window.setFloatingPanel_(True)
            if ns_window.respondsToSelector_("setBecomesKeyOnlyIfNeeded:"):
                ns_window.setBecomesKeyOnlyIfNeeded_(True)
            # NSStatusWindowLevel (25) floats above normal and full-screen windows.
            ns_window.setLevel_(25)
            # CanJoinAllSpaces | Transient | IgnoresCycle | FullScreenAuxiliary
            ns_window.setCollectionBehavior_((1 << 0) | (1 << 3) | (1 << 6) | (1 << 8))
            ns_window.setHidesOnDeactivate_(False)
        except Exception:
            pass

    def _order_front_nonactivating(self):
        if sys.platform == "win32" and QApplication.platformName() != "offscreen":
            try:
                import ctypes as c
                user32 = getattr(getattr(c, "windll", None), "user32", None)
                hwnd = int(self.winId())
                if user32 is not None and hwnd:
                    user32.SetWindowPos.argtypes = [
                        c.c_void_p, c.c_void_p, c.c_int, c.c_int, c.c_int, c.c_int, c.c_uint
                    ]
                    user32.SetWindowPos(
                        hwnd,
                        c.c_void_p(HWND_TOPMOST),
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                    )
                    return
            except Exception:
                pass
        ns_window = self._native_ns_window()
        if ns_window is not None:
            try:
                ns_window.orderFrontRegardless()
                return
            except Exception:
                pass
        self.raise_()

    def _on_anim_step(self, value):
        t = float(value)
        if self._closing:
            self.setWindowOpacity(max(0.0, self._close_start_opacity * (1.0 - t)))
            self._safe_move(self._close_start_pos.x(), round(self._close_start_pos.y() + self.SLIDE_OUT_PX * t))
        elif self._presenting:
            self.setWindowOpacity(min(1.0, t))
            self._safe_move(self._target_pos.x(), round(self._target_pos.y() + self.SLIDE_IN_PX * (1.0 - t)))

    def _on_anim_finished(self):
        if self._closing:
            self._finish_hide()
        elif self._presenting:
            self.setWindowOpacity(1.0)
            self._safe_move(self._target_pos)

    def _finish_hide(self):
        self._closing = False
        self._presenting = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self._click_through)
        self._apply_win32_overlay_hints()
        self.hide()
        self.setWindowOpacity(1.0)

    def _anchor_point(self, avail, width: int, height: int) -> QPoint:
        x = avail.x() + (avail.width() - width) // 2
        y = avail.y() + (avail.height() - height) // 2
        return QPoint(x, y)

    def show_centered(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            width = min(self.target_width, max(260, avail.width() - 48))
            height = min(self.target_height, max(72, avail.height() - 48))
            self.resize(width, height)
            self._target_pos = self._anchor_point(avail, width, height)
        else:
            self.resize(self.target_width, self.target_height)
            self._target_pos = self.pos()

        fresh_open = (not self.isVisible()) or self._closing
        if self._closing:
            self._anim.stop()
            self._closing = False
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self._click_through)

        self._presenting = True
        self._apply_native_overlay_hints()

        if self.animated and fresh_open:
            self._anim.stop()
            self.setWindowOpacity(0.0)
            self._safe_move(self._target_pos.x(), self._target_pos.y() + self.SLIDE_IN_PX)
            if not self.isVisible():
                self.show()
                self._apply_native_overlay_hints()
            self._order_front_nonactivating()
            self._anim.setDuration(self.OPEN_DURATION_MS)
            self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._anim.start()
            return

        if self._anim.state() != QVariantAnimation.State.Running:
            self.setWindowOpacity(1.0)
            self._safe_move(self._target_pos)
        if not self.isVisible():
            self.show()
            self._apply_native_overlay_hints()
        self._order_front_nonactivating()

    def dismiss(self):
        if not self.isVisible():
            self._presenting = False
            self._closing = False
            return
        if self._closing:
            return
        self._presenting = False
        if not self.animated:
            self._anim.stop()
            self._finish_hide()
            return
        self._anim.stop()
        self._closing = True
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._apply_win32_overlay_hints()
        self._close_start_opacity = max(0.05, self.windowOpacity())
        self._close_start_pos = self.pos()
        self._anim.setDuration(self.CLOSE_DURATION_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim.start()

    def closeEvent(self, event):
        self._anim.stop()
        self._closing = False
        self._presenting = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        super().closeEvent(event)


class CheatSheetCanvas(RemoteCanvas):
    """Read-only remote canvas highlighting every active key in the current layer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setMinimumSize(540, 460)

    def _button_highlighted(self, btn) -> bool:
        if not btn.configurable:
            return False
        if self.profile is not None and is_voice_button(self.profile, btn):
            return True
        eff = effective_button(btn, self.layer)
        return any(a.kind != "none" for a in (eff.action, eff.double_action, eff.long_action))

    def label_text(self, btn):
        return cheat_callout_text(self.profile, btn, self.layer)[0]

    def set_runtime_snapshot(self, profile, layer=None):
        super().set_profile(profile, "", layer=layer)
        for btn in profile.buttons:
            callout = self.callouts.get(btn.id)
            if callout is None:
                continue
            callout.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            callout.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            text, state = cheat_callout_text(profile, btn, layer)
            callout.setAccessibleName(text)
            if callout.property("state") != state:
                callout.setProperty("state", state)
                repolish(callout)
        self.layout_callouts()
        self.update()


class CheatSheetOverlay(NonActivatingOverlay):
    """Hold-to-peek / tap-to-pin remote cheat sheet HUD."""

    target_width = 900
    target_height = 640

    def __init__(self, controller, *, animated: bool = True):
        super().__init__("键位速查", animated=animated)
        self.c = controller
        box = QVBoxLayout(self.card)
        box.setContentsMargins(1, 1, 1, 1)
        box.setSpacing(0)

        header = self._build_header("遥控器键位速查", self.c.overlays.close_cheat_sheet, "关闭键位速查")
        self.layer_pill.show_status(StatusView("全局默认层", "", "neutral"))
        box.addWidget(header)

        stage = QWidget(self.card)
        stage.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(16, 10, 16, 10)
        stage_layout.setSpacing(0)
        self.canvas = CheatSheetCanvas(stage)
        stage_layout.addWidget(self.canvas, 1)
        box.addWidget(stage, 1)

        footer_bar, self.mode_hint = self._build_footer(("返回", "确定"))
        box.addWidget(footer_bar)

        self.c.overlays.cheat_sheet_changed.connect(self.sync)

    def sync(self):
        ov = self.c.overlays
        if not ov.cheat_open or ov.cheat_profile is None:
            self.dismiss()
            return
        if ov.cheat_layer is not None:
            self.layer_pill.show_status(StatusView(f"★ {ov.cheat_layer.name} 专属层", "", "ready"))
        elif ov.cheat_app_name:
            self.layer_pill.show_status(StatusView(f"全局默认层 · {ov.cheat_app_name}", "", "neutral"))
        else:
            self.layer_pill.show_status(StatusView("全局默认层", "", "neutral"))
        if ov.cheat_pinned:
            for chip in self._footer_badges:
                chip.show()
            self.mode_hint.setText("收起速查 · 或直接按任意已分配键立即执行（★ 为当前应用专属覆盖）")
        else:
            for chip in self._footer_badges:
                chip.hide()
            self.mode_hint.setText("按住查看中 · 松开按键立即收起（★ 为当前应用专属覆盖）")
        self.canvas.set_runtime_snapshot(ov.cheat_profile, ov.cheat_layer)
        self.show_centered()


class GridCellCard(QPushButton):
    """Single card in the 3x3 Grid Launcher OSD (keys 1..9)."""

    def __init__(self, index: int, callback, parent=None):
        super().__init__(parent)
        self.index = index
        self.setProperty("role", "gridCell")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(184, 86)
        self.clicked.connect(lambda checked=False: callback(self.index))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(9)
        self.badge = QLabel(str(index + 1))
        self.badge.setObjectName("gridNumber")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label = label("", "value", wrap=False)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(top)

        self.sub_label = label("", "caption", wrap=False)
        self.sub_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.sub_label)
        layout.addStretch(1)

    def set_cell(self, cell: GridCell, *, selected: bool):
        empty = cell.action.kind == "none"
        title, sub = grid_cell_texts(cell)
        title_metrics = QFontMetrics(self.title_label.font())
        sub_metrics = QFontMetrics(self.sub_label.font())
        avail = max(110, self.width() - 62)
        self.title_label.setText(title_metrics.elidedText(title, Qt.TextElideMode.ElideRight, avail))
        self.sub_label.setText(sub_metrics.elidedText(sub, Qt.TextElideMode.ElideRight, avail + 26))
        self.setAccessibleName(f"{self.index + 1}：{title}（{sub}）")

        empty_val = "true" if empty else "false"
        sel_val = "true" if selected else "false"
        if self.property("empty") != empty_val or self.property("selected") != sel_val:
            self.setProperty("empty", empty_val)
            self.setProperty("selected", sel_val)
            repolish(self)
        badge_sel = "true" if selected else "false"
        badge_empty = "true" if (empty and not selected) else "false"
        if self.badge.property("empty") != badge_empty or self.badge.property("selected") != badge_sel:
            self.badge.setProperty("empty", badge_empty)
            self.badge.setProperty("selected", badge_sel)
            repolish(self.badge)
        dim_val = "true" if (empty and not selected) else "false"
        if self.title_label.property("emptyTitle") != dim_val:
            self.title_label.setProperty("emptyTitle", dim_val)
            repolish(self.title_label)
        if self.sub_label.property("emptySub") != dim_val:
            self.sub_label.setProperty("emptySub", dim_val)
            repolish(self.sub_label)


class GridLauncherOverlay(NonActivatingOverlay):
    """3x3 visual launcher mapped 1:1 to physical remote number keys 1-9 & D-pad."""

    target_width = 684
    target_height = 456

    def __init__(self, controller, *, animated: bool = True):
        super().__init__("九宫格面板", animated=animated)
        self.c = controller
        box = QVBoxLayout(self.card)
        box.setContentsMargins(1, 1, 1, 1)
        box.setSpacing(0)

        header = self._build_header("九宫格面板", self.c.overlays.close_grid, "关闭九宫格面板")
        self.layer_pill.hide()
        box.addWidget(header)

        stage = QWidget(self.card)
        stage.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        grid = QGridLayout(stage)
        grid.setContentsMargins(20, 16, 20, 16)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self.cells: list[GridCellCard] = []
        for idx in range(GRID_SIZE):
            card = GridCellCard(idx, self.c.overlays.activate_grid_cell, stage)
            grid.addWidget(card, idx // 3, idx % 3)
            self.cells.append(card)
        box.addWidget(stage, 1)

        footer_bar, self.footer = self._build_footer(("1–9", "方向键", "确定", "返回"))
        box.addWidget(footer_bar)

        self.c.overlays.grid_changed.connect(self.sync)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.c.overlays.grid_open and self.c.overlays.grid_action is not None:
            self._populate_cells(self.c.overlays.grid_action, self.c.overlays.grid_selected)

    def _populate_cells(self, action: Action, selected_idx: int):
        for idx, card in enumerate(self.cells):
            cell = action.cells[idx] if idx < len(action.cells) else GridCell()
            card.set_cell(cell, selected=(idx == selected_idx))

    def sync(self):
        ov = self.c.overlays
        if not ov.grid_open or ov.grid_action is None:
            self.dismiss()
            return
        title = ov.grid_action.target.strip() or "九宫格面板"
        self.heading.setText(title)
        if ov.grid_layer_name:
            self.layer_pill.show_status(StatusView(f"★ {ov.grid_layer_name}", "", "ready"))
            self.layer_pill.show()
        else:
            self.layer_pill.hide()
        warn_val = "true" if ov.grid_notice else "false"
        if self.footer.property("warn") != warn_val:
            self.footer.setProperty("warn", warn_val)
            repolish(self.footer)
        if ov.grid_notice:
            for chip in self._footer_badges:
                chip.hide()
            self.footer.setText(ov.grid_notice)
        else:
            for chip in self._footer_badges:
                chip.show()
            self.footer.setText("数字直达格位 · 方向键移动焦点 · 确定执行 · 返回收起")
        self.show_centered()
        self._populate_cells(ov.grid_action, ov.grid_selected)


class ProfileToastOverlay(NonActivatingOverlay):
    """Compact click-through top HUD toast shown when cycling profiles blindly."""

    target_width = 360
    target_height = 96
    OPEN_DURATION_MS = 140
    CLOSE_DURATION_MS = 120
    SLIDE_IN_PX = -10
    SLIDE_OUT_PX = -6

    def __init__(self, controller, *, animated: bool = True):
        super().__init__("方案切换提示", animated=animated, click_through=True)
        self.c = controller
        self.card.setObjectName("toastCard")
        self.card.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        row_layout = QHBoxLayout(self.card)
        row_layout.setContentsMargins(16, 12, 18, 12)
        row_layout.setSpacing(12)

        self.badge = QLabel("1/1", self.card)
        self.badge.setObjectName("toastBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row_layout.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        self.subtitle_label = QLabel("已切换配置方案", self.card)
        self.subtitle_label.setObjectName("toastSubtitle")
        self.subtitle_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label = QLabel("", self.card)
        self.title_label.setObjectName("toastTitle")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text_col.addWidget(self.subtitle_label)
        text_col.addWidget(self.title_label)
        row_layout.addLayout(text_col, 1)

        self.c.overlays.toast_changed.connect(self.sync)

    def _anchor_point(self, avail, width: int, height: int) -> QPoint:
        x = avail.x() + (avail.width() - width) // 2
        y = avail.y() + 48
        return QPoint(x, y)

    def sync(self):
        ov = self.c.overlays
        if not ov.toast_open or not ov.toast_title:
            self.dismiss()
            return
        self.badge.setText(ov.toast_badge or "✓")
        self.badge.setVisible(bool(ov.toast_badge))
        self.subtitle_label.setText(ov.toast_subtitle or "当前配置方案")
        metrics = QFontMetrics(self.title_label.font())
        self.title_label.setText(metrics.elidedText(ov.toast_title, Qt.TextElideMode.ElideRight, 236))
        self.show_centered()
