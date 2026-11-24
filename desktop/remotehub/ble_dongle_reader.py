"""Generic USB Bluetooth HCI dongle (WinUSB + Bumble) BLE HOGP KeyReader.

Bypasses Windows' kernel Bluetooth stack (``bthport.sys`` / ``hidbthle.sys``)
which drops SMP encryption handshakes and hides GATT service ``0x1812`` on many
TV-box BLE remotes (such as ``FIBER0D``).

Safety & generality:
- Auto-discovers *any* standard USB Bluetooth HCI adapter (USB Class 0xE0 /
  SubClass 0x01 / Protocol 0x01) — never hardcoded to one VID:PID.
- On Windows, strictly limits discovery to USB Bluetooth adapters whose driver
  service is ``WinUSB`` (checked via ``cfgmgr32``), so the PC's built-in
  Bluetooth (``BTHUSB``) is never touched.
- Decodes *any* standard BLE ``0x1812`` HID Report Map via ``HIDReports``
  (keyboard Page 7 + consumer control Page 12), and automatically enables
  verified ``FIBER0D`` G.722.1 voice reassembly when firmware + Report Map match.
- Completely bypasses OS HID injection: original keys never leak to Windows
  (even in elevated windows), reporting full exclusive takeover via ``seized``.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from typing import Any, Callable, Iterable

from .ble_remote import (
    BLERemoteReports,
    GenericBLERemoteReports,
    VOICE_COMMAND_REPORT,
    VOICE_TAIL_SECONDS,
    fallback_ble_pair,
    is_verified_fiber0d,
    parse_pnp_id,
)
from .ble_voice_atv import (
    ATV_VOICE_CTL,
    ATV_VOICE_RX,
    ATV_VOICE_SERVICE,
    ATV_VOICE_TX,
    ATVVVoiceSession,
    normalize_uuid,
)
from .model import atomic_write
from .probe_device import ProbeDevice

VOICE_PROTOCOL_LABELS = {
    "fiber0d": "FIBER0D 私有语音 (G.722.1)",
    "atv_voice": "Android TV 通用语音 (ATV Voice / IMA-ADPCM)",
}

BT_HCI_CLASS = (0xE0, 0x01, 0x01)
APPLE_VENDOR = 0x05AC
_DISCOVERY_TIMEOUT_S = 20.0
_CONNECT_TIMEOUT_S = 15.0
_GATT_TIMEOUT_S = 8.0
_MAX_VOICE_SECONDS = 60.0
# Voice needs ~150 notifications/s. At Bumble's default 15-30 ms interval, or the slower
# interval a remote asks for to save battery, audio fell seconds behind speech (Windows
# 2026-11-21: the last frame arrived ~3 s after the voice key was released).
_VOICE_INTERVAL_MS = (7.5, 15.0)
_VOICE_SUPERVISION_MS = 2000
_REMOTE_APPEARANCES = {0x0180, 0x03C0, 0x03C1, 0x03C2, 0x03C4}  # Generic Remote / HID / Keyboard / Mouse / Gamepad
_REMOTE_NAME_HINTS = ("ble", "remote", "voice", "fiber", "rc", "tv", "box", "air", "hid")
_STRONG_NAME_HINTS = ("remote", "voice", "fiber")
# How sure a scan is that an advertiser is a remote. Only SURE is auto-connected and
# ends the scan early: a camera advertising a HID-like appearance was picked over the
# remote on Windows (2026-11-21).
REMOTE_SURE, REMOTE_MAYBE, REMOTE_NO = 2, 1, 0


def _link_text(connection: Any) -> str:
    params = getattr(connection, "parameters", None)
    interval = getattr(params, "connection_interval", None)
    latency = getattr(params, "peripheral_latency", None)
    return f"interval={interval}ms latency={latency}" if interval is not None else "interval=?"


def _voice_timing_text(timing: dict[str, float], frames: int) -> str:
    """How far audio delivery trailed speech: 20 ms per frame is real time."""
    parts = []
    if "start" in timing and "first" in timing:
        parts.append(f"first_after={timing['first'] - timing['start']:.2f}s")
    if "first" in timing and "last" in timing and frames > 1:
        parts.append(f"delivered={timing['last'] - timing['first']:.2f}s audio={frames * 0.02:.2f}s")
    if "stop" in timing and "last" in timing:
        parts.append(f"after_release={timing['last'] - timing['stop']:.2f}s")
    return " ".join(parts)


class NotARemoteError(RuntimeError):
    """The connected BLE peer has no HID service: it advertised like a remote but is something else."""


@dataclass(frozen=True)
class USBBTDongle:
    vendor: int
    product: int
    serial: str = ""
    name: str = "USB 蓝牙棒"
    transport_spec: str = ""

    @property
    def spec(self) -> str:
        if self.transport_spec:
            return self.transport_spec
        return f"usb:{self.vendor:04x}:{self.product:04x}"


@dataclass
class BLEPeerRecord:
    address: str
    name: str = "BLE 遥控器"
    vendor: int = 0
    product: int = 0
    vendor_id_source: int | None = None
    revision: int | None = None
    firmware: str = ""
    voice_capable: bool = False
    voice_protocol: str = ""  # "fiber0d" | "atv_voice" | ""
    gatt_summary: str = ""
    paired: bool = False
    address_type: str = "public"  # "public" (/P) | "random" (/R)

    @property
    def bumble_address(self) -> str:
        base = self.address.split("/")[0].strip().upper()
        suffix = "/P" if self.address_type == "public" else "/R"
        return f"{base}{suffix}"

    def normalized(self) -> BLEPeerRecord:
        raw_addr = self.address.strip().upper()
        addr_type = (self.address_type or "public").strip().lower()
        if "/" in raw_addr:
            base, _, tag = raw_addr.partition("/")
            addr = base.strip()
            if tag.startswith("P"):
                addr_type = "public"
            elif tag.startswith("R"):
                addr_type = "random"
        else:
            addr = raw_addr
        if addr_type not in ("public", "random"):
            addr_type = "public"
        vendor, product = int(self.vendor), int(self.product)
        if not (0 < vendor <= 0xFFFF and 0 < product <= 0xFFFF):
            vendor, product = fallback_ble_pair(addr)
        vsrc = self.vendor_id_source if self.vendor_id_source in (1, 2) else None
        return BLEPeerRecord(
            address=addr,
            name=(self.name or "BLE 遥控器").strip()[:80],
            vendor=vendor,
            product=product,
            vendor_id_source=vsrc,
            revision=self.revision if isinstance(self.revision, int) else None,
            firmware=(self.firmware or "").strip()[:64],
            voice_capable=bool(self.voice_capable or self.voice_protocol),
            voice_protocol=(self.voice_protocol or ("fiber0d" if self.voice_capable else "")).strip()[:32],
            gatt_summary=(self.gatt_summary or "").strip()[:240],
            paired=bool(self.paired),
            address_type=addr_type,
        )

    def to_probe_device(self) -> ProbeDevice:
        rec = self.normalized()
        unit_key = sha256(f"ble-dongle:{rec.address}".encode("ascii")).hexdigest()
        display_name = rec.name
        if "BLE" not in display_name.upper():
            display_name = f"{display_name}（BLE 直连）"
        elif "直连" not in display_name:
            display_name = f"{display_name} · 直连"
        return ProbeDevice(
            id=("ble-dongle", rec.address),
            name=display_name,
            transport="蓝牙",
            vendor=rec.vendor,
            product=rec.product,
            unit_key=unit_key,
            connection=(rec.address,),
            vendor_id_source=rec.vendor_id_source,
            bcd_device=rec.revision,
        )


def is_usb_bluetooth_hci(device: Any) -> bool:
    """Return True if a pyusb device exposes standard USB Bluetooth HCI (E0/01/01)."""
    if (
        getattr(device, "bDeviceClass", None),
        getattr(device, "bDeviceSubClass", None),
        getattr(device, "bDeviceProtocol", None),
    ) == BT_HCI_CLASS:
        return True
    try:
        config = device.get_active_configuration()
    except Exception:
        try:
            config = next(iter(device))
        except Exception:
            return False
    try:
        for intf in config:
            if (
                getattr(intf, "bInterfaceClass", None),
                getattr(intf, "bInterfaceSubClass", None),
                getattr(intf, "bInterfaceProtocol", None),
            ) == BT_HCI_CLASS:
                return True
    except Exception:
        return False
    return False


def _windows_winusb_usb_pairs() -> set[tuple[int, int]]:
    """Return present USB (VID, PID) pairs explicitly bound to WinUSB on Windows."""
    if sys.platform != "win32":
        return set()
    try:
        import ctypes as c
        from .windows_probe_metadata import WindowsProbeMetadata, _usb_pair

        meta = WindowsProbeMetadata()
        flags = 0x101  # CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
        size = c.c_uint32()
        if meta.cfg.CM_Get_Device_ID_List_SizeW(c.byref(size), "USB", flags) or not 0 < size.value <= 262144:
            return set()
        buf = c.create_unicode_buffer(size.value)
        if meta.cfg.CM_Get_Device_ID_ListW("USB", buf, size.value, flags):
            return set()
        pairs: set[tuple[int, int]] = set()
        for inst in buf[:].split("\0"):
            pair = _usb_pair(inst)
            if pair is None:
                continue
            node = meta._node(inst)
            if node is None:
                continue
            svc = meta._text(node, "a45c254e-df1c-4efd-8020-67d146a850e0", 6).casefold()
            if svc == "winusb":
                pairs.add(pair)
        return pairs
    except Exception:
        return set()


def _windows_bt_adapters_by_service() -> list[tuple[int, int, str, str]]:
    """Return present Windows USB Bluetooth adapters as ``(vid, pid, service, name)``."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes as c
        from .windows_probe_metadata import WindowsProbeMetadata, _usb_pair

        meta = WindowsProbeMetadata()
        flags = 0x101
        size = c.c_uint32()
        if meta.cfg.CM_Get_Device_ID_List_SizeW(c.byref(size), "USB", flags) or not 0 < size.value <= 262144:
            return []
        buf = c.create_unicode_buffer(size.value)
        if meta.cfg.CM_Get_Device_ID_ListW("USB", buf, size.value, flags):
            return []
        found: list[tuple[int, int, str, str]] = []
        for inst in buf[:].split("\0"):
            pair = _usb_pair(inst)
            if pair is None:
                continue
            node = meta._node(inst)
            if node is None:
                continue
            svc = meta._text(node, "a45c254e-df1c-4efd-8020-67d146a850e0", 6).strip()
            name = meta._name(node) or f"USB {pair[0]:04X}:{pair[1]:04X}"
            compat = meta._node_property(node, "a45c254e-df1c-4efd-8020-67d146a850e0", 4, 0x2012).decode(
                "utf-16-le", errors="replace"
            ).upper()
            is_bt = (
                svc.casefold() == "bthusb"
                or "CLASS_E0&SUBCLASS_01&PROT_01" in compat
                or "BLUETOOTH" in name.upper()
            )
            if is_bt:
                found.append((pair[0], pair[1], svc, name))
        return found
    except Exception:
        return []


_INTEGRATED_BT_VIDS = frozenset({0x8087, 0x0489, 0x0E8D, 0x13D3, 0x04CA})


def detect_unbound_windows_bt_dongles(
    *,
    adapters_provider: Callable[[], list[tuple[int, int, str, str]]] | None = None,
    platform: str | None = None,
) -> list[tuple[int, int, str, str]]:
    """Return external USB Bluetooth dongles on Windows not yet bound to WinUSB.

    When only 1 ``BTHUSB`` adapter exists, it is assumed to be the PC's built-in
    Bluetooth and is NEVER suggested for Zadig replacement. When multiple
    ``BTHUSB`` adapters exist, integrated motherboard Wi-Fi/BT combo VIDs are
    filtered out whenever an external candidate is also present.
    """
    plat = sys.platform if platform is None else platform
    if plat != "win32":
        return []
    adapters = adapters_provider() if adapters_provider is not None else _windows_bt_adapters_by_service()
    bthusb_count = sum(1 for _, _, svc, _ in adapters if svc.casefold() == "bthusb")
    unbound: list[tuple[int, int, str, str]] = []
    for vid, pid, svc, name in adapters:
        low = svc.casefold()
        if low == "winusb":
            continue
        if low == "bthusb" and bthusb_count <= 1:
            # Single system Bluetooth adapter -> built-in radio, never touch!
            continue
        unbound.append((vid, pid, svc or "默认驱动", name))
    if len(unbound) > 1:
        external = [row for row in unbound if row[0] not in _INTEGRATED_BT_VIDS]
        if external:
            return external
    return unbound


def discover_usb_bluetooth_dongles(
    *,
    usb_devices_provider: Callable[[], Iterable[Any]] | None = None,
    winusb_pairs_provider: Callable[[], set[tuple[int, int]]] | None = None,
    platform: str | None = None,
) -> list[USBBTDongle]:
    """Discover any WinUSB-ready standard USB Bluetooth HCI adapter (no VID/PID lock)."""
    plat = sys.platform if platform is None else platform
    winusb_pairs: set[tuple[int, int]] | None = None
    if plat == "win32":
        winusb_pairs = (
            winusb_pairs_provider()
            if winusb_pairs_provider is not None
            else _windows_winusb_usb_pairs()
        )
        if not winusb_pairs:
            return []

    if usb_devices_provider is not None:
        raw_devices = list(usb_devices_provider() or ())
    else:
        try:
            import libusb_package
            import usb.core
            from usb.backend import libusb1

            backend = libusb1.get_backend(find_library=libusb_package.find_library)
            raw_devices = list(usb.core.find(find_all=True, backend=backend) or ())
        except Exception:
            return []

    candidates: list[tuple[int, int, str, str]] = []
    for dev in raw_devices:
        vendor = int(getattr(dev, "idVendor", 0) or 0)
        product = int(getattr(dev, "idProduct", 0) or 0)
        if not (0 < vendor <= 0xFFFF and 0 < product <= 0xFFFF):
            continue
        if plat == "darwin" and vendor == APPLE_VENDOR:
            continue
        if winusb_pairs is not None and (vendor, product) not in winusb_pairs:
            continue
        if not is_usb_bluetooth_hci(dev):
            continue
        serial = ""
        try:
            serial = str(getattr(dev, "serial_number", "") or "").strip()
        except Exception:
            serial = ""
        name = ""
        try:
            name = str(getattr(dev, "product", "") or "").strip()
        except Exception:
            name = ""
        candidates.append((vendor, product, serial, name or f"USB 蓝牙棒 {vendor:04X}:{product:04X}"))

    pair_counts: dict[tuple[int, int], int] = {}
    for vendor, product, _, _ in candidates:
        pair_counts[(vendor, product)] = pair_counts.get((vendor, product), 0) + 1

    dongles: list[USBBTDongle] = []
    for vendor, product, serial, name in candidates:
        if pair_counts[(vendor, product)] > 1 and serial:
            spec = f"usb:{vendor:04x}:{product:04x}/{serial}"
        else:
            spec = f"usb:{vendor:04x}:{product:04x}"
        dongles.append(USBBTDongle(vendor=vendor, product=product, serial=serial, name=name, transport_spec=spec))
    return dongles


def parse_adapter_spec(adapter: str, dongles: Iterable[USBBTDongle] = ()) -> str:
    """Resolve ``auto`` or any ``usb:<vid>:<pid>[/<serial>]`` Bluetooth HCI adapter spec."""
    text = (adapter or "auto").strip()
    if text.lower() == "auto":
        found = list(dongles) or discover_usb_bluetooth_dongles()
        if not found:
            raise ValueError("未发现已绑定 WinUSB 的外接 USB 蓝牙棒，请插好蓝牙棒并用 Zadig 绑定 WinUSB 后重试")
        if len(found) > 1:
            choices = "、".join(d.spec for d in found)
            raise ValueError(f"发现多只已绑定 WinUSB 的 USB 蓝牙棒（{choices}），请指定其中一只")
        return found[0].spec
    if not re.fullmatch(r"(?:usb|pyusb):[0-9a-fA-F]{4}:[0-9a-fA-F]{4}(?:/[A-Za-z0-9._-]+)?", text):
        raise ValueError("USB 蓝牙棒地址格式应为 auto 或 usb:<VID>:<PID>（例如 usb:0bda:8771）")
    return text


_RTK_DRIVERSTORE_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rtl8761b", ("rtl8761bu_fw.bin", "rtl8761b_fw.bin")),
    ("rtl8761a", ("rtl8761a_fw.bin",)),
    ("rtl8761c", ("rtl8761cu_fw.bin",)),
    ("rtl8723b", ("rtl8723b_fw.bin",)),
    ("rtl8821a", ("rtl8821a_fw.bin",)),
    ("rtl8821c", ("rtl8821c_fw.bin",)),
    ("rtl8822b", ("rtl8822b_fw.bin",)),
    ("rtl8852a", ("rtl8852a_fw.bin",)),
    ("rtl8852b", ("rtl8852b_fw.bin",)),
    ("rtl8852c", ("rtl8852c_fw.bin",)),
)


def ensure_dongle_firmware_staged(
    *,
    driver_store_roots: Iterable[Path] | None = None,
    dest_dir: Path | None = None,
    platform: str | None = None,
) -> list[Path]:
    """Stage BT dongle firmware already installed by Windows into Bumble's firmware dir.

    Some USB BT dongles (e.g. 0BDA:8771, Bumble's ``rtk`` driver) are RAM-based and need
    their firmware patch loaded after a USB reset. Windows Update already ships
    the signed firmware inside ``DriverStore\\FileRepository\\rtkfilter*``;
    copying it locally keeps 10RemoteHub 100% offline with zero third-party
    proprietary firmware redistributed.
    """
    plat = sys.platform if platform is None else platform
    if plat != "win32":
        return []
    try:
        if dest_dir is None:
            from bumble.drivers import rtk
            dest_dir = Path(rtk.rtk_firmware_dir())
        else:
            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []

    if driver_store_roots is None:
        sys_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        repo = sys_root / "System32" / "DriverStore" / "FileRepository"
        roots = [p for p in repo.glob("rtkfilter*") if p.is_dir()] if repo.is_dir() else []
    else:
        roots = [Path(p) for p in driver_store_roots if Path(p).is_dir()]

    staged: list[Path] = []
    for root in roots:
        for src in root.iterdir():
            if not src.is_file():
                continue
            low = src.name.lower()
            if "fw" not in low or not (low.endswith(".dll") or low.endswith(".bin")):
                continue
            targets = ()
            for prefix, names in _RTK_DRIVERSTORE_MAP:
                if low.startswith(prefix):
                    targets = names
                    break
            if not targets:
                continue
            try:
                head = src.read_bytes()[:16]
            except OSError:
                continue
            if not head.startswith(b"Realtech"):
                continue
            for name in targets:
                dst = dest_dir / name
                if not dst.is_file():
                    try:
                        dst.write_bytes(src.read_bytes())
                        staged.append(dst)
                    except OSError:
                        pass
    return staged


def bumble_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("bumble") is not None
    except Exception:
        return False


class BLEDongleStore:
    """Persist Bumble controller identity, LTK bonding keys, and paired BLE peers."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.device_config_path = self.directory / "device.json"
        self.keys_path = self.directory / "keys.json"
        self.peers_path = self.directory / "peers.json"
        self.log_path = self.directory / "session.log"

    def log(self, message: str) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {message}\n")
        except OSError:
            pass

    def ensure_device_config(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.device_config_path.exists():
            # Static random address fallback; real HCI Public address (AddressType.PUBLIC == 0)
            # is used for identity and connection so peers recognize our bond across reconnects.
            raw = bytearray(secrets.token_bytes(6))
            raw[0] |= 0xC0
            addr = ":".join(f"{b:02X}" for b in raw)
            cfg = {
                "name": "10RemoteHub",
                "address": addr,
                "identity_address_type": 0,
                "le_privacy_enabled": False,
                "irk": secrets.token_hex(16),
                "keystore": f"JsonKeyStore:{self.keys_path}",
            }
            atomic_write(self.device_config_path, json.dumps(cfg, ensure_ascii=False, indent=2))
        else:
            # Auto-migrate any older config where identity_address_type was missing or string "public".
            try:
                cfg = json.loads(self.device_config_path.read_text(encoding="utf-8"))
                if isinstance(cfg, dict) and (
                    cfg.get("identity_address_type") != 0 or cfg.get("le_privacy_enabled") is not False
                ):
                    cfg["identity_address_type"] = 0
                    cfg["le_privacy_enabled"] = False
                    atomic_write(self.device_config_path, json.dumps(cfg, ensure_ascii=False, indent=2))
            except (OSError, ValueError):
                pass
        return self.device_config_path

    def load_peers(self) -> dict[str, BLEPeerRecord]:
        if not self.peers_path.exists():
            return {}
        try:
            raw = json.loads(self.peers_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        peers: dict[str, BLEPeerRecord] = {}
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue
            addr = str(item.get("address") or key).strip().upper()
            if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", addr):
                continue
            rec = BLEPeerRecord(
                address=addr,
                name=str(item.get("name") or "BLE 遥控器"),
                vendor=int(item.get("vendor") or 0),
                product=int(item.get("product") or 0),
                vendor_id_source=item.get("vendor_id_source"),
                revision=item.get("revision"),
                firmware=str(item.get("firmware") or ""),
                voice_capable=bool(item.get("voice_capable", False)),
                voice_protocol=str(item.get("voice_protocol") or ""),
                gatt_summary=str(item.get("gatt_summary") or ""),
                paired=bool(item.get("paired", False)),
                address_type=str(item.get("address_type") or "public"),
            ).normalized()
            peers[rec.address] = rec
        return peers

    def save_peer(self, peer: BLEPeerRecord) -> BLEPeerRecord:
        self.directory.mkdir(parents=True, exist_ok=True)
        rec = peer.normalized()
        peers = self.load_peers()
        peers[rec.address] = rec
        payload = {addr: asdict(item) for addr, item in sorted(peers.items())}
        atomic_write(self.peers_path, json.dumps(payload, ensure_ascii=False, indent=2))
        return rec

    def forget(self, address: str) -> None:
        """Drop a peer and its bonding keys (keys.json nests peers under the local address)."""
        addr = address.split("/")[0].strip().upper()
        peers = self.load_peers()
        if peers.pop(addr, None) is not None:
            payload = {a: asdict(item) for a, item in sorted(peers.items())}
            atomic_write(self.peers_path, json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            keys = json.loads(self.keys_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(keys, dict):
            return
        changed = False
        for namespace in keys.values():
            if not isinstance(namespace, dict):
                continue
            for peer in [k for k in namespace if k.split("/")[0].strip().upper() == addr]:
                del namespace[peer]
                changed = True
        if changed:
            atomic_write(self.keys_path, json.dumps(keys, ensure_ascii=False, indent=4))

    def has_bond_for(self, address: str) -> bool:
        if not self.keys_path.exists():
            return False
        try:
            text = self.keys_path.read_text(encoding="utf-8").upper()
        except OSError:
            return False
        return address.strip().upper() in text


def remote_confidence(name: str, service_uuids: Iterable[str], appearance: int | None) -> int:
    """SURE when it advertises the HID service or calls itself a remote; MAYBE on weaker hints."""
    if any("1812" in str(u).lower() for u in (service_uuids or ())):
        return REMOTE_SURE
    low = (name or "").lower()
    if "\u9065\u63a7" in (name or "") or any(tok in low for tok in _STRONG_NAME_HINTS):
        return REMOTE_SURE
    if appearance in _REMOTE_APPEARANCES or any(tok in low for tok in _REMOTE_NAME_HINTS):
        return REMOTE_MAYBE
    return REMOTE_NO


def _advertisement_looks_like_remote(name: str, service_uuids: Iterable[str], appearance: int | None) -> bool:
    return remote_confidence(name, service_uuids, appearance) != REMOTE_NO


class BLEDongleReader:
    """``KeyReader`` backed by any WinUSB-bound USB Bluetooth HCI dongle + Bumble."""

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        dongle_provider: Callable[[], list[USBBTDongle]] | None = None,
        unbound_provider: Callable[[], list[tuple[int, int, str, str]]] | None = None,
        managed_provider: Callable[[], list[Any]] | None = None,
        scanner: Callable[[USBBTDongle, float], list[BLEPeerRecord]] | None = None,
        session_runner: Callable[..., None] | None = None,
        on_voice_event: Callable[[Any], None] | None = None,
        on_voice_ready_changed: Callable[[bool], None] | None = None,
        remote_mic_enabled: bool = False,
    ):
        base = Path(state_dir) if state_dir is not None else (Path.home() / ".10remotehub" / "ble-dongle")
        self.store = BLEDongleStore(base)
        self._dongle_provider = dongle_provider or discover_usb_bluetooth_dongles
        self._unbound_provider = unbound_provider or detect_unbound_windows_bt_dongles
        self._managed_provider = managed_provider
        self._scanner = scanner
        self._session_runner = session_runner
        self.on_voice_event = on_voice_event
        self.on_voice_ready_changed = on_voice_ready_changed

        self.take_over: bool = False
        self.take_over_triggers: tuple[str, ...] = ()
        self.seized: int = 0
        self.seize_error: str = ""
        self.scope: str = ""
        self.unlisted: list[Any] = []

        self._devices: dict[tuple[str, str], tuple[ProbeDevice, BLEPeerRecord]] = {}
        self._discovered_cache: dict[str, BLEPeerRecord] = {}
        # Ids the latest active scan heard advertising, in the order they were heard.
        self.last_scan_ids: list[tuple[str, str]] = []
        # The subset the scan is sure are remotes; only these are connected automatically.
        self.last_scan_sure: list[tuple[str, str]] = []
        self._scan_confidence: dict[str, int] = {}
        # (address, name) of the remote the user picked; a scan then hears nothing else.
        self._scan_target: tuple[str, str] | None = None
        # Peers that connected but had no HID service; later scans skip them.
        self._not_remotes: set[str] = set()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._halt_event: threading.Event = threading.Event()
        self._scan_cancel: threading.Event = threading.Event()
        self._async_stop: asyncio.Event | None = None
        self._active_take_over: bool = False
        self._active_peer: str = ""
        self._session_ready: bool = False
        self._remote_mic_enabled: bool = bool(remote_mic_enabled)
        self._active_voice_capable: bool = False
        self._active_voice_protocol: str = ""
        self._live_stream: Any = None
        self._live_atv_voice: ATVVVoiceSession | None = None
        self._stop_voice_event: asyncio.Event | None = None
        self._cb_on_key: Callable[[Any, str, bool], None] | None = None
        self._cb_on_removed: Callable[[], None] | None = None

    @property
    def is_voice_ready(self) -> bool:
        return bool(
            self._session_ready
            and self._active_take_over
            and self._active_voice_capable
            and self._remote_mic_enabled
        )

    def peer_voice_info_for_id(self, identity: Any) -> dict[str, Any]:
        """Return detected BLE voice protocol & GATT metadata for a ``('ble-dongle', MAC)`` id."""
        if not (isinstance(identity, tuple) and len(identity) >= 2 and identity[0] == "ble-dongle"):
            return {}
        addr = str(identity[1]).strip().upper()
        peer = self._merged_peers().get(addr)
        if peer is None:
            return {}
        proto = peer.voice_protocol or ("fiber0d" if peer.voice_capable else "")
        return {
            "protocol": proto,
            "protocol_label": VOICE_PROTOCOL_LABELS.get(proto, ""),
            "firmware": peer.firmware,
            "gatt_summary": peer.gatt_summary,
            "address": peer.address,
            "paired": peer.paired,
        }

    def peer_voice_info(self, remote: Any) -> dict[str, str]:
        """Return detected BLE voice protocol & GATT summary for ``remote``, if known."""
        if remote is None:
            return {}
        for peer in self._merged_peers().values():
            if remote.accepts(peer.to_probe_device()):
                proto = peer.voice_protocol or ("fiber0d" if peer.voice_capable else "")
                return {
                    "protocol": proto,
                    "protocol_label": VOICE_PROTOCOL_LABELS.get(proto, ""),
                    "firmware": peer.firmware,
                    "gatt_summary": peer.gatt_summary,
                    "address": peer.address,
                    "paired": peer.paired,
                }
        return {}

    def has_voice_capable_peer(self, remote: Any) -> bool:
        info = self.peer_voice_info(remote)
        return bool(info.get("protocol"))

    def _emit_voice_ready(self) -> None:
        cb = self.on_voice_ready_changed
        if cb is not None:
            with suppress(Exception):
                cb(self.is_voice_ready)

    def set_remote_mic_enabled(self, enabled: bool) -> None:
        """Toggle BLE remote microphone decoding live without reconnecting."""
        enabled = bool(enabled)
        if self._remote_mic_enabled == enabled:
            return
        self._remote_mic_enabled = enabled
        loop = self._loop
        stream = self._live_stream
        atv = self._live_atv_voice
        if loop is not None and (stream is not None or atv is not None) and self._active_take_over:
            def _apply():
                want = enabled and self.on_voice_event is not None
                if stream is not None:
                    ev = stream.set_use_remote_mic(want)
                    if self.on_voice_event is not None and ev.voice is not None:
                        self.on_voice_event(ev)
                if atv is not None:
                    ev2 = atv.set_use_remote_mic(want)
                    if self.on_voice_event is not None and ev2.voice is not None:
                        self.on_voice_event(ev2)
                self._emit_voice_ready()
            with suppress(Exception):
                loop.call_soon_threadsafe(_apply)
                return
        self._emit_voice_ready()

    def stop_remote_voice(self) -> None:
        """Ask the active BLE voice stream to send mic-off (0xFB=0x00) immediately."""
        loop = self._loop
        ev = self._stop_voice_event
        if loop is not None and ev is not None:
            with suppress(Exception):
                loop.call_soon_threadsafe(ev.set)

    def available_dongles(self) -> list[USBBTDongle]:
        try:
            return list(self._dongle_provider() or ())
        except Exception:
            return []

    def managed_dongles(self) -> list[Any]:
        """Return present external USB BT dongles (Windows bind/restore, macOS direct scan)."""
        try:
            if self._managed_provider is not None:
                return list(self._managed_provider() or ())
            from .win_usb_driver import ManagedUSBDevice, list_managed_devices

            if sys.platform == "win32":
                return list_managed_devices()[0]
            return [
                ManagedUSBDevice(
                    instance_id=f"usb:{d.vendor:04x}:{d.product:04x}:{d.bus}:{d.address}",
                    vendor=d.vendor,
                    product=d.product,
                    kind="ble_dongle",
                    service="libusb",
                    name=d.label,
                )
                for d in self.available_dongles()
            ]
        except Exception:
            return []

    def dongle_status_hint(self) -> str:
        """Plain-Chinese status for the Add Remote dialog when an external BT dongle is plugged in."""
        dongles = self.available_dongles()
        if dongles:
            ids = "、".join(f"{d.vendor:04X}:{d.product:04X}" for d in dongles)
            if not self._can_use_bumble():
                return f"已检测到外接蓝牙棒（{ids}），但当前安装包缺少 BLE 协议组件。"
            return "外接 BLE 专属蓝牙棒已就绪。把电视盒遥控器按进配对模式（灯快闪）后点上方「重新检测」即可发现。"
        managed = self.managed_dongles()
        if len(managed) == 1:
            dev = managed[0]
            if dev.bound_winusb:
                return "外接 BLE 专属蓝牙棒已就绪。把电视盒遥控器按进配对模式（灯快闪）后点上方「重新检测」即可发现。"
            return (
                f"检测到外接 USB 蓝牙棒（{dev.id_label}，不影响电脑自带蓝牙）。"
                "如需连接电视盒蓝牙遥控器，点下方「设为遥控器专属接收器」即可一键完成。"
            )
        if len(managed) > 1:
            summary = "、".join(d.id_label for d in managed[:3])
            return f"检测到 {len(managed)} 只外接 USB 蓝牙棒（{summary}），请只留一只外接蓝牙棒插在电脑上再一键切换。"
        try:
            unbound = list(self._unbound_provider() or ())
        except Exception:
            unbound = []
        if len(unbound) == 1:
            vid, pid, _svc, _name = unbound[0]
            return (
                f"检测到外接 USB 蓝牙棒（{vid:04X}:{pid:04X}，不影响电脑自带蓝牙）。"
                "如需连接电视盒蓝牙遥控器，点下方「设为遥控器专属接收器」或查看说明。"
            )
        if len(unbound) > 1:
            summary = "、".join(f"{vid:04X}:{pid:04X}" for vid, pid, _, _ in unbound[:3])
            return f"检测到 {len(unbound)} 只外接 USB 蓝牙棒（{summary}），请只留一只外接蓝牙棒插在电脑上再操作。"
        return ""

    def _can_use_bumble(self) -> bool:
        return self._scanner is not None or self._session_runner is not None or bumble_available()

    def _merged_peers(self) -> dict[str, BLEPeerRecord]:
        peers = dict(self.store.load_peers())
        for addr, rec in self._discovered_cache.items():
            if addr not in peers:
                peers[addr] = rec
            else:
                saved = peers[addr]
                if (saved.name in ("", "BLE 遥控器")) and rec.name:
                    peers[addr] = BLEPeerRecord(**{**asdict(saved), "name": rec.name}).normalized()
        return peers

    def _build_device_map(self, peers: dict[str, BLEPeerRecord]) -> list[ProbeDevice]:
        mapping: dict[tuple[str, str], tuple[ProbeDevice, BLEPeerRecord]] = {}
        listed: list[ProbeDevice] = []
        for addr in sorted(peers):
            rec = peers[addr].normalized()
            dev = rec.to_probe_device()
            mapping[dev.id] = (dev, rec)
            listed.append(dev)
        self._devices = mapping
        return listed

    def peek(self) -> list[ProbeDevice]:
        """Fast read-only snapshot without interrupting an active BLE connection."""
        if not self._can_use_bumble() or not self.available_dongles():
            return []
        return self._build_device_map(self._merged_peers())

    def has_matching_peer(self, remote: Any) -> bool:
        """True if a WinUSB BT dongle is plugged in and ``remote`` matches a known BLE peer."""
        if remote is None or not self._can_use_bumble() or not self.available_dongles():
            return False
        for dev in self.peek():
            if remote.accepts(dev):
                return True
        return False

    def cancel_scan(self) -> None:
        """Signal an in-progress active BLE scan to stop immediately."""
        self._scan_cancel.set()

    def _is_target(self, address: str, name: str) -> bool:
        if self._scan_target is None:
            return True
        want_address, want_name = self._scan_target
        if want_address and address.split("/")[0].strip().upper() == want_address:
            return True
        return bool(want_name and name and name.strip() == want_name.strip())

    def scan_dongles(self, duration: float = _DISCOVERY_TIMEOUT_S, target: Any = None) -> list[ProbeDevice]:
        """Actively scan for advertising BLE remotes over the USB BT dongle.

        ``target`` (a blocked remote the user picked: ``address`` / ``name``) limits the
        scan to that remote: it ends as soon as it is heard and nothing else is offered.
        """
        address = str(getattr(target, "address", "") or "").strip().upper()
        name = str(getattr(target, "name", "") or "").strip()
        self._scan_target = (address, name) if target is not None and (address or name) else None
        self._scan_cancel.clear()
        self.last_scan_ids = []
        self.last_scan_sure = []
        self._scan_confidence = {}
        self.stop()
        if not self._can_use_bumble():
            self._devices = {}
            return []
        dongles = self.available_dongles()
        if not dongles:
            self._devices = {}
            return []
        try:
            scanned = (
                self._scanner(dongles[0], duration)
                if self._scanner is not None
                else self._scan_with_bumble(dongles[0], duration)
            )
        except Exception as exc:
            self.store.log(f"scan error: {type(exc).__name__}: {exc}")
            scanned = []
        heard = []
        for rec in scanned:
            norm = rec.normalized()
            if norm.address in self._not_remotes or not self._is_target(norm.address, norm.name):
                continue
            self._discovered_cache[norm.address] = norm
            # An injected test scanner reports remotes only.
            heard.append((self._scan_confidence.get(norm.address, REMOTE_SURE), norm.address))
        heard.sort(key=lambda item: -item[0])              # stable: first heard first within a level
        self.last_scan_ids = [("ble-dongle", addr) for _level, addr in heard]
        self.last_scan_sure = [("ble-dongle", addr) for level, addr in heard if level == REMOTE_SURE]
        return self._build_device_map(self._merged_peers())

    async def _request_voice_interval(self, connection: Any) -> None:
        """Ask the remote for a fast link while it streams voice, unless it already is."""
        params = getattr(connection, "parameters", None)
        interval = getattr(params, "connection_interval", None)
        if interval is not None and interval <= _VOICE_INTERVAL_MS[1] and not getattr(params, "peripheral_latency", 0):
            return
        try:
            await asyncio.wait_for(
                connection.update_parameters(_VOICE_INTERVAL_MS[0], _VOICE_INTERVAL_MS[1], 0, _VOICE_SUPERVISION_MS),
                _GATT_TIMEOUT_S,
            )
        except Exception as exc:  # an old remote may refuse; voice still works, only slower
            self.store.log(f"voice interval request failed: {type(exc).__name__}: {exc}")

    def forget_peer(self, address: str) -> None:
        addr = address.split("/")[0].strip().upper()
        self._not_remotes.add(addr)
        self._discovered_cache.pop(addr, None)
        self._devices.pop(("ble-dongle", addr), None)
        self.last_scan_ids = [i for i in self.last_scan_ids if i != ("ble-dongle", addr)]
        self.last_scan_sure = [i for i in self.last_scan_sure if i != ("ble-dongle", addr)]
        self.store.forget(addr)

    def enumerate(self, *, active_scan: bool | None = None) -> list[ProbeDevice]:
        """Return known/paired BLE peers immediately without blocking UI on radio scan.

        Active BLE radio scanning runs only when explicitly triggered via
        ``scan_dongles()`` (or when a custom test ``scanner`` is injected).
        Preserves any already-live BLE session so opening photo learning or
        refreshing the picker does not tear down an active connection.
        """
        should_scan = (self._scanner is not None) if active_scan is None else bool(active_scan)
        if should_scan:
            return self.scan_dongles()
        if not self._can_use_bumble():
            self._devices = {}
            return []
        dongles = self.available_dongles()
        if not dongles:
            self.stop()
            self._devices = {}
            return []
        return self._build_device_map(self._merged_peers())

    def pause(self) -> None:
        """Detach key callbacks in 0ms while keeping the BLE connection alive."""
        self._cb_on_key = None
        self._cb_on_removed = None

    def is_connected_to(self, identity: Any) -> bool:
        """True when a live BLE session is already active for ``identity``."""
        found = self._devices.get(identity)
        if found is None:
            return False
        return bool(
            self._thread is not None
            and self._thread.is_alive()
            and not self._halt_event.is_set()
            and self._session_ready
            and self._active_peer == found[1].address
            and self._active_take_over == bool(self.take_over)
        )

    def start(
        self,
        identity: Any,
        on_key: Callable[[Any, str, bool], None],
        on_removed: Callable[[], None],
        *,
        expected: ProbeDevice | None = None,
    ) -> tuple[int, int]:
        taking_over = bool(self.take_over)
        triggers = tuple(self.take_over_triggers)
        if not self._can_use_bumble():
            raise RuntimeError("当前环境未安装 Bumble BLE 协议栈")
        dongles = self.available_dongles()
        if not dongles:
            raise RuntimeError("未发现已绑定 WinUSB 的外接 USB 蓝牙棒")
        if identity not in self._devices:
            self._build_device_map(self._merged_peers())
        found = self._devices.get(identity)
        if found is None or (expected is not None and found[0].id != expected.id):
            raise RuntimeError("BLE 遥控器不在当前列表中，请重新检测")
        _dev, peer = found
        dongle = dongles[0]

        # Fast-path: reuse already-connected live BLE session in 0ms (no disconnect / re-pair!).
        if (
            self._thread is not None
            and self._thread.is_alive()
            and not self._halt_event.is_set()
            and self._session_ready
            and self._active_peer == peer.address
            and self._active_take_over == taking_over
        ):
            self._cb_on_key = on_key
            self._cb_on_removed = on_removed
            self.seized = max(1, len(triggers)) if taking_over else 0
            self.seize_error = ""
            self.store.log(f"reuse live session: peer={peer.bumble_address}")
            return 1, 1

        self.stop()
        self._halt_event.clear()
        self._active_take_over = taking_over
        self._active_peer = peer.address
        self._session_ready = False
        self._cb_on_key = on_key
        self._cb_on_removed = on_removed
        self.seized = max(1, len(triggers)) if taking_over else 0
        self.seize_error = ""
        self.scope = f"BLE 专属蓝牙棒（{dongle.vendor:04X}:{dongle.product:04X}）"

        def dispatch_key(channel: Any, trigger: str, down: bool) -> None:
            cb = self._cb_on_key
            if cb is not None:
                cb(channel, trigger, down)

        def dispatch_removed() -> None:
            self._session_ready = False
            cb = self._cb_on_removed
            if cb is not None:
                cb()

        ready_event = threading.Event()
        startup_error: list[str] = []

        def worker():
            try:
                if self._session_runner is not None:
                    self._session_runner(
                        dongle=dongle,
                        peer=peer,
                        taking_over=taking_over,
                        on_key=dispatch_key,
                        on_removed=dispatch_removed,
                        halt=self._halt_event,
                        ready=ready_event,
                        startup_error=startup_error,
                    )
                else:
                    self._run_bumble_session(
                        dongle=dongle,
                        peer=peer,
                        taking_over=taking_over,
                        on_key=dispatch_key,
                        on_removed=dispatch_removed,
                        ready=ready_event,
                        startup_error=startup_error,
                    )
            except Exception as exc:
                self._session_ready = False
                if not ready_event.is_set():
                    startup_error.append(str(exc) or type(exc).__name__)
                    ready_event.set()
                elif not self._halt_event.is_set():
                    dispatch_removed()

        self._thread = threading.Thread(target=worker, name="remotehub-ble-dongle", daemon=True)
        self._thread.start()
        if not taking_over:
            if not ready_event.wait(timeout=_CONNECT_TIMEOUT_S + 5.0):
                self.stop()
                raise RuntimeError("连接 BLE 遥控器超时，请确认遥控器已唤醒或处于配对模式")
            if startup_error:
                err = startup_error[0]
                self.stop()
                raise RuntimeError(err)
            self._session_ready = True
        return 1, 1

    def sync_takeover(self, triggers: Iterable[str]) -> bool:
        if not self._active_take_over or self._thread is None or not self._thread.is_alive():
            return False
        new_seized = max(1, len(tuple(triggers)))
        if new_seized != self.seized:
            self.seized = new_seized
            return True
        return False

    def ensure_takeover(self) -> bool:
        if not self._active_take_over:
            return False
        if self._thread is not None and self._thread.is_alive():
            return False
        if self.seized or not self.seize_error:
            self.seized = 0
            self.seize_error = "BLE 专属蓝牙棒连接线程已停止"
            return True
        return False

    def stop(self) -> None:
        self._halt_event.set()
        self._session_ready = False
        self._cb_on_key = None
        self._cb_on_removed = None
        loop = self._loop
        async_stop = self._async_stop
        if loop is not None and async_stop is not None:
            with suppress(Exception):
                loop.call_soon_threadsafe(async_stop.set)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.2)
        self._thread = None
        self._loop = None
        self._async_stop = None
        self._active_take_over = False
        self._active_peer = ""
        self.seized = 0
        self.seize_error = ""
        self.scope = ""

    close = stop

    # -- Bumble live BLE scanning & GATT session ------------------------------

    def _reset_usb_dongle(self, dongle: USBBTDongle) -> bool:
        """Issue a libusb port reset to recover a stalled WinUSB HCI interrupt endpoint."""
        try:
            import usb.core
        except ImportError:
            return False
        backend = None
        with suppress(Exception):
            import libusb_package

            backend = libusb_package.get_libusb1_backend()
        try:
            dev = usb.core.find(idVendor=dongle.vendor, idProduct=dongle.product, backend=backend)
            if dev is None:
                return False
            dev.reset()
            return True
        except Exception as exc:
            self.store.log(f"usb reset failed ({dongle.spec}): {type(exc).__name__}: {exc}")
            return False

    async def _open_powered_device(self, dongle: USBBTDongle, cfg_path: Path):
        """Open Bumble USB transport and power on HCI device with automatic USB-reset retry."""
        from bumble.device import Device
        from bumble.pairing import PairingConfig
        from bumble.transport import open_transport

        last_exc: Exception | None = None
        for attempt in range(2):
            transport = await open_transport(dongle.spec)
            try:
                device = Device.from_config_file_with_hci(str(cfg_path), transport.source, transport.sink)
                device.config.identity_address_type = PairingConfig.AddressType.PUBLIC
                device.config.le_privacy_enabled = False
                timeout = 4.5 if attempt == 0 else _GATT_TIMEOUT_S
                await asyncio.wait_for(device.power_on(), timeout)
                return transport, device
            except Exception as exc:
                last_exc = exc
                with suppress(Exception):
                    await transport.close()
                if attempt == 0 and isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                    self.store.log(f"power_on({dongle.spec}) timed out, resetting USB dongle and retrying")
                    self._reset_usb_dongle(dongle)
                    await asyncio.sleep(0.4)
                    continue
                raise
        raise last_exc or RuntimeError("无法初始化外接蓝牙棒")

    def _scan_with_bumble(self, dongle: USBBTDongle, duration: float) -> list[BLEPeerRecord]:
        async def _scan() -> list[BLEPeerRecord]:
            from bumble.core import AdvertisingData

            ensure_dongle_firmware_staged()
            cfg_path = self.store.ensure_device_config()
            found: dict[str, BLEPeerRecord] = {}
            merged: dict[str, dict[str, Any]] = {}
            first_found_at: float | None = None
            confidence: dict[str, int] = {}
            transport, device = await self._open_powered_device(dongle, cfg_path)
            try:
                def _extract_uuids(val: Any) -> list[str]:
                    if not val:
                        return []
                    if isinstance(val, (bytes, bytearray)):
                        return [
                            f"{int.from_bytes(val[i:i+2], 'little'):04x}"
                            for i in range(0, len(val) - 1, 2)
                        ]
                    return [str(u) for u in val]

                def on_adv(advertisement):
                    nonlocal first_found_at
                    raw_addr = str(advertisement.address).strip().upper()
                    addr = raw_addr.split("/")[0].strip()
                    is_public = getattr(advertisement.address, "is_public", None)
                    if is_public is None:
                        is_public = "/P" in raw_addr
                    addr_type = "public" if is_public else "random"
                    data = advertisement.data
                    name = (
                        data.get(AdvertisingData.COMPLETE_LOCAL_NAME)
                        or data.get(AdvertisingData.SHORTENED_LOCAL_NAME)
                        or ""
                    )
                    if isinstance(name, (bytes, bytearray)):
                        name = name.decode("utf-8", errors="ignore").strip("\x00 ")
                    uuids: list[str] = []
                    for key in (
                        AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
                        AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
                    ):
                        uuids.extend(_extract_uuids(data.get(key)))
                    appearance = data.get(AdvertisingData.APPEARANCE)
                    if isinstance(appearance, (bytes, bytearray)) and len(appearance) >= 2:
                        appearance = int.from_bytes(appearance[:2], "little")
                    # Merge ADV_IND + SCAN_RSP packets for the same address.
                    entry = merged.setdefault(
                        addr, {"name": "", "uuids": set(), "appearance": None, "address_type": addr_type}
                    )
                    entry["address_type"] = addr_type
                    if name:
                        entry["name"] = str(name)
                    entry["uuids"].update(uuids)
                    if isinstance(appearance, int):
                        entry["appearance"] = appearance
                    if addr in self._not_remotes:
                        return
                    if self._scan_target is not None:
                        # Looking for the remote the user picked: only it counts, and it is sure.
                        if not self._is_target(addr, entry["name"]):
                            return
                        level = REMOTE_SURE
                    else:
                        level = remote_confidence(entry["name"], entry["uuids"], entry["appearance"])
                    if level != REMOTE_NO:
                        prev = found.get(addr)
                        found[addr] = BLEPeerRecord(
                            address=addr,
                            name=entry["name"] or (prev.name if prev else "BLE 遥控器"),
                            address_type=entry["address_type"],
                        ).normalized()
                        confidence[addr] = max(level, confidence.get(addr, REMOTE_NO))
                        # Stop early only for a sure remote; a maybe keeps the radio listening.
                        if level == REMOTE_SURE and first_found_at is None:
                            first_found_at = time.monotonic()

                device.on("advertisement", on_adv)
                await device.start_scanning(active=True)
                try:
                    deadline = time.monotonic() + max(0.5, float(duration))
                    while time.monotonic() < deadline:
                        if self._scan_cancel.is_set():
                            break
                        if first_found_at is not None and (time.monotonic() - first_found_at) >= 0.65:
                            break
                        await asyncio.sleep(0.12)
                finally:
                    with suppress(Exception):
                        await device.stop_scanning()
            finally:
                with suppress(Exception):
                    await transport.close()
            self.store.log(
                f"scan({dongle.spec}) finished: found={[(r.bumble_address, r.name) for r in found.values()]}"
            )
            for addr in found:
                entry = merged[addr]
                self.store.log(
                    f"  heard {addr}: confidence={confidence[addr]} uuids={sorted(entry['uuids'])} "
                    f"appearance={entry['appearance']}"
                )
            self._scan_confidence = confidence
            return list(found.values())

        return asyncio.run(_scan())

    def _run_bumble_session(
        self,
        *,
        dongle: USBBTDongle,
        peer: BLEPeerRecord,
        taking_over: bool,
        on_key: Callable[[Any, str, bool], None],
        on_removed: Callable[[], None],
        ready: threading.Event,
        startup_error: list[str],
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        async_stop = asyncio.Event()
        self._async_stop = async_stop
        try:
            loop.run_until_complete(
                self._bumble_loop(
                    dongle=dongle,
                    peer=peer,
                    taking_over=taking_over,
                    on_key=on_key,
                    on_removed=on_removed,
                    ready=ready,
                    startup_error=startup_error,
                    async_stop=async_stop,
                )
            )
        finally:
            self._loop = None
            self._async_stop = None
            with suppress(Exception):
                loop.close()

    async def _bumble_loop(
        self,
        *,
        dongle: USBBTDongle,
        peer: BLEPeerRecord,
        taking_over: bool,
        on_key: Callable[[Any, str, bool], None],
        on_removed: Callable[[], None],
        ready: threading.Event,
        startup_error: list[str],
        async_stop: asyncio.Event,
    ) -> None:
        from bumble.core import UUID
        from bumble.device import Peer

        ensure_dongle_firmware_staged()
        cfg_path = self.store.ensure_device_config()
        backoff = 0.6
        while not self._halt_event.is_set() and not async_stop.is_set():
            transport = None
            try:
                transport, device = await self._open_powered_device(dongle, cfg_path)
                try:
                    while not self._halt_event.is_set() and not async_stop.is_set():
                        try:
                            connected = await self._connect_and_stream_once(
                                device=device,
                                peer=peer,
                                taking_over=taking_over,
                                uuid_fn=UUID.from_16_bits,
                                peer_cls=Peer,
                                on_key=on_key,
                                ready=ready,
                                async_stop=async_stop,
                            )
                        except (TimeoutError, asyncio.TimeoutError) as timeout_exc:
                            if not taking_over:
                                raise
                            # Remote is likely asleep; keep USB dongle open and keep listening for wakeup.
                            connected = False
                            if not ready.is_set():
                                self.store.log(f"connect waiting (remote asleep): {timeout_exc or 'TimeoutError'}")
                        if not taking_over:
                            if not self._halt_event.is_set():
                                on_removed()
                            return
                        backoff = 0.6 if connected else min(2.0, backoff * 1.4)
                        with suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(async_stop.wait(), timeout=backoff)
                finally:
                    with suppress(Exception):
                        await transport.close()
            except Exception as exc:
                self.store.log(f"session error: {type(exc).__name__}: {exc}")
                if isinstance(exc, NotARemoteError):
                    self.forget_peer(peer.address)
                    self.store.log(f"forgot non-remote peer: {peer.bumble_address}")
                    if not ready.is_set():
                        startup_error.append(
                            f"「{peer.name}」不是遥控器（没有按键服务），已从列表移除。"
                            "请把遥控器按进配对模式（灯快闪）后重新扫描。"
                        )
                        ready.set()
                    elif not self._halt_event.is_set():
                        on_removed()
                    return
                if not taking_over:
                    if not ready.is_set():
                        startup_error.append(f"无法连接 BLE 遥控器：{exc or type(exc).__name__}")
                        ready.set()
                    elif not self._halt_event.is_set():
                        on_removed()
                    return
                # In live Runner mode (taking_over=True), never die permanently on transient startup
                # errors (such as USB dongle handoff from DeviceProbeDialog or temporary HCI hiccup).
                if not self.available_dongles():
                    if not self._halt_event.is_set():
                        on_removed()
                    return
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(async_stop.wait(), timeout=min(3.0, backoff))
                backoff = min(3.0, backoff * 1.5)

    async def _connect_and_stream_once(
        self,
        *,
        device: Any,
        peer: BLEPeerRecord,
        taking_over: bool = False,
        uuid_fn: Callable[[int], Any],
        peer_cls: Any,
        on_key: Callable[[Any, str, bool], None],
        ready: threading.Event,
        async_stop: asyncio.Event,
    ) -> bool:
        from bumble.device import ConnectionParametersPreferences
        from bumble.hci import HCI_LE_1M_PHY, Address, OwnAddressType

        target_addr = peer.bumble_address
        pub_addr = getattr(device, "public_address", None)
        use_public = pub_addr is not None and pub_addr != Address.ANY
        own_type = OwnAddressType.PUBLIC if use_public else OwnAddressType.RANDOM
        self.store.log(f"connect start: target={target_addr} own_type={own_type.name} name={peer.name!r}")
        fast = ConnectionParametersPreferences(
            connection_interval_min=_VOICE_INTERVAL_MS[0], connection_interval_max=_VOICE_INTERVAL_MS[1],
            max_latency=0, supervision_timeout=_VOICE_SUPERVISION_MS,
        )
        connection = await asyncio.wait_for(
            device.connect(target_addr, own_address_type=own_type,
                           connection_parameters_preferences={HCI_LE_1M_PHY: fast}),
            _CONNECT_TIMEOUT_S,
        )
        self.store.log(f"connected: target={target_addr} handle={getattr(connection, 'handle', '?')} "
                       f"{_link_text(connection)}")
        connection.on("connection_parameters_update", lambda *_: self.store.log(f"link update: {_link_text(connection)}"))
        lost = asyncio.Event()
        connection.on("disconnection", lambda *_: lost.set())
        stream = None
        atv_voice = None
        try:
            bonded = self.store.has_bond_for(peer.address)
            self.store.log(f"security start: bonded={bonded}")
            if bonded:
                try:
                    await asyncio.wait_for(connection.encrypt(), _GATT_TIMEOUT_S)
                except Exception as enc_exc:
                    self.store.log(f"encrypt failed ({enc_exc}), retrying pair()")
                    await asyncio.wait_for(connection.pair(), 20.0)
                    if not connection.is_encrypted:
                        await asyncio.wait_for(connection.encrypt(), _GATT_TIMEOUT_S)
            else:
                await asyncio.wait_for(connection.pair(), 20.0)
                if not connection.is_encrypted:
                    await asyncio.wait_for(connection.encrypt(), _GATT_TIMEOUT_S)
            self.store.log(f"security ok: is_encrypted={getattr(connection, 'is_encrypted', '?')}")

            gatt = peer_cls(connection)
            all_services: list[Any] = []
            with suppress(Exception):
                all_services = list(await asyncio.wait_for(gatt.discover_services(), _GATT_TIMEOUT_S) or ())
            svc_uuids = [normalize_uuid(getattr(s, "uuid", "")) for s in all_services]

            firmware = ""
            vsrc, vendor, product, revision = peer.vendor_id_source, peer.vendor, peer.product, peer.revision
            with suppress(Exception):
                dis_svcs = [s for s in all_services if s.uuid == uuid_fn(0x180A)]
                if not dis_svcs:
                    dis_svcs = await asyncio.wait_for(gatt.discover_service(uuid_fn(0x180A)), _GATT_TIMEOUT_S)
                if dis_svcs:
                    await asyncio.wait_for(dis_svcs[0].discover_characteristics(), _GATT_TIMEOUT_S)
                    for ch in dis_svcs[0].characteristics:
                        if ch.uuid == uuid_fn(0x2A28):
                            raw_fw = await asyncio.wait_for(ch.read_value(), _GATT_TIMEOUT_S)
                            firmware = raw_fw.rstrip(b"\x00").decode("ascii", errors="ignore").strip()
                        elif ch.uuid == uuid_fn(0x2A50):
                            raw_pnp = await asyncio.wait_for(ch.read_value(), _GATT_TIMEOUT_S)
                            psrc, pvid, ppid, prev = parse_pnp_id(bytes(raw_pnp))
                            if pvid and ppid:
                                vsrc, vendor, product, revision = psrc, pvid, ppid, prev

            hid_svcs = [s for s in all_services if s.uuid == uuid_fn(0x1812)]
            if not hid_svcs:
                hid_svcs = await asyncio.wait_for(gatt.discover_service(uuid_fn(0x1812)), _GATT_TIMEOUT_S)
            if not hid_svcs:
                raise NotARemoteError("这只 BLE 设备未暴露 0x1812 HID 服务")
            hid_svc = hid_svcs[0]
            await asyncio.wait_for(hid_svc.discover_characteristics(), _GATT_TIMEOUT_S)
            report_map_char = next((c for c in hid_svc.characteristics if c.uuid == uuid_fn(0x2A4B)), None)
            if report_map_char is None:
                raise RuntimeError("这只 BLE 设备缺少 0x2A4B HID 报告描述符")
            report_map = bytes(await asyncio.wait_for(report_map_char.read_value(), _GATT_TIMEOUT_S))

            reports: dict[tuple[int, int], Any] = {}
            for ch in hid_svc.characteristics:
                if ch.uuid != uuid_fn(0x2A4D):
                    continue
                descs = await asyncio.wait_for(ch.discover_descriptors(), _GATT_TIMEOUT_S)
                ref_desc = next((d for d in descs if d.type == uuid_fn(0x2908)), None)
                if ref_desc is None:
                    continue
                ref = tuple(await asyncio.wait_for(ref_desc.read_value(), _GATT_TIMEOUT_S))
                if len(ref) == 2:
                    reports[(int(ref[0]), int(ref[1]))] = ch

            # Detect built-in voice protocol: FIBER0D (G.722.1) or standard Android TV Voice (AB5E0001).
            fiber0d = is_verified_fiber0d(firmware, report_map)
            atv_tx = atv_rx = atv_ctl = None
            atv_voice: ATVVVoiceSession | None = None
            if not fiber0d:
                atv_svc = next((s for s in all_services if normalize_uuid(getattr(s, "uuid", "")) == ATV_VOICE_SERVICE), None)
                if atv_svc is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(atv_svc.discover_characteristics(), _GATT_TIMEOUT_S)
                        for ch in atv_svc.characteristics:
                            u = normalize_uuid(getattr(ch, "uuid", ""))
                            if u == ATV_VOICE_TX:
                                atv_tx = ch
                            elif u == ATV_VOICE_RX:
                                atv_rx = ch
                            elif u == ATV_VOICE_CTL:
                                atv_ctl = ch

            voice_protocol = "fiber0d" if fiber0d else ("atv_voice" if (atv_tx and atv_rx and atv_ctl) else "")
            use_mic = bool(voice_protocol and taking_over and self.on_voice_event is not None and self._remote_mic_enabled)
            stream = (
                BLERemoteReports(firmware, report_map, use_remote_mic=bool(fiber0d and use_mic))
                if fiber0d
                else GenericBLERemoteReports(report_map)
            )
            if voice_protocol == "atv_voice":
                atv_voice = ATVVVoiceSession(use_remote_mic=use_mic)

            self._live_stream = stream
            self._live_atv_voice = atv_voice
            self._active_voice_capable = bool(voice_protocol)
            self._active_voice_protocol = voice_protocol
            stop_voice_ev = asyncio.Event()
            self._stop_voice_event = stop_voice_ev

            in_reps = ",".join(f"{rid:x}" for (rid, k) in sorted(reports) if k == 1)
            out_reps = ",".join(f"{rid:x}" for (rid, k) in sorted(reports) if k == 2)
            short_svcs = ",".join(u.split("-")[0] for u in svc_uuids[:12])
            gatt_summary = f"services={short_svcs};reports=in:{in_reps}|out:{out_reps};rmap={sha256(report_map).hexdigest()[:12]}"

            self.store.save_peer(
                BLEPeerRecord(
                    address=peer.address,
                    name=peer.name,
                    vendor=vendor,
                    product=product,
                    vendor_id_source=vsrc,
                    revision=revision,
                    firmware=firmware,
                    voice_capable=bool(voice_protocol),
                    voice_protocol=voice_protocol,
                    gatt_summary=gatt_summary,
                    paired=True,
                    address_type=peer.address_type,
                )
            )
            self._build_device_map(self._merged_peers())
            self.store.log(
                f"gatt ok: peer={peer.bumble_address} vid={vendor:04x} pid={product:04x} fw={firmware!r} proto={voice_protocol or 'none'} mic={use_mic}"
            )

            queue: asyncio.Queue[tuple[str, int, bytes]] = asyncio.Queue(maxsize=512)
            for (rep_id, kind), ch in reports.items():
                if kind == 1:  # Input Report
                    def _make_cb(rid: int):
                        def _cb(data: bytes):
                            if not lost.is_set() and not async_stop.is_set():
                                with suppress(asyncio.QueueFull):
                                    queue.put_nowait(("hid", rid, bytes(data)))
                        return _cb
                    await asyncio.wait_for(ch.subscribe(_make_cb(rep_id)), _GATT_TIMEOUT_S)

            if atv_voice is not None and atv_tx is not None and atv_rx is not None and atv_ctl is not None:
                def _on_atv_rx(data: bytes):
                    if not lost.is_set() and not async_stop.is_set():
                        with suppress(asyncio.QueueFull):
                            queue.put_nowait(("atv_rx", 0, bytes(data)))

                def _on_atv_ctl(data: bytes):
                    if not lost.is_set() and not async_stop.is_set():
                        with suppress(asyncio.QueueFull):
                            queue.put_nowait(("atv_ctl", 0, bytes(data)))

                await asyncio.wait_for(atv_ctl.subscribe(_on_atv_ctl), _GATT_TIMEOUT_S)
                await asyncio.wait_for(atv_rx.subscribe(_on_atv_rx), _GATT_TIMEOUT_S)
                with suppress(Exception):
                    await asyncio.wait_for(atv_tx.write_value(atv_voice.caps_command(), with_response=False), _GATT_TIMEOUT_S)

            output_voice = reports.get((VOICE_COMMAND_REPORT, 2))
            self._session_ready = True
            ready.set()
            self._emit_voice_ready()

            async def consume():
                clock = asyncio.get_running_loop().time
                deadline = None
                pcm_frames = 0
                timing: dict[str, float] = {}
                while not lost.is_set() and not async_stop.is_set():
                    if stop_voice_ev.is_set():
                        stop_voice_ev.clear()
                        if atv_voice is not None and atv_voice.active:
                            event = atv_voice.stop_voice()
                            if event.command and atv_tx is not None:
                                with suppress(Exception):
                                    await asyncio.wait_for(atv_tx.write_value(event.command, with_response=False), _GATT_TIMEOUT_S)
                        else:
                            event = stream.stop_voice()
                    else:
                        remaining = None if deadline is None else max(0.0, deadline - clock())
                        if remaining == 0.0:
                            if atv_voice is not None and atv_voice.active:
                                event = atv_voice.stop_voice()
                                if event.command and atv_tx is not None:
                                    with suppress(Exception):
                                        await asyncio.wait_for(atv_tx.write_value(event.command, with_response=False), _GATT_TIMEOUT_S)
                            else:
                                event = stream.finish_voice() if stream.ending else stream.stop_voice()
                        else:
                            try:
                                src_kind, rep_id, data = await asyncio.wait_for(
                                    queue.get(), timeout=min(0.15, remaining) if remaining is not None else 0.15
                                )
                            except asyncio.TimeoutError:
                                continue
                            if src_kind == "atv_ctl" and atv_voice is not None:
                                event = atv_voice.on_ctl(data)
                                if event.command and atv_tx is not None:
                                    self.store.log(f"atv voice cmd: 0x{event.command.hex()}")
                                    with suppress(Exception):
                                        await asyncio.wait_for(atv_tx.write_value(event.command, with_response=False), _GATT_TIMEOUT_S)
                                    deadline = clock() + _MAX_VOICE_SECONDS
                            elif src_kind == "atv_rx" and atv_voice is not None:
                                event = atv_voice.on_rx(data)
                            else:
                                if stream.ending and stream.voice_key(rep_id, data) is True:
                                    ev_done = stream.finish_voice()
                                    if self.on_voice_event is not None and (ev_done.voice is not None or ev_done.pcm):
                                        self.on_voice_event(ev_done)
                                event = stream.feed(rep_id, data)
                    if atv_voice is None and event.command and output_voice is not None:
                        self.store.log(f"voice cmd: 0x{event.command.hex()} {_link_text(connection)}")
                        await asyncio.wait_for(output_voice.write_value(event.command, with_response=True), _GATT_TIMEOUT_S)
                        deadline = clock() + (VOICE_TAIL_SECONDS if stream.ending else _MAX_VOICE_SECONDS)
                        if stream.ending:
                            timing["stop"] = clock()
                    if event.voice is True:
                        pcm_frames = 0
                        timing = {"start": clock()}
                        asyncio.get_running_loop().create_task(self._request_voice_interval(connection))
                    if event.pcm:
                        pcm_frames += 1
                        timing.setdefault("first", clock())
                        timing["last"] = clock()
                    if event.voice is False:
                        deadline = None
                        self.store.log(f"voice finished: proto={voice_protocol or 'hid'} pcm_frames={pcm_frames} "
                                       f"{_voice_timing_text(timing, pcm_frames)}")
                    for trigger, down in event.keys:
                        on_key(("ble-dongle", peer.address), trigger, down)
                    if self.on_voice_event is not None and (event.voice is not None or event.pcm):
                        self.on_voice_event(event)

            consumer = asyncio.create_task(consume())
            stop_wait = asyncio.create_task(async_stop.wait())
            lost_wait = asyncio.create_task(lost.wait())
            try:
                await asyncio.wait((consumer, stop_wait, lost_wait), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (consumer, stop_wait, lost_wait):
                    task.cancel()
                await asyncio.gather(consumer, stop_wait, lost_wait, return_exceptions=True)
            return True
        finally:
            self._session_ready = False
            self._live_stream = None
            self._live_atv_voice = None
            self._active_voice_capable = False
            self._active_voice_protocol = ""
            self._stop_voice_event = None
            self._emit_voice_ready()
            if stream is not None:
                ev = stream.disconnect()
                for trigger, down in ev.keys:
                    on_key(("ble-dongle", peer.address), trigger, down)
                if self.on_voice_event is not None and ev.voice is not None:
                    self.on_voice_event(ev)
            if atv_voice is not None:
                ev2 = atv_voice.disconnect()
                if self.on_voice_event is not None and ev2.voice is not None:
                    self.on_voice_event(ev2)
            if not lost.is_set():
                with suppress(Exception):
                    await asyncio.wait_for(connection.disconnect(), 2.0)
