"""Bounded focus diagnostics, with a scalar allowlist and no input content."""
import json
import logging
from logging.handlers import RotatingFileHandler
import re
from threading import RLock


class FocusLog:
    def __init__(self, directory):
        self.lock = RLock()
        self.handler = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.handler = RotatingFileHandler(directory / "chat-focus.log", maxBytes=128 * 1024,
                                               backupCount=2, encoding="utf-8", delay=True)
            self.handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        except OSError:
            pass

    def write(self, event, **fields):
        if self.handler is None: return
        if event not in {"started", "attempt", "result", "focus_request", "binding_saved", "binding_save_failed", "worker_error",
                         "voice_focus_request", "voice_focus_guard"}: return
        data = {"event": event}
        for key, value in fields.items():
            if key in {"attempt", "pid", "error", "elapsed_ms"} and type(value) is int:
                data[key] = value
            elif key in {"enabled", "persistent", "acknowledged"} and type(value) is bool:
                data[key] = value
            elif key == "bundle" and isinstance(value, str) and re.fullmatch(
                r"(?:exe:[a-z0-9][a-z0-9._ ()+-]{0,180}|[A-Za-z0-9][A-Za-z0-9._-]{1,199})",
                value,
            ):
                data[key] = value
            elif key == "status" and value in {"restored", "already", "editing", "blocked", "changed", "unavailable", "unconfirmed", "rebind", "manual_needed", "needs_binding",
                                                  "confirming", "loading", "disabled", "ignored"}:
                data[key] = value
        try:
            with self.lock:
                if self.handler is not None:
                    self.handler.handle(logging.LogRecord("remotehub.focus", logging.INFO, "", 0, json.dumps(data), (), None))
        except OSError:
            pass

    def close(self):
        with self.lock:
            if self.handler is not None:
                self.handler.close()
                self.handler = None
