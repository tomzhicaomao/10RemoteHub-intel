"""Per-device macOS key suppression via ``/usr/bin/hidutil`` UserKeyMapping.

Why this instead of ``IOHIDDeviceOpen(..., kIOHIDOptionsTypeSeizeDevice)``:
- macOS rejects non-root seizure of keyboard-class HID interfaces with
  ``kIOReturnNotPrivileged`` (``0xe00002c1``), which affects most Bluetooth and
  2.4 GHz remotes.
- Many remotes bundle keyboard, consumer-control (volume/media), and air-mouse
  collections into one HID interface; seizing the interface kills the air-mouse.
- ``UserKeyMapping`` runs in ``IOHIDEventService`` *above* raw ``IOHIDDevice``
  input reports: mapping learned keys to ``0x700000000`` (HID Null / No Event)
  silences macOS key & volume delivery for that exact device without root,
  while ``MacHIDProbe`` still receives the original raw HID reports and the
  built-in keyboard and air-mouse stay untouched.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Iterable
import weakref

from .model import atomic_write

HIDUTIL = "/usr/bin/hidutil"
NULL_USAGE = 0x700000000          # HID Keyboard Usage 0 (No Event): silences the key
APPLE_VENDOR = 0x05AC             # Built-in / Apple keyboards — never suppress
ALLOWED_PAGES = (7, 12)           # Keyboard/Keypad (7) and Consumer Control (12)
COMMAND_TIMEOUT = 3.0

_TRIGGER_RE = re.compile(r"^hid:(7|12):([0-9]{1,5})$")
_SRC_MARKER = "HIDKeyboardModifierMappingSrc"


def trigger_to_hid_usage(trigger: str) -> int | None:
    """Convert ``hid:<page>:<usage>`` (pages 7 and 12) to a 64-bit hidutil usage."""
    if not isinstance(trigger, str):
        return None
    match = _TRIGGER_RE.match(trigger.strip())
    if not match:
        return None
    page = int(match.group(1))
    usage = int(match.group(2))
    if page not in ALLOWED_PAGES or not (1 <= usage <= 0xFFFF):
        return None
    return (page << 32) | usage


def usages_from_triggers(triggers: Iterable[str]) -> tuple[int, ...]:
    """Return deduplicated sorted hidutil usage integers from trigger strings."""
    usages = {
        usage
        for trigger in triggers
        if (usage := trigger_to_hid_usage(trigger)) is not None
    }
    return tuple(sorted(usages))


def build_matching(
    vendor: int,
    product: int,
    *,
    transport: str = "",
    location_id: int | None = None,
    built_in: bool = False,
) -> dict | None:
    """Build a per-device hidutil ``--matching`` dict, refusing built-in/Apple devices."""
    if built_in:
        return None
    if type(vendor) is not int or type(product) is not int:
        return None
    if vendor == APPLE_VENDOR or not (0 < vendor <= 0xFFFF) or not (0 < product <= 0xFFFF):
        return None
    matching: dict[str, int | str] = {"VendorID": vendor, "ProductID": product}
    if isinstance(transport, str) and transport.strip():
        matching["Transport"] = transport.strip()
    if type(location_id) is int and location_id > 0:
        matching["LocationID"] = location_id
    return matching


def _matching_candidates(
    vendor: int,
    product: int,
    *,
    transport: str = "",
    location_id: int | None = None,
    built_in: bool = False,
) -> list[dict]:
    """Return ordered matching dicts from most specific to VID/PID fallback."""
    candidates: list[dict] = []
    specs = [
        (transport, location_id),
        (transport, None),
        ("", None),
    ]
    for tr, loc in specs:
        match = build_matching(vendor, product, transport=tr, location_id=loc, built_in=built_in)
        if match is not None and match not in candidates:
            candidates.append(match)
    return candidates


def _count_mappings(output: str) -> int:
    return output.count(_SRC_MARKER)


def _pid_alive(pid: int) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _run_hidutil(args: list[str], timeout: float = COMMAND_TIMEOUT) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            [HIDUTIL, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)


def _atexit_clear(ref: weakref.ref[MacHIDSuppressor]) -> None:
    suppressor = ref()
    if suppressor is not None:
        suppressor.clear()


class MacHIDSuppressor:
    """Manage per-device ``UserKeyMapping`` suppression with verification and crash recovery."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    ):
        self._state_path = Path(state_path) if state_path is not None else None
        self._runner = runner or _run_hidutil
        self._active_matching: dict | None = None
        self._active_usages: tuple[int, ...] = ()
        self._active_spec: tuple[int, int, str, int | None] | None = None
        self.active: bool = False
        self.error: str = ""
        atexit.register(_atexit_clear, weakref.ref(self))
        self.cleanup_stale()

    @property
    def active_usages(self) -> tuple[int, ...]:
        return self._active_usages

    def _set_mapping(self, matching: dict, usages: tuple[int, ...]) -> tuple[int, str, str]:
        payload = {
            "UserKeyMapping": [
                {"HIDKeyboardModifierMappingSrc": usage, "HIDKeyboardModifierMappingDst": NULL_USAGE}
                for usage in usages
            ]
        }
        return self._runner([
            "property",
            "--matching",
            json.dumps(matching, separators=(",", ":")),
            "--set",
            json.dumps(payload, separators=(",", ":")),
        ])

    def _get_mapping(self, matching: dict) -> tuple[int, str, str]:
        return self._runner([
            "property",
            "--matching",
            json.dumps(matching, separators=(",", ":")),
            "--get",
            "UserKeyMapping",
        ])

    def _clear_matching(self, matching: dict) -> None:
        try:
            self._set_mapping(matching, ())
        except Exception:
            pass

    def _write_lease(self, matching: dict) -> None:
        if self._state_path is None:
            return
        try:
            data = json.dumps({"pid": os.getpid(), "matching": matching}, ensure_ascii=False)
            atomic_write(self._state_path, data)
        except OSError:
            pass

    def _remove_lease(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.unlink(missing_ok=True)
        except OSError:
            pass

    def cleanup_stale(self) -> bool:
        """Clear a leftover UserKeyMapping written by a previous crashed instance."""
        if self._state_path is None or not self._state_path.exists():
            return False
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._remove_lease()
            return False
        if not isinstance(raw, dict):
            self._remove_lease()
            return False
        pid = raw.get("pid")
        if type(pid) is int and pid != os.getpid() and _pid_alive(pid):
            return False
        matching = raw.get("matching")
        if isinstance(matching, dict):
            validated = build_matching(
                matching.get("VendorID"),
                matching.get("ProductID"),
                transport=matching.get("Transport", ""),
                location_id=matching.get("LocationID"),
            )
            if validated is not None:
                # Also clear the broader VID/PID match in case LocationID changed after replug.
                self._clear_matching(validated)
                fallback = build_matching(validated["VendorID"], validated["ProductID"])
                if fallback is not None and fallback != validated:
                    self._clear_matching(fallback)
        self._remove_lease()
        return True

    def apply(
        self,
        *,
        vendor: int,
        product: int,
        triggers: Iterable[str],
        transport: str = "",
        location_id: int | None = None,
        built_in: bool = False,
    ) -> bool:
        """Suppress the given triggers on one non-Apple device and verify readback."""
        usages = usages_from_triggers(triggers)
        spec = (vendor, product, transport, location_id)
        if self.active and self._active_spec == spec and self._active_usages == usages:
            if self.ensure():
                return True

        if self._active_matching is not None:
            self.clear()

        if not usages:
            self.active = False
            self.error = ""
            return False

        candidates = _matching_candidates(
            vendor, product, transport=transport, location_id=location_id, built_in=built_in
        )
        if not candidates:
            self.active = False
            self.error = "已保护系统内建设备"
            return False

        last_error = ""
        for matching in candidates:
            rc, stdout, stderr = self._set_mapping(matching, usages)
            if rc != 0:
                last_error = (stderr or stdout).strip() or f"hidutil 退出码 {rc}"
                continue
            if _count_mappings(stdout) >= len(usages):
                self._active_matching = matching
                self._active_usages = usages
                self._active_spec = spec
                self.active = True
                self.error = ""
                self._write_lease(matching)
                return True
            # Some macOS versions print nothing on --set; confirm with --get.
            grc, gstdout, gstderr = self._get_mapping(matching)
            if grc == 0 and _count_mappings(gstdout) >= len(usages):
                self._active_matching = matching
                self._active_usages = usages
                self._active_spec = spec
                self.active = True
                self.error = ""
                self._write_lease(matching)
                return True
            if grc != 0:
                last_error = (gstderr or gstdout).strip() or f"hidutil 查询失败（{grc}）"

        self.active = False
        self.error = last_error or "未匹配到系统键盘事件服务"
        return False

    def ensure(self) -> bool:
        """Verify suppression is still active; re-apply if macOS reset it after sleep/wake."""
        if not self.active or self._active_matching is None or not self._active_usages:
            return False
        rc, stdout, _ = self._get_mapping(self._active_matching)
        if rc == 0 and _count_mappings(stdout) >= len(self._active_usages):
            return True
        rc, stdout, stderr = self._set_mapping(self._active_matching, self._active_usages)
        if rc == 0 and _count_mappings(stdout) >= len(self._active_usages):
            self.error = ""
            return True
        grc, gstdout, _ = self._get_mapping(self._active_matching)
        if grc == 0 and _count_mappings(gstdout) >= len(self._active_usages):
            self.error = ""
            return True
        self.active = False
        self.error = (stderr or stdout).strip() or "原按键屏蔽已失效"
        return False

    def clear(self) -> None:
        """Restore the device's original keys and remove the crash-recovery lease."""
        matching = self._active_matching
        self._active_matching = None
        self._active_usages = ()
        self._active_spec = None
        self.active = False
        self.error = ""
        if matching is not None:
            self._clear_matching(matching)
        self._remove_lease()
