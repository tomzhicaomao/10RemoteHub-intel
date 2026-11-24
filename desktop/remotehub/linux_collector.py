"""Linux unified USB collector (libusb) + evdev fallback for 0416:0602.

Primary path (`LinuxCollector`): detaches `usbhid` per interface on 0416:0602,
claims interfaces 1/2/3/4 over libusb, parses raw HID reports with `HIDReports`,
speaks the vendor voice protocol on interface 3 (0x84 IN / 0x05 OUT), and
re-attaches kernel drivers on stop/exit. Requires only a VID/PID-scoped udev
`uaccess` rule (never global root or `/dev/input` group access to all keyboards).

Fallback path (`LinuxEvdevInput`): reads device-scoped `/dev/input/event*` via
`EVIOCGRAB` when raw `/dev/bus/usb` access is unavailable or for generic models.
"""
from __future__ import annotations

import queue
import select
import threading
import time

from PySide6.QtCore import QObject, Signal

from .input_codes import LinuxKeys

VOICE_INTERFACE = 3
KEY_INTERFACES = (1, 4)
MOUSE_INTERFACE = 2
READ_ENDPOINT = 0x84
WRITE_ENDPOINT = 0x05
RELEASE_GAP = 0.45


def _frame(*prefix):
    return bytes(prefix) + bytes(64 - len(prefix))


class LinuxEvdevInput(QObject):
    """Device-scoped evdev reader; muted automatically when libusb owns interface 4."""

    key = Signal(str, bool)
    status = Signal(str)
    failed = Signal(str)
    ready = Signal()

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.keyboard_muted = False
        self.worker = None
        self.halt = threading.Event()
        self.active = False

    def start(self):
        self.stop()
        self.halt.clear()
        self.worker = threading.Thread(
            target=self._run,
            name="remotehub-linux-evdev",
            daemon=True,
        )
        self.worker.start()

    def stop(self):
        self.halt.set()
        if self.worker:
            self.worker.join(2.0)
            self.worker = None
        self.active = False

    def _run(self):
        devices = []
        normalizers = {}
        try:
            from evdev import InputDevice, ecodes, list_devices

            for path in list_devices():
                try:
                    dev = InputDevice(path)
                except OSError:
                    continue
                if dev.info.vendor != self.profile.vendor or dev.info.product != self.profile.product:
                    dev.close()
                    continue
                if ecodes.EV_KEY not in dev.capabilities():
                    dev.close()
                    continue
                try:
                    dev.grab()
                except OSError:
                    dev.close()
                    continue
                devices.append(dev)
                normalizers[dev.fd] = LinuxKeys()
            if not devices:
                self.failed.emit("未找到可读取的遥控器输入节点；请确认 udev 规则已配置")
                return
            self.active = True
            self.status.emit("遥控器按键已通过 evdev 接入")
            self.ready.emit()
            receiver_pair = (self.profile.vendor, self.profile.product) == (0x0416, 0x0602)
            while not self.halt.is_set():
                if self.keyboard_muted and receiver_pair:
                    self.halt.wait(0.15)
                    continue
                try:
                    readable, _, _ = select.select(devices, [], [], 0.1)
                except (OSError, ValueError):
                    break
                for dev in readable:
                    try:
                        events = list(dev.read())
                    except OSError:
                        return
                    for event in events:
                        if self.keyboard_muted and receiver_pair:
                            continue
                        if receiver_pair:
                            for trigger, down in normalizers[dev.fd].feed(event.type, event.code, event.value):
                                self.key.emit(trigger, down)
                        elif event.type == ecodes.EV_KEY and event.value != 2:
                            self.key.emit(f"ev:{event.code}", event.value == 1)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.active = False
            for dev in devices:
                try:
                    dev.ungrab()
                except OSError:
                    pass
                try:
                    dev.close()
                except OSError:
                    pass


class LinuxCollector(QObject):
    """Unified libusb collector for 0416:0602 on Linux (keys + vendor voice)."""

    collected = Signal(object)
    status = Signal(str)
    failed = Signal(str)
    ready = Signal()
    log = Signal(str)
    key = Signal(str, bool)
    keys_ready = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.halt = threading.Event()
        self.capture_ready = False
        self._lock = threading.Lock()
        self._request = None
        self.frames = queue.Queue()
        self._claimed: list[int] = []
        self._detached: list[int] = []

    def start(self):
        self.stop()
        self.halt.clear()
        self.worker = threading.Thread(target=self._supervise, name="remotehub-linux-collector", daemon=True)
        self.worker.start()

    def _supervise(self):
        quick = 0
        while not self.halt.is_set():
            started = time.monotonic()
            perm_denied = False
            try:
                perm_denied = bool(self._run())
            except Exception as exc:
                self._log(f"worker error: {exc!r}")
                self.failed.emit(f"遥控器采集通道中断：{exc}")
            self.capture_ready = False
            self._clear_session()
            if self.halt.is_set() or perm_denied:
                break
            quick = quick + 1 if time.monotonic() - started < 2 else 0
            if quick >= 3:
                self._log("collector keeps failing; giving up until manual retry")
                self.failed.emit("遥控器 USB 接口反复断开，请拔插接收器或检查 udev 规则后重新检测。")
                break
            self._log(f"collector ended; retry {quick}")
            self.halt.wait(1.5 if quick < 2 else 5)

    def voice_edge(self, down):
        with self._lock:
            self._request = bool(down)

    def record(self, active):
        with self._lock:
            self._request = bool(active)

    def _clear_session(self):
        with self._lock:
            self._request = None
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break

    def stop(self):
        self.halt.set()
        if self.worker:
            self.worker.join(3.0)
            self.worker = None
        self.capture_ready = False
        self._clear_session()

    def _emit(self, payload):
        try:
            self.collected.emit(payload)
        except RuntimeError:
            pass

    def _log(self, message):
        try:
            self.log.emit(message)
        except RuntimeError:
            pass

    def _voice_loop(self, endpoint, stop):
        import usb.core
        while not (self.halt.is_set() or stop.is_set()):
            try:
                frame = bytes(endpoint.read(64, timeout=200))
            except usb.core.USBTimeoutError:
                continue
            except Exception as exc:
                if not (self.halt.is_set() or stop.is_set()):
                    self._log(f"voice read stopped: {exc}")
                return
            if frame:
                while self.frames.qsize() > 64:
                    self.frames.get_nowait()
                self.frames.put(frame)

    def _key_loop(self, endpoint, parser, stop):
        import usb.core
        while not (self.halt.is_set() or stop.is_set()):
            try:
                report = bytes(endpoint.read(64, timeout=200))
            except usb.core.USBTimeoutError:
                continue
            except Exception as exc:
                if not (self.halt.is_set() or stop.is_set()):
                    self._log(f"key read stopped: {exc}")
                return
            if not report:
                continue
            try:
                events = parser.decode(report)
            except ValueError as exc:
                self._log(f"key report rejected: {type(exc).__name__}")
                continue
            for trigger, down in events:
                self.key.emit(trigger, down)

    def _detach_if_active(self, device, number: int):
        try:
            if device.is_kernel_driver_active(number):
                device.detach_kernel_driver(number)
                self._detached.append(number)
        except (NotImplementedError, Exception) as exc:
            self._log(f"detach check interface {number}: {exc}")

    def _claim_keys(self, device):
        import usb.util
        from .hid_reports import HIDReports

        readers = []
        claimed = []
        configuration = device.get_active_configuration()
        for number in KEY_INTERFACES:
            try:
                self._detach_if_active(device, number)
                interface = configuration[(number, 0)]
                usb.util.claim_interface(device, number)
                claimed.append(number)
                descriptor = bytes(device.ctrl_transfer(0x81, 6, 0x2200, number, 4096, timeout=1000))
                parser = HIDReports(descriptor)
                endpoint = next(e for e in interface if e.bEndpointAddress & 0x80)
            except Exception as exc:
                self._log(f"interface {number} unavailable to libusb: {exc}")
                continue
            readers.append((endpoint, parser))
            self._log(f"interface {number} claimed")
        try:
            self._detach_if_active(device, MOUSE_INTERFACE)
            usb.util.claim_interface(device, MOUSE_INTERFACE)
            claimed.append(MOUSE_INTERFACE)
            self._log("interface 2 claimed (pointer parked)")
        except Exception as exc:
            self._log(f"interface 2 stays with kernel: {exc}")
        self._claimed = claimed
        return readers

    def _release_device(self, device, usb_util, voice_claimed):
        if device is None:
            return
        interfaces = list(self._claimed)
        if voice_claimed:
            interfaces.append(VOICE_INTERFACE)
        for number in reversed(interfaces):
            try:
                usb_util.release_interface(device, number)
            except Exception:
                pass
        for number in reversed(self._detached):
            try:
                device.attach_kernel_driver(number)
            except Exception as exc:
                self._log(f"reattach interface {number} failed: {exc}")
        self._claimed = []
        self._detached = []
        try:
            usb_util.dispose_resources(device)
        except Exception:
            pass

    def _run(self) -> bool:
        """Return True if failure is due to permission denial (do not auto-spin)."""
        try:
            import usb.core
            import usb.util
            from usb.backend import libusb1
            try:
                import libusb_package
                backend = libusb1.get_backend(find_library=libusb_package.find_library) or libusb1.get_backend()
            except Exception:
                backend = libusb1.get_backend()
        except Exception as exc:
            self.failed.emit(f"缺少 USB 运行库：{exc}")
            return True

        device = None
        voice_claimed = False
        self._claimed = []
        self._detached = []
        try:
            devices = list(usb.core.find(find_all=True, idVendor=0x0416, idProduct=0x0602, backend=backend))
            if not devices:
                raise RuntimeError("未找到适配的 USB 0416:0602 接收器")
            device = devices[0]
            self._detach_if_active(device, VOICE_INTERFACE)
            interface = device.get_active_configuration()[(VOICE_INTERFACE, 0)]
            usb.util.claim_interface(device, VOICE_INTERFACE)
            voice_claimed = True
            reader = next(e for e in interface if e.bEndpointAddress == READ_ENDPOINT)
            self._log("interface 3 claimed")
            readers = self._claim_keys(device)
        except Exception as exc:
            self._release_device(device, usb.util, voice_claimed)
            text = str(exc)
            is_perm = any(tok in text.lower() for tok in ("access denied", "insufficient permissions", "errno 13", "permission"))
            self._log(f"claim failed: {exc!r}")
            if is_perm:
                self.failed.emit(
                    "无权访问遥控器 USB 接口（Errno 13）。请在「通用设置」按提示安装 70-10remotehub.rules udev 规则后重新检测。"
                )
                return True
            self.failed.emit(f"无法接入遥控器 USB 接口：{exc}")
            return False

        drain_until = time.monotonic() + 0.3
        while time.monotonic() < drain_until:
            try:
                reader.read(64, timeout=60)
            except usb.core.USBTimeoutError:
                pass

        stop = threading.Event()
        readers_threads = [
            threading.Thread(
                target=self._key_loop,
                args=(endpoint, parser, stop),
                name=f"remotehub-linux-keys-{i}",
                daemon=True,
            )
            for i, (endpoint, parser) in enumerate(readers)
        ]
        for thread in readers_threads:
            thread.start()
        voice_thread = threading.Thread(
            target=self._voice_loop,
            args=(reader, stop),
            name="remotehub-linux-voice",
            daemon=True,
        )
        voice_thread.start()
        self.keys_ready.emit(any(number == 4 for number in self._claimed))

        def command(*prefix):
            try:
                device.write(WRITE_ENDPOINT, _frame(*prefix), timeout=1000)
            except Exception as exc:
                self._log(f"command {bytes(prefix).hex()} failed: {exc}")
                raise
            self._log(f"out {bytes(prefix).hex()} phase={phase}")

        phase = 0
        keepalive = 0.0
        deadline = 0.0
        stop_deadline = 0.0
        stop_sent_at = 0.0
        session_start = 0.0
        alternate_sent = False
        last_audio = 0.0
        try:
            command(1, 8, 1, 0)
            self.capture_ready = True
            self._log("ready handshake sent")
            self.ready.emit()
            self.status.emit("遥控器按键与语音接口已就绪")
            while not self.halt.is_set():
                with self._lock:
                    request, self._request = self._request, None
                now = time.monotonic()
                if request is not None:
                    self._log(f"request {'start' if request else 'stop'} phase={phase}")
                if request is True and phase == 0:
                    command(1, 0x82, 1, 0)
                    command(1, 8, 1, 0)
                    phase = 1
                    deadline = now + 8
                elif request is False and phase:
                    command(1, 1, 0, 0)
                    phase = 4
                    alternate_sent = False
                    stop_deadline = now + 0.4
                    stop_sent_at = now
                    self._emit({"type": "end"})
                try:
                    data = self.frames.get(timeout=0.05)
                except queue.Empty:
                    data = b""
                now = time.monotonic()
                if data and data[:2] != b"\x01\x81":
                    self._log(f"in {data[:4].hex()}")
                if data:
                    if data[:2] == b"\x01\x81":
                        last_audio = now
                        if phase == 4 and not alternate_sent and now - stop_sent_at > 0.2:
                            alternate_sent = True
                            self._log("audio after stop; sending alternate stop")
                            command(1, 0, 0)
                        if phase >= 3:
                            self._emit({"type": "audio", "hex": data.hex()})
                    elif (
                        (data[:2] == b"\x01\x86" and (len(data) < 3 or data[2] != 0))
                        or data[:3] == b"\x01\x82\x03"
                    ) and phase == 0:
                        self._log("voice key frame from receiver")
                        command(1, 0x82, 1, 0)
                        command(1, 8, 1, 0)
                        phase = 1
                        deadline = now + 8
                    elif data[:3] == b"\x01\x86\x00" and phase and phase != 4:
                        self._log("remote ended voice session")
                        command(1, 1, 0, 0)
                        if phase == 3:
                            phase = 4
                            alternate_sent = False
                            stop_deadline = now + 0.1
                            stop_sent_at = now
                        else:
                            phase = 0
                        self._emit({"type": "end"})
                    elif data[:4] == b"\x01\x08\x01\x00" and phase == 1:
                        command(1, 1, 1, 0)
                        phase = 2
                    elif data[:4] == b"\x01\x01\x01\x00" and phase == 2:
                        phase = 3
                        session_start = now
                        last_audio = 0.0
                        keepalive = 0.0
                        deadline = now + 60
                        self._emit({"type": "start"})
                if phase == 4 and now >= stop_deadline:
                    phase = 0
                if phase == 3 and not last_audio and now - session_start > 2.0:
                    self._log("no audio after start; closing session")
                    command(1, 1, 0, 0)
                    phase = 0
                    self._emit({"type": "end"})
                if phase == 3 and last_audio and now - last_audio > RELEASE_GAP:
                    self._log(f"audio stopped {now - last_audio:.2f}s; closing session")
                    command(1, 1, 0, 0)
                    phase = 0
                    self._emit({"type": "end"})
                if phase == 3 and now >= keepalive:
                    command(1, 10, 84, 69)
                    keepalive = now + 1
                if phase and now >= deadline:
                    command(1, 1, 0, 0)
                    handshake = phase < 3
                    phase = 0
                    if handshake:
                        self._emit({"type": "warning", "message": "voice handshake timeout"})
                    self._emit({"type": "end"})
        except Exception as exc:
            self._log(f"session error: {exc!r}")
            self.failed.emit(f"遥控器采集通道中断：{exc}")
        finally:
            stop.set()
            self.capture_ready = False
            for thread in (*readers_threads, voice_thread):
                thread.join(0.35)
            if phase:
                try:
                    device.write(WRITE_ENDPOINT, _frame(1, 1, 0, 0), timeout=1000)
                except Exception:
                    pass
            self._release_device(device, usb.util, voice_claimed)
        return False
