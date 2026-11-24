"""Development-era remote file (read for migration) and the shared-HID key runtime."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import sys

from PySide6.QtCore import QObject, Qt, Signal

from .model import Profile, atomic_write, validate_keys
from .store import MAX_FILE, MAX_PROFILES, validate_settings


@dataclass
class LearnedRemote:
    profile: Profile
    transport: str
    unit_key: str = ""
    voice_keys: list[str] = field(default_factory=lambda: ["alt"])
    settings: dict = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.settings, dict):
            self.settings.setdefault("voice_source", "trigger")
            self.settings.setdefault("voice_auto_focus", False)
            if isinstance(self.voice_keys, list) and all(isinstance(key, str) for key in self.voice_keys):
                self.settings.setdefault("voice_keys", "+".join(self.voice_keys))

    def validate(self):
        self.profile.validate()
        if self.transport not in ("USB", "蓝牙") or not isinstance(self.unit_key, str):
            raise ValueError("遥控器连接信息无效")
        if self.unit_key and not re.fullmatch(r"[0-9a-f]{64}", self.unit_key):
            raise ValueError("遥控器标识无效")
        if not isinstance(self.profile.id, str) or not self.profile.id or not self.profile.image or not any(
            button.configurable for button in self.profile.buttons
        ):
            raise ValueError("请导入照片并至少学习一个按键")
        for key in self.profile.buttons:
            if key.configurable and (
                not key.trigger
                or any(not re.fullmatch(r"(?:hid:(?:7|12):[0-9]+|win:[0-9]+:[0246])", code) for code in key.all_triggers)
            ):
                raise ValueError("遥控器的实体键码无效")
        validate_keys(self.voice_keys, allow_modifier=True, platform=sys.platform)
        validate_settings(self.settings)
        if self.settings.get("voice_source") == "remote":
            raise ValueError("共享 HID 接入支持使用识别软件当前的麦克风")

    def candidates(self, devices):
        return [
            d for d in devices
            if (d.transport, d.vendor, d.product) == (self.transport, self.profile.vendor, self.profile.product)
        ]

    def recognized_device(self, candidates):
        if not self.unit_key:
            return None
        matches = [device for device in candidates if device.unit_key == self.unit_key]
        return matches[0] if len(matches) == 1 else None


class LearnedRemoteStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self):
        try:
            with self.path.open("rb") as stream:
                data = stream.read(MAX_FILE + 1)
        except FileNotFoundError:
            return [], ""
        if len(data) > MAX_FILE:
            raise ValueError("遥控器文件过大")
        try:
            raw = json.loads(data)
            if raw["version"] != 1 or not isinstance(raw["remotes"], list):
                raise ValueError("遥控器版本或格式无效")
            if len(raw["remotes"]) > MAX_PROFILES:
                raise ValueError(f"最多保存 {MAX_PROFILES} 只遥控器")
            remotes = []
            for fields in raw["remotes"]:
                profile = Profile.loads(json.dumps(fields["profile"], ensure_ascii=False))
                remotes.append(LearnedRemote(**dict(fields, profile=profile)))
            active = raw["active"]
            self._validate(remotes, active)
            return remotes, active
        except (TypeError, KeyError) as exc:
            raise ValueError("遥控器文件结构无效") from exc

    def save(self, remotes, active):
        self._validate(remotes, active)
        data = json.dumps({"version": 1, "remotes": [asdict(r) for r in remotes], "active": active}, ensure_ascii=False)
        if len(data.encode("utf-8")) > MAX_FILE:
            raise ValueError("遥控器总大小超过 30 MB，请缩小照片后重试")
        atomic_write(self.path, data)

    @staticmethod
    def _validate(remotes, active):
        if len(remotes) > MAX_PROFILES:
            raise ValueError(f"最多保存 {MAX_PROFILES} 只遥控器")
        for remote in remotes:
            remote.validate()
        ids = {remote.profile.id for remote in remotes}
        if len(ids) != len(remotes) or not isinstance(active, str) or (active and active not in ids):
            raise ValueError("遥控器标识无效")


class LearnedRemoteRuntime(QObject):
    """Consume physical edges only from the currently opened HID connection."""

    key_received = Signal(int, object, str, bool)
    device_removed = Signal(int)
    changed = Signal()
    key = Signal(str, bool)
    stopped = Signal()
    feedback = Signal(str)

    def __init__(self, reader, parent=None):
        super().__init__(parent)
        self.reader = reader
        self.generation = 0
        self.device = None
        self.held = {}
        self.scope = ""
        self.take_over_triggers: tuple[str, ...] = ()
        self.key_received.connect(self._on_key, Qt.ConnectionType.QueuedConnection)
        self.device_removed.connect(self._on_removed, Qt.ConnectionType.QueuedConnection)

    def listen(self, device):
        self.stop()
        generation = self.generation
        # Daily use: take the remote over where the platform allows it, so a key
        # does only what is mapped and not its original action too.
        can_take_over = hasattr(self.reader, "take_over")
        has_triggers = hasattr(self.reader, "take_over_triggers")
        if can_take_over:
            self.reader.take_over = True
        if has_triggers:
            self.reader.take_over_triggers = tuple(self.take_over_triggers)
        try:
            opened, total = self.reader.start(
                device.id,
                lambda channel, trigger, down: self.key_received.emit(generation, channel, trigger, down),
                lambda: self.device_removed.emit(generation),
                expected=device,
            )
        except (OSError, RuntimeError) as exc:
            self.feedback.emit(str(exc))
            return False
        finally:
            if can_take_over:
                self.reader.take_over = False     # the add dialog reads shared
            if has_triggers:
                self.reader.take_over_triggers = ()
        self.device = device
        scope = [getattr(self.reader, "scope", "")]
        if opened < total:
            scope.append(f"已打开 {opened}/{total} 个按键接口")
        self.scope = " · ".join(detail for detail in scope if detail)
        self.feedback.emit("")
        self.changed.emit()
        return True

    def sync_takeover(self, triggers) -> bool:
        self.take_over_triggers = tuple(triggers)
        if self.device is None or self.reader is None:
            return False
        sync = getattr(self.reader, "sync_takeover", None)
        if not callable(sync):
            return False
        changed = bool(sync(self.take_over_triggers))
        if changed:
            self.changed.emit()
        return changed

    def ensure_takeover(self) -> bool:
        if self.device is None or self.reader is None:
            return False
        ensure = getattr(self.reader, "ensure_takeover", None)
        if not callable(ensure):
            return False
        changed = bool(ensure())
        if changed:
            self.changed.emit()
        return changed

    def stop(self):
        self.generation += 1
        if self.reader is not None:
            self.reader.stop()
        was_open = self.device is not None
        self.held.clear()
        self.device = None
        self.scope = ""
        if was_open:
            self.stopped.emit()

    def _on_removed(self, generation):
        if generation == self.generation and self.device is not None:
            self.stop()
            self.feedback.emit("遥控器已断开，正在检测重新连接")
            self.changed.emit()

    def _on_key(self, generation, channel, trigger, down):
        if generation != self.generation or self.device is None:
            return
        channels = self.held.get(trigger)
        if down:
            if channels is not None:
                channels.add(channel)
                return
            self.held[trigger] = {channel}
        else:
            if channels is None or channel not in channels:
                return
            channels.remove(channel)
            if channels:
                return
            del self.held[trigger]
        self.key.emit(trigger, down)
