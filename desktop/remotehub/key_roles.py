"""Single source of truth for canonical remote key roles and physical zones.

Only keys that built-in features actually depend on have structured roles:
- Navigation (6): ``up``, ``down``, ``left``, ``right``, ``ok``, ``back``
- Keypad (12): ``1``–``9``, ``0``, ``star``, ``hash``
- Voice (1): ``voice``

All other keys (media, volume, home, menu, custom shortcuts, etc.) belong to
the function zone with ``role == ""`` so remotes with arbitrary extra keys are
never constrained by a rigid enum. Common standard HID media codes still get a
friendly default display name when learned on a photo.
"""
from __future__ import annotations

from typing import Iterable

NAVIGATION_ROLES = ("up", "down", "left", "right", "ok", "back")
DPAD_ROLES = ("up", "down", "left", "right", "ok")
KEYPAD_ROLES = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "star", "0", "hash")
VOICE_ROLES = ("voice",)
KEY_ROLES = frozenset((*NAVIGATION_ROLES, *KEYPAD_ROLES, *VOICE_ROLES))

ZONE_VOICE = "语音"
ZONE_KEYPAD = "数字区"
ZONE_NAVIGATION = "方向区"
ZONE_FUNCTION = "功能区"
ZONE_ORDER = (ZONE_VOICE, ZONE_KEYPAD, ZONE_NAVIGATION, ZONE_FUNCTION)

ROLE_NAMES: dict[str, str] = {
    "up": "上",
    "down": "下",
    "left": "左",
    "right": "右",
    "ok": "确定",
    "back": "返回",
    **{str(n): str(n) for n in range(10)},
    "star": "*",
    "hash": "#",
    "voice": "语音",
}

ROLE_ZONE: dict[str, str] = {
    **{role: ZONE_NAVIGATION for role in NAVIGATION_ROLES},
    **{role: ZONE_KEYPAD for role in KEYPAD_ROLES},
    "voice": ZONE_VOICE,
}

# Maps canonical roles to legacy overlay/catalog navigation IDs.
ROLE_TO_NAV_ID: dict[str, str] = {
    "up": "tv-up",
    "down": "tv-down",
    "left": "tv-left",
    "right": "tv-right",
    "ok": "tv-ok",
    "back": "tv-back",
    **{str(n): f"tv-number-{n}" for n in range(1, 10)},
}

# Maps canonical roles <-> nine-key binding slot keys.
ROLE_TO_NINE_KEY: dict[str, str] = {
    **{str(n): str(n) for n in range(10)},
    "star": "symbols",
    "hash": "mode",
    "left": "left",
    "right": "right",
    "ok": "confirm",
    "back": "back",
}
NINE_KEY_TO_ROLE: dict[str, str] = {slot: role for role, slot in ROLE_TO_NINE_KEY.items()}

# Built-in catalog model button IDs (usb-0416-0602) -> canonical roles.
DEFAULT_ID_TO_ROLE: dict[str, str] = {
    "tv-up": "up",
    "tv-down": "down",
    "tv-left": "left",
    "tv-right": "right",
    "tv-ok": "ok",
    "tv-back": "back",
    "tv-voice": "voice",
    **{f"tv-number-{n}": str(n) for n in range(10)},
    "tv-number-*": "star",
    "tv-number-#": "hash",
}

# Standard HID Page 7 (Keyboard/Keypad) and Page 12 (Consumer) -> core role.
STANDARD_HID_ROLES: dict[str, str] = {
    "hid:7:82": "up",
    "hid:7:81": "down",
    "hid:7:80": "left",
    "hid:7:79": "right",
    "hid:7:40": "ok",
    "hid:7:88": "ok",
    "hid:12:65": "ok",
    "hid:12:66": "up",
    "hid:12:67": "down",
    "hid:12:68": "left",
    "hid:12:69": "right",
    "hid:12:548": "back",   # AC Back
    "hid:7:41": "back",     # Escape
    "hid:7:42": "back",     # Backspace
    **{f"hid:7:{30 + n - 1}": str(n) for n in range(1, 10)},
    **{f"hid:7:{89 + n - 1}": str(n) for n in range(1, 10)},
    "hid:7:39": "0",
    "hid:7:98": "0",
    "hid:7:85": "star",     # Keypad *
}

# Friendly default display names for common function/media HID codes (role stays "").
FRIENDLY_MEDIA_NAMES: dict[str, str] = {
    "hid:12:233": "音量 +",
    "hid:12:234": "音量 −",
    "hid:12:226": "静音",
    "hid:12:547": "主页",
    "hid:7:101": "菜单",
    "hid:12:64": "菜单",
    "hid:12:205": "播放 / 暂停",
    "hid:12:181": "下一曲",
    "hid:12:182": "上一曲",
    "hid:12:156": "频道 +",
    "hid:12:157": "频道 −",
    "hid:7:75": "上一页",
    "hid:7:78": "下一页",
}


def zone_for_role(role: str) -> str:
    return ROLE_ZONE.get(role, ZONE_FUNCTION)


def infer_role_from_trigger(trigger: str) -> str:
    if not isinstance(trigger, str):
        return ""
    return STANDARD_HID_ROLES.get(trigger.strip(), "")


def friendly_name_from_trigger(trigger: str) -> str:
    if not isinstance(trigger, str):
        return ""
    code = trigger.strip()
    role = STANDARD_HID_ROLES.get(code, "")
    if role:
        return ROLE_NAMES[role]
    return FRIENDLY_MEDIA_NAMES.get(code, "")


def resolve_button_role(button) -> str:
    """Resolve a button's canonical role: explicit role -> built-in ID -> standard HID trigger."""
    explicit = getattr(button, "role", "")
    if explicit in KEY_ROLES:
        return explicit
    builtin = DEFAULT_ID_TO_ROLE.get(getattr(button, "id", ""), "")
    if builtin:
        return builtin
    triggers: Iterable[str] = getattr(button, "all_triggers", ())
    for code in triggers:
        inferred = infer_role_from_trigger(code)
        if inferred:
            return inferred
    return ""


def assign_button_role(profiles, button_id: str, role: str) -> bool:
    """Assign ``role`` to ``button_id`` across ``profiles``, keeping each core role unique."""
    if role and role not in KEY_ROLES:
        return False
    changed = False
    for profile in profiles:
        for button in profile.buttons:
            if button.id == button_id:
                if button.role != role:
                    button.role = role
                    changed = True
            elif role and button.role == role:
                button.role = ""
                changed = True
    return changed


def sync_roles_from_nine_key(profiles, bindings: dict[str, str]) -> bool:
    """Persist nine-key binding slots back onto each button's canonical role across profiles."""
    changed = False
    for slot, button_id in bindings.items():
        role = NINE_KEY_TO_ROLE.get(slot, "")
        if role and button_id:
            changed = assign_button_role(profiles, button_id, role) or changed
    return changed
