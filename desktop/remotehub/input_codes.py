"""Normalize platform input identities to the HID identities in the fixed key table.

Windows RAWKEYBOARD scan codes and Linux evdev MSC_SCAN / Set-1 codes map only
verified standard keys; unknown codes are never guessed.
"""

WINDOWS_KEYS = {
    **{(i + 2, 0): i + 30 for i in range(10)},
    (0x1C, 0): 40,
    (1, 0): 41,
    (0x3F, 0): 62,
    (0x37, 0): 85,
    (0x48, 2): 82,
    (0x4B, 2): 80,
    (0x4D, 2): 79,
    (0x50, 2): 81,
    (0x5D, 2): 101,
    (0x49, 2): 75,
    (0x51, 2): 78,
}

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
