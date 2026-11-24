"""Profile management and foreground-app layer overrides for ``AppController``."""
from __future__ import annotations

from pathlib import Path
import uuid

from .app_layers import LayerApp, layer_app_from_path, match_app_layer, running_layer_apps
from .inventory import receiver_name
from .model import (
    Action, AppLayer, Button, GESTURE_FIELDS, MAX_APP_LAYERS, Profile,
    atomic_write, effective_button, validate_app_layer_id,
)
from .photo_template import apply_fixed_keys
from .remote_library import RECEIVER_MODEL
from .store import MAX_PROFILES


class ProfileLayerMixin:
    def profile_names(self):
        counts: dict[str, int] = {}
        entries = []
        for p in self.profiles:
            base = (p.name or "").strip() or receiver_name(p, self.monitor.devices)
            counts[base] = counts.get(base, 0) + 1
            entries.append((base, p))
        seen: dict[str, int] = {}
        labels = []
        for base, p in entries:
            if counts[base] > 1:
                seen[base] = seen.get(base, 0) + 1
                hw = receiver_name(p, self.monitor.devices)
                labels.append(f"{base} #{seen[base]} ({hw})")
            else:
                labels.append(base)
        return labels

    # -- context-aware app layers ---------------------------------------------

    @property
    def editing_layer(self) -> AppLayer | None:
        if not self.editing_layer_id:
            return None
        layer = next((item for item in self.profile.app_layers if item.id == self.editing_layer_id), None)
        if layer is None:
            self.editing_layer_id = ""
        return layer

    @property
    def app_layers_enabled(self) -> bool:
        return bool(self.settings.get("app_layers_enabled", True))

    def _sync_layer_monitor(self):
        active = self.app_layers_enabled and any(layer.enabled for layer in self.profile.app_layers)
        self.foreground_apps.set_watching(active)

    def _drop_idle_gestures(self):
        for trigger in list(self.gestures):
            gesture = self.gestures[trigger]
            if not gesture.holding and trigger not in self.pressed:
                gesture.cancel()
                gesture.deleteLater()
                self.gestures.pop(trigger, None)

    def _on_foreground_app_changed(self, app_id: str, name: str):
        self._drop_idle_gestures()
        if self.overlays.nine_key_open and self.nine_key_app_id and app_id != self.nine_key_app_id:
            self.overlays.close_nine_key()
        if self.overlays.cheat_open:
            self.overlays.refresh_cheat_context(self.profile, self.active_runtime_layer(), name)

    def active_runtime_layer(self) -> AppLayer | None:
        if not self.app_layers_enabled or not self.profile.app_layers:
            return None
        return match_app_layer(self.profile, self.foreground_apps.current_app_id())

    def effective_runtime_button(self, button: Button) -> Button:
        if self._is_voice_button(button):
            return button
        return effective_button(button, self.active_runtime_layer())

    def set_app_layers_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        if enabled == self.app_layers_enabled:
            return True
        had_key = "app_layers_enabled" in self.settings
        previous = self.settings.get("app_layers_enabled")
        self.settings["app_layers_enabled"] = enabled
        if not self.save():
            if had_key:
                self.settings["app_layers_enabled"] = previous
            else:
                self.settings.pop("app_layers_enabled", None)
            self.mapping_changed.emit()
            return False
        self._sync_layer_monitor()
        self._drop_idle_gestures()
        self.mapping_changed.emit()
        self.tell("已开启按前台应用自动切换按键层。" if enabled else "已暂停自动切层，当前使用全局默认层。")
        return True

    def select_editing_layer(self, layer_id: str) -> bool:
        target = ""
        if layer_id:
            layer = next((item for item in self.profile.app_layers if item.id == layer_id), None)
            if layer is None:
                return False
            target = layer.id
        if target == self.editing_layer_id:
            return True
        self.editing_layer_id = target
        self.mapping_changed.emit()
        return True

    def available_layer_apps(self) -> list[LayerApp]:
        if not self.foreground_apps.live:
            return []
        try:
            return running_layer_apps()
        except Exception:
            return []

    def base_action(self, button_id: str, gesture: str = "single") -> Action:
        button = next((b for b in self.profile.buttons if b.id == button_id), None)
        if button is None or gesture not in GESTURE_FIELDS:
            return Action()
        return getattr(button, GESTURE_FIELDS[gesture])

    def layer_override_action(self, button_id: str, gesture: str = "single", layer: AppLayer | None = None) -> Action | None:
        target = self.editing_layer if layer is None else layer
        if target is None:
            return None
        return target.overrides.get(button_id, {}).get(gesture)

    def is_gesture_overridden(self, button_id: str, gesture: str = "single", layer: AppLayer | None = None) -> bool:
        return self.layer_override_action(button_id, gesture, layer) is not None

    def layer_override_count(self, layer: AppLayer | None = None) -> tuple[int, int]:
        target = self.editing_layer if layer is None else layer
        keys = [b for b in self.configurable_buttons() if not self._is_voice_button(b)]
        if target is None:
            return 0, len(keys)
        valid = {b.id for b in keys}
        return sum(1 for bid, gestures in target.overrides.items() if bid in valid and gestures), len(keys)

    def add_app_layer(self, app: LayerApp) -> AppLayer | None:
        try:
            app_id = validate_app_layer_id(app.app_id)
        except ValueError as exc:
            self.fail(str(exc))
            return None
        name = (app.name or "").strip()[:100] or app_id
        existing = next((item for item in self.profile.app_layers if item.app_id.casefold() == app_id.casefold()), None)
        if existing is not None:
            self.editing_layer_id = existing.id
            self.mapping_changed.emit()
            self.tell(f"已切换到「{existing.name}」应用层。")
            return existing
        if len(self.profile.app_layers) >= MAX_APP_LAYERS:
            self.fail(f"每个设备最多创建 {MAX_APP_LAYERS} 个应用层")
            return None
        layer = AppLayer(app_id=app_id, name=name)
        self.profile.app_layers.append(layer)
        if not self.save():
            self.profile.app_layers.pop()
            return None
        self.editing_layer_id = layer.id
        self._sync_layer_monitor()
        self._drop_idle_gestures()
        self.mapping_changed.emit()
        self.tell(f"已创建「{layer.name}」应用层；未单独设置的按键自动继承全局默认。")
        return layer

    def add_app_layer_from_path(self, path: str | Path, *, platform: str | None = None) -> AppLayer | None:
        try:
            app = layer_app_from_path(path, platform=platform)
        except Exception as exc:
            self.fail(str(exc))
            return None
        return self.add_app_layer(app)

    def set_app_layer_enabled(self, layer_id: str, enabled: bool) -> bool:
        layer = next((item for item in self.profile.app_layers if item.id == layer_id), None)
        if layer is None:
            return False
        enabled = bool(enabled)
        if layer.enabled == enabled:
            return True
        previous = layer.enabled
        layer.enabled = enabled
        if not self.save():
            layer.enabled = previous
            self.mapping_changed.emit()
            return False
        self._sync_layer_monitor()
        self._drop_idle_gestures()
        self.mapping_changed.emit()
        self.tell(f"已启用「{layer.name}」应用层。" if enabled else f"已停用「{layer.name}」应用层。")
        return True

    def rename_app_layer(self, layer_id: str, name: str) -> bool:
        layer = next((item for item in self.profile.app_layers if item.id == layer_id), None)
        if layer is None:
            return False
        name = name.strip()
        if not name or len(name) > 100:
            self.fail("应用层名称应为 1–100 字")
            return False
        if name == layer.name:
            return True
        previous = layer.name
        layer.name = name
        if not self.save():
            layer.name = previous
            return False
        self.mapping_changed.emit()
        self.tell("应用层已重命名。")
        return True

    def clear_app_layer_overrides(self, layer_id: str) -> bool:
        layer = next((item for item in self.profile.app_layers if item.id == layer_id), None)
        if layer is None:
            return False
        if not layer.overrides:
            self.tell(f"「{layer.name}」当前没有专属按键覆盖。")
            return True
        previous = {bid: dict(gestures) for bid, gestures in layer.overrides.items()}
        layer.overrides.clear()
        if not self.save():
            layer.overrides.update(previous)
            return False
        if self.action_undo and len(self.action_undo) >= 5 and self.action_undo[4] == layer.id:
            self.action_undo = None
        self._drop_idle_gestures()
        self.mapping_changed.emit()
        self.tell(f"已清空「{layer.name}」的专属设置，全部按键恢复继承全局默认。")
        return True

    def delete_app_layer(self, layer_id: str) -> bool:
        idx = next((i for i, item in enumerate(self.profile.app_layers) if item.id == layer_id), None)
        if idx is None:
            return False
        removed = self.profile.app_layers.pop(idx)
        if not self.save():
            self.profile.app_layers.insert(idx, removed)
            return False
        if self.editing_layer_id == removed.id:
            self.editing_layer_id = ""
        if self.action_undo and len(self.action_undo) >= 5 and self.action_undo[4] == removed.id:
            self.action_undo = None
        self._sync_layer_monitor()
        self._drop_idle_gestures()
        self.mapping_changed.emit()
        self.tell(f"已删除「{removed.name}」应用层。")
        return True

    # -- profiles --------------------------------------------------------------

    def select_profile(self, index, *, keep_capture=True):
        if not 0 <= index < len(self.profiles):
            return
        old_profile = self.profile
        new_profile = self.profiles[index]
        same_hardware = (
            keep_capture
            and (old_profile.vendor, old_profile.product) == (new_profile.vendor, new_profile.product)
        )
        self.voice.cancel()
        self._cancel_hide_result()
        if self.test_timer.isActive():
            self.test_timer.stop()
            self.test_action = None
            self.test_pending.emit(0)
        self._clear_gestures()
        self.pressed.clear()
        self.press_buttons.clear()
        try:
            self.keys.release()
        except Exception:
            pass
        if same_hardware and self.enabled:
            if getattr(self.device, "windows", None) is not None:
                self.device.windows.profile = new_profile
            if getattr(self.device, "linux_evdev", None) is not None:
                self.device.linux_evdev.profile = new_profile
        else:
            if self.usb.authenticated or self.usb.ready or self.usb_connecting:
                self.usb_connecting = False
                self.recording_requested = False
                self.usb.stop()
            self.set_enabled(False)
            self.mapping_attempted = False
        self.action_undo = None
        self.synced_action_undo = []
        self._mouse_cleared_undo = None
        self.editing_layer_id = ""
        self.index = index
        if self.settings.get("active_profile_id") != new_profile.id and not self.load_error:
            self.settings["active_profile_id"] = new_profile.id
            try:
                self.store.save()
            except ValueError:
                pass
        self.selected_id = self._default_selection(self.selected_id)
        self._sync_layer_monitor()
        self.profiles_changed.emit()
        self.mapping_changed.emit()
        self.refresh_device()
        if same_hardware and keep_capture and not self.enabled:
            self.ensure_mapping()

    def _default_selection(self, preferred=""):
        keys = self.configurable_buttons()
        ids = [b.id for b in keys]
        for candidate in (preferred, "tv-number-1"):
            if candidate in ids:
                return candidate
        return ids[0] if ids else ""

    def _unique_profile_name(self, name):
        names = {p.name for p in self.profiles}
        if name not in names:
            return name
        number = 2
        base = name[:95]
        while f"{base} {number}" in names:
            number += 1
        return f"{base} {number}"

    def next_new_profile_name(self, base: str = "新方案") -> str:
        return self._unique_profile_name(base)

    def create_profile(self, name: str = "", *, copy_current: bool = False) -> bool:
        try:
            if len(self.profiles) >= MAX_PROFILES:
                raise ValueError(f"最多保存 {MAX_PROFILES} 个设备配置")
            if name:
                cleaned = name.strip()
                if not cleaned or len(cleaned) > 100:
                    raise ValueError("配置名称应为 1–100 字")
                base_name = cleaned
            else:
                base_name = f"{self.profile.name} 副本" if copy_current else "新方案"
            if not self.has_remote:
                raise ValueError("请先添加遥控器")
            if copy_current:
                profile = Profile.loads(self.profile.dumps())
            elif self.fixed_model:
                from .photo_template import television_profile
                profile = television_profile()
            else:
                # Same photo and learned keys, no actions or app layers.
                profile = Profile.loads(self.profile.dumps())
                profile.app_layers = []
                for button in profile.buttons:
                    for name in GESTURE_FIELDS.values():
                        setattr(button, name, Action())
            apply_fixed_keys(profile, model_id=RECEIVER_MODEL if self.fixed_model else "")
            profile.id = uuid.uuid4().hex
            profile.name = self._unique_profile_name(base_name)
            profile.validate()
            self.profiles.append(profile)
            if not self.save():
                self.profiles.pop()
                return False
            self.select_profile(len(self.profiles) - 1, keep_capture=True)
            self.tell(f"已新建配置方案「{profile.name}」。")
            return True
        except Exception as exc:
            self.fail(str(exc))
            return False

    def import_profile(self, path, replace=False):
        try:
            if not self.has_remote:
                raise ValueError("请先添加遥控器")
            path = Path(path)
            if path.stat().st_size > 14_000_000:
                raise ValueError("配置文件过大")
            profile = Profile.loads(path.read_text(encoding="utf-8"))
            if self.remote is not None:
                accepted = {(c.vendor, c.product) for c in self.remote.connections}
                if accepted and (profile.vendor, profile.product) not in accepted:
                    raise ValueError(
                        f"这份按键配置对应型号 {profile.vendor:04X}:{profile.product:04X}，"
                        f"与当前遥控器「{self.remote.name}」不匹配；请先切换到对应遥控器后再导入。"
                    )
            apply_fixed_keys(profile, model_id=RECEIVER_MODEL if self.fixed_model else "")
            profile.validate()
            if replace:
                profile.id = self.profile.id
                previous = self.profile
                self.profiles[self.index] = profile
                if not self.save():
                    self.profiles[self.index] = previous
                    return False
                self.select_profile(self.index, keep_capture=False)
            else:
                profile.id = uuid.uuid4().hex
                profile.name = self._unique_profile_name(profile.name)
                if len(self.profiles) >= MAX_PROFILES:
                    raise ValueError(f"最多保存 {MAX_PROFILES} 个设备配置")
                self.profiles.append(profile)
                if not self.save():
                    self.profiles.pop()
                    return False
                self.select_profile(len(self.profiles) - 1, keep_capture=False)
            self.tell("按键配置已导入；不会自动启用或执行动作。")
            return True
        except Exception as exc:
            self.fail(str(exc))
            return False

    def rename_profile(self, name):
        name = name.strip()
        if not name or len(name) > 100:
            self.fail("配置名称应为 1–100 字")
            return False
        if name == self.profile.name:
            return True
        previous = self.profile.name
        self.profile.name = name
        if not self.save():
            self.profile.name = previous
            return False
        self.profiles_changed.emit()
        self.tell("配置已重命名。")
        return True

    def delete_profile(self):
        if len(self.profiles) <= 1:
            self.fail("至少需要保留一个按键配置")
            return False
        index = self.index
        removed = self.profiles.pop(index)
        if not self.save():
            self.profiles.insert(index, removed)
            return False
        self.select_profile(min(index, len(self.profiles) - 1))
        self.tell("按键配置已删除。")
        return True

    def export_profile(self, path):
        try:
            atomic_write(Path(path), self.profile.dumps())
            self.tell("按键配置已导出，包含图片与按键动作，不包含语音设置。")
        except Exception as exc:
            self.fail(str(exc))

    def _cycle_profile_action(self, owner: str):
        candidates = [
            i for i, p in enumerate(self.profiles)
            if (p.vendor, p.product) == (self.profile.vendor, self.profile.product)
        ]
        names = self.profile_names()
        if len(candidates) <= 1:
            cur_name = names[self.index] if names else self.profile.name
            hw = receiver_name(self.profile, self.monitor.devices)
            display = "默认方案" if cur_name == hw and cur_name else cur_name
            self.overlays.show_toast(display, subtitle="当前仅有 1 套配置方案", badge="1/1")
            return f"当前仅有 1 套配置方案（{display}）。"
        was_pressed = bool(owner and owner in self.pressed)
        pos = candidates.index(self.index) if self.index in candidates else 0
        next_pos = (pos + 1) % len(candidates)
        next_idx = candidates[next_pos]
        self.select_profile(next_idx, keep_capture=True)
        if was_pressed and owner:
            self._switched_release = owner
            self.pressed.add(owner)
        names = self.profile_names()
        chosen_name = names[self.index] if self.index < len(names) else self.profile.name
        badge = f"{next_pos + 1}/{len(candidates)}"
        self.overlays.show_toast(chosen_name, subtitle="已切换配置方案", badge=badge)
        return f"已切换至配置方案「{chosen_name}」（{badge}）。"
