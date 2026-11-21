"""Application state and rules, kept apart from widget layout.

The window builds widgets and forwards user intent here. It never mutates a
Profile or talks to a device itself. Everything below runs without showing a
window; see tests/test_controller.py. Data flow and invariants: ARCHITECTURE.md.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
import sys
import time
import uuid
import wave

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaDevices

from .actions import ActionRunner, KeyOutput
from .app_layers import ForegroundAppTracker, LayerApp, layer_app_from_path, match_app_layer, running_layer_apps
from .branding import BRAND
from .audio import AudioBridge
from .devices import DeviceInput
from .inventory import USBMonitor, receiver_name, receiver_state
from .macro_runner import MacroRunner
from .model import Action, AppLayer, Button, GESTURE_FIELDS, MAX_APP_LAYERS, MOUSE_BUTTONS, Profile, atomic_write, effective_button, validate_app_layer_id
from .overlays import OverlayManager
from . import autostart, permissions
from .photo_template import apply_fixed_keys, is_fixed_model, is_voice_button
from .presentation import action_text
from .session import VoiceSession
from .store import MAX_PROFILES, ConfigStore, validate_settings
from .usb_bridge import USBBridge
from .virtual_mic import VirtualMic, is_windows_cable, is_voice_output
from .windows import PendingHide
from .launch import Launcher, LaunchRequest
from .diagnostics import Diagnostics

TEST_DELAY = 3  # seconds between "测试" and running the action


@dataclass(frozen=True)
class StatusView:
    """What a status pill shows. tone: neutral | ready | busy | ok | error."""
    title: str
    detail: str = ""
    tone: str = "neutral"
    diagnostic: str = ""
    settings_link: bool = False


def _b64(raw):
    return base64.b64encode(raw).decode()


class AppController(QObject):
    # A dongle that briefly re-enumerates (voice key, hub power event) must not
    # tear down a working capture; only a persistent absence counts as unplugged.
    ABSENCE_GRACE = 3.0
    message = Signal(str, bool)       # text, is_error
    profiles_changed = Signal()       # list or current index changed
    mapping_changed = Signal()        # selection, action, name, rect or photo changed
    physical_key = Signal(str, bool)  # verified key event for first-run feedback
    device_changed = Signal()         # receiver state / enabled / ready changed
    learning_changed = Signal(bool)
    voice_changed = Signal()          # settings, outputs, USB link or session owner changed
    level = Signal(float)
    test_pending = Signal(int)        # seconds left; 0 = idle
    permission_opened = Signal(str)   # Show the companion guide only on user request.
    update_found = Signal(object)     # updates.Update
    update_none = Signal()
    update_error = Signal(str)
    update_downloaded = Signal(str)

    def __init__(self, directory, parent=None, *, auto_mapping=True):
        super().__init__(parent)
        self.store = ConfigStore(Path(directory))
        self.store.load()
        from .chat_focus_service import ChatFocusService
        self.chat_focus = ChatFocusService(self.store.directory, self, live=auto_mapping)
        self.diagnostics = Diagnostics(self.store.directory / "logs")
        self.diagnostics.write("app_started")
        self._connection_feedback = False
        self._inventory_signature = None
        self.message.connect(self._feedback_replaced)
        active_id = self.settings.get("active_profile_id", "")
        self.index = next((i for i, p in enumerate(self.profiles) if p.id == active_id), 0)
        self.selected_id = ""
        self.editing_layer_id = ""
        self.action_undo = None
        self.synced_action_undo = []
        self._mouse_cleared_undo = None
        self.enabled = False
        self.keys_ready = False
        self.learning = False
        self.pressed = set()
        self.press_buttons: dict[str, Button] = {}
        self.switcher_pressed = set()
        self.gestures = {}
        self.foreground_apps = ForegroundAppTracker(self, live=auto_mapping)
        self.foreground_apps.changed.connect(self._on_foreground_app_changed)
        self.learned_release = None
        self._switched_release = None
        self.shutting_down = False
        self.host = None  # widget whose winId() Windows raw input registers on
        # Key input starts by itself when a supported receiver appears. A failed
        # attempt is not retried on every 2 s scan; retry_connection() re-arms it.
        self.auto_mapping = auto_mapping
        self.mapping_attempted = False
        self.mapping_error = ""
        self.transient_error = ""
        self.recovery_attempts = 0
        self.helper_missing = False
        self._absent_since = None
        self.usb_connecting = False
        self.recording_requested = False   # remote key asked for a recording
        self.voice_auto_pending = False    # audio arrived before its physical key
        self.voice_trigger = None          # physical trigger that started it
        self.outputs = []  # [(description, id bytes)]
        self.keys = KeyOutput()
        self.launcher = Launcher(self)
        self.launcher.finished.connect(self.message)
        self.audio = AudioBridge(self)
        route = None
        if auto_mapping and sys.platform == "darwin":
            from .mac_input import MacInputRoute
            route = MacInputRoute(self)
            route.measured.connect(lambda operation, elapsed: self.diagnostics.write(
                "default_input_route", operation=operation, milliseconds=round(elapsed, 1)))
        elif auto_mapping and sys.platform == "win32":
            from .win_audio_route import WindowsInputRoute
            route = WindowsInputRoute(self, output_name=self._selected_output_description)
            route.measured.connect(lambda operation, elapsed: self.diagnostics.write(
                "default_input_route", operation=operation, milliseconds=round(elapsed, 1)))
        elif auto_mapping and sys.platform.startswith("linux"):
            from .linux_audio_route import LinuxInputRoute
            route = LinuxInputRoute(self)
            route.measured.connect(lambda operation, elapsed: self.diagnostics.write(
                "default_input_route", operation=operation, milliseconds=round(elapsed, 1)))
        self.voice = VoiceSession(self.audio, self.keys, self, route=route)
        self.voice.log = self.diagnostics.write
        self.keys.log = self.diagnostics.write
        self.audio.log = self.diagnostics.write
        self.overlays = OverlayManager(self)
        self.overlays.grid_activated.connect(self._execute_grid_action)
        self.macros = MacroRunner(self._execute_macro_step, self, send_enter=lambda: self.keys.shortcut(["enter"]))
        self.macros.finished.connect(self.tell)
        self.macros.aborted.connect(self.tell)
        self.macros.notice.connect(self.tell)
        self.macros.failed.connect(self.fail)
        self.text_enter_timer = QTimer(self)
        self.text_enter_timer.setSingleShot(True)
        self.text_enter_timer.setInterval(MacroRunner.TEXT_PASTE_SETTLE_MS)
        self.text_enter_timer.timeout.connect(self._fire_text_enter)
        self._voice_focus_token = 0
        self._voice_focus_timer = QTimer(self)
        self._voice_focus_timer.setSingleShot(True)
        # Below VoiceSession's one-second pending-audio budget; a slow AX
        # check never drops speech, it only stops holding the recognizer.
        self._voice_focus_timer.setInterval(600)
        self._voice_focus_timer.timeout.connect(self._on_voice_focus_timeout)
        self.voice.activation_gate = self._pre_voice_focus_gate
        self.runner = ActionRunner(
            self.keys,
            self.voice_button,
            overlay=self._run_overlay_action,
            macro=self._run_macro_action,
            cycle_profile=self._cycle_profile_action,
            text_enter=self._schedule_text_enter,
        )
        self.runner.windows.log = self.diagnostics.write
        self.switcher = self.runner.switcher
        self.switcher.log = self.diagnostics.write
        self.holds = self.runner.holds
        self.mouse_holds = self.runner.mouse_holds
        self.pending_hide = None
        self.hide_timer = QTimer(self)
        self.hide_timer.setInterval(50)
        self.hide_timer.timeout.connect(self._check_hide_result)
        self.device = DeviceInput(self)
        # All three desktop platforms hand decoded frames from their collector
        # to the same USBBridge without spawning a legacy separate worker.
        self.usb = USBBridge(self.voice, self.store.directory / "runtime", self,
                             collector=self.device if (sys.platform in ("darwin", "win32") or sys.platform.startswith("linux")) else None,
                             log=self.diagnostics.write)
        self.monitor = USBMonitor(self)
        self.virtual_mic = VirtualMic(self)
        self.microphone_installed = self.virtual_mic.installed()
        self.microphone_watch = QTimer(self)
        self.microphone_watch.setInterval(2000)
        self.microphone_watch.timeout.connect(self._check_microphone_installation)
        if sys.platform == "darwin":
            self.microphone_watch.start()
        self.media_devices = QMediaDevices(self)
        self.media_devices.audioOutputsChanged.connect(self.refresh_outputs)
        self.wav = None
        self.wav_timer = QTimer(self)
        self.wav_timer.setInterval(20)
        self.wav_timer.timeout.connect(self._wav_tick)
        self.test_action = None
        self.test_left = 0
        self.test_timer = QTimer(self)
        self.test_timer.setInterval(1000)
        self.test_timer.timeout.connect(self._test_tick)
        self.recovery_timer = QTimer(self)
        self.recovery_timer.setSingleShot(True)
        self.recovery_timer.setInterval(750)
        self.recovery_timer.timeout.connect(self._recover_transient)

        self.device.key.connect(self.on_key)
        self.usb.key.connect(self.on_key)
        self.device.stopped.connect(self._device_stopped)
        self.device.ready.connect(self.input_ready)
        self.monitor.changed.connect(self._inventory_changed)
        self.usb.changed.connect(self.refresh_device)
        self.usb.changed.connect(self.voice_changed)
        self.usb.started.connect(self._usb_ready)
        self.usb.disconnected.connect(self._usb_released)
        self.usb.recording_stopped.connect(self._recording_stopped)
        self.usb.recording_started.connect(self._recording_started)
        self.device.failed.connect(self._device_failed)
        collector_tag = "linux_collector" if sys.platform.startswith("linux") else "win_collector"
        self.device.log.connect(lambda message, tag=collector_tag: self.diagnostics.write(tag, message=message))
        for source in (self.device, self.usb, self.virtual_mic):
            source.status.connect(self.tell)
            if source is not self.device:
                source.failed.connect(self.fail)
        self.audio.changed.connect(self.tell)
        self.audio.level.connect(self.level)
        self.voice.failed.connect(self.fail)
        self.voice.expired.connect(self._voice_expired)
        self.voice.changed.connect(self.voice_changed)
        self.voice.finished.connect(self._voice_finished)

        self._apply_voice_settings()
        self.refresh_outputs()
        self.selected_id = self._default_selection()
        self._sync_layer_monitor()

    # -- read-only views -----------------------------------------------------

    @property
    def profiles(self):
        return self.store.profiles

    @property
    def settings(self):
        return self.store.settings

    @property
    def load_error(self):
        return self.store.load_error

    @property
    def profile(self) -> Profile:
        return self.profiles[min(max(0, self.index), len(self.profiles) - 1)]

    @property
    def fixed_model(self):
        return is_fixed_model(self.profile)

    @property
    def selected(self) -> Button | None:
        return next((b for b in self.profile.buttons if b.id == self.selected_id and b.configurable), None)

    def configurable_buttons(self):
        return [b for b in self.profile.buttons if b.configurable]

    @property
    def selected_voice_key(self):
        return is_voice_button(self.profile, self.selected)

    def assigned_count(self):
        keys = [b for b in self.configurable_buttons() if not is_voice_button(self.profile, b)]
        return sum(any(action.kind != "none" for action in (b.action, b.double_action, b.long_action))
                   for b in keys), len(keys)

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
        if self.overlays.cheat_open:
            self.overlays.refresh_cheat_context(self.profile, self.active_runtime_layer(), name)

    def active_runtime_layer(self) -> AppLayer | None:
        if not self.app_layers_enabled or not self.profile.app_layers:
            return None
        return match_app_layer(self.profile, self.foreground_apps.current_app_id())

    def effective_runtime_button(self, button: Button) -> Button:
        if is_voice_button(self.profile, button):
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
        if not self.auto_mapping:
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
        keys = [b for b in self.configurable_buttons() if not is_voice_button(self.profile, b)]
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

    # -- feedback & persistence ----------------------------------------------

    def _feedback_replaced(self, text, error):
        self._connection_feedback = False

    def tell(self, text):
        self.message.emit(text, False)

    def fail(self, text):
        self.diagnostics.write("error", kind="application")
        self.message.emit(str(text), True)

    def _cancel_hide_result(self):
        self.hide_timer.stop()
        self.pending_hide = None

    def _action_feedback(self, result):
        if isinstance(result, LaunchRequest):
            self.launcher.start(result)
        elif isinstance(result, PendingHide):
            self.pending_hide = result
            self.hide_deadline = time.monotonic() + 1.0
            self.tell(getattr(result, "request_message", "已请求收起应用"))
            self.hide_timer.start()
        elif isinstance(result, str):
            self.tell(result)

    def _check_hide_result(self):
        if self.pending_hide is None:
            return
        try:
            result = self.pending_hide.poll(expired=time.monotonic() >= self.hide_deadline)
        except Exception as exc:
            result = (f"无法检查收起结果：{exc}", True)
        if result is not None:
            self._cancel_hide_result()
            self.message.emit(*result)

    def save(self):
        try:
            self.store.save()
            return True
        except Exception as exc:
            self.fail(str(exc))
            return False

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
        self.set_learning(False)
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
            if copy_current:
                profile = Profile.loads(self.profile.dumps())
            else:
                from .photo_template import television_profile
                profile = television_profile()
            apply_fixed_keys(profile)
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
            path = Path(path)
            if path.stat().st_size > 14_000_000:
                raise ValueError("配置文件过大")
            profile = Profile.loads(path.read_text(encoding="utf-8"))
            apply_fixed_keys(profile)
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

    # -- mapping ---------------------------------------------------------------

    def select(self, ident):
        """Select a configurable key. Blocked keys (电源/TV) are ignored."""
        button = next((b for b in self.profile.buttons if b.id == ident), None)
        if button is None or not button.configurable or ident == self.selected_id:
            return
        self.selected_id = ident
        self.mapping_changed.emit()

    def find_cycle_profile_targets(self, button_id: str, gesture: str = "single"):
        """Return (empty_targets, conflicting_targets) on sibling same-hardware profiles."""
        if gesture not in GESTURE_FIELDS:
            return [], []
        src_btn = next((b for b in self.profile.buttons if b.id == button_id and b.configurable), None)
        if src_btn is None:
            return [], []
        field = GESTURE_FIELDS[gesture]
        empty_targets = []
        conflicts = []
        names = self.profile_names()
        for idx, other in enumerate(self.profiles):
            if other is self.profile or (other.vendor, other.product) != (self.profile.vendor, self.profile.product):
                continue
            target_btn = next(
                (
                    b for b in other.buttons
                    if b.configurable and not is_voice_button(other, b)
                    and (b.id == src_btn.id or (src_btn.trigger and b.trigger == src_btn.trigger))
                ),
                None,
            )
            if target_btn is None:
                continue
            existing = getattr(target_btn, field)
            scheme_label = names[idx] if idx < len(names) else other.name
            if existing.kind == "none":
                empty_targets.append((other, target_btn, scheme_label))
            elif existing.kind != "cycle_profile":
                conflicts.append((other, target_btn, existing, scheme_label))
        return empty_targets, conflicts

    def assign(self, action: Action, gesture="single", *, explicit_override=False, sync_conflicts=False):
        button = self.selected
        if gesture not in GESTURE_FIELDS:
            raise ValueError("未知按键手势")
        if button is None:
            self.fail("请先在图上选择一个按键")
            return False
        if self.selected_voice_key and (gesture != "single" or action != Action("voice")):
            self.fail("这个实体语音键固定用于开始和结束语音输入，请在右侧设置麦克风、唤起键和说话方式。")
            return False
        if gesture != "single" and action.kind == "voice":
            self.fail("双击和长按不支持语音动作")
            return False
        if action.kind == "mouse":
            if gesture != "single":
                self.fail("绑定鼠标按键会独占整颗按键，请在「单击」位置选择（已含单击、双击和长按）。")
                return False
            if action.target not in MOUSE_BUTTONS:
                self.fail("实体按键只支持绑定鼠标左键、右键或中键")
                return False
        try:
            action.validate(sys.platform)
        except ValueError as exc:
            self.fail(str(exc))
            return False
        gesture_label = {"single": "", "double": "双击", "long": "长按"}[gesture]
        prefix = f"「{button.name}」{gesture_label}"
        layer = self.editing_layer
        if layer is not None:
            if self.selected_voice_key:
                return True
            overrides = layer.overrides.get(button.id, {})
            effective_single = overrides.get("single", button.action)
            if gesture != "single" and effective_single.kind == "mouse" and action.kind != "none":
                self.fail("该按键当前已独占模拟鼠标，不支持同时设置双击或长按；如需变更，请先修改单击动作。")
                return False
            had_override = gesture in overrides
            previous = overrides.get(gesture, Action())
            cleared_layer_gestures: dict[str, Action] = {}
            if action.kind == "none" and not explicit_override:
                if not had_override:
                    return True
                self._cancel_button_actions(button.trigger)
                overrides.pop(gesture, None)
                if not overrides:
                    layer.overrides.pop(button.id, None)
                if not self.save():
                    layer.overrides.setdefault(button.id, {})[gesture] = previous
                    self.mapping_changed.emit()
                    return False
                self.action_undo = (self.profile.id, button.id, gesture, previous, layer.id, True)
                self.synced_action_undo = []
                self._mouse_cleared_undo = None
                self.mapping_changed.emit()
                self.tell(f"{prefix}已恢复继承全局默认（{layer.name}）。")
                return True
            if had_override and action == previous:
                if action.kind != "mouse" or not any(g in overrides for g in ("double", "long")):
                    return True
            self._cancel_button_actions(button.trigger)
            if action.kind == "mouse":
                for extra_g in ("double", "long"):
                    if extra_g in overrides:
                        cleared_layer_gestures[extra_g] = overrides.pop(extra_g)
            layer.overrides.setdefault(button.id, {})[gesture] = action
            if not self.save():
                if had_override:
                    layer.overrides.setdefault(button.id, {})[gesture] = previous
                else:
                    layer.overrides.get(button.id, {}).pop(gesture, None)
                for extra_g, old_extra in cleared_layer_gestures.items():
                    layer.overrides.setdefault(button.id, {})[extra_g] = old_extra
                if not layer.overrides.get(button.id):
                    layer.overrides.pop(button.id, None)
                self.mapping_changed.emit()
                return False
            self.action_undo = (self.profile.id, button.id, gesture, previous, layer.id, had_override)
            self.synced_action_undo = []
            self._mouse_cleared_undo = (self.profile.id, button.id, layer.id, cleared_layer_gestures) if cleared_layer_gestures else None
            self.mapping_changed.emit()
            if action.kind == "none":
                self.tell(f"{prefix}在「{layer.name}」中已设为无操作。")
            elif action.kind == "mouse":
                self.tell(f"「{button.name}」（{layer.name}）已专属模拟{action_text(action)}（短按单点 · 连按双击 · 按住即长按）。")
            else:
                self.tell(f"{prefix}（{layer.name}） → {action_text(action)}，已保存。")
            return True
        if gesture != "single" and button.action.kind == "mouse" and action.kind != "none":
            self.fail("该按键当前已独占模拟鼠标，不支持同时设置双击或长按。")
            return False
        field = GESTURE_FIELDS[gesture]
        previous = getattr(button, field)
        cleared_base_gestures: dict[str, Action] = {}
        sync_targets = []
        if action.kind == "cycle_profile":
            empty_targets, conflicts = self.find_cycle_profile_targets(button.id, gesture)
            sync_targets = [(prof, btn, Action()) for prof, btn, _label in empty_targets]
            if sync_conflicts:
                sync_targets.extend((prof, btn, old_act) for prof, btn, old_act, _label in conflicts)
        has_extra_base_gestures = action.kind == "mouse" and (button.double_action.kind != "none" or button.long_action.kind != "none")
        if action == previous and not sync_targets and not has_extra_base_gestures:
            return True
        self._cancel_button_actions(button.trigger)
        setattr(button, field, action)
        if action.kind == "mouse":
            if button.double_action.kind != "none":
                cleared_base_gestures["double"] = button.double_action
                button.double_action = Action()
            if button.long_action.kind != "none":
                cleared_base_gestures["long"] = button.long_action
                button.long_action = Action()
        for _prof, target_btn, _old_act in sync_targets:
            setattr(target_btn, field, Action("cycle_profile"))
        if not self.save():
            setattr(button, field, previous)
            for extra_g, old_extra in cleared_base_gestures.items():
                setattr(button, GESTURE_FIELDS[extra_g], old_extra)
            for _prof, target_btn, old_act in sync_targets:
                setattr(target_btn, field, old_act)
            self.mapping_changed.emit()
            return False
        self.action_undo = (self.profile.id, button.id, gesture, previous, "", False)
        self.synced_action_undo = [(prof.id, target_btn.id, gesture, old_act) for prof, target_btn, old_act in sync_targets]
        self._mouse_cleared_undo = (self.profile.id, button.id, "", cleared_base_gestures) if cleared_base_gestures else None
        self.mapping_changed.emit()
        if action.kind == "none":
            self.tell(prefix + "已清除动作。")
        elif action.kind == "mouse":
            extra_note = "（已清除原双击/长按，可点撤销恢复）" if cleared_base_gestures else ""
            self.tell(f"「{button.name}」已专属模拟{action_text(action)}（短按单点 · 连按双击 · 按住即长按）{extra_note}。")
        elif sync_targets:
            self.tell(f"{prefix} → {action_text(action)}，已保存（已同步到另外 {len(sync_targets)} 套方案）。")
        else:
            self.tell(prefix + " → " + action_text(action) + "，已保存。")
        return True

    def clear_action(self, gesture="single"):
        return self.assign(Action(), gesture)

    def mute_in_layer(self, gesture="single"):
        if self.editing_layer is None:
            return self.clear_action(gesture)
        return self.assign(Action("none"), gesture, explicit_override=True)

    def undo_action(self):
        if self.action_undo is None:
            return False
        if len(self.action_undo) == 4:
            profile_id, button_id, gesture, previous = self.action_undo
            layer_id, had_override = "", False
        else:
            profile_id, button_id, gesture, previous, layer_id, had_override = self.action_undo
        if self.profile.id != profile_id:
            return False
        button = next((b for b in self.profile.buttons if b.id == button_id), None)
        if button is None:
            self.action_undo = None
            self.mapping_changed.emit()
            return False
        if layer_id:
            layer = next((item for item in self.profile.app_layers if item.id == layer_id), None)
            if layer is None:
                self.action_undo = None
                self.mapping_changed.emit()
                return False
            current_had = gesture in layer.overrides.get(button.id, {})
            current_val = layer.overrides.get(button.id, {}).get(gesture, Action())
            self._cancel_button_actions(button.trigger)
            if had_override:
                layer.overrides.setdefault(button.id, {})[gesture] = previous
            else:
                layer.overrides.get(button.id, {}).pop(gesture, None)
            extra_cleared = {}
            if (self._mouse_cleared_undo
                    and self._mouse_cleared_undo[:3] == (self.profile.id, button.id, layer.id)):
                extra_cleared = self._mouse_cleared_undo[3]
                for extra_g, extra_old in extra_cleared.items():
                    layer.overrides.setdefault(button.id, {})[extra_g] = extra_old
            if not layer.overrides.get(button.id):
                layer.overrides.pop(button.id, None)
            if not self.save():
                if current_had:
                    layer.overrides.setdefault(button.id, {})[gesture] = current_val
                else:
                    layer.overrides.get(button.id, {}).pop(gesture, None)
                for extra_g in extra_cleared:
                    layer.overrides.get(button.id, {}).pop(extra_g, None)
                if not layer.overrides.get(button.id):
                    layer.overrides.pop(button.id, None)
                return False
            self.action_undo = None
            self._mouse_cleared_undo = None
            self.editing_layer_id = layer.id
            self.selected_id = button.id
            self.mapping_changed.emit()
            self.tell(f"已撤销「{button.name}」在「{layer.name}」中的上一次修改。")
            return True
        field = GESTURE_FIELDS[gesture]
        current = getattr(button, field)
        self._cancel_button_actions(button.trigger)
        setattr(button, field, previous)
        extra_base = {}
        if self._mouse_cleared_undo and self._mouse_cleared_undo[:3] == (self.profile.id, button.id, ""):
            extra_base = self._mouse_cleared_undo[3]
            for extra_g, extra_old in extra_base.items():
                setattr(button, GESTURE_FIELDS[extra_g], extra_old)
        restored_peers = []
        for peer_pid, peer_bid, peer_gesture, peer_old in self.synced_action_undo:
            peer_prof = next((p for p in self.profiles if p.id == peer_pid), None)
            if peer_prof is None:
                continue
            peer_btn = next((b for b in peer_prof.buttons if b.id == peer_bid), None)
            if peer_btn is None:
                continue
            peer_field = GESTURE_FIELDS[peer_gesture]
            restored_peers.append((peer_btn, peer_field, getattr(peer_btn, peer_field)))
            setattr(peer_btn, peer_field, peer_old)
        if not self.save():
            setattr(button, field, current)
            for extra_g in extra_base:
                setattr(button, GESTURE_FIELDS[extra_g], Action())
            for peer_btn, peer_field, peer_cur in restored_peers:
                setattr(peer_btn, peer_field, peer_cur)
            return False
        self.action_undo = None
        self.synced_action_undo = []
        self._mouse_cleared_undo = None
        self._mouse_cleared_undo = None
        self.editing_layer_id = ""
        self.selected_id = button.id
        self.mapping_changed.emit()
        self.tell(f"已撤销「{button.name}」的上一次动作修改。")
        return True

    def rename(self, name):
        button = self.selected
        name = name.strip()
        if button is None or name == button.name:
            return
        if not name or len(name) > 100:
            self.fail("按键名称应为 1–100 字")
            self.mapping_changed.emit()  # put the old name back in the field
            return
        previous = button.name
        button.name = name
        if not self.save():
            button.name = previous
        self.mapping_changed.emit()

    def set_rect(self, rect):
        button = self.selected
        if button is None:
            return
        x = min(max(0.0, rect[0]), .99)
        y = min(max(0.0, rect[1]), .99)
        w = max(.01, min(rect[2], 1 - x))
        h = max(.01, min(rect[3], 1 - y))
        rect = [x, y, w, h]
        if rect == button.rect:
            return
        previous = button.rect
        button.rect = rect
        if not self.save():
            button.rect = previous
        self.mapping_changed.emit()

    def add_button(self):
        if len(self.profile.buttons) >= 128:
            self.fail("每个设备最多 128 个按键")
            return None
        button = Button(name=f"按键 {len(self.profile.buttons) + 1}")
        self.profile.buttons.append(button)
        if not self.save():
            self.profile.buttons.pop()
            return None
        self.selected_id = button.id
        self.mapping_changed.emit()
        return button

    def remove_button(self):
        button = self.selected
        if button is None:
            return
        previous = self.profile.buttons
        previous_overrides = {
            layer.id: dict(layer.overrides[button.id])
            for layer in self.profile.app_layers
            if button.id in layer.overrides
        }
        self.profile.buttons = [b for b in previous if b.id != button.id]
        for layer in self.profile.app_layers:
            layer.overrides.pop(button.id, None)
        if not self.save():
            self.profile.buttons = previous
            for layer in self.profile.app_layers:
                if layer.id in previous_overrides:
                    layer.overrides[button.id] = previous_overrides[layer.id]
            return
        if self.action_undo and self.action_undo[1] == button.id:
            self.action_undo = None
        self.selected_id = self._default_selection()
        self.mapping_changed.emit()

    def replace_photo(self, path):
        try:
            if Path(path).stat().st_size > 20_000_000:
                raise ValueError("请选择小于 20 MB 的照片")
            image = QImage(str(path))
            if image.isNull():
                raise ValueError("图片无法读取")
            if max(image.width(), image.height()) > 1600:
                image = image.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            previous = self.profile.image
            self.profile.image = _b64(bytes(data))
            if not self.save():
                self.profile.image = previous
                return
        except Exception as exc:
            self.fail(str(exc))
            return
        self.mapping_changed.emit()
        self.tell("照片已替换。请检查按键位置，必要时在「名称与位置」里重新框选。")

    # -- device state ------------------------------------------------------------

    def _state(self):
        return receiver_state(self.profile, self.monitor.devices, checked=self.monitor.checked,
                              error=self.monitor.error, keys_ready=self.keys_ready, voice_ready=self.usb.ready)

    def _collector_missing(self):
        if sys.platform != "darwin":
            return False
        try:
            from .mac_device_helper import installation_status
            return installation_status()[0] == 1
        except Exception:
            return False

    def device_view(self) -> StatusView:
        state = self._state()
        if not state.usable:
            if self.monitor.error:
                return StatusView(state.title, "无法读取系统 USB 设备清单，请点「重新检测」。详情见通用设置。",
                                  "error", self.monitor.error, True)
            return StatusView(state.title, state.detail)
        if self.transient_error:
            return StatusView("正在重新连接", "USB 读取暂时中断，正在恢复按键映射。", "busy")
        if self.helper_missing and not self.enabled and not self.mapping_error:
            return StatusView("遥控器已接入，采集组件未安装",
                              "安装读取组件后才能读取按键和声音。",
                              "ready")
        if self.mapping_error and not self.usb.ready:
            return StatusView("设备连接出错", self.mapping_error, "error", self.mapping_error, True)
        if state.usable and self.enabled and not self.keys_ready and not self.usb.ready:
            return StatusView("正在接入遥控器按键", "USB 已识别，正在启动按键监听。", "busy")
        tone = "ok" if self.keys_ready or self.usb.ready else "ready"
        return StatusView(state.title, state.detail, tone)

    def _usb_holds_receiver(self):
        # macOS shares a single collector for keys and audio, so the receiver is
        # never held twice. Windows keeps Raw Input for keys and libusb for audio.
        return False

    def refresh_device(self):
        if self.shutting_down:
            return
        state = self._state()
        if state.code in ("absent", "unsupported"):
            if self.voice.owner == "trigger":
                # The receiver is gone; never leave a recognition shortcut held.
                self.voice.cancel()
            now = time.monotonic()
            if self._absent_since is None:
                self._absent_since = now
            if state.code == "unsupported" or now - self._absent_since >= self.ABSENCE_GRACE:
                # Unplugged for real: drop live input and retry on replug.
                self.mapping_attempted = False
                self.mapping_error = ""
                if self.enabled:
                    self.set_enabled(False)
                if self.usb.ready:
                    self.usb.stop()
        else:
            self._absent_since = None
        if self._connection_feedback:
            view = self.device_view()
            self.message.emit(view.detail, view.tone == "error")
            self._connection_feedback = True
        self.device_changed.emit()

    def _inventory_changed(self):
        state = self._state()
        receivers = [(d.connection, d.serial) for d in self.monitor.devices
                     if (d.vendor, d.product) == (self.profile.vendor, self.profile.product)]
        signature = (state.code if not state.usable else "present", self.monitor.error, receivers)
        if signature != self._inventory_signature:
            self.diagnostics.write("usb_inventory", state=signature[0], error=self.monitor.error[:1000])
            self._inventory_signature = signature
        self.refresh_device()
        if state.usable and self.transient_error and not self.recovery_timer.isActive():
            self.recovery_timer.start()
        self.ensure_mapping()

    def ensure_mapping(self):
        """Start key input once per plug-in / retry. Never in safe (smoke) mode."""
        if (self.shutting_down or not self.auto_mapping or self.enabled or self.mapping_attempted
                or self.usb_connecting or self._usb_holds_receiver()):
            return
        if not self._state().usable:
            return
        if self._collector_missing():
            # Fresh install: nothing to read keys with yet. This is a state, not
            # a failure, so the first launch must not open with a red error.
            self.mapping_attempted = True
            self.helper_missing = True
            self.device_changed.emit()
            return
        self.helper_missing = False
        self.mapping_attempted = True
        self.set_enabled(True)

    def retry_connection(self):
        self.diagnostics.write("connection_retry")
        self.recovery_timer.stop()
        self.transient_error = ""
        self.recovery_attempts = 0
        self.mapping_attempted = False
        self.mapping_error = ""
        self.helper_missing = False
        self.monitor.refresh()
        self.device_changed.emit()

    def _require_receiver(self, profile=None):
        if not self.monitor.checked or time.monotonic() - self.monitor.updated > 6:
            self.monitor.refresh()
            raise RuntimeError("请等待 USB 检测完成后再接入")
        state = receiver_state(profile or self.profile, self.monitor.devices,
                               checked=self.monitor.checked, error=self.monitor.error)
        if not state.usable:
            raise RuntimeError(state.title + "：" + state.detail)

    def set_enabled(self, on):
        on = bool(on)
        if on == self.enabled:
            self.device_changed.emit()
            return
        self.pressed.clear()
        self._clear_gestures()
        self.keys_ready = False
        try:
            if on:
                self._require_receiver()
                # The collector uses USB directly, not a GUI IOHID open. It
                # preflights its signed installation without requesting TCC.
                self.mapping_error = ""
                self.helper_missing = False
                self.enabled = True
                self.diagnostics.write("capture_start")
                self.device.start(self.profile, self.host)
            else:
                self.enabled = False
                self.diagnostics.write("capture_stop")
                if self.voice.owner == "trigger":
                    self.voice.cancel()  # release a held recognition shortcut
                self.device.stop()
                self.set_learning(False)
        except Exception as exc:
            self.enabled = False
            if on:
                self._device_failed(str(exc))
                self.device.stop()
            else:
                self.fail(str(exc))
        self.refresh_device()

    def input_ready(self, generation):
        if generation == self.device.generation and self.enabled:
            self.diagnostics.write("capture_ready", generation=generation)
            self.recovery_timer.stop()
            self.transient_error = ""
            self.recovery_attempts = 0
            self.keys_ready = True
            self.mapping_error = ""
            self.refresh_device()
            self.ensure_remote_audio()

    def _device_stopped(self, generation):
        if generation != self.device.generation:
            return
        self.diagnostics.write("capture_stopped", generation=generation)
        self.enabled = False
        self.keys_ready = False
        self.pressed.clear()
        self._clear_gestures()
        if not self.mapping_error and not self.transient_error:
            self._device_failed("按键监听意外停止，组件未提供详细原因。请重新检测；若仍失败，请退出后重新打开应用。")
        if self.voice.owner == "trigger":
            self.voice.cancel()
        self.set_learning(False)
        self.refresh_device()

    def _device_failed(self, error):
        text = str(error).strip() or "按键组件返回了空错误。"
        disconnected = any(code in text for code in ("LIBUSB_ERROR_IO", "LIBUSB_ERROR_NO_DEVICE"))
        if disconnected and self.auto_mapping and not self.shutting_down and self.recovery_attempts < 1:
            self.transient_error = text
            self.mapping_error = ""
            self.mapping_attempted = False
            self.diagnostics.write("capture_transient_io", reason=text)
            self.recovery_timer.start()
            self.device_changed.emit()
            return
        self.recovery_timer.stop()
        self.transient_error = ""
        self.mapping_error = text
        self.diagnostics.write("capture_failed")
        self.fail(self.device_view().detail)
        self._connection_feedback = True
        self.device_changed.emit()

    def _recover_transient(self):
        if not self.transient_error:
            return
        if not self._state().usable:
            # Re-enumeration can take longer than the retry delay. The next
            # inventory update restarts recovery; do not abandon it here.
            self.diagnostics.write("capture_waiting_for_receiver")
            self.device_changed.emit()
            return
        self.transient_error = ""
        self.recovery_attempts = 1
        self.mapping_attempted = False
        self.mapping_error = ""
        self.diagnostics.write("capture_transient_retry")
        self.monitor.refresh()
        self.ensure_mapping()

    # -- permissions & login startup (read-only until the user clicks) -----------

    def permission_status(self):
        return permissions.permission_status()

    def local_version(self):
        """(version, build) of the running App; source runs report the file version."""
        import plistlib
        from pathlib import Path
        if getattr(sys, "frozen", False):
            bundle = next((p for p in Path(sys.executable).parents if p.suffix == ".app"), None)
            if bundle is not None:
                info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
                return info.get("CFBundleShortVersionString", "0"), info.get("CFBundleVersion", "0")
        for candidate in (Path(__file__).resolve().parents[1] / "VERSION",
                          Path(getattr(sys, "_MEIPASS", "")) / "VERSION"):
            try:
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8").strip(), "0"
            except OSError:
                pass
        return "0", "0"

    def check_updates(self, manual=False):
        import platform, threading
        if sys.platform != "darwin":
            # Windows 版没有 .app 更新器；调试阶段用整包替换，不假装能自动更新。
            self.update_error.emit("当前平台暂不支持自动更新；请替换整个程序目录。")
            return
        from . import updates
        version, build = self.local_version()
        arch = "arm64" if platform.machine() == "arm64" else "x86_64"
        self.diagnostics.write("update_check", manual=manual, version=version, build=build)

        def work():
            try:
                found = updates.check(local_version=version, local_build=build, arch=arch)
            except updates.UpdateError as exc:
                self.update_error.emit(str(exc))
                return
            except Exception as exc:
                self.update_error.emit("检查更新失败：" + str(exc))
                return
            self.diagnostics.write("update_result", found=found.version if found else "")
            if found is None:
                self.update_none.emit()
            else:
                self.update_found.emit(found)
        threading.Thread(target=work, daemon=True, name="remotehub-update-check").start()

    def download_update(self, update):
        import platform, tempfile, threading
        from . import updates
        arch = "arm64" if platform.machine() == "arm64" else "x86_64"
        asset = update.assets.get(arch)
        if asset is None:
            self.update_error.emit("这个版本没有本机架构的安装包")
            return
        target = Path.home() / "Library/Caches/RemoteHub/updates" / asset.name
        self.diagnostics.write("update_download", version=update.version, arch=arch)

        def work():
            try:
                updates.download(asset, target)
            except Exception as exc:
                self.update_error.emit("下载更新失败：" + str(exc))
                return
            self.update_downloaded.emit(str(target))
        threading.Thread(target=work, daemon=True, name="remotehub-update-download").start()

    def onboarding_needed(self):
        return not self.settings.get("onboarded")

    def finish_onboarding(self):
        if self.settings.get("onboarded"):
            return
        self.settings["onboarded"] = "1"
        self.save()

    def install_collector(self, remove=False):
        from .mac_device_helper import open_installer
        if remove:
            self.set_enabled(False)
        open_installer(remove)

    def open_permission(self, name):
        try:
            permissions.open_permission_settings(name)
            self.permission_opened.emit(name)
        except Exception as exc:
            self.fail(str(exc))

    def autostart_enabled(self):
        return autostart.is_enabled()

    def set_autostart(self, enabled):
        """Raises on failure so the caller can put the checkbox back."""
        autostart.set_enabled(enabled)

    # -- learning & key dispatch ---------------------------------------------------

    def set_learning(self, on):
        on = bool(on)
        if on:
            self.overlays.close()
            self.macros.close()
        if on != self.learning:
            self.learning = on
            self.learning_changed.emit(on)

    def toggle_learning(self):
        if self.learning:
            self.set_learning(False)
            return
        if self.selected is None:
            self.fail("请先选择一个按键")
            return
        if not self.enabled and not self.usb.authenticated:
            self.fail("请先启用按键映射，或接入 USB 音频")
            return
        self.set_learning(True)
        self.tell("现在按一次遥控器上要绑定的实体键。学习期间不执行映射动作。")

    def _voice_key(self, trigger):
        """The dedicated voice key bound to this trigger, if any."""
        for button in self.profile.buttons:
            if button.configurable and button.trigger == trigger:
                if is_voice_button(self.profile, button) or self.effective_runtime_button(button).action.kind == "voice":
                    return button
        return None

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
        layer = self.active_runtime_layer()
        currently_pressed = bool(owner and owner in self.pressed)
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

    def _run_action(self, action, down, owner):
        if action.kind in ("hold", "mouse", "grid", "cheat_sheet", "macro", "cycle_profile") or (
            action.kind == "common" and action.target == "app_switcher"
        ):
            return self.runner.run(action, down, owner=owner)
        return self.runner.run(action, down)

    def _cancel_button_actions(self, trigger):
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
        return {button.trigger: button.id for button in self.configurable_buttons() if button.trigger}

    def on_key(self, trigger, down):
        if trigger and not self.learning and (self.switcher.active or trigger in self.switcher_pressed):
            controls = self._switcher_controls()
            role = controls.get(trigger)
            try:
                if down:
                    self.pressed.add(trigger)
                else:
                    self.pressed.discard(trigger)
                    self.switcher_pressed.discard(trigger)
                if down and trigger in self.switcher_pressed:
                    if self.switcher.active and role in ("tv-left", "tv-right"):
                        self.switcher.handle_key(trigger, True, controls)
                    return
                if down:
                    self.switcher_pressed.add(trigger)
                else:
                    self.switcher_pressed.discard(trigger)
                if self.switcher.active:
                    self.switcher.handle_key(trigger, down, controls)
            except Exception as exc:
                self.switcher.close()
                self.fail(str(exc))
            return
        # The collector session follows the physical key, not the mapping: it must
        # also close when voice is disabled or was remapped while the key was down.
        if trigger and not self.learning and self._voice_key(trigger) is not None:
            if down and self.overlays.active:
                self.overlays.close()
            if down and self.macros.active:
                self.macros.close()
            if not down or trigger not in self.pressed:
                self.diagnostics.write("voice_key", down=down)
            self._voice_edge(down)
        if not down and trigger == self.voice_trigger:
            # The key that started a recording always ends it, even if its
            # mapping was changed while it was held.
            self.voice_trigger = None
            self.pressed.discard(trigger)
            self.press_buttons.pop(trigger, None)
            self.overlays.clear_physical_press(trigger)
            self.macros.clear_physical_press(trigger)
            try:
                self.voice_button(False)
            except Exception as exc:
                self.fail(str(exc))
            return
        if not down and trigger == self.learned_release:
            # Release of the key that was just learned must not fire its action.
            self.learned_release = None
            self.pressed.discard(trigger)
            self.press_buttons.pop(trigger, None)
            self.overlays.clear_physical_press(trigger)
            self.macros.clear_physical_press(trigger)
            return
        if trigger and trigger == self._switched_release:
            if down and trigger in self.pressed:
                # Ignore HID auto-repeat while the button that switched profiles is still held.
                return
            if not down:
                self._switched_release = None
                self.pressed.discard(trigger)
                self.press_buttons.pop(trigger, None)
                self.overlays.clear_physical_press(trigger)
                self.macros.clear_physical_press(trigger)
                return
            self._switched_release = None
        if trigger and not self.learning and (self.overlays.active or self.overlays.has_pending_release(trigger)):
            is_repeat = bool(down and trigger in self.pressed)
            role = self._switcher_controls().get(trigger)
            outcome = self.overlays.handle_key(trigger, down, role, is_repeat=is_repeat)
            if outcome == "consumed":
                if down:
                    self.pressed.add(trigger)
                else:
                    self.pressed.discard(trigger)
                self.press_buttons.pop(trigger, None)
                pending = self.gestures.pop(trigger, None)
                if pending is not None:
                    pending.cancel()
                    pending.deleteLater()
                return
        if trigger and not self.learning and (self.macros.active or self.macros.has_pending_release(trigger)):
            is_repeat = bool(down and trigger in self.pressed)
            role = self._switcher_controls().get(trigger)
            outcome = self.macros.handle_key(trigger, down, role, is_repeat=is_repeat)
            if outcome == "consumed":
                if down:
                    self.pressed.add(trigger)
                else:
                    self.pressed.discard(trigger)
                self.press_buttons.pop(trigger, None)
                pending = self.gestures.pop(trigger, None)
                if pending is not None:
                    pending.cancel()
                    pending.deleteLater()
                return
        if down:
            if trigger in self.pressed:
                base_button = next((b for b in self.profile.buttons if b.configurable and b.trigger == trigger), None)
                if base_button is not None and not self.learning:
                    from .actions import is_repeatable
                    from .gestures import KeyGesture
                    button = self.press_buttons.get(trigger) or self.effective_runtime_button(base_button)
                    action = button.action
                    if not KeyGesture.needed(button) and is_repeatable(action):
                        try:
                            self._action_feedback(self.runner.run(action, True))
                        except Exception as exc:
                            self.fail(str(exc))
                return
            self.pressed.add(trigger)
            self.overlays.note_physical_press(trigger)
        else:
            self.pressed.discard(trigger)
            self.overlays.clear_physical_press(trigger)
        if self.learning:
            button = self.selected
            if down and button:
                if any(b.trigger == trigger and b.id != button.id for b in self.profile.buttons):
                    self.fail("这个实体键已绑定其他按键，请先解除旧绑定")
                    return
                button.trigger = trigger
                self.learned_release = trigger
                self.press_buttons.pop(trigger, None)
                self.set_learning(False)
                self.mapping_changed.emit()
                self.save()
                self.tell("实体按键已绑定")
            return
        if not trigger:
            return
        base_button = next((b for b in self.profile.buttons if b.configurable and b.trigger == trigger), None)
        if base_button is None:
            self.press_buttons.pop(trigger, None)
            return
        if down:
            button = self.effective_runtime_button(base_button)
            self.press_buttons[trigger] = button
        else:
            button = self.press_buttons.pop(trigger, None) or self.effective_runtime_button(base_button)
        self.physical_key.emit(trigger, down)
        self.select(base_button.id)
        from .gestures import KeyGesture
        gesture = self.gestures.get(trigger)
        if KeyGesture.needed(button):
            if gesture is None:
                gesture = KeyGesture(button, self)
                gesture.triggered.connect(lambda action, origin=trigger:
                                          self._run_gesture_action(action, origin))
                gesture.released.connect(lambda action, origin=trigger:
                                         self._run_gesture_action(action, origin, False))
                self.gestures[trigger] = gesture
            elif down:
                expected = (button.action, button.double_action, button.long_action)
                current = (gesture.single_action, gesture.double_action, gesture.long_action)
                if current != expected:
                    if gesture.awaiting_second and not gesture.holding:
                        gesture.cancel()
                    if not gesture.holding:
                        gesture.single_action, gesture.double_action, gesture.long_action = expected
            gesture.handle(down)
            return
        if gesture is not None:
            if not down and gesture.holding:
                gesture.handle(False)
                return
            if not gesture.holding:
                gesture.cancel()
                gesture.deleteLater()
                self.gestures.pop(trigger, None)
        action = Action("voice") if is_voice_button(self.profile, base_button) else button.action
        voice = action.kind == "voice"
        if down:
            # A later press can show the same app again; don't report stale results.
            self._cancel_hide_result()
        try:
            if voice and down:
                if self.recording_requested:
                    if (self.voice_auto_pending and self.remote_audio
                            and self.usb.voice.owner == "usb" and not self.usb.cancelled):
                        self.voice_auto_pending = False
                        self.voice_trigger = trigger
                        self.diagnostics.write("voice_key_adopted")
                        return
                    if self.settings.get("record_mode", "hold") == "hold":
                        return
                self.voice_trigger = trigger
            message = self._run_action(action, down, trigger)
            self._action_feedback(message)
        except Exception as exc:
            if voice:
                self.voice_trigger = None
            self.fail(str(exc))

    # -- delayed test ----------------------------------------------------------------

    def schedule_test(self, gesture="single"):
        button = self.selected
        if button is None or gesture not in GESTURE_FIELDS:
            return
        effective = effective_button(button, self.editing_layer)
        action = getattr(effective, GESTURE_FIELDS[gesture])
        if action.kind == "none":
            return
        if action.kind == "voice":
            self.fail("语音动作需要先接入音频来源；可在「语音设置」页用录音文件验证通路。")
            return
        self.test_action = action
        self.test_left = TEST_DELAY
        self.test_timer.start()
        self.test_pending.emit(self.test_left)
        self.tell(f"{TEST_DELAY} 秒后执行「{action_text(self.test_action)}」，请切换到目标应用。")

    def cancel_test(self):
        if self.test_timer.isActive():
            self.test_timer.stop()
            self.test_action = None
            self.test_pending.emit(0)
            self.tell("已取消测试。")

    def _test_tick(self):
        self.test_left -= 1
        if self.test_left > 0:
            self.test_pending.emit(self.test_left)
            return
        self.test_timer.stop()
        action, self.test_action = self.test_action, None
        self.test_pending.emit(0)
        self._cancel_hide_result()
        try:
            result = self.runner.run(action)
            if result is None and action.kind not in ("macro", "grid", "cheat_sheet"):
                self.tell("测试动作已执行。")
            else:
                self._action_feedback(result)
        except Exception as exc:
            self.fail(str(exc))

    # -- voice settings ----------------------------------------------------------------

    def voice_settings(self):
        s = self.settings
        return {"voice_source": s.get("voice_source", "disabled"), "record_mode": s.get("record_mode", "hold"),
                "voice_keys": s.get("voice_keys", "alt"), "voice_mode": s.get("voice_mode", "hold"),
                "input_routing": s.get("input_routing", "automatic"),
                "voice_auto_focus": bool(s.get("voice_auto_focus", True)),
                "gain": s.get("gain", 12), "codec": s.get("codec", ""), "output": s.get("output", "")}

    def set_voice_auto_focus(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        if bool(self.settings.get("voice_auto_focus", True)) == enabled:
            return True
        previous = dict(self.settings)
        self.settings["voice_auto_focus"] = enabled
        try:
            validate_settings(self.settings)
        except ValueError:
            self.settings.clear()
            self.settings.update(previous)
            return False
        if not self.save():
            self.settings.clear()
            self.settings.update(previous)
            return False
        self.voice_changed.emit()
        return True

    @property
    def remote_audio(self):
        return self.settings.get("voice_source", "disabled") == "remote"

    @property
    def voice_enabled(self):
        return self.settings.get("voice_source", "disabled") != "disabled"

    def _apply_voice_settings(self):
        s = self.voice_settings()
        self.voice.trigger_keys = [k for k in s["voice_keys"].split("+") if k]
        self.voice.trigger_mode = s["voice_mode"]
        self.voice.gain = s["gain"]
        self.voice.switch_input = s["input_routing"] == "automatic"

    def update_voice(self, **changes):
        """Validate, apply and persist voice fields. Returns False if rejected."""
        if self.voice.owner is not None or self.recording_requested:
            self.fail("请先停止语音，再修改语音设置")
            self.voice_changed.emit()  # put the fields back
            return False
        if "voice_keys" in changes:
            changes["voice_keys"] = "+".join(k.strip().lower() for k in changes["voice_keys"].split("+") if k.strip())
        candidate = {**self.settings, **changes}
        try:
            validate_settings(candidate)
            from .model import validate_keys
            validate_keys(candidate.get("voice_keys", "alt").split("+"), allow_modifier=True, platform=sys.platform)
            if self.fixed_model and candidate.get("voice_mode", "hold") == "manual":
                raise ValueError("实体语音键需要发送唤起键，请选择按住或切换方式")
            if candidate.get("voice_source", "disabled") == "trigger" and candidate.get("voice_mode", "hold") == "manual":
                raise ValueError("仅触发识别时需要发送识别快捷键，请选择按住或切换方式")
        except ValueError as exc:
            self.fail("语音设置未保存：" + str(exc))
            self.voice_changed.emit()
            return False
        previous = dict(self.settings)
        self.settings.update(changes)
        if not self.save():
            # Keep runtime and memory in step with what is on disk.
            self.settings.clear()
            self.settings.update(previous)
            self.voice_changed.emit()
            return False
        self._apply_voice_settings()
        if not self.remote_audio and (self.usb.ready or self.usb_connecting):
            self.stop_usb()
        self.ensure_remote_audio()
        self.voice_changed.emit()
        return True

    def _selected_output_description(self) -> str:
        return next((name for name, ident in self.outputs if ident == self.voice.output_id), "")

    def refresh_outputs(self):
        devices = list(self.audio.outputs())
        if sys.platform == "win32":
            self.microphone_installed = any(is_windows_cable(d) for d in devices)
        elif sys.platform.startswith("linux"):
            self.microphone_installed = self.virtual_mic.installed() or any(is_voice_output(d) for d in devices)
        else:
            self.microphone_installed = self.virtual_mic.installed()
        self.outputs = [(d.description(), bytes(d.id())) for d in devices if is_voice_output(d)]
        wanted = self.settings.get("output", "")
        self.voice.output_id = next((ident for _, ident in self.outputs if _b64(ident) == wanted),
                                    self.outputs[0][1] if self.outputs else b"")
        self.ensure_remote_audio()
        self.voice_changed.emit()

    def _check_microphone_installation(self):
        if self.virtual_mic.installed() != self.microphone_installed:
            self.refresh_outputs()

    def microphone_view(self):
        if self.voice.output_id:
            return StatusView("已就绪", "可以接收遥控器音频。", "ok")
        if sys.platform == "win32":
            if self.microphone_installed:
                return StatusView("已安装，尚未选择", "在下方选择虚拟声卡的播放端；识别软件那边选对应的录音端。", "neutral")
            return StatusView("还没有虚拟声卡", "安装一次 VB-CABLE 即可使用遥控器麦克风。", "neutral")
        if sys.platform.startswith("linux"):
            if self.microphone_installed:
                return StatusView("未启用虚拟麦克风", "点击「启用虚拟麦克风」即可在用户态即时创建，无需管理员权限或重启。", "neutral")
            return StatusView("缺少音频工具", "未检测到 pactl 或 pw-loopback，请先安装 pulseaudio-utils 或 pipewire-bin。", "neutral")
        if self.microphone_installed:
            return StatusView("已安装，尚未生效", "点击「完成设置」启用麦克风；若安装器提示未能启用，请保存工作后重启 Mac。", "neutral")
        return StatusView("尚未就绪", "请先安装虚拟麦克风。", "neutral")

    def check_microphone(self):
        self.refresh_outputs()
        state = self.microphone_view()
        self.tell(f"{state.title}。{state.detail}")

    def set_output(self, ident: bytes):
        ident = bytes(ident or b"")
        if self.update_voice(output=_b64(ident)):
            self.voice.output_id = ident
            self.voice_changed.emit()

    def setup_virtual_mic(self):
        try:
            if sys.platform == "darwin":
                from .devices import resources
                from .installers import open_package
                pkg = resources() / "components" / "RemoteHubAudio.pkg"
                if not pkg.exists():
                    pkg = resources() / "build" / "RemoteHubAudio.pkg"
                if not pkg.exists():
                    raise FileNotFoundError("虚拟麦克风安装文件缺失，请重新安装 10RemoteHub。")
                open_package(pkg)
                self.tell("已打开安装器。安装结束会自动启用麦克风，声音和录音将短暂中断，请先暂停通话与录音。")
            elif sys.platform == "win32":
                # The voice page explains the step inline and owns the download
                # link; never open a browser from here.
                self.tell("请在语音设置页按提示安装 VB-CABLE，装完点「重新检测」。")
            elif sys.platform.startswith("linux"):
                self.virtual_mic.start_linux()
                QTimer.singleShot(450, self.refresh_outputs)
                self.tell("已在当前会话创建 10RemoteHub 虚拟麦克风，退出应用时会自动清理。")
            else:
                raise RuntimeError("此版本尚不支持当前系统的遥控器麦克风，请选择电脑麦克风。")
        except Exception as exc:
            self.fail(str(exc))

    # -- USB voice link ------------------------------------------------------------------

    def ensure_remote_audio(self):
        if (self.usb.collector is not None and self.auto_mapping and not self.shutting_down
                and self.keys_ready and self.device.capture_ready and self.remote_audio
                and self.voice.output_id and not self.usb.ready and not self.usb_connecting):
            self.start_usb(self.profile)

    def usb_view(self) -> StatusView:
        if not self.voice_enabled:
            return StatusView("未启用", "选择麦克风后启用语音输入。", "neutral")
        if not self.remote_audio:
            return StatusView("不需要", "仅触发识别时不连接遥控器音频。", "neutral")
        if self.usb.ready:
            return StatusView("已连接", "按遥控器语音键开始说话。", "ok")
        if self.usb_connecting or self.usb.authenticated or self.usb.server.isListening():
            return StatusView("正在连接", "正在准备遥控器音频通路。", "busy")
        if not self.voice.output_id:
            return StatusView("未连接", "请先完成虚拟麦克风设置。", "neutral")
        return StatusView("未连接", "确认接收器已插好。" if self.usb.collector is not None else
                          "点击「连接遥控器音频」后即可使用。", "neutral")

    def voice_view(self) -> StatusView:
        if not self.voice_enabled:
            return StatusView("未启用", "选择麦克风后启用语音输入。", "neutral")
        owner = self.voice.owner
        if self.voice.routing:
            return StatusView("正在准备麦克风", "", "busy")
        if self.voice.ending:
            return StatusView("正在结束语音", "", "busy")
        if owner == "usb":
            return StatusView("正在录音", "", "ok")
        if owner == "trigger":
            return StatusView("识别中", "", "ok")
        if owner == "wav":
            return StatusView("正在播放测试录音", "", "ok")
        if self.recording_requested:
            return StatusView("正在开始录音", "", "busy")
        if self.remote_audio and not self.voice.output_id:
            return StatusView("虚拟麦克风尚未就绪", "", "neutral")
        return StatusView("空闲", "", "ready")

    def usb_preflight(self):
        """Every check that can fail before asking the user to hand over the receiver."""
        if not self.remote_audio:
            raise RuntimeError("当前语音设置不需要连接遥控器音频")
        if self.voice.owner is not None or self.recording_requested:
            raise RuntimeError("请先停止当前语音")
        if not self.voice.output_id:
            raise RuntimeError("虚拟麦克风尚未就绪，请到语音设置查看处理方法。")
        profile = next((p for p in self.profiles if is_fixed_model(p)), None)
        if profile is None:
            raise RuntimeError("未找到适配型号的遥控器配置")
        self._require_receiver(profile)
        return profile

    def start_usb(self, profile):
        """Attach audio to the Mac collector, or start the confirmed legacy worker."""
        self.usb_connecting = True
        if self.usb.collector is None:
            self.set_enabled(False)
            if self.device.stop() is False:
                self.usb_connecting = False
                self.fail("遥控器暂时无法连接音频，请重新检测后再试。")
                self.voice_changed.emit()
                return
        if self.profile is not profile:
            self.select_profile(self.profiles.index(profile))
        try:
            if sys.platform == "win32" and self.usb.collector is None:
                self.set_enabled(True)   # legacy worker path only
            self.usb.start()
        except Exception as exc:
            self.usb_connecting = False
            self.mapping_attempted = False
            self.ensure_mapping()
            self.fail(str(exc))
        self.voice_changed.emit()

    def stop_usb(self):
        self.usb_connecting = False
        self.recording_requested = False
        self.usb.stop()
        if self.usb.collector is None:
            self.mapping_attempted = False
            # Only the separate worker needs to hand the receiver back to HID.
            QTimer.singleShot(1000, self.ensure_mapping)
        self.voice_changed.emit()

    def _usb_ready(self):
        self.usb_connecting = False
        self.mapping_error = ""
        self.tell("遥控器音频已就绪，按语音键开始说话。")
        self.voice_changed.emit()

    def _usb_released(self):
        self.diagnostics.write("audio_channel_disconnected")
        self.usb_connecting = False
        self.recording_requested = False
        self.voice_auto_pending = False
        self.voice_trigger = None
        if self.usb.collector is None:
            self.mapping_attempted = False
            QTimer.singleShot(1000, self.ensure_mapping)
        self.voice_changed.emit()

    def _recording_started(self):
        self.recording_requested = True
        self.voice_auto_pending = self.voice_trigger is None
        self.voice_changed.emit()

    def _recording_stopped(self):
        self.recording_requested = False
        self.voice_auto_pending = False
        self.voice_changed.emit()

    def _voice_expired(self):
        self.tell("本次语音已到 60 秒，自动结束；松开语音键后可重新开始。")

    def _voice_finished(self):
        self.recording_requested = False
        self.voice_auto_pending = False
        self.voice_trigger = None
        if self.usb.recording_requested:
            self.usb.stop_audio()
        self.voice_changed.emit()

    def _voice_edge(self, down):
        """Windows and Linux collectors keep the receiver's voice session aligned with the physical key."""
        if sys.platform != "win32" and not sys.platform.startswith("linux"):
            return
        try:
            self.device.voice_edge(down)
        except Exception as exc:
            self.diagnostics.write("voice_edge_failed", error=str(exc)[:300])

    def voice_button(self, down):
        """ActionRunner callback for keys mapped to 语音输入.

        record_mode hold: press starts, release stops. toggle: press starts,
        next press stops. Source trigger: only the recognition shortcut is sent.
        """
        if not self.voice_enabled:
            self.voice_trigger = None
            if down:
                self.tell("语音未启用，请先选择麦克风。")
            return
        mode = self.settings.get("record_mode", "hold")
        if not down:
            if mode == "hold":
                self.stop_voice()
            return
        if self.recording_requested:
            if mode == "toggle":
                self.stop_voice()
            return
        if self.voice.owner is not None:
            raise RuntimeError("上一段语音尚未结束，请稍后再按")
        self._begin_voice_capture()

    def _begin_voice_capture(self):
        if self.remote_audio:
            if not self.usb.ready:
                self.ensure_remote_audio()   # collector may have just come up
            self.usb.start_audio()
        else:
            self.voice.start("trigger")
        self.recording_requested = True
        self.voice_changed.emit()

    def _pre_voice_focus_gate(self, owner):
        """VoiceSession hook: runs for every session start, including the USB
        stream the receiver opens on its own before the key event arrives.

        Returning True holds the recognition shortcut until the focus check
        resolves (or times out); audio is buffered meanwhile.
        """
        if self.shutting_down or not self.settings.get("voice_auto_focus", True):
            return False
        bundle = self.chat_focus.should_focus_for_voice()
        if bundle is None:
            return False
        self._voice_focus_token += 1
        token = self._voice_focus_token
        self._voice_focus_timer.start()
        self.chat_focus.request_voice_focus(bundle, token, self._on_voice_focus_done)
        return True

    def _on_voice_focus_timeout(self):
        self._resolve_pre_voice_focus(self._voice_focus_token, "timeout")

    def _on_voice_focus_done(self, token: int, status: str):
        self._resolve_pre_voice_focus(token, status)

    def _resolve_pre_voice_focus(self, token: int, status: str):
        if token != self._voice_focus_token:
            return
        self._voice_focus_token += 1
        self._voice_focus_timer.stop()
        self.diagnostics.write("voice_focus_guard", status=status)
        self.voice.open_gate()

    # -- WAV path test ---------------------------------------------------------------------

    def play_wav(self, path):
        try:
            if self.voice.owner is not None or self.recording_requested:
                raise RuntimeError("请先停止当前语音")
            if not self.voice.output_id:
                raise RuntimeError("虚拟麦克风尚未就绪，请到语音设置查看处理方法。")
            wav = wave.open(str(path), "rb")
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2) \
                    or wav.getnframes() > 16000 * 120:
                wav.close()
                raise ValueError("请选择不超过两分钟的 16 kHz 单声道 16-bit WAV")
            try:
                self.voice.start("wav")
            except Exception:
                wav.close()
                raise
            self.wav = wav
            self.wav_timer.start()
        except Exception as exc:
            self.fail(str(exc))

    def _wav_tick(self):
        try:
            pcm = self.wav.readframes(320)
            if pcm:
                self.voice.feed("wav", pcm)
            else:
                self.wav.close()
                self.wav = None
                self.wav_timer.stop()
                self.voice.finish("wav")
        except Exception as exc:
            self.stop_voice()
            self.fail(str(exc))

    def stop_voice(self):
        """Also the emergency stop in 通用设置 and the tray."""
        self._voice_focus_token += 1
        self._voice_focus_timer.stop()
        self.recording_requested = False
        self.wav_timer.stop()
        if self.wav:
            self.wav.close()
            self.wav = None
        self.usb.stop_audio()  # USB recording ends when the worker sends its tail
        if self.voice.owner is not None and self.voice.owner != "usb":
            self.voice.finish(self.voice.owner)
        self.voice_changed.emit()

    # -- lifecycle -------------------------------------------------------------------------

    def start(self):
        """Begin read-only USB inventory polling. Not called in --smoke runs."""
        self.monitor.start()
        try:
            # Only an already-enabled login item is upgraded; never enables one.
            if getattr(sys, "frozen", False) and autostart.refresh():
                self.diagnostics.write("autostart_upgraded")
        except Exception as exc:
            self.diagnostics.write("autostart_upgrade_failed", error=str(exc))

    def shutdown(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        self.foreground_apps.close()
        self.chat_focus.shutdown()
        self.diagnostics.write("app_stopping")
        self.launcher.stop()
        self.microphone_watch.stop()
        self._cancel_hide_result()
        self.test_timer.stop()
        self.recovery_timer.stop()
        self.text_enter_timer.stop()
        self._voice_focus_token += 1
        self._voice_focus_timer.stop()
        self.macros.close()
        self._clear_gestures()
        self.monitor.stop()
        self.device.stop()
        self.usb.stop()
        self.virtual_mic.stop()
        self.wav_timer.stop()
        if self.wav:
            self.wav.close()
            self.wav = None
        self.voice.cancel()
        try:
            self.keys.release()
        except Exception:
            pass
        self.voice.close()
        self.diagnostics.close()
