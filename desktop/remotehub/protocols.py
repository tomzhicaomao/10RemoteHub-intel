"""Protocol Library (协议库): identifies transport, key, and voice protocols.

Unlike a product/shell catalog, protocols never assume button count or physical
appearance (e.g. a 20-key and a 30-key remote may share the exact same chip and
protocol). Every user imports their own remote's real photo, while the protocol
library handles key transport and built-in voice decoding automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import models


@dataclass(frozen=True)
class ProtocolSpec:
    id: str
    name: str
    kind: str  # "key" | "voice"
    transport: str  # "USB" | "蓝牙"
    summary: str


KEY_PROTOCOLS: dict[str, ProtocolSpec] = {
    "usb-0416-0602": ProtocolSpec(
        id="usb-0416-0602",
        name="专用 USB 接收器协议 (0416:0602)",
        kind="key",
        transport="USB",
        summary="支持全键读取与独占",
    ),
    "ble-dongle-hid": ProtocolSpec(
        id="ble-dongle-hid",
        name="标准 BLE HID (0x1812) · 外接蓝牙棒直连",
        kind="key",
        transport="蓝牙",
        summary="全键独占，绕过系统蓝牙限制",
    ),
    "usb-hid-generic": ProtocolSpec(
        id="usb-hid-generic",
        name="标准 USB HID 协议",
        kind="key",
        transport="USB",
        summary="通用键盘 / 媒体按键读取",
    ),
    "ble-hid-generic": ProtocolSpec(
        id="ble-hid-generic",
        name="标准蓝牙 HID 协议",
        kind="key",
        transport="蓝牙",
        summary="系统原生蓝牙按键读取",
    ),
}

VOICE_PROTOCOLS: dict[str, ProtocolSpec] = {
    "usb-0416-0602": ProtocolSpec(
        id="usb-0416-0602",
        name="USB 0416:0602 专用语音协议",
        kind="voice",
        transport="USB",
        summary="内置解码，支持遥控器自带麦克风",
    ),
    "fiber0d": ProtocolSpec(
        id="fiber0d",
        name="FIBER0D BLE 语音协议 (G.722.1)",
        kind="voice",
        transport="蓝牙",
        summary="内置 G.722.1 实时解码，支持遥控器自带麦克风",
    ),
    "atv_voice": ProtocolSpec(
        id="atv_voice",
        name="Android TV 通用 BLE 语音协议 (ATV Voice / IMA-ADPCM)",
        kind="voice",
        transport="蓝牙",
        summary="按 AB5E0001 服务自动识别，支持遥控器自带麦克风",
    ),
}


@dataclass(frozen=True)
class ProtocolMatch:
    key_protocol: ProtocolSpec
    voice_protocol: ProtocolSpec | None
    probed: bool = True

    @property
    def voice_supported(self) -> bool:
        return self.voice_protocol is not None

    @property
    def via_dongle(self) -> bool:
        return self.key_protocol.id == "ble-dongle-hid"

    def access_lines(self) -> list[str]:
        """Plain-language access level of a readable connection, without protocol jargon."""
        if self.key_protocol.id == "usb-0416-0602":
            detail = "配套接收器 0416:0602，全部按键都能读。"
        elif self.via_dongle:
            detail = "已通过外接蓝牙棒直连，按键都能读。"
        elif self.key_protocol.transport == "蓝牙":
            detail = "电脑蓝牙已经读到这只遥控器的按键。"
        else:
            detail = "电脑已经读到这个 USB 接收器的按键。"
        return [f"{access_heading(ACCESS_DIRECT)}：{detail}"]


# -- plain-language access levels ---------------------------------------------------
#
# The add dialog, the photo microphone step, the remote list and the voice page all
# describe support the same way: what the user can expect, not which protocol matched.

ACCESS_DIRECT = "direct"
ACCESS_ONE_CLICK = "one_click"
ACCESS_UNSUPPORTED = "unsupported"
ACCESS_NOT_SEEN = "not_seen"

ACCESS_LEVELS = {
    ACCESS_DIRECT: ("✅", "直接接入"),
    ACCESS_ONE_CLICK: ("🔧", "点一下就能接入"),
    ACCESS_UNSUPPORTED: ("⚠️", "目前无法自动接入"),
    ACCESS_NOT_SEEN: ("❓", "看不到设备"),
}

MIC_DIRECT = "direct"
MIC_DONGLE = "dongle"
MIC_NONE = "none"

MIC_LEVELS = {
    MIC_DIRECT: "直接能用",
    MIC_DONGLE: "换蓝牙棒直连能用",
    MIC_NONE: "目前用不了",
}


def access_heading(level: str) -> str:
    icon, title = ACCESS_LEVELS[level]
    return f"{icon} {title}"


DONGLE_MISSING = "外接 USB 蓝牙棒直连可以绕开这个限制，但当前版本还没有带上蓝牙棒直连组件，暂时用不了。"


def dongle_route_available() -> bool:
    """Whether this build carries the user-space Bluetooth stack (Bumble) the dongle route needs.

    It is not a locked release dependency yet; without it the app must not promise the route
    or send the user to switch a dongle's driver.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("bumble") is not None
    except Exception:
        return False


def dongle_route_note(platform: str) -> str:
    return "" if platform == "win32" else "（Mac 上这条路还在验证）"


def mic_text(level: str, platform: str) -> str:
    """What the user can do with the remote's own microphone, one plain sentence each."""
    head = f"遥控器麦克风{MIC_LEVELS[level]}："
    if level == MIC_DIRECT:
        return head + "保存后到「语音」页选「遥控器麦克风」，按语音键说话即可。"
    if level == MIC_DONGLE:
        return (head + "目前版本通过电脑自带蓝牙收不到这只遥控器的声音；插一只外接 USB 蓝牙棒直连后就能收音"
                f"{dongle_route_note(platform)}。在那之前语音键可以触发识别，用电脑麦克风说话。")
    return (head + "语音键仍可触发识别，用电脑麦克风说话；想用遥控器自带的麦克风，"
            "可以点「复制给 AI 的求助」请 AI 协助适配。")


def dongle_voice_pair(transport: str, vendor: int, product: int) -> bool:
    """A Bluetooth remote whose voice the external-dongle route is verified to decode."""
    from .ble_remote import FIBER0D_PAIR

    return transport == "蓝牙" and (vendor, product) == FIBER0D_PAIR


def mic_level(*, supported: bool, dongle_voice: bool, dongle_probed: bool) -> str:
    """Microphone level: built-in decoding, a verified dongle route, or none yet.

    ``dongle_voice`` marks a known voice remote over the system Bluetooth stack; once the
    dongle itself has probed the remote without a voice match, that route is gone.
    """
    if supported:
        return MIC_DIRECT
    if dongle_voice and not dongle_probed:
        return MIC_DONGLE
    return MIC_NONE


def device_mic_level(device: Any, match: "ProtocolMatch") -> str:
    return mic_level(
        supported=match.voice_supported,
        dongle_voice=dongle_route_available() and dongle_voice_pair(
            str(getattr(device, "transport", "") or ""),
            int(getattr(device, "vendor", 0) or 0), int(getattr(device, "product", 0) or 0)),
        dongle_probed=match.via_dongle,
    )


def identify_protocol(device: Any, reader: Any = None) -> ProtocolMatch:
    """Identify key & voice protocols for a detected ``ProbeDevice`` without assuming its shell."""
    dev_id = getattr(device, "id", None)
    vendor = int(getattr(device, "vendor", 0) or 0)
    product = int(getattr(device, "product", 0) or 0)
    transport = str(getattr(device, "transport", "") or "")
    is_ble_dongle = isinstance(dev_id, tuple) and bool(dev_id) and dev_id[0] == "ble-dongle"

    if is_ble_dongle:
        key_proto = KEY_PROTOCOLS["ble-dongle-hid"]
        info = _query_ble_peer_info(reader, dev_id)
        probed = bool(info.get("paired") or info.get("firmware") or info.get("gatt_summary") or info.get("protocol"))
        v_id = info.get("protocol", "")
        voice_proto = VOICE_PROTOCOLS.get(v_id)
        return ProtocolMatch(key_protocol=key_proto, voice_protocol=voice_proto, probed=probed)

    if transport == "USB" and (vendor, product) in models.supported_pairs():
        return ProtocolMatch(
            key_protocol=KEY_PROTOCOLS["usb-0416-0602"],
            voice_protocol=VOICE_PROTOCOLS["usb-0416-0602"],
            probed=True,
        )

    if transport == "蓝牙":
        return ProtocolMatch(key_protocol=KEY_PROTOCOLS["ble-hid-generic"], voice_protocol=None, probed=True)

    return ProtocolMatch(key_protocol=KEY_PROTOCOLS["usb-hid-generic"], voice_protocol=None, probed=True)


def _query_ble_peer_info(reader: Any, dev_id: Any) -> dict[str, Any]:
    if reader is None:
        return {}
    fn = getattr(reader, "peer_voice_info_for_id", None)
    if callable(fn):
        try:
            return dict(fn(dev_id) or {})
        except Exception:
            return {}
    for child in getattr(reader, "readers", ()):
        fn = getattr(child, "peer_voice_info_for_id", None)
        if callable(fn):
            try:
                res = fn(dev_id)
                if res:
                    return dict(res)
            except Exception:
                continue
    return {}
