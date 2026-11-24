"""Android TV / Google ATV Voice (AB5E0001) BLE voice protocol & IMA-ADPCM decoder.

Covers Android TV / Google TV voice remotes and any other BLE remote that exposes
the standard ATV Voice GATT service:

- Service: ``AB5E0001-5A21-4F05-BC7D-AF01F617B664``
- TX (Host -> Remote write): ``AB5E0002-5A21-4F05-BC7D-AF01F617B664``
- RX (Remote -> Host audio notify): ``AB5E0003-5A21-4F05-BC7D-AF01F617B664``
- CTL (Remote -> Host control notify): ``AB5E0004-5A21-4F05-BC7D-AF01F617B664``

Outputs 16 kHz, 16-bit signed little-endian mono PCM compatible with ``AudioBridge``.
"""
from __future__ import annotations

import struct

from .ble_remote import ReportEvents

ATV_VOICE_SERVICE = "ab5e0001-5a21-4f05-bc7d-af01f617b664"
ATV_VOICE_TX = "ab5e0002-5a21-4f05-bc7d-af01f617b664"
ATV_VOICE_RX = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
ATV_VOICE_CTL = "ab5e0004-5a21-4f05-bc7d-af01f617b664"

# Control opcodes (Remote -> Host on CTL, Host -> Remote on TX)
_CTL_AUDIO_STOP = 0x00
_CTL_AUDIO_START = 0x04
_CTL_START_SEARCH = 0x08
_CTL_AUDIO_SYNC = 0x0A
_CTL_CAPS_RESP = 0x0B
_CTL_MIC_OPEN_REQ = 0x0C

_TX_GET_CAPS = 0x0A
_TX_MIC_OPEN = 0x0C
_TX_MIC_CLOSE = 0x0D

_CODEC_ADPCM_8K = 0x0001
_CODEC_ADPCM_16K = 0x0002

# Standard Intel/DVI IMA-ADPCM tables.
_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)
_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487,
    12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)


def decode_ima_adpcm_frame(
    payload: bytes,
    *,
    predictor: int = 0,
    step_index: int = 0,
    high_nibble_first: bool = True,
) -> tuple[list[int], int, int]:
    """Decode IMA-ADPCM bytes into signed 16-bit PCM sample ints."""
    pred = max(-32768, min(32767, int(predictor)))
    idx = max(0, min(88, int(step_index)))
    samples: list[int] = []
    for byte in payload:
        nibbles = ((byte >> 4) & 0x0F, byte & 0x0F) if high_nibble_first else (byte & 0x0F, (byte >> 4) & 0x0F)
        for code in nibbles:
            step = _STEP_TABLE[idx]
            diff = step >> 3
            if code & 1:
                diff += step >> 2
            if code & 2:
                diff += step >> 1
            if code & 4:
                diff += step
            if code & 8:
                pred -= diff
            else:
                pred += diff
            if pred > 32767:
                pred = 32767
            elif pred < -32768:
                pred = -32768
            idx += _INDEX_TABLE[code]
            if idx < 0:
                idx = 0
            elif idx > 88:
                idx = 88
            samples.append(pred)
    return samples, pred, idx


def upsample_8k_to_16k(samples: list[int], prev_sample: int | None = None) -> list[int]:
    """Simple 2x linear interpolation from 8 kHz to 16 kHz."""
    if not samples:
        return []
    out: list[int] = []
    prev = samples[0] if prev_sample is None else prev_sample
    for cur in samples:
        out.append((prev + cur) >> 1)
        out.append(cur)
        prev = cur
    return out


def normalize_uuid(val: object) -> str:
    return str(val or "").strip().lower()


class ATVVVoiceSession:
    """Protocol state machine for Android TV Voice (AB5E0001) over BLE GATT."""

    def __init__(self, *, use_remote_mic: bool = True):
        self.use_remote_mic = bool(use_remote_mic)
        self.version = (1, 0)
        self.codecs_supported = _CODEC_ADPCM_8K | _CODEC_ADPCM_16K
        self.frame_size = 134  # 6-byte header + 128-byte IMA-ADPCM payload (v0.4/v1.0 default)
        self.sample_rate = 16000
        self.active = False
        self.ending = False
        self._buf = bytearray()
        self._pred = 0
        self._step_index = 0
        self._last_8k_sample: int | None = None

    @staticmethod
    def caps_command() -> bytes:
        """GET_CAPS (v1.0, ADPCM 8kHz + 16kHz)."""
        return bytes([_TX_GET_CAPS, 0x01, 0x00, 0x00, _CODEC_ADPCM_8K | _CODEC_ADPCM_16K])

    def set_use_remote_mic(self, enabled: bool) -> ReportEvents:
        enabled = bool(enabled)
        if enabled == self.use_remote_mic:
            return ReportEvents()
        self.use_remote_mic = enabled
        if not enabled and self.active:
            return self.stop_voice()
        return ReportEvents()

    def _mic_open_command(self) -> bytes:
        if self.version[0] >= 1:
            # 16 kHz preferred when remote advertises ADPCM_16K, else 8 kHz.
            if self.codecs_supported & _CODEC_ADPCM_16K:
                self.sample_rate = 16000
                return bytes([_TX_MIC_OPEN, 0x00, _CODEC_ADPCM_16K])
            self.sample_rate = 8000
            return bytes([_TX_MIC_OPEN, 0x00, _CODEC_ADPCM_8K])
        self.sample_rate = 8000
        return bytes([_TX_MIC_OPEN])

    def on_ctl(self, data: bytes) -> ReportEvents:
        if not data:
            return ReportEvents()
        opcode = data[0]
        if opcode == _CTL_CAPS_RESP and len(data) >= 9:
            self.version = (data[1], data[2])
            self.codecs_supported = (data[3] << 8) | data[4]
            frame_sz = (data[5] << 8) | data[6]
            if 20 <= frame_sz <= 512:
                self.frame_size = frame_sz
            return ReportEvents()
        if opcode in (_CTL_START_SEARCH, _CTL_MIC_OPEN_REQ):
            if not self.use_remote_mic:
                return ReportEvents()
            cmd = self._mic_open_command()
            if self.active:
                return ReportEvents(command=cmd)
            self._reset_stream()
            self.active = True
            self.ending = False
            return ReportEvents(voice=True, command=cmd)
        if opcode == _CTL_AUDIO_START:
            if not self.use_remote_mic:
                return ReportEvents()
            if len(data) >= 4:
                codec_byte = data[3]
                if codec_byte == _CODEC_ADPCM_16K:
                    self.sample_rate = 16000
                elif codec_byte == _CODEC_ADPCM_8K:
                    self.sample_rate = 8000
            if not self.active:
                self._reset_stream()
                self.active = True
                self.ending = False
                return ReportEvents(voice=True)
            return ReportEvents()
        if opcode == _CTL_AUDIO_SYNC and len(data) >= 6:
            # [0x0A, codec, seq_hi, seq_lo, pred_hi, pred_lo, step_idx]
            if len(data) >= 7:
                self._pred = struct.unpack_from(">h", data, 4)[0]
                self._step_index = max(0, min(88, data[6]))
            return ReportEvents()
        if opcode == _CTL_AUDIO_STOP:
            return self.finish_voice()
        return ReportEvents()

    def on_rx(self, data: bytes) -> ReportEvents:
        if not self.use_remote_mic or not self.active or not data:
            return ReportEvents()
        self._buf.extend(data)
        pcm_out = bytearray()
        while len(self._buf) >= self.frame_size:
            frame = bytes(self._buf[: self.frame_size])
            del self._buf[: self.frame_size]
            if len(frame) <= 6:
                continue
            # ATV Voice ADPCM frame: [seq:2B BE, id:1B, prev_pred:2B BE signed, step_idx:1B, adpcm...]
            pred = struct.unpack_from(">h", frame, 3)[0]
            step_idx = frame[5]
            samples, self._pred, self._step_index = decode_ima_adpcm_frame(
                frame[6:], predictor=pred, step_index=step_idx, high_nibble_first=True
            )
            if self.sample_rate == 8000:
                samples = upsample_8k_to_16k(samples, self._last_8k_sample)
                if samples:
                    self._last_8k_sample = samples[-1]
            if samples:
                pcm_out.extend(struct.pack(f"<{len(samples)}h", *samples))
        return ReportEvents(pcm=bytes(pcm_out)) if pcm_out else ReportEvents()

    def stop_voice(self) -> ReportEvents:
        was_active = self.active
        self.active = False
        self.ending = False
        self._buf.clear()
        return ReportEvents(voice=False if was_active else None, command=bytes([_TX_MIC_CLOSE]))

    def finish_voice(self) -> ReportEvents:
        was_active = self.active
        self.active = False
        self.ending = False
        self._buf.clear()
        return ReportEvents(voice=False) if was_active else ReportEvents()

    def disconnect(self) -> ReportEvents:
        return self.finish_voice()

    def _reset_stream(self) -> None:
        self._buf.clear()
        self._pred = 0
        self._step_index = 0
        self._last_8k_sample = None
