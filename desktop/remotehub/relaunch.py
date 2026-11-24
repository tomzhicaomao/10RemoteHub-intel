"""Reopen the app after it quits, e.g. so a newly installed virtual microphone shows up.

macOS restarts its audio service when the microphone driver is installed, and a
running app keeps the device list it had before; only a new process sees the device.
The new copy waits for this one to exit, since only one instance may run.
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QProcess

# Positional arguments after the script are the command to start once ``$1`` (the pid) is gone.
_WAIT_THEN_RUN = 'pid="$1"; shift; while /bin/kill -0 "$pid" 2>/dev/null; do /bin/sleep 0.2; done; exec "$@"'


def launch_command(executable: str, argv: list[str]) -> list[str]:
    """Command that starts this app again: the bundle when packaged, else the same interpreter call."""
    marker = ".app/Contents/MacOS/"
    if marker in executable:
        return ["/usr/bin/open", executable[: executable.index(marker) + len(".app")]]
    return [executable, *argv[1:]]


def schedule_relaunch() -> bool:
    """Start a detached waiter that reopens the app once this process has exited."""
    command = launch_command(sys.executable, list(getattr(sys, "orig_argv", sys.argv)))
    started, _pid = QProcess.startDetached(
        "/bin/sh", ["-c", _WAIT_THEN_RUN, "sh", str(os.getpid()), *command], os.getcwd()
    )
    return bool(started)
