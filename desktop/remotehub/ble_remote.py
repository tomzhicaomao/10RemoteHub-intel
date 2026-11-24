"""Streaming report handling for BLE HOGP (0x1812) remotes, including verified FIBER0D voice."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import struct
import zlib

from .codec.g7221 import Decoder
from .hid_reports import HIDReports


VERIFIED_FIRMWARE = "FIBER0D_U_R01_592_34516"
FIBER0D_PAIR = (0x0416, 0x0300)
_REPORT_MAP_SHA256 = "733334cbea2e5bd20b281552735c580b8e3aae1987001eb08c7be841581000a3"
VOICE_COMMAND_REPORT = 0xFB
_VOICE_KEY_REPORT = 0xF8
_VOICE_FRAME_REPORT = 0xFC
_VOICE_KEY_USAGE = "hid:7:234"
VOICE_TAIL_SECONDS = 0.080
BLE_FALLBACK_VENDOR = 0xF1BE


def parse_pnp_id(raw: bytes) -> tuple[int | None, int, int, int | None]:
    """Decode standard GATT Device Information PnP ID (0x2A50): 7 bytes ``<BHHH``."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 7:
        return None, 0, 0, None
    source, vendor, product, revision = struct.unpack_from("<BHHH", raw, 0)
    vendor_source = source if source in (1, 2) else None
    return vendor_source, int(vendor), int(product), int(revision)


def fallback_ble_pair(address: str) -> tuple[int, int]:
    """Deterministic non-zero (vendor, product) pair when a BLE peer lacks PnP ID."""
    clean = (address or "").strip().upper().encode("ascii", errors="ignore")
    product = (zlib.crc32(clean) & 0xFFFF) or 1
    return BLE_FALLBACK_VENDOR, product


def is_verified_fiber0d(firmware: str, report_map: bytes) -> bool:
    return (
        firmware == VERIFIED_FIRMWARE
        and sha256(report_map).hexdigest() == _REPORT_MAP_SHA256
    )


@dataclass(frozen=True)
class ReportEvents:
    keys: tuple[tuple[str, bool], ...] = ()
    voice: bool | None = None
    command: bytes = b""
    pcm: bytes = b""


class _VoiceFragments:
    """Reassemble three notifications in one sequence before decoding audio."""

    def __init__(self):
        self.parts = []
        self.sequence = None
        self.previous = None
        self.errors = 0

    def feed(self, packet: bytes) -> bytes:
        if len(packet) != 20:
            self.parts.clear()
            self.errors += 1
            return b""
        sequence, index = struct.unpack_from("<HH", packet)
        if index == 0:
            if self.parts:
                self.errors += 1
            self.parts.clear()
            if self.previous is not None:
                delta = (sequence - self.previous) & 0xffff
                if not 0 < delta < 0x8000:
                    self.errors += 1
                    return b""
                if delta != 1:
                    self.errors += 1
            self.sequence = sequence
            self.parts.append(packet)
            return b""
        if not self.parts or sequence != self.sequence or index != len(self.parts):
            self.parts.clear()
            self.errors += 1
            return b""
        self.parts.append(packet)
        if index != 2:
            return b""
        first, second, third = self.parts
        self.parts.clear()
        if third[14:16] != first[:2] or third[16:20] != second[16:20]:
            self.errors += 1
            return b""
        self.previous = sequence
        return first[4:] + second[4:] + third[4:12]

    def finish(self):
        if self.parts:
            self.parts.clear()
            self.errors += 1


class BLERemoteReports:
    """Translate the selected remote's notifications into keys, commands and PCM.

    The connection writes nonempty commands to output report FB. On release
    it continues receiving the audio tail before calling finish_voice().
    """

    def __init__(self, firmware: str, report_map: bytes, *, use_remote_mic: bool):
        if firmware != VERIFIED_FIRMWARE or sha256(report_map).hexdigest() != _REPORT_MAP_SHA256:
            raise ValueError("BLE 遥控器 FIBER0D 的固件或按键报告布局尚未验证")
        self.hid = HIDReports(report_map)
        self.use_remote_mic = use_remote_mic
        self.decoder = Decoder() if use_remote_mic else None
        self.fragments = _VoiceFragments()
        self.active = False
        self.ending = False

    def set_use_remote_mic(self, enabled: bool) -> ReportEvents:
        enabled = bool(enabled)
        if enabled == self.use_remote_mic:
            return ReportEvents()
        self.use_remote_mic = enabled
        if enabled:
            if self.decoder is None:
                self.decoder = Decoder()
            return ReportEvents()
        ev = self.finish_voice()
        self.decoder = None
        return ev

    @staticmethod
    def voice_key(report, data):
        if report == _VOICE_KEY_REPORT and len(data) == 20 and data[:2] == b"\x82\x03" and data[2] in (0, 1):
            return bool(data[2])
        return None

    def feed(self, report: int, data: bytes) -> ReportEvents:
        if not 0 <= report <= 255:
            raise ValueError("BLE 报告编号无效")
        if report == _VOICE_KEY_REPORT:
            if self.decoder is None:
                return ReportEvents()
            down = self.voice_key(report, data)
            if down is None:
                return ReportEvents()
            if not down:
                return self.stop_voice()
            if self.active:
                return ReportEvents()
            self.active = True
            self.fragments = _VoiceFragments()
            self.decoder.reset()
            return ReportEvents(voice=True, command=b"\x01")
        if report == _VOICE_FRAME_REPORT:
            if self.decoder is None or not self.active:
                return ReportEvents()
            frame = self.fragments.feed(data)
            return ReportEvents(pcm=self.decoder.decode(frame)) if frame else ReportEvents()
        keys = tuple(
            (code, down) for code, down in self.hid.decode(bytes([report]) + data)
            if code.startswith(("hid:7:", "hid:12:")) and (not self.use_remote_mic or code != _VOICE_KEY_USAGE)
        )
        return ReportEvents(keys=keys)

    def stop_voice(self) -> ReportEvents:
        if not self.active or self.ending:
            return ReportEvents()
        self.ending = True
        return ReportEvents(command=b"\x00")

    def finish_voice(self) -> ReportEvents:
        self.fragments.finish()
        was_active = self.active
        self.active = self.ending = False
        return ReportEvents(voice=False) if was_active else ReportEvents()

    def disconnect(self) -> ReportEvents:
        self.hid.previous.clear()
        return self.finish_voice()


class GenericBLERemoteReports:
    """Decode keyboard (Page 7) and consumer-control (Page 12) reports from any BLE 0x1812 remote."""

    def __init__(self, report_map: bytes):
        self.hid = HIDReports(report_map)
        self.active = False
        self.ending = False
        self._held: set[str] = set()

    def set_use_remote_mic(self, enabled: bool) -> ReportEvents:
        return ReportEvents()

    @staticmethod
    def voice_key(_report, _data):
        return None

    def feed(self, report: int, data: bytes) -> ReportEvents:
        if not 0 <= report <= 255:
            raise ValueError("BLE 报告编号无效")
        payload = (bytes([report]) + data) if self.hid.has_ids else bytes(data)
        try:
            decoded = self.hid.decode(payload)
        except ValueError:
            return ReportEvents()
        keys = tuple((code, down) for code, down in decoded if code.startswith(("hid:7:", "hid:12:")))
        for code, down in keys:
            if down:
                self._held.add(code)
            else:
                self._held.discard(code)
        return ReportEvents(keys=keys)

    def stop_voice(self) -> ReportEvents:
        return ReportEvents()

    def finish_voice(self) -> ReportEvents:
        return ReportEvents()

    def disconnect(self) -> ReportEvents:
        self.hid.previous.clear()
        released = tuple((code, False) for code in sorted(self._held))
        self._held.clear()
        return ReportEvents(keys=released)
