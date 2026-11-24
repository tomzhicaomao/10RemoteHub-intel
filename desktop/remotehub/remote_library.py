"""Remote library: remotes, the connections they accept, and their profiles.

A *remote* is the handset the user holds: photo, key table, profiles and voice
settings. A *connection* is what the computer actually sees: a USB receiver or a
Bluetooth HID device. One remote can accept several connections (spare
receivers) and one connection can be accepted by several remotes (one receiver
shared by different handsets). Only one remote is active at a time; it reads
from any present connection it accepts.

On-disk format (``remotes.json``)::

    {"version": 2,
     "remotes": [{"id", "name", "model_id", "voice_protocol", "connections": [
                     {"transport", "vendor", "product", "units": [unit_key, ...]}],
                  "voice", "active_profile_id", "profiles": [...]}],
     "active": "<remote id>",
     "settings": {...global settings...}}

``voice_protocol`` names the built-in voice protocol of the handset's own
microphone (``protocols.VOICE_PROTOCOLS``), recorded when the user marked the
voice key on the photo and the connection matched that protocol. It is written
only when set, so older 0.3.0 builds still load remotes without a microphone.

An empty ``units`` list accepts every unit of that connection type. The bundled
receiver ``USB 0416:0602`` is read by the dedicated collector (keys and remote
microphone); every other connection by the shared HID reader (keys only).

The first load migrates 0.2.0 ``profiles.json`` and the development
``learned-remotes.json``. Those files are only read, never rewritten, so an
older version keeps working after a downgrade.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import uuid

from .model import Profile, atomic_write, profile_to_dict
from .store import MAX_FILE, MAX_PROFILES, ConfigStore, validate_settings

VERSION = 2
TRANSPORTS = ("USB", "蓝牙")
MAX_REMOTES = 24
MAX_CONNECTIONS = 8
MAX_UNITS = 16
RECEIVER_MODEL = "usb-0416-0602"
RECEIVER_PAIR = (0x0416, 0x0602)
# Settings that belong to one remote. Everything else is application-wide.
REMOTE_SETTINGS = (
    "voice_source", "voice_keys", "voice_mode", "record_mode", "input_routing", "output", "gain",
    "codec", "voice_auto_focus", "guide_voice_choice", "guide_verified_voice", "app_layers_enabled",
)
TRIGGER = re.compile(r"(?:hid:(?:7|12):[0-9]{1,5}|win:[0-9]{1,5}:[0246])\Z")
RECEIVER_NAME = "遥控器 0416:0602"


def placeholder_profile():
    return Profile(id="no-remote", name="未添加遥控器", vendor=0, product=0)


@dataclass
class Connection:
    transport: str
    vendor: int
    product: int
    units: list[str] = field(default_factory=list)
    # Bluetooth only: who assigned the vendor ID (1 Bluetooth SIG, 2 USB-IF), so
    # an equal number from the other registry is not taken for this remote.
    vendor_id_source: int | None = None

    @property
    def receiver(self):
        """Bundled receiver: dedicated collector with keys and microphone audio."""
        return self.transport == "USB" and (self.vendor, self.product) == RECEIVER_PAIR

    def accepts(self, device):
        """``device`` is a ProbeDevice-like object from a reader."""
        source = getattr(device, "vendor_id_source", None)
        return ((device.transport, device.vendor, device.product) == (self.transport, self.vendor, self.product)
                and (not self.units or device.unit_key in self.units)
                and (self.vendor_id_source is None or source == self.vendor_id_source))

    def label(self):
        kind = "USB 接收器" if self.transport == "USB" else "蓝牙"
        scope = f" · 限 {len(self.units)} 个" if self.units else ""
        return f"{kind} {self.vendor:04X}:{self.product:04X}{scope}"

    def validate(self):
        if self.transport not in TRANSPORTS:
            raise ValueError("连接方式无效")
        if any(type(n) is not int or not 0 <= n <= 0xFFFF for n in (self.vendor, self.product)):
            raise ValueError("接收端型号编号无效")
        if (not isinstance(self.units, list) or len(self.units) > MAX_UNITS
                or any(not isinstance(u, str) or not re.fullmatch(r"[0-9a-f]{64}", u) for u in self.units)
                or len(set(self.units)) != len(self.units)):
            raise ValueError("接收端标识无效")
        if self.vendor_id_source is not None and (type(self.vendor_id_source) is not int
                                                  or self.vendor_id_source not in (1, 2) or self.transport != "蓝牙"):
            raise ValueError("蓝牙厂商编号来源无效")


@dataclass
class Remote:
    name: str
    connections: list[Connection]
    profiles: list[Profile]
    model_id: str = ""
    active_profile_id: str = ""
    voice_protocol: str = ""
    voice: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def uses_receiver(self):
        return any(c.receiver for c in self.connections)

    @property
    def is_fixed_model(self):
        return self.model_id == RECEIVER_MODEL

    @property
    def mic_protocol(self):
        """Voice protocol of the handset microphone: the bundled model always has the receiver's."""
        return RECEIVER_MODEL if self.is_fixed_model else self.voice_protocol

    def supports_remote_mic_for(self, readers=None):
        if readers is None:
            from .readers import default_registry

            readers = default_registry()
        return readers.supports_remote_mic(self)

    @property
    def supports_remote_mic(self):
        return self.supports_remote_mic_for()

    @property
    def active_profile(self):
        return next((p for p in self.profiles if p.id == self.active_profile_id), self.profiles[0])

    def accepts(self, device):
        return any(c.accepts(device) for c in self.connections)

    def connection_for(self, device):
        return next((c for c in self.connections if c.accepts(device)), None)

    def validate(self, *, readers=None):
        if not isinstance(self.id, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", self.id):
            raise ValueError("遥控器标识无效")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 100:
            raise ValueError("遥控器名称应为 1–100 字")
        if not isinstance(self.model_id, str) or len(self.model_id) > 64:
            raise ValueError("遥控器型号无效")
        if self.voice_protocol:
            from .protocols import VOICE_PROTOCOLS

            if self.voice_protocol not in VOICE_PROTOCOLS:
                raise ValueError("遥控器语音协议无效")
        if not isinstance(self.connections, list) or not 1 <= len(self.connections) <= MAX_CONNECTIONS:
            raise ValueError(f"每只遥控器接受 1–{MAX_CONNECTIONS} 种接收端")
        kinds = set()
        for connection in self.connections:
            connection.validate()
            kind = (connection.transport, connection.vendor, connection.product, connection.vendor_id_source)
            if kind in kinds:
                raise ValueError("同一种接收端只能登记一次")
            kinds.add(kind)
        if not isinstance(self.voice, dict) or set(self.voice) - set(REMOTE_SETTINGS):
            raise ValueError("遥控器语音设置无效")
        validate_settings(self.voice)
        if not self.supports_remote_mic_for(readers) and self.voice.get("voice_source") == "remote":
            raise ValueError("这只遥控器使用识别软件当前的麦克风")
        if not 1 <= len(self.profiles) <= MAX_PROFILES:
            raise ValueError(f"每只遥控器保存 1–{MAX_PROFILES} 个配置方案")
        ids = set()
        for profile in self.profiles:
            profile.validate()
            if profile.id in ids:
                raise ValueError("配置方案标识重复")
            ids.add(profile.id)
            if not self.is_fixed_model:
                for button in profile.buttons:
                    if button.configurable:
                        for code in button.all_triggers:
                            if not TRIGGER.fullmatch(code):
                                raise ValueError("遥控器的实体键码无效")
        if self.active_profile_id and self.active_profile_id not in ids:
            raise ValueError("当前配置方案无效")

    def to_json(self):
        data = {
            "id": self.id, "name": self.name, "model_id": self.model_id,
            "connections": [{k: v for k, v in asdict(c).items() if not (k == "vendor_id_source" and v is None)}
                            for c in self.connections],
            "voice": self.voice,
            "active_profile_id": self.active_profile_id, "profiles": [profile_to_dict(p) for p in self.profiles],
        }
        if self.voice_protocol:
            data["voice_protocol"] = self.voice_protocol
        return data

    @classmethod
    def from_json(cls, data):
        if not isinstance(data, dict):
            raise ValueError("遥控器格式无效")
        fields = dict(data)
        if set(fields) - {"id", "name", "model_id", "voice_protocol", "connections", "voice",
                          "active_profile_id", "profiles"}:
            raise ValueError("遥控器字段无效")
        if not isinstance(fields.get("voice_protocol", ""), str):
            raise ValueError("遥控器语音协议无效")
        profiles = [Profile.loads(json.dumps(p, ensure_ascii=False)) for p in fields.pop("profiles")]
        connections = []
        for raw in fields.pop("connections"):
            if not isinstance(raw, dict) or set(raw) - {"transport", "vendor", "product", "units", "vendor_id_source"}:
                raise ValueError("接收端格式无效")
            connections.append(Connection(**raw))
        remote = cls(profiles=profiles, connections=connections, **fields)
        if "voice_protocol" not in data:
            # Earlier 0.3.0 builds saved a photo-learned receiver remote without its protocol;
            # a marked voice key on the photo is the user's own "it has a microphone".
            if (not remote.is_fixed_model and remote.uses_receiver
                    and any(b.role == "voice" for p in profiles for b in p.buttons)):
                remote.voice_protocol = RECEIVER_MODEL
        return remote


def receiver_connection():
    return Connection("USB", *RECEIVER_PAIR)


def split_settings(settings):
    """Return (global settings, per-remote settings)."""
    shared = {k: v for k, v in settings.items() if k not in REMOTE_SETTINGS and k != "active_profile_id"}
    own = {k: v for k, v in settings.items() if k in REMOTE_SETTINGS}
    return shared, own


class RemoteLibraryStore:
    """Persistent remotes plus the store interface used by ``AppController``.

    ``profiles`` and ``settings`` are the live objects of the active remote: the
    controller edits them in place and calls ``save``. Per-remote settings are
    kept in ``settings`` while that remote is active and written back on save or
    when another remote is activated.
    """

    def __init__(self, directory: Path, *, readers=None):
        self.directory = Path(directory)
        self.readers = readers
        self.remotes: list[Remote] = []
        self.active = ""
        self.shared: dict = {}
        self.settings: dict = {}
        self.load_error = ""
        self.migrated_from: list[str] = []
        self._placeholder = [placeholder_profile()]

    @property
    def path(self):
        return self.directory / "remotes.json"

    # -- views -----------------------------------------------------------------

    @property
    def remote(self) -> Remote | None:
        return next((r for r in self.remotes if r.id == self.active), None)

    @property
    def profiles(self):
        remote = self.remote
        return remote.profiles if remote is not None else self._placeholder

    # -- load / migrate ----------------------------------------------------------

    def load(self):
        try:
            if self.path.exists():
                self._load_library()
                self._enter(self.remote)
            else:
                self._migrate()
                self._enter(self.remote)
                if self.migrated_from:
                    self.save()
        except (json.JSONDecodeError, UnicodeError, KeyError, TypeError, AttributeError):
            self._reset("遥控器配置格式不完整或已损坏，未覆盖原文件；请恢复备份后重启。")
            self._enter(self.remote)
        except OSError:
            self._reset("无法读取遥控器配置，未覆盖原文件；请检查文件访问权限和存储空间。")
            self._enter(self.remote)
        except ValueError as exc:
            self._reset(f"遥控器配置内容无效（{exc}），未覆盖原文件。")
            self._enter(self.remote)

    def _reset(self, message):
        self.remotes, self.active, self.shared = [], "", {}
        self.load_error = message

    def _load_library(self):
        if self.path.stat().st_size > MAX_FILE:
            raise ValueError("配置文件过大")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data["version"] != VERSION or not isinstance(data["remotes"], list):
            raise ValueError("不支持的配置版本")
        remotes = [Remote.from_json(raw) for raw in data["remotes"]]
        shared = data.get("settings", {})
        validate_settings(shared)
        self._check(remotes, data["active"], readers=self.readers)
        self.remotes, self.active, self.shared = remotes, data["active"], dict(shared)

    def _migrate(self):
        remotes, shared = [], {}
        legacy = ConfigStore(self.directory)
        if legacy.path.exists():
            legacy.load()
            if legacy.load_error:
                raise ValueError("0.2.0 配置无法读取")
            shared, own = split_settings(legacy.settings)
            receiver = [p for p in legacy.profiles if (p.vendor, p.product) == RECEIVER_PAIR]
            if receiver:
                active = legacy.settings.get("active_profile_id", "")
                remotes.append(Remote(
                    name=RECEIVER_NAME, connections=[receiver_connection()],
                    profiles=receiver, model_id=RECEIVER_MODEL, voice=own,
                    active_profile_id=active if any(p.id == active for p in receiver) else receiver[0].id,
                ))
            self.migrated_from.append(legacy.path.name)
        remotes.extend(self._learned_remotes(remotes))
        names = set()
        for remote in remotes:
            remote.name = _unique(remote.name, names)
        self._check(remotes, remotes[0].id if remotes else "", readers=self.readers)
        self.remotes, self.active, self.shared = remotes, remotes[0].id if remotes else "", shared

    def _learned_remotes(self, existing):
        from .learned_remotes import LearnedRemoteStore
        from .photo_template import is_fixed_model

        path = self.directory / "learned-remotes.json"
        if not path.exists():
            return []
        try:
            learned, _ = LearnedRemoteStore(path).load()
        except (json.JSONDecodeError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("开发期遥控器配置已损坏或无法读取") from exc
        self.migrated_from.append(path.name)
        result = []
        for item in learned:
            _, own = split_settings(item.settings)
            if own.get("voice_source") == "remote":
                own["voice_source"] = "trigger"
            pair = (item.profile.vendor, item.profile.product)
            receiver = next((r for r in existing if r.is_fixed_model), None)
            if (pair == RECEIVER_PAIR and item.transport == "USB" and is_fixed_model(item.profile)
                    and receiver is not None and len(receiver.profiles) < MAX_PROFILES):
                item.profile.id = uuid.uuid4().hex
                receiver.profiles.append(item.profile)
                continue
            if pair == RECEIVER_PAIR and item.transport == "USB":
                own.setdefault("voice_source", "trigger")
            # The development version tied a remote to one unit. Accept every
            # unit of that type: spare receivers must keep working.
            result.append(Remote(
                name=item.profile.name, connections=[Connection(item.transport, *pair)],
                profiles=[item.profile], voice=own, active_profile_id=item.profile.id,
            ))
        return result

    @staticmethod
    def _check(remotes, active, *, readers=None):
        if len(remotes) > MAX_REMOTES:
            raise ValueError(f"最多保存 {MAX_REMOTES} 只遥控器")
        for remote in remotes:
            remote.validate(readers=readers)
        ids = {r.id for r in remotes}
        if len(ids) != len(remotes) or not isinstance(active, str) or (active and active not in ids):
            raise ValueError("遥控器标识无效")
        if remotes and not active:
            raise ValueError("缺少当前遥控器")

    # -- switching -----------------------------------------------------------------

    def _leave(self):
        """Write the live settings back: shared keys to the library, the rest to the remote."""
        self.shared, _ = split_settings(self.settings)
        remote = self.remote
        if remote is not None:
            _, own = split_settings(self.settings)
            remote.voice = own
            active = self.settings.get("active_profile_id", remote.active_profile_id)
            # The controller saves before it selects a replacement profile.
            remote.active_profile_id = active if any(p.id == active for p in remote.profiles) else remote.profiles[0].id

    def _enter(self, remote):
        # Replace contents in place: the controller and pages hold this dict.
        self.settings.clear()
        self.settings.update(self.shared)
        if remote is not None:
            self.settings.update(remote.voice)
            if not remote.active_profile_id or remote.active_profile_id not in {p.id for p in remote.profiles}:
                remote.active_profile_id = remote.profiles[0].id
            self.settings["active_profile_id"] = remote.active_profile_id

    def activate(self, identity):
        """Make another remote current and persist it. Rolls back on failure."""
        if identity not in {r.id for r in self.remotes}:
            raise ValueError("找不到这只遥控器")
        self._leave()
        previous = self.active
        self.active = identity
        self._enter(self.remote)
        try:
            self.save()
        except (OSError, ValueError):
            self.active = previous
            self._enter(self.remote)
            raise

    def add(self, remote: Remote):
        if len(self.remotes) >= MAX_REMOTES:
            raise ValueError(f"最多保存 {MAX_REMOTES} 只遥控器")
        remote.name = _unique(remote.name.strip() or "遥控器", {r.name for r in self.remotes})
        remote.validate(readers=self.readers)
        self._leave()
        previous = list(self.remotes), self.active
        self.remotes.append(remote)
        self.active = remote.id
        self._enter(remote)
        try:
            self.save()
        except (OSError, ValueError):
            self.remotes, self.active = previous
            self._enter(self.remote)
            raise

    def replace(self, remote: Remote):
        """Store an edited copy of an existing remote (more keys learned, renamed)."""
        index = next(i for i, r in enumerate(self.remotes) if r.id == remote.id)
        remote.validate(readers=self.readers)
        self._leave()
        previous = self.remotes[index]
        self.remotes[index] = remote
        self.active = remote.id
        self._enter(remote)
        try:
            self.save()
        except (OSError, ValueError):
            self.remotes[index] = previous
            self._enter(self.remote)
            raise

    def rename(self, identity, name):
        name = name.strip() if isinstance(name, str) else ""
        if not name or len(name) > 100:
            raise ValueError("遥控器名称应为 1–100 字")
        remote = next(r for r in self.remotes if r.id == identity)
        if any(r.name == name and r.id != identity for r in self.remotes):
            raise ValueError("已有同名遥控器")
        old, remote.name = remote.name, name
        try:
            self.save()
        except (OSError, ValueError):
            remote.name = old
            raise

    def remove(self, identity):
        self._leave()
        previous = list(self.remotes), self.active
        self.remotes = [r for r in self.remotes if r.id != identity]
        if self.active == identity:
            self.active = self.remotes[0].id if self.remotes else ""
        self._enter(self.remote)
        try:
            self.save()
        except (OSError, ValueError):
            self.remotes, self.active = previous
            self._enter(self.remote)
            raise

    # -- save --------------------------------------------------------------------

    def serialize(self):
        self._leave()
        validate_settings(self.shared)
        self._check(self.remotes, self.active, readers=self.readers)
        text = json.dumps({"version": VERSION, "remotes": [r.to_json() for r in self.remotes],
                           "active": self.active, "settings": self.shared}, ensure_ascii=False, indent=2)
        if len(text.encode("utf-8")) > MAX_FILE:
            raise ValueError("配置总量超过 30 MB，请减少图片大小")
        return text

    def save(self):
        if self.load_error:
            raise ValueError("遥控器配置读取失败，本次不会覆盖原文件")
        atomic_write(self.path, self.serialize())


def _unique(name, names):
    base = name[:95]
    candidate, number = name, 2
    while candidate in names:
        candidate = f"{base} {number}"
        number += 1
    names.add(candidate)
    return candidate


def sync_layout(remote: Remote, source: Profile):
    """Copy the photo and physical key table of ``source`` to the remote's other
    profiles. Existing actions stay; new keys arrive without actions."""
    from copy import deepcopy

    from .model import GESTURE_FIELDS, Action

    for profile in remote.profiles:
        if profile is source:
            continue
        profile.image = source.image
        existing = {button.id: button for button in profile.buttons}
        buttons = []
        for button in source.buttons:
            target = existing.get(button.id)
            if target is None:
                target = deepcopy(button)
                for name in GESTURE_FIELDS.values():
                    setattr(target, name, Action())
            else:
                target.name = button.name
                target.trigger = button.trigger
                target.extra_triggers = list(button.extra_triggers)
                target.rect = list(button.rect)
                target.role = button.role
            buttons.append(target)
        kept = {button.id for button in buttons}
        for layer in profile.app_layers:
            for identity in list(layer.overrides):
                if identity not in kept:
                    layer.overrides.pop(identity, None)
        profile.buttons = buttons


def remote_for_device(name, device, profile, model_id="", voice_protocol=""):
    """A new remote that accepts every unit of the connection type it was added with."""
    connection = connection_for_device(device)
    own_mic = model_id == RECEIVER_MODEL or bool(voice_protocol)
    voice = {"voice_source": "trigger"} if connection.receiver and not own_mic else {}
    return Remote(
        name=name, connections=[connection],
        profiles=[profile], model_id=model_id, voice=voice, active_profile_id=profile.id,
        voice_protocol=voice_protocol,
    )


def connection_for_device(device):
    source = getattr(device, "vendor_id_source", None) if device.transport == "蓝牙" else None
    return Connection(device.transport, device.vendor, device.product,
                      vendor_id_source=source if source in (1, 2) else None)


def with_connection(remote: Remote, device):
    """Accept ``device``'s connection type too (e.g. a spare receiver of another model)."""
    if not remote.accepts(device) and len(remote.connections) < MAX_CONNECTIONS:
        remote.connections.append(connection_for_device(device))
    return remote
