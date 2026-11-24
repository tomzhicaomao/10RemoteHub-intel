"""Overlay preferences, nine-key/grid/cheat-sheet overlays, macros, and navigation mapping for ``AppController``."""
from __future__ import annotations

import time

from .input_overlay_preferences import preferences, validate_preferences
from .key_roles import ROLE_TO_NAV_ID, ROLE_TO_NINE_KEY, resolve_button_role, sync_roles_from_nine_key
from .launch import LaunchRequest
from .model import Action, Button, GESTURE_FIELDS
from .nine_key import BINDING_ROLES, DEFAULT_BUTTON_IDS, missing_bindings
from .windows import PendingHide

_CANONICAL_NAV_IDS = frozenset({
    "tv-up", "tv-down", "tv-left", "tv-right", "tv-ok", "tv-back",
    *(f"tv-number-{n}" for n in range(1, 10)),
})
_NINE_KEY_TO_NAV = {
    "left": "tv-left", "right": "tv-right", "confirm": "tv-ok", "back": "tv-back",
    **{str(n): f"tv-number-{n}" for n in range(1, 10)},
}
_NAV_NAME_MAP = {
    "上": "tv-up", "方向上": "tv-up", "up": "tv-up",
    "下": "tv-down", "方向下": "tv-down", "down": "tv-down",
    "左": "tv-left", "方向左": "tv-left", "left": "tv-left",
    "右": "tv-right", "方向右": "tv-right", "right": "tv-right",
    "确定": "tv-ok", "确认": "tv-ok", "ok": "tv-ok", "enter": "tv-ok", "回车": "tv-ok",
    "返回": "tv-back", "退出": "tv-back", "back": "tv-back", "esc": "tv-back",
    **{str(n): f"tv-number-{n}" for n in range(1, 10)},
    **{f"数字{n}": f"tv-number-{n}" for n in range(1, 10)},
}
_STANDARD_HID_NAV = {
    "hid:7:82": "tv-up", "hid:7:81": "tv-down", "hid:7:80": "tv-left", "hid:7:79": "tv-right",
    "hid:7:40": "tv-ok", "hid:7:88": "tv-ok", "hid:12:65": "tv-ok",
    "hid:7:41": "tv-back", "hid:7:42": "tv-back", "hid:12:548": "tv-back",
    "hid:12:66": "tv-up", "hid:12:67": "tv-down", "hid:12:68": "tv-left", "hid:12:69": "tv-right",
    **{f"hid:7:{30 + n - 1}": f"tv-number-{n}" for n in range(1, 10)},
    **{f"hid:7:{89 + n - 1}": f"tv-number-{n}" for n in range(1, 10)},
}


class OverlayMacroMixin:
    @property
    def input_overlay_settings(self):
        return preferences(self.settings)

    def set_input_overlay_setting(self, key, value):
        previous = self.settings.get("input_overlays", {})
        if self.input_overlay_settings[key] == value:
            return True
        updated = previous | {key: value}
        validate_preferences(updated)
        self.settings["input_overlays"] = updated
        if not self.save():
            if previous:
                self.settings["input_overlays"] = previous
            else:
                self.settings.pop("input_overlays", None)
            self.input_overlays_changed.emit()
            return False
        self.input_overlays_changed.emit()
        return True

    def _clear_gestures(self):
        self.switcher.close()
        self.overlays.close()
        self.macros.close()
        self.holds.close()
        self.mouse_holds.close()
        self.switcher_pressed.clear()
        self.press_buttons.clear()
        for gesture in self.gestures.values():
            gesture.cancel()
            gesture.deleteLater()
        self.gestures.clear()

    def _schedule_text_enter(self, _owner: str = ""):
        self.text_enter_timer.stop()
        self.text_enter_timer.start()

    def _fire_text_enter(self):
        if self.shutting_down:
            return
        try:
            self.keys.shortcut(["enter"])
        except Exception as exc:
            self.fail(str(exc))

    def _run_macro_action(self, action: Action, owner: str):
        currently_pressed = bool(owner and owner in self.pressed)
        self.macros.start(action, owner, currently_pressed=currently_pressed)

    def _execute_macro_step(self, step_action: Action, _index: int):
        self._cancel_hide_result()
        result = self._run_action(step_action, True, "macro")
        if isinstance(result, LaunchRequest):
            self.launcher.start(result)
        elif isinstance(result, PendingHide):
            self.pending_hide = result
            self.hide_deadline = time.monotonic() + 1.0
            self.hide_timer.start()

    def _run_overlay_action(self, action: Action, down: bool, owner: str):
        currently_pressed = bool(owner and owner in self.pressed)
        if action.kind == "nine_key" and down:
            if self.recording_requested or self.voice.owner is not None:
                raise RuntimeError("请先结束语音输入")
            buttons = self.nine_key_buttons()
            bindings = self.nine_key_bindings(action)
            if missing_bindings(bindings, BINDING_ROLES):
                raise RuntimeError("请在「设置九键键位」按提示登记全部 15 个键。")
            if not owner and "back" not in bindings:
                raise RuntimeError("请从实体遥控器上唤起九键输入")
            self.holds.close()
            self.mouse_holds.close()
            self.nine_key_app_id = self.foreground_apps.current_app_id()
            names = {role: buttons[ident].name for role, ident in bindings.items()}
            shared_opener = ("back" not in bindings and owner and
                             any(buttons[ident].matches_trigger(owner) for ident in bindings.values()))
            self.overlays.open_nine_key(owner, bindings, names,
                                        shared_opener=bool(shared_opener),
                                        currently_pressed=currently_pressed)
            return
        layer = self.active_runtime_layer()
        if action.kind == "cheat_sheet":
            gesture = self.gestures.get(owner)
            from_long = bool(gesture is not None and getattr(gesture, "_from_long", False))
            self.overlays.run_cheat_sheet(
                owner,
                down,
                profile=self.profile,
                layer=layer,
                app_name=self.foreground_apps.current_app_name(),
                currently_pressed=currently_pressed,
                from_long=from_long,
            )
            return
        if action.kind == "grid" and down:
            self.overlays.open_grid(
                action,
                owner,
                layer_name=layer.name if layer is not None else "",
                currently_pressed=currently_pressed,
            )

    def _execute_grid_action(self, action: Action):
        self._cancel_hide_result()
        try:
            message = self._run_action(action, True, "grid")
            self._action_feedback(message)
        except Exception as exc:
            self.fail(str(exc))

    def nine_key_buttons(self):
        return {button.id: button for button in self.configurable_buttons()
                if button.trigger and not self._is_voice_button(button)}

    def nine_key_defaults(self):
        available = self.nine_key_buttons()
        defaults = {}
        used = set()
        if self.fixed_model:
            for role, ident in DEFAULT_BUTTON_IDS.items():
                if ident in available and ident not in used:
                    defaults[role] = ident
                    used.add(ident)
            return {} if missing_bindings(defaults, BINDING_ROLES) else defaults
        # Generic remotes: derive defaults from unified Button.role / standard HID roles.
        for button in available.values():
            slot = ROLE_TO_NINE_KEY.get(resolve_button_role(button), "")
            if slot and slot not in defaults and button.id not in used:
                defaults[slot] = button.id
                used.add(button.id)
        return defaults

    def nine_key_bindings(self, action):
        available = self.nine_key_buttons()
        defaults = self.nine_key_defaults()
        saved = action.spec.get("bindings")
        source = defaults if saved is None else saved
        bindings = {role: source[role] for role in BINDING_ROLES if source.get(role) in available}
        back = source.get("back") if saved is not None else defaults.get("back")
        if back not in available:
            back = defaults.get("back")
        if saved is None and self.fixed_model and defaults:
            legacy = self.input_overlay_settings["confirm_key"]
            if legacy in available and legacy != back and legacy not in bindings.values():
                bindings["confirm"] = legacy
        if back is not None and back in available and back not in bindings.values():
            bindings["back"] = back
        return bindings

    def sync_nine_key_roles(self, bindings: dict[str, str]) -> bool:
        """Persist confirmed nine-key slot roles back onto the remote's buttons across all profiles."""
        if self.fixed_model or self.remote is None:
            return False
        return sync_roles_from_nine_key(self.remote.profiles, bindings)

    def _paste_nine_key(self, candidate):
        current_app = self.foreground_apps.current_app_id()
        if self.nine_key_app_id and self.nine_key_app_id != current_app:
            self.overlays.close_nine_key()
            return
        try:
            self.keys.input_text(candidate.text)
        except Exception as exc:
            self.overlays.set_nine_key_notice("无法上屏，请检查输入权限")
            self.fail(str(exc))
            return
        self.overlays.accept_nine_key_candidate(candidate)

    def _run_action(self, action, down, owner):
        if action.kind in ("hold", "mouse", "grid", "cheat_sheet", "nine_key", "macro", "cycle_profile") or (
            action.kind == "common" and action.target == "app_switcher"
        ):
            return self.runner.run(action, down, owner=owner)
        return self.runner.run(action, down)

    def _cancel_button_actions(self, target):
        triggers = target.all_triggers if isinstance(target, Button) else ([target] if target else [])
        for trigger in triggers:
            self.press_buttons.pop(trigger, None)
            pending = self.gestures.pop(trigger, None)
            if pending is not None:
                pending.cancel()
                pending.deleteLater()
            self.holds.release(trigger)
            self.mouse_holds.release(trigger)
            if self.switcher.owner == trigger:
                self.switcher.close()
            if self.overlays.cheat_owner == trigger:
                self.overlays.close_cheat_sheet()
            if self.overlays.grid_owner == trigger:
                self.overlays.close_grid()
            if self.overlays.nine_key_owner == trigger:
                self.overlays.close_nine_key()
            self.overlays.clear_physical_press(trigger)
            if self.macros.owner == trigger:
                self.macros.close()
            else:
                self.macros.clear_physical_press(trigger)

    def _run_gesture_action(self, action, trigger, down=True):
        try:
            self._action_feedback(self._run_action(action, down, trigger))
        except Exception as exc:
            self.fail(str(exc))

    def _switcher_controls(self):
        return {
            code: button.id
            for button in self.configurable_buttons()
            for code in button.all_triggers
        }

    def _nine_key_nav_by_id(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        actions = [
            getattr(button, field)
            for button in self.profile.buttons
            for field in GESTURE_FIELDS.values()
        ] + [
            action
            for layer in getattr(self.profile, "app_layers", ())
            for overrides in layer.overrides.values()
            for action in overrides.values()
        ]
        for action in actions:
            if action.kind != "nine_key":
                continue
            bindings = action.spec.get("bindings") or {}
            for role, ident in bindings.items():
                canonical = _NINE_KEY_TO_NAV.get(role)
                if canonical and ident and ident not in mapping:
                    mapping[ident] = canonical
        return mapping

    def _button_canonical_nav(self, button: Button, nine_key_roles: dict[str, str]) -> str:
        if button.role in ROLE_TO_NAV_ID:
            return ROLE_TO_NAV_ID[button.role]
        if self.fixed_model or button.id in _CANONICAL_NAV_IDS:
            return button.id
        if button.id in nine_key_roles:
            return nine_key_roles[button.id]
        short_name = button.name.split("·", 1)[0].strip().casefold()
        if short_name in _NAV_NAME_MAP:
            return _NAV_NAME_MAP[short_name]
        for code in button.all_triggers:
            if code in _STANDARD_HID_NAV:
                return _STANDARD_HID_NAV[code]
        return button.id

    def _navigation_controls(self) -> dict[str, str]:
        """Map triggers to canonical nav roles (tv-left/right/ok/back/up/down/number) for overlays."""
        controls = {} if self.fixed_model else dict(_STANDARD_HID_NAV)
        nine_key_roles = self._nine_key_nav_by_id()
        for button in self.configurable_buttons():
            role = self._button_canonical_nav(button, nine_key_roles)
            for code in button.all_triggers:
                controls[code] = role
        return controls

    def _navigation_role(self, trigger: str) -> str | None:
        if not trigger:
            return None
        return self._navigation_controls().get(trigger)
