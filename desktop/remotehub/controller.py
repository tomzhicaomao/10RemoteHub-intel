"""Application state and rules, kept apart from widget layout.

The window builds widgets and forwards user intent here. It never mutates a
Profile or talks to a device itself. Everything below runs without showing a
window; see tests/test_controller.py. Data flow and invariants: ARCHITECTURE.md.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import sys
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaDevices

from .actions import ActionRunner, KeyOutput
from .app_layers import ForegroundAppTracker
from .branding import BRAND
from .audio import AudioBridge
from .devices import DeviceInput
from .inventory import USBMonitor
from .macro_runner import MacroRunner
from .model import Action, Button, GESTURE_FIELDS, MOUSE_BUTTONS, Profile, effective_button
from .overlays import OverlayManager
from . import autostart, permissions
from .photo_template import is_fixed_model, is_reserved_voice_key, is_voice_button
from .presentation import action_text
from .session import VoiceSession
from .controller_overlays import OverlayMacroMixin
from .controller_profiles import ProfileLayerMixin
from .controller_remotes import RemoteInputMixin
from .controller_voice import VoiceControllerMixin
from .remote_library import RECEIVER_MODEL, RECEIVER_PAIR, RemoteLibraryStore, sync_layout
from .usb_bridge import USBBridge
from .virtual_mic import VirtualMic
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


class AppController(RemoteInputMixin, VoiceControllerMixin, OverlayMacroMixin, ProfileLayerMixin, QObject):
    # A dongle that briefly re-enumerates (voice key, hub power event) must not
    # tear down a working capture; only a persistent absence counts as unplugged.
    ABSENCE_GRACE = 3.0
    message = Signal(str, bool)       # text, is_error
    profiles_changed = Signal()       # list or current index changed
    remotes_changed = Signal()        # remote library or active remote changed
    remote_key = Signal(str, bool)    # every edge from the shared-HID remote, after on_key
    mapping_changed = Signal()        # selection, action, name, rect or photo changed
    physical_key = Signal(str, bool)  # verified key event for first-run feedback
    device_changed = Signal()         # receiver state / enabled / ready changed
    voice_changed = Signal()          # settings, outputs, USB link or session owner changed
    voice_key_active = Signal(bool)   # live highlight for marked physical voice button
    ble_voice_event = Signal(object)  # ReportEvents from BLEDongleReader background thread
    ble_voice_ready_changed = Signal(bool)
    input_overlays_changed = Signal()
    level = Signal(float)
    test_pending = Signal(int)        # seconds left; 0 = idle
    permission_opened = Signal(str)   # Show the companion guide only on user request.
    relaunch_requested = Signal()     # quit and reopen, e.g. to see a newly installed microphone
    update_found = Signal(object)     # updates.Update
    update_none = Signal()
    update_error = Signal(str)
    update_downloaded = Signal(str)

    def __init__(self, directory, parent=None, *, auto_mapping=True, store=None, live=None, readers=None):
        super().__init__(parent)
        live = auto_mapping if live is None else live
        self.live = live
        self.store = store if store is not None else RemoteLibraryStore(Path(directory))
        self.store.load()
        from .chat_focus_service import ChatFocusService
        self.chat_focus = ChatFocusService(self.store.directory, self, live=live)
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
        self.pressed = set()
        self.press_buttons: dict[str, Button] = {}
        self.switcher_pressed = set()
        self.gestures = {}
        self.foreground_apps = ForegroundAppTracker(self, live=live)
        self.foreground_apps.changed.connect(self._on_foreground_app_changed)
        self._switched_release = None
        self.shutting_down = False
        self.host = None  # widget whose winId() Windows raw input registers on
        # Key input starts by itself when a supported receiver appears. A failed
        # attempt is not retried on every 2 s scan; retry_connection() re-arms it.
        self.auto_mapping = auto_mapping
        self.mapping_attempted = False
        self.mapping_error = ""
        self.last_error = ""
        self._last_device_release = True
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
        self.overlays.nine_key_confirmed.connect(self._paste_nine_key)
        self.nine_key_app_id = ""
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
        self._init_remote_input(readers=readers)
        if getattr(self.store, "readers", None) is None:
            try:
                self.store.readers = self.readers
            except AttributeError:
                pass
        self.virtual_mic = VirtualMic(self)
        self.microphone_installed = self.virtual_mic.installed()
        self.microphone_installed_at_start = self.microphone_installed
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
        self.test_trigger = ""
        self.test_left = 0
        self.test_timer = QTimer(self)
        self.test_timer.setInterval(1000)
        self.test_timer.timeout.connect(self._test_tick)
        self.recovery_timer = QTimer(self)
        self.recovery_timer.setSingleShot(True)
        self.recovery_timer.setInterval(750)
        self.recovery_timer.timeout.connect(self._recover_transient)

        self.device.key.connect(self.on_key)
        self.device.key.connect(self.remote_key)
        self.usb.key.connect(self.on_key)
        self.usb.key.connect(self.remote_key)
        self.ble_voice_event.connect(self._on_ble_voice_event)
        self.ble_voice_ready_changed.connect(self._on_ble_voice_ready_changed)
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
        remote = self.remote
        if remote is not None:
            return is_fixed_model(remote) and (self.profile.vendor, self.profile.product) == RECEIVER_PAIR
        return is_fixed_model(self.profile)

    def _is_voice_button(self, button, profile=None):
        target = self.profile if profile is None else profile
        if self.remote is None:
            return bool(self.fixed_model and is_voice_button(target, button, model_id=RECEIVER_MODEL))
        return is_reserved_voice_key(self.remote, target, button)

    @property
    def selected(self) -> Button | None:
        return next((b for b in self.profile.buttons if b.id == self.selected_id and b.configurable), None)

    def configurable_buttons(self):
        return [b for b in self.profile.buttons if b.configurable]

    @property
    def selected_voice_key(self):
        return self._is_voice_button(self.selected)

    def assigned_count(self):
        keys = [b for b in self.configurable_buttons() if not self._is_voice_button(b)]
        return sum(any(action.kind != "none" for action in (b.action, b.double_action, b.long_action))
                   for b in keys), len(keys)

    # -- feedback & persistence ----------------------------------------------

    def _feedback_replaced(self, text, error):
        self._connection_feedback = False

    def tell(self, text):
        self.message.emit(text, False)

    def fail(self, text):
        self.last_error = str(text)
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

    def _sync_remote_layout_and_save(self) -> bool:
        remote = self.remote
        current = self.profile
        if remote is None or len(remote.profiles) <= 1:
            return self.save()
        backups = [(idx, deepcopy(prof)) for idx, prof in enumerate(remote.profiles) if prof is not current]
        sync_layout(remote, current)
        if not self.save():
            for idx, previous in backups:
                remote.profiles[idx] = previous
            return False
        return True

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
                    if b.configurable and not self._is_voice_button(b, other)
                    and (b.id == src_btn.id or bool(set(src_btn.all_triggers).intersection(b.all_triggers)))
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
                self._cancel_button_actions(button)
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
            self._cancel_button_actions(button)
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
        self._cancel_button_actions(button)
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
            self._cancel_button_actions(button)
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
        self._cancel_button_actions(button)
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
        if not self._sync_remote_layout_and_save():
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
        if not self._sync_remote_layout_and_save():
            button.rect = previous
        self.mapping_changed.emit()

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
        if not self._sync_remote_layout_and_save():
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
            if not self._sync_remote_layout_and_save():
                self.profile.image = previous
                return
        except Exception as exc:
            self.fail(str(exc))
            return
        self.mapping_changed.emit()
        self.tell("照片已替换。请检查按键位置，必要时在「名称与位置」里重新框选。")

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

    # -- key dispatch ------------------------------------------------------------

    def _button_for_trigger(self, trigger: str, *, configurable_only: bool = True) -> Button | None:
        if not trigger:
            return None
        return next(
            (
                b for b in self.profile.buttons
                if (not configurable_only or b.configurable) and b.matches_trigger(trigger)
            ),
            None,
        )

    def on_key(self, trigger, down):
        if self.shutting_down or self.remote_input_paused:
            return
        if trigger and (self.switcher.active or trigger in self.switcher_pressed):
            controls = self._navigation_controls()
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
        if trigger and self._voice_key(trigger) is not None:
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
        if trigger and (self.overlays.active or self.overlays.has_pending_release(trigger)):
            is_repeat = bool(down and trigger in self.pressed)
            role = self._switcher_controls().get(trigger)
            outcome = self.overlays.handle_key(
                trigger, down, role, is_repeat=is_repeat, nav_role=self._navigation_role(trigger),
            )
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
        if trigger and (self.macros.active or self.macros.has_pending_release(trigger)):
            is_repeat = bool(down and trigger in self.pressed)
            role = self._navigation_role(trigger)
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
                base_button = self._button_for_trigger(trigger)
                if base_button is not None:
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
        if not trigger:
            return
        base_button = self._button_for_trigger(trigger)
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
        action = Action("voice") if self._is_voice_button(base_button) else button.action
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
        self.test_trigger = button.trigger
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
            result = (self._run_action(action, True, self.test_trigger) if action.kind == "nine_key"
                      else self.runner.run(action))
            if result is None and action.kind not in ("macro", "grid", "cheat_sheet", "nine_key"):
                self.tell("测试动作已执行。")
            else:
                self._action_feedback(result)
        except Exception as exc:
            self.fail(str(exc))

    # -- lifecycle -------------------------------------------------------------------------

    def start(self):
        """Begin read-only USB inventory polling. Not called in --smoke runs."""
        self.monitor.start()
        self.resume_remote_input()
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
        self.hid_scan_timer.stop()
        self._close_readers()
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
