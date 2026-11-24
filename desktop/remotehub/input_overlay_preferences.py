"""Configuration shared by the nine-key and voice overlays."""
from __future__ import annotations

CORNERS = (
    ("top_left", "左上"), ("top_right", "右上"),
    ("bottom_left", "左下"), ("bottom_right", "右下"),
)
POSITIONS = (("input", "输入框旁"), *CORNERS)
MIN_OPACITY = 65
CONFIRM_KEYS = {
    "tv-ok": "确定", "tv-number-#": "#", "tv-number-0": "0", "tv-up": "上",
    "tv-down": "下", "tv-menu": "菜单", "tv-home": "主页", "tv-m": "M",
    "tv-settings": "设置", "tv-mute": "静音", "tv-channel-up": "频道＋",
    "tv-channel-down": "频道－", "tv-volume-up": "音量＋", "tv-volume-down": "音量－",
}

DEFAULTS = {
    "nine_position": "input",
    "nine_fallback": "bottom_right",
    "nine_compact": False,
    "confirm_key": "tv-ok",
    "voice_position": "top_right",
    "voice_fallback": "top_right",
    "show_voice": True,
    "opacity": 76,
}


def preferences(settings):
    return DEFAULTS | settings.get("input_overlays", {})


def validate_preferences(values):
    if not isinstance(values, dict):
        raise ValueError("输入浮层设置格式无效")
    for key, choices in (("nine_position", POSITIONS), ("voice_position", POSITIONS),
                         ("nine_fallback", CORNERS), ("voice_fallback", CORNERS)):
        if key in values and not any(values[key] == position for position, _ in choices):
            raise ValueError("输入浮层位置无效")
    for key in ("nine_compact", "show_voice"):
        if key in values and type(values[key]) is not bool:
            raise ValueError("输入浮层显示设置无效")
    if "confirm_key" in values:
        key = values["confirm_key"]
        if not isinstance(key, str) or key not in CONFIRM_KEYS:
            raise ValueError("上屏键无效")
    opacity = values.get("opacity", DEFAULTS["opacity"])
    if type(opacity) is not int or not MIN_OPACITY <= opacity <= 100:
        raise ValueError(f"底板不透明度应为 {MIN_OPACITY}–100%")
