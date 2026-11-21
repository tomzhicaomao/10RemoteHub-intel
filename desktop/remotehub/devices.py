"""Input adapters return device-scoped events, never learn a global keyboard."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import sys
from PySide6.QtCore import QObject, Signal

from .hid_reports import HIDReports


def resources():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


class DeviceInput(QObject):
    key = Signal(str, bool)
    status = Signal(str)
    failed = Signal(str)
    stopped = Signal(int)
    ready = Signal(int)
    collected = Signal(object)
    capture_stopped = Signal()
    log = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mac = None
        self.release_confirmed = True
        self.capture_ready = False
        self.reports = {}
        self.windows = None
        self.linux_evdev = None
        self.collector = None
        self.generation = 0
        self.ignored_reports = 0
        self._linux_ready_sent = False
        self._linux_evdev_failed = ""
        self._linux_collector_failed = ""

    def start(self, profile, window=None):
        self.stop()
        generation = self.generation
        if sys.platform == "darwin":
            from .mac_device_helper import MacDeviceHelper
            if (profile.vendor, profile.product) != (0x0416, 0x0602):
                raise RuntimeError("采集程序只支持 USB 0416:0602")
            self.mac = MacDeviceHelper(self)
            self.mac.line.connect(lambda line, g=generation: self._event(line, g))
            self.mac.failed.connect(lambda message, g=generation: self._mac_failed(message, g))
            self.mac.start()
        elif sys.platform == "win32":
            from .windows_collector import WindowsCollector
            from .windows_input import WindowsInput
            self.windows = WindowsInput(
                profile,
                self.key.emit,
                self.status.emit,
                window,
                self._unmapped_key,
            )
            self.windows.start()
            # Keep the receiver's own voice session alive: the dongle re-enumerates
            # on a voice-key release unless the host closes the session itself.
            self.collector = WindowsCollector(self)
            self.collector.collected.connect(self.collected)
            self.collector.status.connect(self.status)
            self.collector.log.connect(self.log)
            self.collector.key.connect(self.key)
            self.collector.keys_ready.connect(self._keys_from_collector)
            self.collector.ready.connect(lambda g=generation: self._win_ready(g))
            self.collector.failed.connect(lambda message, g=generation: self._win_failed(message, g))
            self.collector.start()
            self.ready.emit(generation)
        elif sys.platform.startswith("linux"):
            from .linux_collector import LinuxCollector, LinuxEvdevInput
            self._linux_ready_sent = False
            self._linux_evdev_failed = ""
            self._linux_collector_failed = ""
            if (profile.vendor, profile.product) == (0x0416, 0x0602):
                self.collector = LinuxCollector(self)
                self.collector.collected.connect(self.collected)
                self.collector.status.connect(self.status)
                self.collector.log.connect(self.log)
                self.collector.key.connect(self.key)
                self.collector.keys_ready.connect(self._keys_from_collector)
                self.collector.ready.connect(lambda g=generation: self._linux_collector_ready(g))
                self.collector.failed.connect(lambda message, g=generation: self._linux_collector_error(message, g))
                self.collector.start()
            else:
                self._start_linux_evdev(profile, generation)
        else:
            raise RuntimeError("当前系统暂不支持遥控器输入采集")

    def _start_linux_evdev(self, profile, generation):
        from .linux_collector import LinuxEvdevInput
        if self.linux_evdev is not None:
            return
        self.linux_evdev = LinuxEvdevInput(profile, self)
        self.linux_evdev.key.connect(self.key)
        self.linux_evdev.status.connect(self.status)
        self.linux_evdev.ready.connect(lambda g=generation: self._linux_evdev_ready(g))
        self.linux_evdev.failed.connect(lambda message, g=generation: self._linux_evdev_error(message, g))
        self.linux_evdev.start()

    def _linux_collector_ready(self, generation):
        if generation != self.generation:
            return
        self.capture_ready = True
        self._linux_ready_sent = True
        self.status.emit("遥控器语音通道已接入")
        self.ready.emit(generation)

    def _linux_evdev_ready(self, generation):
        if generation != self.generation:
            return
        if not self._linux_ready_sent:
            self._linux_ready_sent = True
            self.ready.emit(generation)

    def _linux_collector_error(self, message, generation):
        if generation != self.generation:
            return
        was_ready = self.capture_ready
        self.capture_ready = False
        if was_ready:
            self.capture_stopped.emit()
        self._linux_collector_failed = message
        # If libusb could not claim the receiver (e.g. only /dev/input is accessible),
        # try evdev key fallback before declaring overall capture failure.
        if not self._linux_ready_sent and self.linux_evdev is None:
            from .model import Profile
            self._start_linux_evdev(Profile(vendor=0x0416, product=0x0602), generation)
            return
        if self.linux_evdev is not None and self.linux_evdev.active:
            self.status.emit(message)
            return
        self.failed.emit(message)
        self.stopped.emit(generation)

    def _linux_evdev_error(self, message, generation):
        if generation != self.generation:
            return
        self._linux_evdev_failed = message
        if self.capture_ready:
            return
        primary = self._linux_collector_failed or message
        self.failed.emit(primary)
        self.stopped.emit(generation)

    def _keys_from_collector(self, keyboard_taken):
        """Keyboard reports come from the collector; fallback input stays muted."""
        if self.windows is not None:
            self.windows.keyboard_muted = bool(keyboard_taken)
        if self.linux_evdev is not None:
            self.linux_evdev.keyboard_muted = bool(keyboard_taken)
        if keyboard_taken:
            self.status.emit("遥控器按键已由采集程序接管")

    def _unmapped_key(self, scan, flags, vk):
        self.log.emit(f"unmapped keyboard scan={scan} flags={flags} vk={vk}")

    def _win_ready(self, generation):
        if generation != self.generation:
            return
        self.capture_ready = True
        self.status.emit("遥控器语音通道已接入")
        # The collector finishes its handshake after key input is already up, so
        # re-announce readiness; the controller only then can attach audio.
        self.ready.emit(generation)

    def _win_failed(self, message, generation):
        """Voice interface problems must never take key input down with them."""
        if generation != self.generation:
            return
        was_ready = self.capture_ready
        self.capture_ready = False
        if was_ready:
            self.capture_stopped.emit()
        self.status.emit(message)

    def voice_edge(self, down):
        if self.collector is not None:
            self.collector.voice_edge(down)

    def _mac_failed(self, message, generation):
        if generation != self.generation:
            return
        self.capture_ready = False
        self.capture_stopped.emit()
        self.failed.emit(message)
        if self.mac:
            self.release_confirmed = self.mac.stop()
        self.stopped.emit(generation)

    def _event(self, line, generation):
        if generation != self.generation:
            return
        try:
            d = json.loads(line)
            if d["type"] == "descriptor":
                self.reports[d["interface"]] = HIDReports(bytes.fromhex(d["hex"]))
            elif d["type"] == "report":
                try:
                    parser = self.reports[d["interface"]]
                    events = parser.decode(bytes.fromhex(d["hex"]))
                except (ValueError, KeyError, TypeError):
                    # Bad input is not a device/transport failure. Do not clear
                    # parser state or synthesize key releases from partial data.
                    self.ignored_reports += 1
                    if self.ignored_reports == 1 or self.ignored_reports % 100 == 0:
                        logging.getLogger(__name__).warning("Ignored malformed HID reports: %d", self.ignored_reports)
                    return
                for trigger, down in events:
                    self.key.emit(trigger, down)
            elif d["type"] in ("audio", "start", "end", "warning"):
                self.collected.emit(d)
            elif d["type"] == "key":
                self.key.emit(d["trigger"], d["down"])
            elif d["type"] == "debug":
                message = f"{d['event']}:phase={d['phase']},code={d['code']}"
                if "hex" in d:
                    message += f",hex={d['hex']}"
                self.log.emit(message)
            elif d["type"] == "ready" and d["interfaces"] > 0:
                self.release_confirmed = True
                self.capture_ready = True
                if self.mac:
                    self.mac.confirmed = True
                self.status.emit("遥控器按键已连接")
                self.ready.emit(generation)
            elif d["type"] == "error":
                self._mac_failed(d["message"], generation)
            elif d["type"] == "removed":
                self._mac_failed("遥控器已断开，请重新检测", generation)
        except (ValueError, KeyError, TypeError):
            self._mac_failed("采集程序返回了无效数据", generation)

    def record(self, active):
        if self.collector is not None and self.capture_ready:
            self.collector.record(active)
            return
        if not self.mac or not self.capture_ready:
            raise RuntimeError("采集程序尚未连接")
        self.mac.record(active)

    def stop(self):
        self.capture_ready = False
        self.reports = {}
        self.capture_stopped.emit()
        self.generation += 1
        if self.mac:
            if not self.mac.stop():
                self.release_confirmed = False
            self.mac.line.disconnect()
            self.mac.failed.disconnect()
            self.mac.deleteLater()
            self.mac = None
        if self.windows:
            self.windows.stop()
            self.windows = None
        if self.linux_evdev:
            self.linux_evdev.stop()
            self.linux_evdev.deleteLater()
            self.linux_evdev = None
        if self.collector:
            self.collector.stop()
            self.collector.deleteLater()
            self.collector = None
        return self.release_confirmed
