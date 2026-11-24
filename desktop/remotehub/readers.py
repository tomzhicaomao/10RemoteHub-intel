"""Unified input-reader contract and registry for all key acquisition paths.

Every remote acquisition path — dedicated receiver collection (0416:0602),
shared OS HID (USB/Bluetooth), or future private/GATT/Linux readers — registers
a ``ReaderEntry`` here instead of hardcoding branches inside ``AppController``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from .probe_device import ProbeDevice
from .remote_library import RECEIVER_MODEL

if TYPE_CHECKING:
    from .remote_library import Connection, Remote

RECEIVER_PAIR = (0x0416, 0x0602)


@runtime_checkable
class KeyReader(Protocol):
    """Unified interface shared by dedicated and shared key readers."""

    def enumerate(self) -> list[ProbeDevice]:
        """List readable devices currently visible to this reader."""

    def start(
        self,
        identity: Any,
        on_key: Callable[[Any, str, bool], None],
        on_removed: Callable[[], None],
        *,
        expected: ProbeDevice | None = None,
    ) -> tuple[int, int]:
        """Listen only to the selected device and return (opened, total)."""

    def stop(self) -> None:
        """Stop callbacks and release interfaces opened by ``start``."""

    def close(self) -> None:
        """Release resources when shutting down."""


def peek_devices(reader: Any) -> list[ProbeDevice]:
    """Read-only device snapshot that never interrupts an active session."""
    peek = getattr(reader, "peek", None)
    if callable(peek):
        return list(peek())
    return list(getattr(reader, "devices", ()))


class ReceiverReader:
    """``KeyReader`` adapter around the dedicated 0416:0602 collector."""

    scope = "专用采集"

    def __init__(
        self,
        device=None,
        monitor=None,
        profile_provider: Callable[[], Any] | None = None,
        host_provider: Callable[[], Any] | None = None,
    ):
        self.device = device
        self.monitor = monitor
        self.profile_provider = profile_provider
        self.host_provider = host_provider
        self._key_slot = None
        self._stopped_slot = None
        self._active_generation: int | None = None

    def enumerate(self) -> list[ProbeDevice]:
        if self.monitor is None:
            return []
        found: list[ProbeDevice] = []
        for idx, item in enumerate(getattr(self.monitor, "devices", ())):
            if (item.vendor, item.product) != RECEIVER_PAIR:
                continue
            unit_key = hashlib.sha256(item.serial.encode("utf-8")).hexdigest() if item.serial else ""
            conn = item.connection or str(idx)
            found.append(
                ProbeDevice(
                    id=("receiver", conn),
                    name=getattr(item, "name", "") or "USB 0416:0602",
                    transport="USB",
                    vendor=item.vendor,
                    product=item.product,
                    unit_key=unit_key,
                    connection=(idx + 1,),
                )
            )
        return found

    def peek(self) -> list[ProbeDevice]:
        return self.enumerate()

    def _disconnect_slots(self) -> None:
        if self.device is None:
            return
        if self._key_slot is not None:
            try:
                self.device.key.disconnect(self._key_slot)
            except (RuntimeError, TypeError):
                pass
            self._key_slot = None
        if self._stopped_slot is not None:
            try:
                self.device.stopped.disconnect(self._stopped_slot)
            except (RuntimeError, TypeError):
                pass
            self._stopped_slot = None

    def start(self, identity, on_key, on_removed, *, expected=None) -> tuple[int, int]:
        if self.device is None or self.monitor is None:
            raise RuntimeError("专用读取组件不可用")
        devices = self.enumerate()
        match = next((d for d in devices if d.id == identity), None)
        if match is None or (expected is not None and match != expected):
            raise RuntimeError("未找到所选的专用接收器，请重新检测")
        self.stop()
        from .model import Profile

        profile = (
            self.profile_provider()
            if self.profile_provider is not None
            else Profile(name=match.name, vendor=match.vendor, product=match.product)
        )
        host = self.host_provider() if self.host_provider is not None else None
        expected_gen = self.device.generation
        self._active_generation = expected_gen
        self._key_slot = lambda trigger, down: on_key(("receiver", 0), trigger, down)
        self._stopped_slot = (
            lambda gen: on_removed() if gen == self._active_generation else None
        )
        self.device.key.connect(self._key_slot)
        self.device.stopped.connect(self._stopped_slot)
        try:
            self.device.start(profile, host)
        except Exception:
            self._disconnect_slots()
            self._active_generation = None
            raise
        return 1, 1

    def stop(self) -> None:
        self._active_generation = None
        self._disconnect_slots()
        if self.device is not None:
            self.device.stop()

    def close(self) -> None:
        self.stop()


class CompositeReader:
    """Merge multiple non-dedicated readers while enforcing one active session at a time."""

    def __init__(self, readers: list[KeyReader]):
        self.readers = [r for r in readers if r is not None]
        self._owners: dict[Any, KeyReader] = {}
        self._active_reader: KeyReader | None = None

    @property
    def scope(self) -> str:
        if self._active_reader is not None:
            return getattr(self._active_reader, "scope", "")
        scopes = [getattr(r, "scope", "") for r in self.readers]
        return " · ".join(s for s in scopes if s)

    @property
    def unlisted(self) -> list[Any]:
        items: list[Any] = []
        for reader in self.readers:
            items.extend(getattr(reader, "unlisted", ()) or ())
        return items

    def enumerate(self) -> list[ProbeDevice]:
        devices: list[ProbeDevice] = []
        owners: dict[Any, KeyReader] = {}
        first_error: Exception | None = None
        succeeded = False
        for reader in self.readers:
            try:
                listed = list(reader.enumerate())
                succeeded = True
            except (OSError, RuntimeError) as exc:
                if first_error is None:
                    first_error = exc
                continue
            for dev in listed:
                if dev.id not in owners:
                    owners[dev.id] = reader
                    devices.append(dev)
        if not succeeded and first_error is not None:
            raise first_error
        self._owners = owners
        return devices

    def peek(self) -> list[ProbeDevice]:
        devices: list[ProbeDevice] = []
        seen: set[Any] = set()
        for reader in self.readers:
            for dev in peek_devices(reader):
                if dev.id not in seen:
                    seen.add(dev.id)
                    devices.append(dev)
        return devices

    @property
    def take_over(self) -> bool:
        return any(getattr(reader, "take_over", False) for reader in self.readers)

    @take_over.setter
    def take_over(self, value: bool) -> None:
        for reader in self.readers:
            if hasattr(reader, "take_over"):
                reader.take_over = value

    @property
    def take_over_triggers(self) -> tuple[str, ...]:
        for reader in self.readers:
            if hasattr(reader, "take_over_triggers"):
                return tuple(getattr(reader, "take_over_triggers", ()))
        return ()

    @take_over_triggers.setter
    def take_over_triggers(self, value: tuple[str, ...]) -> None:
        triggers = tuple(value)
        for reader in self.readers:
            if hasattr(reader, "take_over_triggers"):
                reader.take_over_triggers = triggers

    @property
    def seized(self) -> int:
        return getattr(self._active_reader, "seized", 0) if self._active_reader is not None else 0

    @property
    def seize_error(self) -> str:
        return getattr(self._active_reader, "seize_error", "") if self._active_reader is not None else ""

    def sync_takeover(self, triggers) -> bool:
        sync = getattr(self._active_reader, "sync_takeover", None)
        return bool(sync(triggers)) if callable(sync) else False

    def ensure_takeover(self) -> bool:
        ensure = getattr(self._active_reader, "ensure_takeover", None)
        return bool(ensure()) if callable(ensure) else False

    def dongle_status_hint(self) -> str:
        for reader in self.readers:
            hint_fn = getattr(reader, "dongle_status_hint", None)
            if callable(hint_fn):
                text = hint_fn()
                if text:
                    return text
        return ""

    def managed_dongles(self) -> list:
        for reader in self.readers:
            fn = getattr(reader, "managed_dongles", None)
            if callable(fn):
                found = fn()
                if found:
                    return list(found)
        return []

    def scan_ble_dongles(self, target=None) -> list:
        found = []
        for reader in self.readers:
            fn = getattr(reader, "scan_dongles", None)
            if callable(fn):
                found.extend(fn(target=target) or ())
        return found

    def last_ble_scan_ids(self) -> list:
        found = []
        for reader in self.readers:
            found.extend(getattr(reader, "last_scan_ids", None) or ())
        return found

    def last_ble_scan_sure(self) -> list:
        found = []
        for reader in self.readers:
            found.extend(getattr(reader, "last_scan_sure", None) or ())
        return found

    def cancel_ble_scan(self) -> None:
        for reader in self.readers:
            fn = getattr(reader, "cancel_scan", None)
            if callable(fn):
                fn()

    def blocked_ble_remotes(self) -> list:
        items = []
        seen = set()
        for reader in self.readers:
            fn = getattr(reader, "blocked_ble_remotes", None)
            if callable(fn):
                for entry in fn() or ():
                    key = (getattr(entry, "vendor", 0), getattr(entry, "product", 0), getattr(entry, "name", "").casefold())
                    if key not in seen:
                        seen.add(key)
                        items.append(entry)
        return items

    def start(self, identity, on_key, on_removed, *, expected=None) -> tuple[int, int]:
        owner = self._owners.get(identity)
        if owner is None:
            self.enumerate()
            owner = self._owners.get(identity)
        if owner is None:
            self.stop()
            raise RuntimeError("找不到所选的遥控器读取端")
        for reader in self.readers:
            if reader is not owner:
                reader.stop()
        self._active_reader = owner
        try:
            return owner.start(identity, on_key, on_removed, expected=expected)
        except Exception:
            self._active_reader = None
            raise

    def is_connected_to(self, identity) -> bool:
        owner = self._owners.get(identity)
        fn = getattr(owner, "is_connected_to", None) if owner is not None else None
        return bool(fn(identity)) if callable(fn) else False

    def pause(self) -> None:
        if self._active_reader is not None:
            pause_fn = getattr(self._active_reader, "pause", None)
            if callable(pause_fn):
                pause_fn()
                return
        self.stop()

    def stop(self) -> None:
        self._active_reader = None
        for reader in self.readers:
            reader.stop()

    def close(self) -> None:
        self._active_reader = None
        for reader in self.readers:
            reader.close()


def invoke_factory(factory: Callable[..., Any], context: Any = None) -> Any:
    """Invoke a reader factory supporting both ``factory()`` and ``factory(context)``."""
    try:
        sig = inspect.signature(factory)
        positional = [
            p for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    except (TypeError, ValueError):
        positional, has_varargs = [], True
    if has_varargs or positional:
        return factory(context)
    return factory()


@dataclass(frozen=True)
class ReaderEntry:
    """Declarative registration record for an input acquisition path.

    Priority conventions:
    - ``10``: dedicated receiver collector (exclusive USB interface + remote mic)
    - ``50``: default for specialized extension readers (e.g. BLE GATT, custom USB)
    - ``200``: generic shared-HID fallback catch-all
    """

    id: str
    name: str
    handles: Callable[[Connection], bool]
    factory: Callable[..., KeyReader | None]
    remote_mic: bool = False
    remote_mic_for: Callable[[Remote], bool] | None = None
    dedicated: bool = False
    priority: int = 50
    is_present: Callable[[Any, Remote], bool] | None = None
    platforms: tuple[str, ...] = ()

    def platform_supported(self, platform: str | None = None) -> bool:
        if not self.platforms:
            return True
        plat = sys.platform if platform is None else platform
        return any(plat.startswith(prefix) for prefix in self.platforms)

    def supports_mic(self, remote: Remote) -> bool:
        if not self.remote_mic:
            return False
        if self.remote_mic_for is not None:
            return bool(self.remote_mic_for(remote))
        return True


class ReaderRegistry:
    """Ordered registry selecting the single active reader path for a remote."""

    def __init__(self):
        self._entries: list[ReaderEntry] = []

    def register(self, entry: ReaderEntry, *, replace: bool = False) -> ReaderEntry:
        if not isinstance(entry.id, str) or not entry.id.strip():
            raise ValueError("读取端标识不能为空")
        existing_idx = next((i for i, item in enumerate(self._entries) if item.id == entry.id), None)
        if existing_idx is not None:
            if not replace:
                raise ValueError(f"读取端已注册：{entry.id}")
            self._entries[existing_idx] = entry
        else:
            self._entries.append(entry)
        return entry

    def unregister(self, entry_id: str) -> ReaderEntry | None:
        idx = next((i for i, item in enumerate(self._entries) if item.id == entry_id), None)
        if idx is None:
            return None
        return self._entries.pop(idx)

    def get(self, entry_id: str) -> ReaderEntry | None:
        return next((item for item in self._entries if item.id == entry_id), None)

    def clone(self) -> ReaderRegistry:
        copy = ReaderRegistry()
        copy._entries = list(self._entries)
        return copy

    def entries(self, *, platform: str | None = None) -> list[ReaderEntry]:
        supported = [
            (entry.priority, idx, entry)
            for idx, entry in enumerate(self._entries)
            if entry.platform_supported(platform)
        ]
        supported.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in supported]

    def primary_entry_for_connection(
        self,
        connection: Connection,
        *,
        platform: str | None = None,
    ) -> ReaderEntry | None:
        for entry in self.entries(platform=platform):
            if entry.handles(connection):
                return entry
        return None

    def matching(
        self,
        remote: Remote | None,
        *,
        platform: str | None = None,
    ) -> list[ReaderEntry]:
        if remote is None:
            return []
        entries = self.entries(platform=platform)
        order = {entry.id: index for index, entry in enumerate(entries)}
        matched: dict[str, ReaderEntry] = {}
        for connection in remote.connections:
            primary = next((entry for entry in entries if entry.handles(connection)), None)
            if primary is not None and primary.id not in matched:
                matched[primary.id] = primary
        return sorted(matched.values(), key=lambda entry: order[entry.id])

    def select(
        self,
        remote: Remote | None,
        context: Any = None,
        *,
        platform: str | None = None,
    ) -> ReaderEntry | None:
        if remote is None:
            return None
        entries = self.entries(platform=platform)
        # First check any conditional extension/dedicated entry that handles a connection
        # and confirms live presence for this remote (e.g. dedicated 0416:0602 or BLE dongle).
        for entry in entries:
            if entry.is_present is None:
                continue
            if any(entry.handles(conn) for conn in remote.connections) and entry.is_present(context, remote):
                return entry
        candidates = self.matching(remote, platform=platform)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        for entry in candidates:
            if entry.is_present is None or entry.is_present(context, remote):
                return entry
        return candidates[0]

    def supports_remote_mic(
        self,
        remote: Remote | None,
        *,
        platform: str | None = None,
    ) -> bool:
        if remote is None:
            return False
        for entry in self.entries(platform=platform):
            if any(entry.handles(conn) for conn in remote.connections) and entry.supports_mic(remote):
                return True
        return False


def _create_receiver_reader(controller: Any = None) -> ReceiverReader:
    if controller is None:
        return ReceiverReader()
    return ReceiverReader(
        controller.device,
        controller.monitor,
        lambda: controller.profile,
        lambda: controller.host,
    )


def _create_shared_hid_reader(controller: Any = None):
    if controller is not None:
        if getattr(controller, "hid_reader", None) is not None:
            return controller.hid_reader
        if not getattr(controller, "auto_mapping", True) or getattr(controller, "shutting_down", False):
            return None
    if sys.platform == "win32":
        from .windows_probe import WindowsHIDProbe

        reader = WindowsHIDProbe()
    elif sys.platform == "darwin":
        from .mac_hid_probe import MacHIDProbe

        state_path = None
        if controller is not None and getattr(controller, "store", None) is not None:
            directory = getattr(controller.store, "directory", None)
            if directory is not None:
                state_path = Path(directory) / "mac-hid-takeover.json"
        reader = MacHIDProbe(state_path=state_path)
    else:
        return None
    if controller is not None:
        controller.hid_reader = reader
    return reader


def _create_ble_dongle_reader(controller: Any = None):
    if controller is not None:
        if not getattr(controller, "auto_mapping", True) or getattr(controller, "shutting_down", False):
            return None
    from .ble_dongle_reader import BLEDongleReader

    state_dir = None
    on_voice_event = None
    on_voice_ready_changed = None
    remote_mic_enabled = False
    if controller is not None:
        if getattr(controller, "store", None) is not None:
            directory = getattr(controller.store, "directory", None)
            if directory is not None:
                state_dir = Path(directory) / "ble-dongle"
        voice_sig = getattr(controller, "ble_voice_event", None)
        if voice_sig is not None and hasattr(voice_sig, "emit"):
            on_voice_event = voice_sig.emit
        ready_sig = getattr(controller, "ble_voice_ready_changed", None)
        if ready_sig is not None and hasattr(ready_sig, "emit"):
            on_voice_ready_changed = ready_sig.emit
        remote_mic_enabled = bool(
            getattr(controller, "remote_audio", False)
            and getattr(getattr(controller, "voice", None), "output_id", b"")
        )
    return BLEDongleReader(
        state_dir=state_dir,
        on_voice_event=on_voice_event,
        on_voice_ready_changed=on_voice_ready_changed,
        remote_mic_enabled=remote_mic_enabled,
    )


def _ble_dongle_supports_remote_mic(remote: Remote) -> bool:
    from .ble_remote import FIBER0D_PAIR

    if remote.mic_protocol in ("fiber0d", "atv_voice"):
        return True
    return any(
        not conn.receiver and conn.transport == "蓝牙" and (conn.vendor, conn.product) == FIBER0D_PAIR
        for conn in remote.connections
    )


def _ble_dongle_present(context: Any, remote: Remote | None) -> bool:
    if context is None or remote is None:
        return False
    reader_for = getattr(context, "_reader_for", None)
    if not callable(reader_for):
        return False
    reader = reader_for("ble-dongle")
    has_peer = getattr(reader, "has_matching_peer", None)
    return bool(callable(has_peer) and has_peer(remote))


_DEFAULT_REGISTRY = ReaderRegistry()
_DEFAULT_REGISTRY.register(
    ReaderEntry(
        id="receiver",
        name="专用采集",
        handles=lambda conn: conn.receiver,
        factory=_create_receiver_reader,
        remote_mic=True,
        # The receiver decodes the handset microphone itself; a handset counts as having one when
        # it is the bundled model or the user marked its voice key (``Remote.mic_protocol``).
        remote_mic_for=lambda remote: remote.mic_protocol == RECEIVER_MODEL,
        dedicated=True,
        priority=10,
        is_present=lambda ctx, _remote: bool(ctx is not None and ctx._receiver_present()),
    )
)
_DEFAULT_REGISTRY.register(
    ReaderEntry(
        id="hid",
        name="共享 HID",
        handles=lambda conn: not conn.receiver and conn.transport in ("USB", "蓝牙"),
        factory=_create_shared_hid_reader,
        remote_mic=False,
        dedicated=False,
        priority=200,
    )
)
_DEFAULT_REGISTRY.register(
    ReaderEntry(
        id="ble-dongle",
        name="BLE 专属蓝牙棒",
        handles=lambda conn: not conn.receiver and conn.transport == "蓝牙",
        factory=_create_ble_dongle_reader,
        remote_mic=True,
        remote_mic_for=_ble_dongle_supports_remote_mic,
        dedicated=False,
        priority=210,
        is_present=_ble_dongle_present,
    )
)


def default_registry() -> ReaderRegistry:
    """Return the built-in global ``ReaderRegistry``."""
    return _DEFAULT_REGISTRY
