"""Read-only physical USB inventory. Never open, claim or reset a receiver."""
from dataclasses import dataclass
import json
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import threading
import time
from PySide6.QtCore import QObject, QTimer, Signal

from . import models as model_registry


@dataclass(frozen=True)
class USBDevice:
    vendor: int
    product: int
    serial: str
    name: str
    connection: str
    bcd_device: int | None = None

    @property
    def supported(self):
        return (self.vendor, self.product) in model_registry.supported_pairs()

    @property
    def fingerprint(self):
        return model_registry.fingerprint(self)

    @property
    def display_name(self):
        name = " ".join(self.name.split())
        return name if name and name != "USB 设备" else f"USB {self.vendor:04X}:{self.product:04X}"


def receiver_name(profile, devices=()):
    """Use the model ID, not firmware advertising or an individual serial."""
    return f"USB {profile.vendor:04X}:{profile.product:04X}"


def mac_inventory(payload):
    found = {}

    def walk(entries):
        for d in entries:
            if "idVendor" in d and "idProduct" in d:
                connection = str(d.get("IORegistryEntryID", d.get("locationID", d.get("IORegistryEntryName", ""))))
                bcd = d.get("bcdDevice")
                found[connection] = USBDevice(
                    int(d["idVendor"]),
                    int(d["idProduct"]),
                    str(d.get("USB Serial Number", "")).strip(),
                    str(d.get("USB Product Name", d.get("IORegistryEntryName", "USB 设备"))),
                    connection,
                    int(bcd) if isinstance(bcd, int) else None,
                )
            walk(d.get("IORegistryEntryChildren", []))

    walk(plistlib.loads(payload))
    return list(found.values())


def _parse_windows_usb_row(instance: str, friendly: str, hardware_ids=(), hid_rows=()) -> USBDevice | None:
    match = re.match(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})([^\\]*)\\(.+)$", instance, re.I)
    if not match or "&MI_" in match[3].upper():
        return None
    vendor, product = int(match[1], 16), int(match[2], 16)
    # Windows may manufacture an instance suffix from the USB port. Never
    # call that a serial: only accept a matching HID-reported serial string.
    serial = next(
        (
            str(d["serial_number"])
            for d in hid_rows
            if d.get("vendor_id") == vendor
            and d.get("product_id") == product
            and d.get("serial_number")
            and str(d["serial_number"]).casefold() == match[4].casefold()
        ),
        "",
    )
    bcd = None
    for hw in hardware_ids or ():
        rev = re.search(r"REV_([0-9A-F]{4})\b", str(hw), re.I)
        if rev:
            bcd = int(rev.group(1), 16)
            break
    return USBDevice(vendor, product, serial, friendly or "USB 设备", instance, bcd)


def windows_inventory(payload, hid_rows=()):
    rows = json.loads(payload) if payload.strip() else []
    if isinstance(rows, dict):
        rows = [rows]
    devices = []
    for row in rows:
        hw_ids = row.get("HardwareIds") or ()
        if isinstance(hw_ids, str):
            hw_ids = (hw_ids,)
        parsed = _parse_windows_usb_row(
            row.get("InstanceId", ""),
            row.get("FriendlyName") or "USB 设备",
            hw_ids,
            hid_rows,
        )
        if parsed is not None:
            devices.append(parsed)
    return devices


def windows_setupapi_inventory(setupapi=None, hid_rows=()):
    """In-process read-only Win32 SetupAPI USB enumeration (<1 ms, zero child processes)."""
    import ctypes as c
    from ctypes import wintypes as w

    api = setupapi or c.WinDLL("setupapi", use_last_error=True)

    class SP_DEVINFO_DATA(c.Structure):
        _fields_ = [
            ("cbSize", w.DWORD),
            ("ClassGuid", c.c_byte * 16),
            ("DevInst", w.DWORD),
            ("Reserved", c.c_void_p),
        ]

    if setupapi is None:
        api.SetupDiGetClassDevsW.argtypes = [c.c_void_p, w.LPCWSTR, w.HWND, w.DWORD]
        api.SetupDiGetClassDevsW.restype = c.c_void_p
        api.SetupDiEnumDeviceInfo.argtypes = [c.c_void_p, w.DWORD, c.POINTER(SP_DEVINFO_DATA)]
        api.SetupDiEnumDeviceInfo.restype = w.BOOL
        api.SetupDiGetDeviceInstanceIdW.argtypes = [
            c.c_void_p,
            c.POINTER(SP_DEVINFO_DATA),
            w.LPWSTR,
            w.DWORD,
            c.POINTER(w.DWORD),
        ]
        api.SetupDiGetDeviceInstanceIdW.restype = w.BOOL
        api.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            c.c_void_p,
            c.POINTER(SP_DEVINFO_DATA),
            w.DWORD,
            c.POINTER(w.DWORD),
            c.c_void_p,
            w.DWORD,
            c.POINTER(w.DWORD),
        ]
        api.SetupDiGetDeviceRegistryPropertyW.restype = w.BOOL
        api.SetupDiDestroyDeviceInfoList.argtypes = [c.c_void_p]
        api.SetupDiDestroyDeviceInfoList.restype = w.BOOL

    DIGCF_PRESENT = 0x02
    DIGCF_ALLCLASSES = 0x04
    SPDRP_DEVICEDESC = 0x00
    SPDRP_HARDWAREID = 0x01
    SPDRP_FRIENDLYNAME = 0x0C
    INVALID_HANDLE_VALUE = c.c_void_p(-1).value

    dev_set = api.SetupDiGetClassDevsW(None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if not dev_set or dev_set == INVALID_HANDLE_VALUE:
        raise c.WinError(c.get_last_error())

    def read_prop_strings(info, prop_id: int) -> list[str]:
        reg_type = w.DWORD(0)
        needed = w.DWORD(0)
        buf = c.create_string_buffer(2048)
        ok = api.SetupDiGetDeviceRegistryPropertyW(
            dev_set, c.byref(info), prop_id, c.byref(reg_type), buf, len(buf), c.byref(needed)
        )
        if not ok:
            if needed.value > len(buf) and needed.value <= 65536:
                buf = c.create_string_buffer(needed.value)
                ok = api.SetupDiGetDeviceRegistryPropertyW(
                    dev_set, c.byref(info), prop_id, c.byref(reg_type), buf, len(buf), c.byref(needed)
                )
            if not ok:
                return []
        raw_bytes = buf.raw[: max(0, min(len(buf), int(needed.value)))]
        text = raw_bytes.decode("utf-16-le", errors="ignore")
        return [part.strip() for part in text.split("\x00") if part.strip()]

    devices: list[USBDevice] = []
    try:
        index = 0
        while index < 4096:
            info = SP_DEVINFO_DATA()
            info.cbSize = c.sizeof(SP_DEVINFO_DATA)
            if not api.SetupDiEnumDeviceInfo(dev_set, index, c.byref(info)):
                break
            index += 1
            inst_buf = c.create_unicode_buffer(512)
            req = w.DWORD(0)
            if not api.SetupDiGetDeviceInstanceIdW(dev_set, c.byref(info), inst_buf, len(inst_buf), c.byref(req)):
                continue
            instance = inst_buf.value.strip()
            if not instance.upper().startswith("USB\\VID_"):
                continue
            names = read_prop_strings(info, SPDRP_FRIENDLYNAME) or read_prop_strings(info, SPDRP_DEVICEDESC)
            hw_ids = read_prop_strings(info, SPDRP_HARDWAREID)
            parsed = _parse_windows_usb_row(instance, names[0] if names else "USB 设备", hw_ids, hid_rows)
            if parsed is not None:
                devices.append(parsed)
    finally:
        api.SetupDiDestroyDeviceInfoList(dev_set)
    return devices


def linux_inventory(root=Path("/sys/bus/usb/devices")):
    """Read-only Linux USB sysfs scan; never opens /dev/bus/usb or detaches drivers."""
    root = Path(root)
    if not root.exists():
        return []
    devices = []
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        try:
            if not (path / "idVendor").exists() or not (path / "idProduct").exists():
                continue

            def read(name, default=""):
                file = path / name
                return file.read_text(encoding="utf-8", errors="replace").strip() if file.exists() else default

            raw_bcd = read("bcdDevice")
            bcd = int(raw_bcd, 16) if raw_bcd and re.fullmatch(r"[0-9a-fA-F]{1,4}", raw_bcd) else None
            devices.append(
                USBDevice(
                    int(read("idVendor"), 16),
                    int(read("idProduct"), 16),
                    read("serial"),
                    read("product", "USB 设备") or "USB 设备",
                    path.name,
                    bcd,
                )
            )
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            continue  # hot-unplug during enumeration
    return devices


def list_usb_devices():
    if sys.platform == "darwin":
        return mac_inventory(
            subprocess.run(
                ["/usr/sbin/ioreg", "-r", "-c", "IOUSBHostDevice", "-a"],
                capture_output=True,
                check=True,
                timeout=6,
            ).stdout
        )
    if sys.platform == "win32":
        try:
            return windows_setupapi_inventory()
        except Exception:
            script = (
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                "@(Get-PnpDevice -PresentOnly -ErrorAction Stop | "
                "Where-Object {$_.InstanceId -like 'USB\\VID_*'} | "
                "Select-Object InstanceId,FriendlyName,HardwareIds) | ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                check=True,
                timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return windows_inventory(result.stdout.decode("utf-8-sig"))
    if sys.platform.startswith("linux"):
        return linux_inventory()
    raise RuntimeError("当前系统不支持 USB 设备枚举")


@dataclass(frozen=True)
class DeviceState:
    code: str
    title: str
    detail: str
    usable: bool = False


def receiver_state(profile, devices, *, checked=False, error="", keys_ready=False, voice_ready=False):
    if error:
        return DeviceState("error", "USB 检测失败", error)
    if not checked:
        return DeviceState("checking", "尚未完成 USB 检测", "正在读取系统 USB 设备清单。")
    vidpid = f"VID {profile.vendor:04X} / PID {profile.product:04X}"
    if (profile.vendor, profile.product) not in model_registry.supported_pairs():
        return DeviceState("unsupported", "当前型号尚未适配", vidpid + "；不能仅靠修改型号编号认定已支持。")
    candidates = [d for d in devices if (d.vendor, d.product) == (profile.vendor, profile.product)]
    if not candidates:
        return DeviceState(
            "absent",
            "未发现这款接收器",
            "请确认接收器插在电脑的 USB 接口上，或更换接口后重新检测。支持型号：" + vidpid + "。",
        )
    device = candidates[0]
    if model_registry.same_dongle(device):
        # Same vendor/product, different bcd or serial: another remote on the
        # same dongle scheme. Never reuse the adapted key table for it.
        return DeviceState(
            "unadapted",
            "检测到未适配的遥控器",
            f"{model_registry.fingerprint(device)}；与已适配型号的版本或序列号不一致，按键和语音参数不能沿用。",
        )
    if voice_ready:
        return DeviceState("voice_ready", "USB 语音通道已接入", vidpid, True)
    if keys_ready:
        return DeviceState("keys_ready", "遥控器按键已接入", vidpid, True)
    return DeviceState("detected", "已识别适配接收器", "USB 已识别，按键监听尚未接入；可点「重新检测」重试。", True)


class USBMonitor(QObject):
    changed = Signal()
    _result = Signal(object, str)

    def __init__(self, parent=None, enumerate_devices=list_usb_devices):
        super().__init__(parent)
        self.enumerate = enumerate_devices
        self.devices = []
        self.error = ""
        self.checked = False
        self.updated = 0.0
        self.busy = False
        self.closed = False
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self._result.connect(self.apply)

    def start(self):
        self.timer.start()
        self.refresh()

    def refresh(self):
        if self.closed or self.busy:
            return
        self.busy = True

        def collect():
            try:
                devices, error = self.enumerate(), ""
            except Exception as exc:
                devices, error = [], str(exc)
            if not self.closed:
                try:
                    self._result.emit(devices, error)
                except RuntimeError:
                    pass  # window destroyed during a read-only scan

        threading.Thread(target=collect, daemon=True, name="remotehub-usb-inventory").start()

    def apply(self, devices, error=""):
        self.busy = False
        if self.closed:
            return
        self.devices = devices
        self.error = error
        self.checked = True
        self.updated = time.monotonic()
        self.changed.emit()

    def stop(self):
        self.closed = True
        self.timer.stop()
