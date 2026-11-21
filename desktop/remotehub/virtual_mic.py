"""Virtual-microphone discovery and userspace lifecycle across platforms.

- macOS: CoreAudio HAL plug-in (`RemoteHubAudio.driver`) installed once.
- Windows: user-installed signed virtual audio cable (`VB-CABLE` / `Voicemeeter`).
- Linux: zero-privilege userspace sink/source pair via PipeWire / PulseAudio
  (`pactl` `module-null-sink` + `module-remap-source`, or `pw-loopback`),
  created on demand and torn down cleanly on exit.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

from PySide6.QtCore import QObject, QProcess, Signal

from .branding import BRAND

LINUX_FEED_NODE = "remotehub.feed"
LINUX_MIC_NODE = "remotehub.microphone"


def is_remotehub_output(device):
    """Only our feed may receive remote audio; never fall back to speakers."""
    try:
        raw_id = bytes(device.id())
    except Exception:
        raw_id = b""
    if raw_id in {LINUX_FEED_NODE.encode(), BRAND["microphone_name"].encode(), BRAND["audio_feed_name"].encode()}:
        return True
    return device.description() in {BRAND["microphone_name"], BRAND["audio_feed_name"]}


# Windows cannot host a userland virtual microphone (kernel audio drivers need a
# Microsoft signature), so remote audio is played into a signed virtual cable.
WINDOWS_CABLES = ("cable input", "vb-audio", "voicemeeter input", "virtual cable")


def is_windows_cable(device):
    """Render side only: CABLE Output is the capture side the recognizer uses."""
    name = device.description().casefold()
    if "cable output" in name:
        return False
    return any(item in name for item in WINDOWS_CABLES)


def is_voice_output(device, *, platform: str | None = None):
    """Where remote audio may be played on this platform."""
    plat = platform or sys.platform
    return is_windows_cable(device) if plat == "win32" else is_remotehub_output(device)


class VirtualMic(QObject):
    status = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process: QProcess | None = None
        self._pactl_modules: list[int] = []

    @staticmethod
    def installed():
        """Whether the platform's virtual-mic backend is present on disk."""
        if sys.platform == "darwin":
            return Path(
                "/Library/Audio/Plug-Ins/HAL/RemoteHubAudio.driver/Contents/MacOS/RemoteHubAudio"
            ).is_file()
        if sys.platform.startswith("linux"):
            return bool(shutil.which("pactl") or shutil.which("pw-loopback"))
        return False

    @property
    def active_linux(self) -> bool:
        return bool(self._pactl_modules) or (
            self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning
        )

    def start_linux(self):
        if self.active_linux:
            self.status.emit("虚拟麦克风已在运行")
            return
        pactl = shutil.which("pactl")
        if pactl:
            try:
                self._start_pactl(pactl)
                self.status.emit("已启用虚拟麦克风；退出软件时自动释放")
                return
            except Exception as pactl_exc:
                if not shutil.which("pw-loopback"):
                    raise RuntimeError(f"无法创建虚拟麦克风：{pactl_exc}") from pactl_exc
        self._start_pw_loopback()

    def _cleanup_stale_pactl_modules(self, pactl: str):
        try:
            res = subprocess.run(
                [pactl, "list", "short", "modules"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if res.returncode != 0:
                return
            rows = []
            for line in res.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3 and (LINUX_FEED_NODE in parts[2] or LINUX_MIC_NODE in parts[2]):
                    try:
                        rows.append(int(parts[0]))
                    except ValueError:
                        continue
            for mod_id in reversed(rows):
                subprocess.run([pactl, "unload-module", str(mod_id)], capture_output=True, timeout=3, check=False)
        except Exception:
            pass

    def _start_pactl(self, pactl: str):
        self._cleanup_stale_pactl_modules(pactl)
        feed_desc = BRAND["audio_feed_name"].replace(" ", r"\ ")
        mic_desc = BRAND["microphone_name"].replace(" ", r"\ ")
        sink_res = subprocess.run(
            [
                pactl,
                "load-module",
                "module-null-sink",
                f"sink_name={LINUX_FEED_NODE}",
                f"sink_properties=device.description={feed_desc}",
                "rate=48000",
                "channels=2",
            ],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        )
        sink_id = int(sink_res.stdout.strip())
        try:
            src_res = subprocess.run(
                [
                    pactl,
                    "load-module",
                    "module-remap-source",
                    f"source_name={LINUX_MIC_NODE}",
                    f"master={LINUX_FEED_NODE}.monitor",
                    f"source_properties=device.description={mic_desc}",
                ],
                capture_output=True,
                text=True,
                timeout=4,
                check=True,
            )
            src_id = int(src_res.stdout.strip())
        except Exception:
            subprocess.run([pactl, "unload-module", str(sink_id)], capture_output=True, timeout=3, check=False)
            raise
        self._pactl_modules = [sink_id, src_id]

    def _start_pw_loopback(self):
        executable = shutil.which("pw-loopback")
        if not executable:
            raise RuntimeError("系统缺少 pactl 或 PipeWire pw-loopback 组件，无法创建虚拟麦克风")
        self.process = QProcess(self)
        self.process.readyReadStandardError.connect(self._error)
        self.process.finished.connect(self._finished)
        self.process.start(
            executable,
            [
                "--name=remotehub",
                "--channels=2",
                "--latency=40",
                "--capture-props=media.class=Audio/Sink node.name="
                + LINUX_FEED_NODE
                + " node.description="
                + json.dumps(BRAND["audio_feed_name"])
                + " audio.rate=48000",
                "--playback-props=media.class=Audio/Source node.name="
                + LINUX_MIC_NODE
                + " node.description="
                + json.dumps(BRAND["microphone_name"])
                + " audio.rate=48000",
            ],
        )
        if not self.process.waitForStarted(3000):
            self.stop()
            raise RuntimeError("虚拟麦克风启动失败")
        self.status.emit("已启用虚拟麦克风；退出软件时自动释放")

    def _error(self):
        if self.process:
            text = bytes(self.process.readAllStandardError()).decode(errors="replace").strip()
            if text:
                self.failed.emit(text)

    def _finished(self, *_):
        process, self.process = self.process, None
        if process:
            process.deleteLater()
        self.status.emit("虚拟麦克风已停止")

    def stop(self):
        if self._pactl_modules:
            pactl = shutil.which("pactl")
            if pactl:
                for mod_id in reversed(self._pactl_modules):
                    try:
                        subprocess.run(
                            [pactl, "unload-module", str(mod_id)],
                            capture_output=True,
                            timeout=3,
                            check=False,
                        )
                    except Exception:
                        pass
            self._pactl_modules = []
        process = self.process
        if process:
            self.process = None
            process.terminate()
            if not process.waitForFinished(2000):
                process.kill()
                process.waitForFinished(1000)
            process.deleteLater()
