"""Voice settings, virtual-microphone setup, USB audio link, and WAV tests for ``AppController``."""
from __future__ import annotations

import base64
import sys
import wave

from PySide6.QtCore import QTimer

from .model import Action
from .remote_library import RECEIVER_MODEL, RECEIVER_PAIR
from .store import validate_settings
from .virtual_mic import is_voice_output, is_windows_cable


def _b64(raw):
    return base64.b64encode(raw).decode()


class VoiceControllerMixin:
    def _voice_key(self, trigger):
        """The dedicated voice key bound to this trigger, if any."""
        button = self._button_for_trigger(trigger)
        if button is not None and (
            self._is_voice_button(button) or self.effective_runtime_button(button).action.kind == "voice"
        ):
            return button
        return None

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
            if candidate.get("voice_source") == "remote" and not self.supports_remote_mic:
                raise ValueError("这只遥控器使用识别软件当前的麦克风")
            if self.receiver_voice_key and candidate.get("voice_mode", "hold") == "manual":
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

    @property
    def receiver_voice_key(self) -> bool:
        """The physical voice key belongs to the 0416:0602 receiver (bundled or photo-learned)."""
        remote = self.remote
        return bool(self.fixed_model or (remote is not None and remote.voice_protocol == RECEIVER_MODEL))

    def assign_voice_trigger_button(self, button_id: str) -> bool:
        """Bind an existing configurable button on a generic remote to Action('voice')."""
        if self.fixed_model or not button_id:
            return False
        button = next(
            (b for b in self.profile.buttons if b.id == button_id and b.configurable and b.trigger),
            None,
        )
        if button is None:
            return False
        from .key_roles import assign_button_role

        previous = button.action
        previous_roles = [(p, [(b, b.role) for b in p.buttons]) for p in self.remote.profiles] if self.remote else []
        button.action = Action("voice")
        # A 「语音」 mark without a key code is only the photo position of the voice key; keep it
        # rather than leaving a nameless, unpressable key behind in the mapping page.
        photo_mark = any(b.role == "voice" and not b.trigger for b in self.profile.buttons)
        if self.remote is not None and not photo_mark:
            assign_button_role(self.remote.profiles, button.id, "voice")
        if not self.save():
            button.action = previous
            for _, snapshot in previous_roles:
                for b, old_role in snapshot:
                    b.role = old_role
            self.voice_changed.emit()
            return False
        self.mapping_changed.emit()
        self.profiles_changed.emit()
        self.voice_changed.emit()
        self.tell(f"已将「{button.name}」设为「语音输入」键。")
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

    @property
    def microphone_needs_relaunch(self) -> bool:
        """macOS: the driver went in while the app ran, so this process cannot see the device yet."""
        return (sys.platform == "darwin" and self.microphone_installed and not self.voice.output_id
                and not getattr(self, "microphone_installed_at_start", True))

    def microphone_view(self):
        from .controller import StatusView

        if self.microphone_needs_relaunch:
            return StatusView("已安装，重新打开 App 后生效",
                              "安装时系统音频服务重启过，正在运行的 App 看不到新麦克风。"
                              "点「重新打开 App」，App 会自动退出并重新打开。", "neutral")
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
            self._sync_ble_remote_mic()
            self.voice_changed.emit()

    def setup_virtual_mic(self):
        try:
            if sys.platform == "darwin" and self.microphone_needs_relaunch:
                self.relaunch_requested.emit()
                return
            if sys.platform == "darwin":
                from .devices import resources
                from .installers import open_package
                pkg = resources() / "components" / "RemoteHubAudio.pkg"
                if not pkg.exists():
                    pkg = resources() / "build" / "RemoteHubAudio.pkg"
                if not pkg.exists():
                    raise FileNotFoundError("虚拟麦克风安装文件缺失，请重新安装 10RemoteHub。")
                open_package(pkg)
                self.tell("已打开安装器。安装时声音和录音会短暂中断，请先暂停通话与录音；装完点「重新打开 App」启用麦克风。")
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

    # -- BLE dongle remote microphone ----------------------------------------------------

    def uses_ble_remote_mic(self) -> bool:
        """True when the active remote's remote-mic path is a BLE dongle (e.g. FIBER0D)."""
        return bool(self.supports_remote_mic and not self.uses_dedicated_reader())

    def _ble_dongle_reader(self):
        reader_for = getattr(self, "_reader_for", None)
        return reader_for("ble-dongle") if callable(reader_for) else None

    def ble_voice_ready(self) -> bool:
        reader = self._ble_dongle_reader()
        return bool(reader is not None and getattr(reader, "is_voice_ready", False))

    def ble_voice_protocol_info(self) -> dict[str, str]:
        """Return detected BLE voice protocol metadata & GATT summary for the active remote."""
        reader = self._ble_dongle_reader()
        fn = getattr(reader, "peer_voice_info", None) if reader is not None else None
        info = dict(fn(self.remote) or {}) if callable(fn) and self.remote is not None else {}
        if not info.get("protocol") and self.uses_ble_remote_mic():
            from .ble_dongle_reader import VOICE_PROTOCOL_LABELS
            from .ble_remote import FIBER0D_PAIR

            if self.remote is not None and any(
                not c.receiver and c.transport == "蓝牙" and (c.vendor, c.product) == FIBER0D_PAIR
                for c in self.remote.connections
            ):
                info["protocol"] = "fiber0d"
                info["protocol_label"] = VOICE_PROTOCOL_LABELS["fiber0d"]
        return info

    def has_marked_voice_button(self) -> bool:
        profile = getattr(self, "profile", None)
        if profile is None:
            return False
        return any(
            getattr(b, "role", "") == "voice" or getattr(b, "id", "") == "tv-voice"
            for b in getattr(profile, "buttons", ())
        )

    def voice_protocol_diagnostic_text(self) -> str:
        """Build a privacy-safe diagnostic snippet to help add a new remote's voice protocol."""
        remote = self.remote
        conns = []
        if remote is not None:
            for c in remote.connections:
                conns.append(f"{c.transport} {c.vendor:04X}:{c.product:04X}")
        info = self.ble_voice_protocol_info()
        lines = [
            "=== 10RemoteHub 遥控器语音协议诊断 ===",
            f"遥控器名称：{remote.name if remote is not None else '未选择'}",
            f"连接标识：{', '.join(conns) if conns else '未知'}",
            f"物理语音键已标记：{'是' if self.has_marked_voice_button() else '否'}",
            f"已识别内置语音协议：{info.get('protocol_label') or '未命中（私有协议）'}",
        ]
        if info.get("firmware"):
            lines.append(f"BLE 固件版本：{info['firmware']}")
        if info.get("gatt_summary"):
            lines.append(f"GATT 指纹：{info['gatt_summary']}")
        lines.append(
            "说明：请将以上信息发给 AI 或开发者，协助分析该型号的 BLE 语音特征通道与音频编码格式（如 ADPCM / G.722.1 / mSBC / Opus）。"
        )
        return "\n".join(lines)

    def _sync_ble_remote_mic(self) -> None:
        reader = self._ble_dongle_reader()
        setter = getattr(reader, "set_remote_mic_enabled", None) if reader is not None else None
        if callable(setter):
            want = bool(
                self.auto_mapping
                and not self.shutting_down
                and self.uses_ble_remote_mic()
                and self.remote_audio
                and self.voice.output_id
            )
            setter(want)

    def _on_ble_voice_ready_changed(self, _ready: bool) -> None:
        self.voice_changed.emit()

    def _on_ble_voice_event(self, event) -> None:
        if self.shutting_down:
            return
        voice_edge = getattr(event, "voice", None)
        pcm = getattr(event, "pcm", b"")
        if voice_edge is True:
            if not (self.remote_audio and self.voice.output_id):
                return
            self.overlays.close_nine_key()
            if self.voice.owner is None:
                try:
                    self.voice.start("ble")
                except Exception as exc:
                    self.fail(str(exc))
                    reader = self._ble_dongle_reader()
                    stop_fn = getattr(reader, "stop_remote_voice", None) if reader is not None else None
                    if callable(stop_fn):
                        stop_fn()
                    return
                self.recording_requested = True
                self.voice_key_active.emit(True)
                self.tell("正在通过遥控器麦克风录音…")
                self.voice_changed.emit()
        if pcm and self.voice.owner == "ble":
            try:
                self.voice.feed("ble", pcm)
            except Exception as exc:
                self.fail(str(exc))
                self.stop_voice()
                return
        if voice_edge is False:
            self.recording_requested = False
            self.voice_key_active.emit(False)
            if self.voice.owner == "ble":
                self.voice.finish("ble")
            self.voice_changed.emit()

    # -- USB voice link ------------------------------------------------------------------

    def ensure_remote_audio(self):
        self._sync_ble_remote_mic()
        if (self.usb.collector is not None and self.auto_mapping and not self.shutting_down
                and self.supports_remote_mic and self.uses_dedicated_reader() and self.keys_ready
                and self.device.capture_ready and self.remote_audio
                and self.voice.output_id and not self.usb.ready and not self.usb_connecting):
            self.start_usb(self.profile)

    def usb_view(self):
        from .controller import StatusView

        if not self.voice_enabled:
            return StatusView("未启用", "选择麦克风后启用语音输入。", "neutral")
        if not self.remote_audio:
            return StatusView("不需要", "仅触发识别时不连接遥控器音频。", "neutral")
        if self.uses_ble_remote_mic():
            if not self.voice.output_id:
                return StatusView("未连接", "请先完成虚拟麦克风设置。", "neutral")
            if self.ble_voice_ready():
                return StatusView("已连接", "按住遥控器语音键开始说话。", "ok")
            return StatusView("等待连接", "按遥控器任意键唤醒后自动连上语音通路。", "busy")
        if self.usb.ready:
            return StatusView("已连接", "按遥控器语音键开始说话。", "ok")
        if self.usb_connecting or self.usb.authenticated or self.usb.server.isListening():
            return StatusView("正在连接", "正在准备遥控器音频通路。", "busy")
        if not self.voice.output_id:
            return StatusView("未连接", "请先完成虚拟麦克风设置。", "neutral")
        return StatusView("未连接", "确认接收器已插好。" if self.usb.collector is not None else
                          "点击「连接遥控器音频」后即可使用。", "neutral")

    def voice_view(self):
        from .controller import StatusView

        if not self.voice_enabled:
            return StatusView("未启用", "选择麦克风后启用语音输入。", "neutral")
        owner = self.voice.owner
        if self.voice.routing:
            return StatusView("正在准备麦克风", "", "busy")
        if self.voice.ending:
            return StatusView("正在结束语音", "", "busy")
        if owner in ("usb", "ble"):
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
        if not self.supports_remote_mic:
            raise RuntimeError("这只遥控器使用识别软件当前的麦克风")
        if self.voice.owner is not None or self.recording_requested:
            raise RuntimeError("请先停止当前语音")
        if not self.voice.output_id:
            raise RuntimeError("虚拟麦克风尚未就绪，请到语音设置查看处理方法。")
        # Any profile of this remote on the receiver pair carries the voice key (fixed or photo-marked).
        on_receiver = [p for p in self.profiles if (p.vendor, p.product) == RECEIVER_PAIR]
        profile = self.profile if self.profile in on_receiver else next(iter(on_receiver), None)
        if profile is None:
            raise RuntimeError("这只遥控器没有使用 0416:0602 接收器的配置方案")
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
        self.overlays.close_nine_key()
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
        self.voice_key_active.emit(False)
        if self.usb.recording_requested:
            self.usb.stop_audio()
        reader = self._ble_dongle_reader()
        stop_fn = getattr(reader, "stop_remote_voice", None) if reader is not None else None
        if callable(stop_fn):
            stop_fn()
        self.voice_changed.emit()

    def _voice_edge(self, down):
        """Windows and Linux collectors keep the receiver's voice session aligned with the physical key."""
        if not self.supports_remote_mic or (sys.platform != "win32" and not sys.platform.startswith("linux")):
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
        self.overlays.close_nine_key()
        if self.remote_audio:
            if self.uses_ble_remote_mic():
                self.tell("请直接按住遥控器上的实体语音键说话。")
                return
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
        reader = self._ble_dongle_reader()
        stop_fn = getattr(reader, "stop_remote_voice", None) if reader is not None else None
        if callable(stop_fn):
            stop_fn()
        if self.voice.owner is not None and self.voice.owner != "usb":
            self.voice.finish(self.voice.owner)
        self.voice_changed.emit()
