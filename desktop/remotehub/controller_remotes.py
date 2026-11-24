"""Remote selection and registry-backed input routing for ``AppController``.

One remote is active at a time, and at most one input path runs at a time.
Paths register through ``readers.ReaderRegistry`` (built-ins: ``receiver``
for the dedicated 0416:0602 collector and ``hid`` for shared OS USB/Bluetooth
HID; third-party/test paths register as additional ``ReaderEntry`` items).

Windows registers Raw Input once per process, so switching remotes, changing
active reader paths, or opening the add dialog stops active readers first.
"""
from __future__ import annotations

import sys
import time

from PySide6.QtCore import QTimer

from .inventory import receiver_state
from .readers import CompositeReader, ReaderEntry, default_registry, invoke_factory, peek_devices
from .remote_library import Remote


class RemoteInputMixin:
    HID_SCAN_MS = 2000
    TAKEOVER_ENSURE_S = 10.0

    @staticmethod
    def _remote_triggers(remote: Remote | None) -> tuple[str, ...]:
        if remote is None:
            return ()
        seen: list[str] = []
        for profile in remote.profiles:
            for button in profile.buttons:
                for code in button.all_triggers:
                    if code not in seen:
                        seen.append(code)
        return tuple(seen)

    def _init_remote_input(self, readers=None):
        self.readers = readers if readers is not None else default_registry()
        self.hid_reader = None          # tests inject a reader; live mode creates one lazily
        self._hid_runtime = None
        self._reader_instances = {}
        self._runtimes = {}
        self.hid_candidates = []
        self.hid_preferred = None       # device chosen in the add dialog
        self.hid_error = ""
        self.discovered = []            # last shared-HID listing, to offer catalog Bluetooth remotes
        self.remote_input_paused = False
        self._last_takeover_ensure = 0.0
        self.hid_scan_timer = QTimer(self)
        self.hid_scan_timer.setInterval(self.HID_SCAN_MS)
        self.hid_scan_timer.timeout.connect(self.scan_hid)

    # -- views -------------------------------------------------------------------

    @property
    def remote(self) -> Remote | None:
        return getattr(self.store, "remote", None)

    @property
    def remotes(self):
        return list(getattr(self.store, "remotes", ()))

    @property
    def has_remote(self):
        return self.remote is not None

    @property
    def uses_receiver(self):
        remote = self.remote
        # Every desktop collector (macOS, Windows, Linux) reads the bundled receiver.
        return remote is not None and remote.uses_receiver

    @property
    def supports_remote_mic(self):
        remote = self.remote
        if remote is None:
            return False
        # For BLE dongle remotes whose GATT was actually probed, trust the real GATT-discovered
        # voice protocol instead of blindly assuming a VID:PID always has a microphone.
        reader = self._ble_dongle_reader()
        info_fn = getattr(reader, "peer_voice_info", None) if reader is not None else None
        if callable(info_fn):
            info = info_fn(remote) or {}
            if info.get("paired") or info.get("firmware") or info.get("gatt_summary"):
                return bool(info.get("protocol"))
        if self.readers.supports_remote_mic(remote):
            return True
        fn = getattr(reader, "has_voice_capable_peer", None) if reader is not None else None
        return bool(callable(fn) and fn(remote))

    @property
    def learns_in_enrollment(self):
        """Shared-HID and non-fixed remotes learn keys in the photo dialog, not in place."""
        remote = self.remote
        return remote is not None and not remote.is_fixed_model

    def active_reader_entry(self) -> ReaderEntry | None:
        return self.readers.select(self.remote, self)

    def uses_dedicated_reader(self) -> bool:
        entry = self.active_reader_entry()
        return bool(entry is not None and entry.dedicated)

    def input_path(self) -> str:
        """Selected registered reader ID (e.g. ``receiver``, ``hid``) or ``""``."""
        entry = self.active_reader_entry()
        return entry.id if entry is not None else ""

    def _receiver_present(self):
        return any((d.vendor, d.product) == (0x0416, 0x0602) for d in self.monitor.devices)

    def _reader_for(self, entry_id: str):
        if entry_id == "hid" and self.hid_reader is not None:
            return self.hid_reader
        if entry_id in self._reader_instances:
            return self._reader_instances[entry_id]
        entry = self.readers.get(entry_id)
        if entry is None:
            return None
        try:
            reader = invoke_factory(entry.factory, self)
        except Exception as exc:  # missing framework, sandboxed test host
            self.hid_error = str(exc)
            return None
        if reader is not None:
            self._reader_instances[entry_id] = reader
        return reader

    def _runtime_for(self, entry_id: str):
        reader = self._reader_for(entry_id)
        if reader is None:
            return None
        runtime = self._runtimes.get(entry_id)
        if runtime is None or runtime.reader is not reader:
            if runtime is not None:
                runtime.stop()
            from .learned_remotes import LearnedRemoteRuntime

            runtime = LearnedRemoteRuntime(reader, self)
            runtime.key.connect(self.on_key)
            runtime.key.connect(self.remote_key)
            runtime.changed.connect(self._hid_changed)
            runtime.stopped.connect(self._hid_stopped)
            runtime.feedback.connect(self._hid_feedback)
            self._runtimes[entry_id] = runtime
        if entry_id == "hid":
            self._hid_runtime = runtime
        return runtime

    @property
    def hid(self):
        return self._runtime_for("hid")

    def _hid_reader(self):
        return self._reader_for("hid")

    def enrollment_reader(self):
        """Return a reader for the add-remote dialog across all non-dedicated registrations."""
        readers = []
        seen = set()
        for entry in self.readers.entries():
            if entry.dedicated:
                continue
            reader = self._reader_for(entry.id)
            if reader is not None and id(reader) not in seen:
                seen.add(id(reader))
                readers.append(reader)
        if not readers:
            return None
        if len(readers) == 1:
            return readers[0]
        return CompositeReader(readers)

    def active_runtime(self):
        entry = self.active_reader_entry()
        if entry is not None and not entry.dedicated:
            runtime = self._runtimes.get(entry.id)
            if runtime is not None:
                return runtime
        return next((rt for rt in self._runtimes.values() if rt.device is not None), self._hid_runtime)

    @property
    def hid_device(self):
        runtime = self.active_runtime()
        return runtime.device if runtime is not None else None

    active_device = hid_device

    def remote_view(self):
        """Status of the active remote's connection for the sidebar and lists."""
        from .controller import StatusView

        remote = self.remote
        if remote is None:
            return StatusView("未添加遥控器", "点「添加遥控器」，插入接收器或连接蓝牙后按一下遥控器。")
        if self.remote_input_paused:
            return StatusView("映射已暂停", "关闭添加窗口后恢复。", "neutral")
        if self.uses_dedicated_reader():
            return self.device_view()
        runtime = self.active_runtime()
        device = runtime.device if runtime is not None else None
        if device is not None:
            connection = remote.connection_for(device)
            scope = getattr(runtime.reader, "scope", "") if runtime is not None else ""
            reader = runtime.reader if runtime is not None else None
            seized = getattr(reader, "seized", 0)
            why = getattr(reader, "seize_error", "")
            if seized and not why:
                detail = "已接管原按键：按键只执行映射的动作。"
            elif seized and why:
                detail = f"仅部分接管原按键，部分键仍会同时生效（{why}）。"
            elif why:
                detail = f"系统不允许接管这只遥控器，原按键会同时生效（{why}）。"
            elif hasattr(reader, "take_over"):
                detail = "尚未学入可接管的按键，原按键会同时生效。"
            else:
                detail = "原按键会同时生效。"
            detail += f"{scope}。" if scope else ""
            return StatusView("已连接 · " + (connection.label() if connection else device.name), detail, "ok")
        if self.hid_error:
            return StatusView("暂时无法读取遥控器", self.hid_error, "error", self.hid_error, True)
        accepted = "、".join(c.label() for c in remote.connections)
        return StatusView("等待遥控器连接", f"插入接收器或连接蓝牙：{accepted}。", "ready")

    # -- non-dedicated reader polling ------------------------------------------

    def _refresh_discovered_hid(self):
        hid_reader = self._reader_for("hid")
        if hid_reader is None:
            return
        try:
            devices = (
                hid_reader.peek()
                if self.enabled and hasattr(hid_reader, "peek")
                else hid_reader.enumerate()
            )
        except (OSError, RuntimeError) as exc:
            self.hid_error = str(exc)
            self.device_changed.emit()
            return
        if [d.id for d in devices] != [d.id for d in self.discovered]:
            self.discovered = devices
            self.device_changed.emit()

    def scan_hid(self):
        """Every 2 s: reconnect the active non-dedicated remote, and list newly paired
        devices read-only even while another remote is being read."""
        if self.remote_input_paused or self.shutting_down:
            return
        entry = self.active_reader_entry()
        if entry is None or entry.dedicated:
            self._stop_readers()
            self._refresh_discovered_hid()
            return
        if self.enabled:
            self.set_enabled(False)
        self._stop_readers(except_id=entry.id)
        runtime = self._runtime_for(entry.id)
        if runtime is None:
            return
        if runtime.device is not None:
            runtime.sync_takeover(self._remote_triggers(self.remote))
            now = time.monotonic()
            if now - self._last_takeover_ensure >= self.TAKEOVER_ENSURE_S:
                self._last_takeover_ensure = now
                runtime.ensure_takeover()
            try:
                devices = peek_devices(runtime.reader)
            except (OSError, RuntimeError):
                return
            if [d.id for d in devices] != [d.id for d in self.discovered]:
                self.discovered = devices
                self.device_changed.emit()
            return
        try:
            devices = runtime.reader.enumerate()
        except (OSError, RuntimeError) as exc:
            self.hid_error = str(exc)
            self.device_changed.emit()
            return
        listing_changed = [d.id for d in devices] != [d.id for d in self.discovered]
        self.discovered = devices
        remote = self.remote
        if remote is None:
            if listing_changed:
                self.device_changed.emit()
            return
        candidates = [d for d in devices if remote.accepts(d)]
        self.hid_candidates = candidates
        if not candidates:
            self.hid_error = ""
            self.device_changed.emit()
            return
        preferred = self.hid_preferred if self.hid_preferred in candidates else None
        device = preferred or candidates[0]
        runtime.take_over_triggers = self._remote_triggers(remote)
        if runtime.listen(device):
            self.hid_preferred = device
            self._last_takeover_ensure = time.monotonic()
        self.device_changed.emit()

    scan_readers = scan_hid

    def _hid_feedback(self, text):
        # Opening can fail on every 2 s scan; report each distinct reason once.
        if text and text != self.hid_error:
            self.hid_error = text
            self.tell(text)
        elif not text and self.hid_device is not None:
            self.hid_error = ""

    def _hid_changed(self):
        self.device_changed.emit()

    def _hid_stopped(self):
        self.pressed.clear()
        self._clear_gestures()
        self.text_enter_timer.stop()
        if self.voice.owner == "trigger":
            self.voice.cancel()
        self.device_changed.emit()

    def _stop_readers(self, *, except_id: str | None = None):
        for entry_id, runtime in list(self._runtimes.items()):
            if entry_id != except_id:
                runtime.stop()
        if except_id != "hid" and self._hid_runtime is not None and self._hid_runtime is not self._runtimes.get("hid"):
            self._hid_runtime.stop()

    def _stop_hid(self):
        self._stop_readers()

    def _close_readers(self):
        self._stop_readers()
        seen = set()
        for reader in [*self._reader_instances.values(), self.hid_reader]:
            if reader is None or id(reader) in seen:
                continue
            seen.add(id(reader))
            try:
                reader.close()
            except Exception:
                pass
        self._reader_instances.clear()

    # -- pausing -----------------------------------------------------------------

    def pause_remote_input(self) -> bool:
        """Release every input source, e.g. while the add dialog reads devices."""
        self.remote_input_paused = True
        self.hid_scan_timer.stop()
        self.voice.cancel()
        if self.usb.authenticated or self.usb.ready or self.usb_connecting:
            self.usb_connecting = False
            self.recording_requested = False
            self.usb.stop()
        released = True
        if self.enabled:
            self.set_enabled(False)
            released = getattr(self, "_last_device_release", True)
        elif (self.device.mac is not None or self.device.collector is not None
              or self.device.windows is not None or self.device.linux_evdev is not None):
            released = self.device.stop() is not False
            if not released:
                self.device.release_confirmed = False
        if not self.device.release_confirmed:
            released = False
        self.mapping_attempted = False
        self._stop_readers()
        self.device_changed.emit()
        return released

    def resume_remote_input(self):
        self.remote_input_paused = False
        self.mapping_attempted = False
        if not self.device.release_confirmed:
            if self.enabled:
                self.set_enabled(False)
            self.device_changed.emit()
            return
        entry = self.active_reader_entry()
        if (entry is None or not entry.dedicated) and self.enabled:
            self.set_enabled(False)
        if entry is not None and entry.dedicated:
            self._stop_readers()
        self.ensure_mapping()
        has_custom_reader = (
            entry is not None
            and not entry.dedicated
            and self._reader_for(entry.id) is not None
        )
        if self.auto_mapping or self.hid_reader is not None or has_custom_reader:
            self.hid_scan_timer.start()
            self.scan_hid()
        self.device_changed.emit()

    # -- library -----------------------------------------------------------------

    def _remote_changed(self):
        self.index = next((i for i, p in enumerate(self.profiles)
                           if p.id == self.settings.get("active_profile_id")), 0)
        self.action_undo = None
        self.synced_action_undo = []
        self._mouse_cleared_undo = None
        self.editing_layer_id = ""
        self.selected_id = self._default_selection()
        self._apply_voice_settings()
        self._sync_layer_monitor()
        self.remotes_changed.emit()
        self.profiles_changed.emit()
        self.mapping_changed.emit()
        self.voice_changed.emit()
        self.device_changed.emit()

    def _library_action(self, action, success="", *, require_release=False):
        paused = self.remote_input_paused
        released = True if paused else self.pause_remote_input()
        if require_release and not released:
            self.fail("当前遥控器读取组件未能释放设备，请重新检测或拔插接收器后再切换。")
            if not paused:
                self.resume_remote_input()
            return False
        try:
            action()
        except (OSError, ValueError) as exc:
            self.fail(str(exc))
            if not paused:
                self.resume_remote_input()
            return False
        self._remote_changed()
        if not paused:
            self.resume_remote_input()
        if success:
            self.tell(success)
        return True

    def select_remote(self, identity):
        if identity == self.store.active:
            return True
        return self._library_action(lambda: self.store.activate(identity), require_release=True)

    def add_remote(self, remote, device=None):
        self.hid_preferred = device
        return self._library_action(lambda: self.store.add(remote), f"已添加「{remote.name}」。")

    def update_remote(self, remote, device=None):
        """Save a remote edited in the learning dialog (keys, photo, name)."""
        self.hid_preferred = device or self.hid_preferred
        return self._library_action(lambda: self.store.replace(remote), "遥控器已保存。")

    def rename_remote(self, identity, name):
        try:
            self.store.rename(identity, name)
        except (OSError, ValueError, StopIteration) as exc:
            self.fail(str(exc) or "找不到这只遥控器")
            return False
        self.remotes_changed.emit()
        return True

    def remove_remote(self, identity):
        name = next((r.name for r in self.remotes if r.id == identity), "")
        return self._library_action(lambda: self.store.remove(identity), f"已删除「{name}」。")

    def shared_connection_users(self, device):
        """Other remotes that accept the same connection as ``device``."""
        return [r for r in self.remotes if r.accepts(device)]

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

    def device_view(self):
        from .controller import StatusView

        state = self._state()
        if not state.usable:
            if self.monitor.error:
                return StatusView(state.title, "无法读取系统 USB 设备清单，请点「重新检测」。详情见通用设置。",
                                  "error", self.monitor.error, True)
            return StatusView(state.title, state.detail)
        if self.transient_error:
            return StatusView("正在重新连接", "USB 读取暂时中断，正在恢复按键映射。", "busy")
        if self.helper_missing and not self.enabled and not self.mapping_error:
            return StatusView("遥控器已接入，读取组件未安装",
                              "点下方「完成设置」安装读取组件，装好后才能读取按键和声音。",
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
        if not self.uses_dedicated_reader():
            # The collector belongs to dedicated receiver registrations only.
            if self.voice.owner == "trigger":
                self.voice.cancel()
            if self.usb.authenticated or self.usb.ready or self.usb_connecting:
                self.stop_usb()
            if self.enabled:
                self.set_enabled(False)
            self.device_changed.emit()
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
        if self.uses_dedicated_reader():
            self._stop_readers()
        self.refresh_device()
        if state.usable and self.transient_error and not self.recovery_timer.isActive():
            self.recovery_timer.start()
        self.ensure_mapping()
        self.scan_hid()

    def ensure_mapping(self):
        """Start key input once per plug-in / retry. Never in safe (smoke) mode."""
        if (self.shutting_down or not self.auto_mapping or self.enabled or self.mapping_attempted
                or self.usb_connecting or self._usb_holds_receiver() or self.remote_input_paused
                or not self.uses_dedicated_reader()):
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
        self.hid_error = ""
        self.helper_missing = False
        if not self.device.release_confirmed:
            if self.device.mac is None and self.device.collector is None:
                self.device.release_confirmed = True
            elif self.device.stop() is not False:
                self.device.release_confirmed = True
        self.monitor.refresh()
        if not self.remote_input_paused and self.input_path() and not self.uses_dedicated_reader():
            self.scan_hid()
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
                released = self.device.stop()
                self._last_device_release = released is not False
                if released is False:
                    self.device.release_confirmed = False
                    self.mapping_error = "遥控器读取组件未能释放设备，请重新检测或拔插接收器后再试。"
                    self.fail(self.mapping_error)
        except Exception as exc:
            self.enabled = False
            if on:
                self._device_failed(str(exc))
                self._last_device_release = self.device.stop() is not False
            else:
                self._last_device_release = False
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
