"""Switch the Windows default capture device for the duration of a take.

The remote's audio is played into a virtual cable; input methods record from the
system default microphone, so the default has to point at the cable while the
voice key is held and go back afterwards. Uses the undocumented IPolicyConfig
interface, which is what every "set default device" tool on Windows uses.
"""
from __future__ import annotations

import contextlib
import ctypes
import re
import time
from ctypes import wintypes as w

from PySide6.QtCore import QObject, Signal

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
CLSID_CPolicyConfigClient = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
IID_IPolicyConfig = "{F8679F50-850A-41CF-9C72-430F290290C8}"

CLSCTX_ALL = 0x17
eCapture, eRender = 0x1, 0x0
eConsole, eMultimedia, eCommunications = 0, 1, 2
ROLES = (eConsole, eMultimedia, eCommunications)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def guid(text):
    value = GUID()
    if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(value)) != 0:
        raise ValueError(f"bad GUID {text}")
    return value


def _method(pointer, index, *argtypes):
    """Resolve the index-th vtable function and declare its signature."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return prototype(vtable[index])


@contextlib.contextmanager
def _com_apartment():
    ole32 = getattr(getattr(ctypes, "windll", None), "ole32", None)
    hr = ole32.CoInitialize(None) if ole32 is not None else -1
    try:
        yield
    finally:
        if ole32 is not None and hr in (0, 1):
            ole32.CoUninitialize()


def _create(clsid, iid):
    pointer = ctypes.c_void_p()
    hr = ctypes.windll.ole32.CoCreateInstance(
        ctypes.byref(guid(clsid)), None, CLSCTX_ALL, ctypes.byref(guid(iid)), ctypes.byref(pointer))
    if hr != 0:
        raise OSError(f"CoCreateInstance failed 0x{hr & 0xFFFFFFFF:08x}")
    return pointer


def current_default(capture=True, role=eConsole):
    """Endpoint id of the current default device, or '' when unavailable."""
    with _com_apartment():
        enumerator = _create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
        try:
            # vtable order: EnumAudioEndpoints, GetDefaultAudioEndpoint, ...
            get_default = _method(enumerator, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            device = ctypes.c_void_p()
            if get_default(enumerator, eCapture if capture else eRender, role, ctypes.byref(device)) != 0:
                return ""
            try:
                get_id = _method(device, 5, ctypes.POINTER(ctypes.c_wchar_p))
                value = ctypes.c_wchar_p()
                if get_id(device, ctypes.byref(value)) != 0:
                    return ""
                try:
                    return value.value or ""
                finally:
                    if ctypes.cast(value, ctypes.c_void_p).value:
                        ctypes.windll.ole32.CoTaskMemFree(value)
            finally:
                release = _method(device, 2)
                release(device)
        finally:
            release = _method(enumerator, 2)
            release(enumerator)


def set_endpoint(endpoint_id, role):
    """Point one role at this endpoint id."""
    with _com_apartment():
        policy = _create(CLSID_CPolicyConfigClient, IID_IPolicyConfig)
        try:
            set_endpoint_method = _method(policy, 13, ctypes.c_wchar_p, ctypes.c_int)
            hr = set_endpoint_method(policy, endpoint_id, role)
            if hr & 0x80000000:
                raise OSError(f"系统麦克风切换失败（0x{hr & 0xFFFFFFFF:08X}）")
        finally:
            release = _method(policy, 2)
            release(policy)


def set_default(endpoint_id):
    """Point every role at this endpoint id."""
    with _com_apartment():
        for role in ROLES:
            set_endpoint(endpoint_id, role)


def defaults(capture=True):
    """Current endpoint id for every role."""
    with _com_apartment():
        return {role: current_default(capture, role) for role in ROLES}


def qt_id_to_endpoint(qt_id: bytes) -> str:
    """Qt exposes the Windows endpoint id as its device id bytes."""
    try:
        return qt_id.decode("utf-16-le").strip("\x00")
    except UnicodeDecodeError:
        return qt_id.decode("utf-8", "ignore")


def _is_cable_capture(description: str) -> bool:
    name = description.casefold()
    if "cable input" in name:
        return False
    return ("cable output" in name
            or ("vb-audio" in name and "output" in name)
            or ("voicemeeter" in name and "output" in name)
            or ("virtual cable" in name and "output" in name))


def _adapter_group(description: str) -> str:
    text = description.strip().casefold()
    if not text.endswith(")"):
        return ""
    depth = 0
    for index in range(len(text) - 1, -1, -1):
        if text[index] == ")":
            depth += 1
        elif text[index] == "(":
            depth -= 1
            if depth == 0:
                return text[index:]
    return ""


def _instance_prefix(description: str) -> str:
    for sample in (description.strip().casefold(), _adapter_group(description).lstrip("(")):
        match = re.match(r"^(\d+\s*-\s*)", sample)
        if match:
            return re.sub(r"\s+", "", match.group(1))
    return ""


def match_cable_input(inputs, output_description: str = "") -> str:
    """Choose the capture endpoint paired with the selected virtual-cable render device."""
    candidates = [(desc, endpoint) for desc, endpoint in inputs if endpoint and _is_cable_capture(desc)]
    if not candidates:
        return ""
    target = (output_description or "").strip()
    if target:
        expected = re.sub(r"\binput\b", "output", target.casefold())
        for desc, endpoint in candidates:
            if desc.strip().casefold() == expected:
                return endpoint
        group = _adapter_group(target)
        if group:
            for desc, endpoint in candidates:
                if _adapter_group(desc) == group:
                    return endpoint
        prefix = _instance_prefix(target)
        if prefix:
            for desc, endpoint in candidates:
                if _instance_prefix(desc) == prefix:
                    return endpoint
    return candidates[0][1]


class WindowsInputRoute(QObject):
    """Temporarily point every default microphone role at the cable.

    Mirrors the macOS router: the input method keeps using the system default,
    so it does not have to be configured for our device, and the previous
    defaults are restored the moment the take ends.
    """

    ready = Signal()
    failed = Signal(str)
    released = Signal()
    measured = Signal(str, float)

    def __init__(self, parent=None, find=None, *, output_name=None):
        super().__init__(parent)
        self._output_name = output_name
        self.find = find or self._find_cable
        self.saved = {}
        self.targets = {}
        self.state = "idle"
        self._failing = False

    def _find_cable(self):
        from PySide6.QtMultimedia import QMediaDevices
        selected = self._output_name() if callable(self._output_name) else (self._output_name or "")
        inputs = [(device.description(), qt_id_to_endpoint(bytes(device.id())))
                  for device in QMediaDevices.audioInputs()]
        return match_cable_input(inputs, selected)

    def acquire(self):
        if self.state == "acquired":
            self.ready.emit()
            return
        started = time.monotonic()
        try:
            if self.saved:
                self._restore()  # Retry a failed restoration before taking another lease.
            endpoint = self.find()
            if not endpoint:
                raise RuntimeError("没有找到 CABLE Output 虚拟麦克风，请确认 VB-CABLE 已安装")
            previous = defaults(capture=True)
            if any(not value for value in previous.values()):
                raise RuntimeError("无法读取原默认麦克风，本次未切换")
            for role, original in previous.items():
                if original == endpoint:
                    continue  # Already the user's choice; we do not own this role.
                self.saved[role] = original
                self.targets[role] = endpoint
                set_endpoint(endpoint, role)
                if current_default(True, role) != endpoint:
                    raise RuntimeError("系统麦克风切换未确认，本次语音已取消")
            self.state = "acquired"
            self.measured.emit("acquire", (time.monotonic() - started) * 1000)
            self.ready.emit()
        except Exception as exc:
            message = str(exc)
            try:
                self._restore()
            except Exception as restore_error:
                message += "；恢复失败：" + str(restore_error)
            self._failing = True
            try:
                self.failed.emit(message)
            finally:
                self._failing = False

    def _restore(self):
        errors = []
        for role, original in list(self.saved.items()):
            try:
                # A later user/system selection outranks our temporary lease.
                current = current_default(True, role)
                if not current:
                    raise RuntimeError("无法读取当前默认麦克风，保留恢复状态供重试")
                if current == self.targets.get(role) and current != original:
                    set_endpoint(original, role)
                    if current_default(True, role) != original:
                        raise RuntimeError("原麦克风恢复未确认，请在系统设置检查")
                self.saved.pop(role)
                self.targets.pop(role, None)
            except Exception as exc:
                errors.append(str(exc))  # Still restore the other roles; keep failed ones retryable.
        self.state = "restore_failed" if self.saved else "idle"
        if errors:
            raise RuntimeError("；".join(errors))

    def release(self):
        if self._failing:
            self.released.emit()
            return
        started = time.monotonic()
        try:
            self._restore()
            self.measured.emit("release", (time.monotonic() - started) * 1000)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.released.emit()

    def close(self):
        """Restore any outstanding microphone lease during application shutdown."""
        if self.saved or self.state != "idle":
            self.release()
