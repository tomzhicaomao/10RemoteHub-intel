"""Normalize platform input identities to the HID identities in the fixed key table.

Windows RAWKEYBOARD scan codes and Linux evdev MSC_SCAN / Set-1 codes map only
verified standard keys; unknown codes are never guessed.
"""
from __future__ import annotations

import re

WINDOWS_KEYS = {
    # Digits 1-9, 0 (top row)
    **{(i + 2, 0): i + 30 for i in range(10)},
    # Enter, Esc, Backspace, Tab, Space
    (0x1C, 0): 40,
    (0x01, 0): 41,
    (0x0E, 0): 42,
    (0x0F, 0): 43,
    (0x39, 0): 44,
    # F1-F10, F11-F12
    (0x3B, 0): 58,
    (0x3C, 0): 59,
    (0x3D, 0): 60,
    (0x3E, 0): 61,
    (0x3F, 0): 62,
    (0x40, 0): 63,
    (0x41, 0): 64,
    (0x42, 0): 65,
    (0x43, 0): 66,
    (0x44, 0): 67,
    (0x57, 0): 68,
    (0x58, 0): 69,
    # Navigation cluster (extended Set-1 E0)
    (0x52, 2): 73,   # Insert
    (0x47, 2): 74,   # Home
    (0x49, 2): 75,   # PageUp
    (0x53, 2): 76,   # Delete
    (0x4F, 2): 77,   # End
    (0x51, 2): 78,   # PageDown
    (0x4D, 2): 79,   # Right
    (0x4B, 2): 80,   # Left
    (0x50, 2): 81,   # Down
    (0x48, 2): 82,   # Up
    # Numpad operators, Enter, digits 0-9
    (0x35, 2): 84,   # Numpad /
    (0x37, 0): 85,   # Numpad *
    (0x4A, 0): 86,   # Numpad -
    (0x4E, 0): 87,   # Numpad +
    (0x1C, 2): 88,   # Numpad Enter
    (0x4F, 0): 89,   # Numpad 1
    (0x50, 0): 90,   # Numpad 2
    (0x51, 0): 91,   # Numpad 3
    (0x4B, 0): 92,   # Numpad 4
    (0x4C, 0): 93,   # Numpad 5
    (0x4D, 0): 94,   # Numpad 6
    (0x47, 0): 95,   # Numpad 7
    (0x48, 0): 96,   # Numpad 8
    (0x49, 0): 97,   # Numpad 9
    (0x52, 0): 98,   # Numpad 0
    (0x53, 0): 99,   # Numpad .
    # Application / Menu
    (0x5D, 2): 101,
}

# Reverse lookup: HID Keyboard Page 7 usage -> Windows Set-1 (scan_code, extended_bool).
_HID7_TO_WINDOWS_SCAN: dict[int, tuple[int, bool]] = {
    usage: (scan, bool(flags & 2))
    for (scan, flags), usage in WINDOWS_KEYS.items()
}

# HID Consumer Control Page 12 usage -> Windows Virtual Key (vkCode seen in WH_KEYBOARD_LL).
WINDOWS_CONSUMER_VKS: dict[int, int] = {
    181: 0xB0,  # Scan Next Track -> VK_MEDIA_NEXT_TRACK
    182: 0xB1,  # Scan Previous Track -> VK_MEDIA_PREV_TRACK
    183: 0xB2,  # Stop -> VK_MEDIA_STOP
    205: 0xB3,  # Play/Pause -> VK_MEDIA_PLAY_PAUSE
    226: 0xAD,  # Mute -> VK_VOLUME_MUTE
    233: 0xAF,  # Volume Increment -> VK_VOLUME_UP
    234: 0xAE,  # Volume Decrement -> VK_VOLUME_DOWN
    387: 0xB5,  # AL Consumer Control Config -> VK_LAUNCH_MEDIA_SELECT
    394: 0xB4,  # AL Email Reader -> VK_LAUNCH_MAIL
    402: 0xB7,  # AL Calculator -> VK_LAUNCH_APP2
    404: 0xB6,  # AL Local Machine Browser -> VK_LAUNCH_APP1
    545: 0xAA,  # AC Search -> VK_BROWSER_SEARCH
    547: 0xAC,  # AC Home -> VK_BROWSER_HOME
    548: 0xA6,  # AC Back -> VK_BROWSER_BACK
    549: 0xA7,  # AC Forward -> VK_BROWSER_FORWARD
    550: 0xA9,  # AC Stop -> VK_BROWSER_STOP
    551: 0xA8,  # AC Refresh -> VK_BROWSER_REFRESH
    554: 0xAB,  # AC Bookmarks -> VK_BROWSER_FAVORITES
}

_HID7_RE = re.compile(r"^hid:7:([0-9]{1,5})$")
_HID12_RE = re.compile(r"^hid:12:([0-9]{1,5})$")
_WIN_RE = re.compile(r"^win:([0-9]{1,5}):([0246])$")

# Linux input-event-codes.h -> USB HID keyboard usages (page 7).
LINUX_KEYS = {
    **{i + 2: i + 30 for i in range(10)},
    28: 40,
    1: 41,
    103: 82,
    105: 80,
    106: 79,
    108: 81,
    139: 101,
    63: 62,
    104: 75,
    109: 78,
    55: 85,
}


def windows_keyboard(scan, flags):
    usage = WINDOWS_KEYS.get((scan, flags & 6))
    return f"hid:7:{usage}" if usage is not None else None


def windows_scan_for_trigger(trigger: str) -> tuple[int, bool] | None:
    """Return ``(scan_code, is_extended)`` for a ``hid:7:*`` or ``win:*:*`` trigger."""
    if not isinstance(trigger, str):
        return None
    text = trigger.strip()
    m_win = _WIN_RE.match(text)
    if m_win:
        scan = int(m_win.group(1))
        flags = int(m_win.group(2))
        return (scan, bool(flags & 2)) if scan > 0 else None
    m_hid = _HID7_RE.match(text)
    if m_hid:
        return _HID7_TO_WINDOWS_SCAN.get(int(m_hid.group(1)))
    return None


def windows_vk_for_consumer_trigger(trigger: str) -> int | None:
    """Return the Windows VK code generated for a ``hid:12:*`` consumer usage, or None."""
    if not isinstance(trigger, str):
        return None
    m_hid = _HID12_RE.match(trigger.strip())
    if not m_hid:
        return None
    return WINDOWS_CONSUMER_VKS.get(int(m_hid.group(1)))


class LinuxKeys:
    """Normalize Linux evdev EV_MSC/MSC_SCAN + EV_KEY streams to fixed HID triggers."""

    def __init__(self):
        self.scan = None
        self.held = {}

    def feed(self, kind, code, value):
        if kind == 4 and code == 4:  # EV_MSC / MSC_SCAN
            page, usage = value >> 16, value & 0xFFFF
            self.scan = f"hid:{page}:{usage}" if page in (7, 12) and usage else None
        elif kind == 0:  # SYN_REPORT / SYN_DROPPED
            self.scan = None
            if code == 3:
                events = [(key, False) for key in self.held.values()]
                self.held.clear()
                return events
        elif kind == 1:  # EV_KEY
            scan, self.scan = self.scan, None
            if value == 2:
                return []  # kernel repeat, not another physical press
            fallback = LINUX_KEYS.get(code)
            trigger = scan or self.held.get(code) or (f"hid:7:{fallback}" if fallback else None)
            if trigger and value in (0, 1):
                if value:
                    if code in self.held:
                        return []
                    self.held[code] = trigger
                else:
                    self.held.pop(code, None)
                return [(trigger, bool(value))]
        return []
