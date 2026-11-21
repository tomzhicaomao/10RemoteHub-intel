"""Headless state and remote-key routing for Cheat Sheet and 3x3 Grid overlays.

Kept free of QtWidgets so AppController tests run without creating windows;
MainWindow binds lightweight non-activating OSD widgets to the signals here.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, QTimer, Signal

from .model import Action, AppLayer, Profile

GRID_DIRECTIONS = {
    "tv-up": (-1, 0),
    "tv-down": (1, 0),
    "tv-left": (0, -1),
    "tv-right": (0, 1),
}
GRID_NUMBERS = {f"tv-number-{n}": n - 1 for n in range(1, 10)}


class OverlayManager(QObject):
    HOLD_PEEK_THRESHOLD = 0.22  # seconds: >= hold-to-peek ("松手即隐"), < tap-to-pin
    CHEAT_TIMEOUT_MS = 15_000
    GRID_TIMEOUT_MS = 20_000
    TOAST_TIMEOUT_MS = 1_400
    DEFAULT_GRID_INDEX = 4      # center cell ("5") for 1-step D-pad reach everywhere

    cheat_sheet_changed = Signal()
    grid_changed = Signal()
    toast_changed = Signal()
    grid_activated = Signal(object)  # Action chosen from a configured cell

    def __init__(self, parent=None, *, clock=time.monotonic):
        super().__init__(parent)
        self.clock = clock
        self.cheat_open = False
        self.cheat_pinned = False
        self.cheat_owner = ""
        self.cheat_profile: Profile | None = None
        self.cheat_layer: AppLayer | None = None
        self.cheat_app_name = ""
        self._cheat_force_hold = False

        self.grid_open = False
        self.grid_owner = ""
        self.grid_action: Action | None = None
        self.grid_layer_name = ""
        self.grid_selected = self.DEFAULT_GRID_INDEX
        self.grid_notice = ""

        self.toast_open = False
        self.toast_title = ""
        self.toast_subtitle = ""
        self.toast_badge = ""

        self._press_times: dict[str, float] = {}
        self._consumed_releases: set[str] = set()

        self.cheat_timer = QTimer(self)
        self.cheat_timer.setSingleShot(True)
        self.cheat_timer.setInterval(self.CHEAT_TIMEOUT_MS)
        self.cheat_timer.timeout.connect(self.close_cheat_sheet)

        self.grid_timer = QTimer(self)
        self.grid_timer.setSingleShot(True)
        self.grid_timer.setInterval(self.GRID_TIMEOUT_MS)
        self.grid_timer.timeout.connect(self.close_grid)

        self.toast_timer = QTimer(self)
        self.toast_timer.setSingleShot(True)
        self.toast_timer.setInterval(self.TOAST_TIMEOUT_MS)
        self.toast_timer.timeout.connect(self.close_toast)

    @property
    def active(self) -> bool:
        return self.cheat_open or self.grid_open

    def has_pending_release(self, trigger: str) -> bool:
        return bool(trigger and trigger in self._consumed_releases)

    def note_physical_press(self, trigger: str):
        if trigger:
            self._press_times[trigger] = self.clock()

    def clear_physical_press(self, trigger: str):
        self._press_times.pop(trigger, None)
        self._consumed_releases.discard(trigger)

    # -- Cheat Sheet -----------------------------------------------------------

    def run_cheat_sheet(
        self,
        owner: str,
        down: bool,
        *,
        profile: Profile,
        layer: AppLayer | None = None,
        app_name: str = "",
        currently_pressed: bool = False,
        from_long: bool = False,
    ):
        if not down:
            if self.cheat_open and owner and owner == self.cheat_owner and not self.cheat_pinned:
                self.close_cheat_sheet()
            return
        if self.grid_open:
            self.close_grid()
        if self.cheat_open:
            self.close_cheat_sheet()
            if currently_pressed and owner:
                self._consumed_releases.add(owner)
            return
        self.cheat_open = True
        self.cheat_owner = owner or ""
        self.cheat_profile = profile
        self.cheat_layer = layer
        self.cheat_app_name = app_name or ""
        self._cheat_force_hold = bool(from_long)
        if currently_pressed and owner:
            self.cheat_pinned = False
            self._press_times.setdefault(owner, self.clock())
            self.cheat_timer.stop()
        else:
            self.cheat_pinned = True
            self.cheat_timer.start(self.CHEAT_TIMEOUT_MS)
        self.cheat_sheet_changed.emit()

    def refresh_cheat_context(self, profile: Profile, layer: AppLayer | None, app_name: str = ""):
        if not self.cheat_open:
            return
        self.cheat_profile = profile
        self.cheat_layer = layer
        self.cheat_app_name = app_name or ""
        self.cheat_sheet_changed.emit()

    def close_cheat_sheet(self):
        self.cheat_timer.stop()
        if not self.cheat_open:
            return
        self.cheat_open = False
        self.cheat_pinned = False
        self.cheat_owner = ""
        self.cheat_profile = None
        self.cheat_layer = None
        self.cheat_app_name = ""
        self._cheat_force_hold = False
        self.cheat_sheet_changed.emit()

    # -- 3x3 Grid Launcher -----------------------------------------------------

    def open_grid(
        self,
        action: Action,
        owner: str,
        *,
        layer_name: str = "",
        currently_pressed: bool = False,
    ):
        if self.cheat_open:
            self.close_cheat_sheet()
        if self.grid_open and owner and owner == self.grid_owner:
            self.close_grid()
            if currently_pressed:
                self._consumed_releases.add(owner)
            return
        self.grid_open = True
        self.grid_owner = owner or ""
        self.grid_action = action
        self.grid_layer_name = layer_name or ""
        self.grid_selected = self.DEFAULT_GRID_INDEX
        self.grid_notice = ""
        if currently_pressed and owner:
            self._consumed_releases.add(owner)
        self.grid_timer.start(self.GRID_TIMEOUT_MS)
        self.grid_changed.emit()

    def move_grid(self, drow: int, dcol: int):
        if not self.grid_open:
            return
        row, col = divmod(self.grid_selected, 3)
        new_row = min(2, max(0, row + drow))
        new_col = min(2, max(0, col + dcol))
        self.grid_selected = new_row * 3 + new_col
        self.grid_notice = ""
        self.grid_timer.start(self.GRID_TIMEOUT_MS)
        self.grid_changed.emit()

    def activate_grid_cell(self, index: int | None = None) -> bool:
        if not self.grid_open or self.grid_action is None:
            return False
        idx = self.grid_selected if index is None else index
        if not 0 <= idx < len(self.grid_action.cells):
            return False
        self.grid_selected = idx
        cell = self.grid_action.cells[idx]
        if cell.action.kind == "none":
            self.grid_notice = f"格位 {idx + 1} 尚未设置动作"
            self.grid_timer.start(self.GRID_TIMEOUT_MS)
            self.grid_changed.emit()
            return False
        chosen = cell.action
        self.close_grid()
        self.grid_activated.emit(chosen)
        return True

    def close_grid(self):
        self.grid_timer.stop()
        if not self.grid_open:
            return
        self.grid_open = False
        self.grid_owner = ""
        self.grid_action = None
        self.grid_layer_name = ""
        self.grid_selected = self.DEFAULT_GRID_INDEX
        self.grid_notice = ""
        self.grid_changed.emit()

    def show_toast(self, title: str, *, subtitle: str = "", badge: str = ""):
        self.toast_open = True
        self.toast_title = title.strip()
        self.toast_subtitle = subtitle.strip()
        self.toast_badge = badge.strip()
        self.toast_timer.start(self.TOAST_TIMEOUT_MS)
        self.toast_changed.emit()

    def close_toast(self):
        self.toast_timer.stop()
        if not self.toast_open:
            return
        self.toast_open = False
        self.toast_title = ""
        self.toast_subtitle = ""
        self.toast_badge = ""
        self.toast_changed.emit()

    def close(self):
        self.close_cheat_sheet()
        self.close_grid()
        self.close_toast()
        self._press_times.clear()
        self._consumed_releases.clear()

    # -- Key interception ------------------------------------------------------

    def handle_key(self, trigger: str, down: bool, role: str | None, *, is_repeat: bool = False) -> str:
        """Route a remote key while an overlay is open or awaiting release.

        Returns:
          - "consumed": caller must not dispatch normal button actions
          - "passthrough": Cheat Sheet closed on another button's press; dispatch it
          - "none": no overlay state applied
        """
        if not down and trigger in self._consumed_releases:
            self._consumed_releases.discard(trigger)
            self._press_times.pop(trigger, None)
            return "consumed"

        if self.grid_open:
            if not down:
                self._press_times.pop(trigger, None)
                return "consumed"
            if role in GRID_DIRECTIONS:
                self._consumed_releases.add(trigger)
                self.move_grid(*GRID_DIRECTIONS[role])
                return "consumed"
            if is_repeat:
                return "consumed"
            self._consumed_releases.add(trigger)
            if role == "tv-back" or (self.grid_owner and trigger == self.grid_owner):
                self.close_grid()
                return "consumed"
            if role == "tv-ok":
                self.activate_grid_cell()
                return "consumed"
            if role in GRID_NUMBERS:
                self.activate_grid_cell(GRID_NUMBERS[role])
                return "consumed"
            self.grid_notice = "按数字键 1–9 或方向键选择，按返回收起"
            self.grid_timer.start(self.GRID_TIMEOUT_MS)
            self.grid_changed.emit()
            return "consumed"

        if self.cheat_open:
            if trigger and trigger == self.cheat_owner:
                if not down:
                    started = self._press_times.pop(trigger, None)
                    if not self.cheat_pinned:
                        held_long = self._cheat_force_hold or (
                            started is not None and (self.clock() - started) >= self.HOLD_PEEK_THRESHOLD
                        )
                        if held_long:
                            self.close_cheat_sheet()
                        else:
                            self.cheat_pinned = True
                            self.cheat_timer.start(self.CHEAT_TIMEOUT_MS)
                            self.cheat_sheet_changed.emit()
                    return "consumed"
                if is_repeat:
                    return "consumed"
                self._consumed_releases.add(trigger)
                self.close_cheat_sheet()
                return "consumed"
            if not down:
                self._press_times.pop(trigger, None)
                return "consumed"
            if is_repeat:
                return "consumed"
            if role in ("tv-back", "tv-ok"):
                self._consumed_releases.add(trigger)
                self.close_cheat_sheet()
                return "consumed"
            self.close_cheat_sheet()
            return "passthrough"

        return "none"
