"""Chat focus policy and explicit app preferences. No native calls or Qt."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import plistlib
from pathlib import Path, PureWindowsPath
import re
import sys
import time

from .model import APP_LAYER_ID_RE, SELF_APP_IDS, atomic_write, normalize_app_layer_id
from .chat_focus_locator import valid as valid_locator

EDIT_ROLES = {"AXTextArea", "AXTextField", "AXComboBox"}
BLOCKED_ROLES = {"AXSheet", "AXDialog", "AXMenu", "AXMenuItem", "AXMenuBar"}
BROWSERS = {
    "com.apple.Safari",
    "com.google.Chrome",
    "com.microsoft.edgemac",
    "org.mozilla.firefox",
    "com.brave.Browser",
    "com.operasoftware.Opera",
    "exe:chrome.exe",
    "exe:msedge.exe",
    "exe:firefox.exe",
    "exe:brave.exe",
    "exe:opera.exe",
    "exe:vivaldi.exe",
    "exe:arc.exe",
    "exe:iexplore.exe",
}
CHAT_IDS = {"prompt-textarea", "chat-input", "chat-input-textarea", "composer-input", "message-input"}
CHAT_HINTS = {
    "ask anything", "ask codex", "how can i help", "message chatgpt", "message claude",
    "reply to claude", "message deepseek", "输入消息", "请输入消息", "发消息", "发送消息",
    "输入问题", "随便问", "和 kimi 聊聊", "向 deepseek 发送消息", "向 kimi 提问", "向豆包提问",
}
MESSAGES = {
    "waiting": "等待切回应用", "manual_needed": "无法明确识别，请指定聊天输入框",
    "rebind": "无法匹配已指定的输入框，请重新指定", "restored": "已恢复聊天输入框",
    "saved": "已保存定位规则，切回应用时自动查找",
    "needs_binding": "尚未保存定位规则，请指定聊天输入框",
    "confirming": "已请求聚焦，正在确认",
    "already": "聊天输入框已有焦点", "editing": "正在其他位置编辑，保持现状",
    "blocked": "当前有菜单或对话框，本次未聚焦", "changed": "切回后你先有了点击/按键/滚动，本次让位未聚焦",
    "unavailable": "暂时无法读取应用，请检查辅助功能权限" if sys.platform == "darwin" else "暂时无法读取应用窗口（可能未就绪或以管理员权限运行）",
    "unconfirmed": "已请求聚焦，暂未收到确认",
    "loading": "等待应用窗口就绪",
}


@dataclass(frozen=True)
class App:
    bundle: str
    pid: int
    name: str


@dataclass
class Binding:
    app: App
    window: object
    element: object


def app_from_path(path, *, platform: str | None = None):
    target_platform = platform or sys.platform
    if target_platform == "win32" and PureWindowsPath(str(path)).suffix.casefold() != ".app":
        from .win_uia import pe_friendly_name, resolve_windows_app_alias, resolve_windows_shortcut
        orig_win = PureWindowsPath(str(path))
        resolved = resolve_windows_shortcut(path) if orig_win.suffix.casefold() == ".lnk" else resolve_windows_app_alias(path)
        win_p = PureWindowsPath(resolved)
        if win_p.suffix.casefold() != ".exe":
            raise ValueError("请选择 Windows 应用（.exe）或快捷方式（.lnk）")
        bundle = normalize_app_layer_id("exe:" + win_p.name)
        if bundle.casefold() in {b.casefold() for b in BROWSERS}:
            raise ValueError("暂不支持按整个浏览器启用，请选择桌面聊天应用")
        name = (
            orig_win.stem.strip()
            if orig_win.suffix.casefold() == ".lnk" and orig_win.stem.strip()
            else (pe_friendly_name(resolved) or win_p.stem or win_p.name).strip()
        )[:200]
        validate_preferences({"enabled": False, "apps": {bundle: {"name": name, "mode": "auto", "enabled": True}}})
        return App(bundle, 0, name)
    path = Path(path).expanduser()
    if path.suffix.casefold() != ".app":
        raise ValueError("请选择 macOS 应用（.app）")
    with (path / "Contents" / "Info.plist").open("rb") as source:
        info = plistlib.load(source)
    schemes = {str(s).casefold() for row in info.get("CFBundleURLTypes", [])
               for s in row.get("CFBundleURLSchemes", [])}
    if schemes & {"http", "https"}:
        raise ValueError("暂不支持按整个浏览器启用，请选择桌面聊天应用")
    bundle = info.get("CFBundleIdentifier", "")
    name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or path.stem
    validate_preferences({"enabled": False, "apps": {bundle: {"name": name, "mode": "auto", "enabled": True}}})
    return App(bundle, 0, name)


def validate_preferences(value):
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("聊天聚焦设置格式无效")
    entries = value.get("apps")
    if not isinstance(entries, dict) or len(entries) > 24:
        raise ValueError("最多添加 24 个应用")
    browser_keys = {b.casefold() for b in BROWSERS}
    for bundle, entry in entries.items():
        if (
            not isinstance(bundle, str)
            or "/" in bundle
            or "\\" in bundle
            or "\x00" in bundle
            or not APP_LAYER_ID_RE.fullmatch(bundle)
            or bundle.casefold() in SELF_APP_IDS
        ):
            raise ValueError("应用标识无效")
        if bundle.casefold() in browser_keys:
            raise ValueError("暂不支持把整个浏览器加入白名单，请选择桌面聊天应用")
        if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                or not 1 <= len(entry["name"]) <= 200 or entry.get("mode") not in {"auto", "manual"}
                or type(entry.get("enabled")) is not bool):
            raise ValueError("应用聚焦设置无效")
        if "locator" in entry and not valid_locator(entry["locator"]):
            raise ValueError("输入框定位规则无效")


class Preferences:
    def __init__(self, directory):
        self.path = Path(directory) / "chat-focus.json"
        self.data = {"enabled": False, "apps": {}}
        self.error = ""
        if self.path.exists():
            try:
                if self.path.stat().st_size > 64_000:
                    raise ValueError("配置过大")
                data = json.loads(self.path.read_text(encoding="utf-8"))
                validate_preferences(data)
                self.data = data
            except (OSError, ValueError, TypeError):
                self.error = "聊天聚焦设置读取失败，功能保持关闭；原文件未覆盖。"

    def save(self, data):
        if self.error:
            raise ValueError(self.error)
        validate_preferences(data)
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2))
        self.data = deepcopy(data)


def chat_metadata(identifier, placeholder):
    identifier = str(identifier or "").strip().casefold()
    placeholder = str(placeholder or "").strip().casefold()
    if any(word in identifier + " " + placeholder for word in ("search", "rename", "password", "搜索", "重命名", "密码")):
        return False
    return identifier in CHAT_IDS or placeholder in CHAT_HINTS


def context(backend, element, window, target=None):
    for _ in range(64):
        if element is None:
            return "unavailable"
        role = backend.read(element, "AXRole")
        if role is None:
            return "unavailable"
        if role in BLOCKED_ROLES or backend.read(element, "AXSubrole") in {"AXDialog", "AXSystemDialog", "AXSecureTextField"}:
            return "blocked"
        if element == target:
            return "already"
        if role in EDIT_ROLES:
            return "editing"
        if element == window:
            return "clear"
        element = backend.read(element, "AXParent")
    return "unavailable"


def attached(backend, target, window):
    for _ in range(64):
        if target == window:
            return True
        if target is None:
            break
        target = backend.read(target, "AXParent")
    return False


def restore(backend, binding, stamp, cancelled, log=lambda *a, **k: None):
    app, window, target = binding.app, binding.window, binding.element
    if cancelled() or backend.frontmost() != app or backend.window(app) != window:
        return "changed"
    if backend.modal(window):
        return "blocked"
    if (backend.read(target, "AXRole") != "AXTextArea" or backend.read(target, "AXEnabled") is not True
            or not backend.settable(target) or not attached(backend, target, window)):
        return "rebind"
    focused = backend.focused(app)
    state = context(backend, focused, window, target)
    if state != "clear":
        return state
    if (cancelled() or backend.frontmost() != app or backend.window(app) != window
            or backend.focused(app) != focused or backend.input_stamp() != stamp):
        return "changed"
    acknowledged = backend.focus(target)
    log("focus_request", bundle=app.bundle, pid=app.pid, acknowledged=bool(acknowledged),
        error=getattr(backend, "last_focus_error", None))
    if (backend.frontmost() == app and backend.window(app) == window
            and backend.focused(app) == target and backend.read(target, "AXFocused") is True):
        return "restored"
    return "confirming"


class FocusEngine:
    """Single worker owns all native references; every activation gets one attempt."""
    def __init__(self, backend, clock=time.monotonic, log=lambda *a, **k: None):
        self.backend, self.clock = backend, clock
        self.log = log
        self.attempt = 0
        self.confirmation = None
        self.preferences = {"enabled": False, "apps": {}}
        self.bindings = {}
        self.states = {}
        self.last_front = None
        self.seeded = False
        self.selecting = ""
        self.draft = None
        self.deadline = 0
        self.pending = None
        self.capture_status = "waiting"

    def configure(self, preferences):
        old = self.preferences
        self.preferences = deepcopy(preferences)
        for bundle in list(self.bindings):
            if bundle not in preferences["apps"] or old["apps"].get(bundle, {}).get("mode") != preferences["apps"][bundle]["mode"]:
                self.bindings.pop(bundle, None)
        self.states = {b: s for b, s in self.states.items() if b in preferences["apps"]}
        if old["enabled"] != preferences["enabled"]:
            self.seeded = False
            self.pending = None
        if self.confirmation:
            entry = preferences["apps"].get(self.confirmation["binding"].app.bundle)
            if not preferences["enabled"] or not entry or not entry["enabled"]:
                self._finish(self.confirmation, "changed")
                self.confirmation = None
        if self.selecting and self.selecting not in preferences["apps"]:
            self.cancel()

    def begin(self, bundle):
        if bundle not in self.preferences["apps"]:
            raise ValueError("请先选择这个应用")
        self.selecting, self.draft = bundle, None
        self.deadline = self.clock() + 30
        self.pending = None
        self.confirmation = None
        self.capture_status = "waiting"

    def cancel(self):
        self.selecting, self.draft = "", None
        self.seeded = False
        self.pending = None
        self.confirmation = None

    def confirm(self, bundle):
        draft = self.draft
        if not draft or self.selecting != bundle or draft.app.bundle != bundle:
            raise ValueError("还没有取得输入框，请切到目标应用并点击聊天输入框")
        if not attached(self.backend, draft.element, draft.window):
            raise ValueError("输入框已经变化，请重新指定")
        describe = getattr(self.backend, "describe", None)
        locator = describe(draft.app, draft.window, draft.element) if describe else None
        if not attached(self.backend, draft.element, draft.window):
            raise ValueError("输入框已经变化，请重新指定")
        self.bindings[bundle] = draft
        self.states[bundle] = "waiting"
        self.cancel()
        return locator

    def _finish(self, attempt, status):
        app = attempt["app"]
        self.states[app.bundle] = status
        self.log("result", attempt=attempt["id"], bundle=app.bundle, pid=app.pid,
                 status=status, elapsed_ms=max(0, int((self.clock() - attempt["started"]) * 1000)))

    def _check_confirmation(self, front, cancelled):
        pending = self.confirmation
        b, binding = self.backend, pending["binding"]
        if (cancelled() or front != binding.app or b.window(binding.app) != binding.window
                or b.input_stamp() != pending["stamp"]):
            status = "changed"
        elif b.focused(binding.app) == binding.element and b.read(binding.element, "AXFocused") is True:
            status = "restored"
        elif self.clock() >= pending["until"]:
            status = "unconfirmed"
        else:
            return
        self._finish(pending, status)
        self.confirmation = None

    @property
    def needs_polling(self):
        return bool((self.selecting and self.clock() < self.deadline)
                    or self.pending is not None
                    or self.confirmation is not None
                    or (self.preferences["enabled"] and any(
                        e.get("enabled") for e in self.preferences["apps"].values()) and not self.seeded))

    def snapshot(self):
        return {"states": dict(self.states), "selecting": self.selecting,
                "candidate_ready": self.draft is not None,
                "capture_status": self.capture_status,
                "seconds_left": max(0, int(self.deadline - self.clock())) if self.selecting else 0}

    def tick(self, cancelled=lambda: False):
        if cancelled():
            return
        b = self.backend
        front = b.frontmost()
        if self.confirmation:
            self._check_confirmation(front, cancelled)
            if self.confirmation:
                return
        if front is None:
            return
        if self.selecting:
            self.last_front = front
            if front.bundle != self.selecting or self.clock() >= self.deadline:
                return
            b.prepare(front, cancelled)
            if cancelled() or b.frontmost() != front:
                return
            window, target = b.window(front), b.focused(front)
            self.capture_status = "window_unavailable" if window is None else "focus_unavailable" if target is None else "not_editor"
            if (window is not None and target is not None and not b.modal(window)
                    and b.read(target, "AXRole") == "AXTextArea" and b.read(target, "AXEnabled") is True
                    and b.settable(target) and attached(b, target, window)
                    and not cancelled() and b.frontmost() == front and b.focused(front) == target):
                self.draft = Binding(front, window, target)
                self.capture_status = "captured"
            return
        if not self.preferences["enabled"]:
            self.last_front, self.seeded = front, True
            self.pending = None
            return
        if not self.seeded:
            self.last_front, self.seeded = None, True
        entry = self.preferences["apps"].get(front.bundle)
        if front != self.last_front:
            if self.pending:
                self._finish(self.pending, "changed")
            self.last_front = front
            self.pending = None
            if entry and entry["enabled"]:
                self.attempt += 1
                self.pending = {"stamp": b.input_stamp(), "until": self.clock() + 3, "scans": 0,
                                "id": self.attempt, "app": front, "started": self.clock()}
                self.log("attempt", attempt=self.attempt, bundle=front.bundle, pid=front.pid)
        if self.pending is None:
            return
        if not entry or not entry["enabled"]:
            self.pending = None
            return
        stamp = self.pending["stamp"]
        if cancelled() or b.input_stamp() != stamp:
            self._finish(self.pending, "changed")
            self.pending = None
            return
        if self.clock() >= self.pending["until"]:
            self._finish(self.pending, self.states.get(front.bundle) if self.states.get(front.bundle) in
                         {"rebind", "needs_binding", "manual_needed"} else "unavailable")
            self.pending = None
            return
        b.prepare(front, cancelled)
        if cancelled() or b.frontmost() != front:
            self._finish(self.pending, "changed")
            self.pending = None
            return
        window = b.window(front)
        if window is None:
            self.states[front.bundle] = "loading"
            return
        if b.modal(window):
            self._finish(self.pending, "blocked")
            self.pending = None
            return
        binding = self.bindings.get(front.bundle)
        if binding and (binding.app != front or binding.window != window or not attached(b, binding.element, window)):
            self.bindings.pop(front.bundle, None)
            binding = None
        if binding is None:
            locator = entry.get("locator")
            target = (b.resolve(front, window, locator, cancelled) if locator else
                      b.discover(front, window, cancelled) if entry["mode"] == "auto" else None)
            self.pending["scans"] += 1
            if target is None:
                self.states[front.bundle] = ("rebind" if locator else "needs_binding") if entry["mode"] == "manual" else "manual_needed"
                if self.pending["scans"] >= 3:
                    self._finish(self.pending, self.states[front.bundle])
                    self.pending = None
                return
            binding = Binding(front, window, target)
            self.bindings[front.bundle] = binding
        attempt, self.pending = self.pending, None
        # Consume the attempt before writing: even a readback exception must not
        # cause another focus request on the next worker tick.
        try:
            status = restore(b, binding, stamp, cancelled,
                             lambda event, **fields: self.log(event, attempt=attempt["id"], **fields))
        except Exception:
            self._finish(attempt, "unavailable")
            raise
        if status == "confirming":
            self.confirmation = dict(attempt, binding=binding, until=self.clock() + 1.6)
        else:
            self._finish(attempt, status)
        self.states[front.bundle] = status
        if status == "rebind":
            self.bindings.pop(front.bundle, None)


def ensure_focused_for_voice(engine: FocusEngine, bundle: str, cancelled=lambda: False) -> str:
    """Fast pre-voice focus check on the single AX worker before starting dictation."""
    if cancelled() or not engine.preferences.get("enabled"):
        return "disabled"
    entry = engine.preferences.get("apps", {}).get(bundle)
    if not entry or not entry.get("enabled"):
        return "ignored"
    b = engine.backend
    front = b.frontmost()
    if front is None or front.bundle != bundle:
        return "changed"
    b.prepare(front, cancelled)
    if cancelled() or b.frontmost() != front:
        return "changed"
    window = b.window(front)
    if window is None:
        return "loading"
    if b.modal(window):
        return "blocked"
    binding = engine.bindings.get(bundle)
    if binding and (binding.app != front or binding.window != window or not attached(b, binding.element, window)):
        engine.bindings.pop(bundle, None)
        binding = None
    if binding is None:
        locator = entry.get("locator")
        target = (
            b.resolve(front, window, locator, cancelled)
            if locator
            else b.discover(front, window, cancelled)
            if entry.get("mode") == "auto"
            else None
        )
        if target is None:
            status = ("rebind" if locator else "needs_binding") if entry.get("mode") == "manual" else "manual_needed"
            engine.states[bundle] = status
            return status
        binding = Binding(front, window, target)
        engine.bindings[bundle] = binding
    if (b.read(binding.element, "AXRole") != "AXTextArea"
            or b.read(binding.element, "AXEnabled") is not True
            or not b.settable(binding.element)
            or not attached(b, binding.element, window)):
        engine.bindings.pop(bundle, None)
        engine.states[bundle] = "rebind"
        return "rebind"
    focused = b.focused(front)
    if focused is not None:
        state = context(b, focused, window, binding.element)
        if state in ("already", "editing", "blocked"):
            if state == "already":
                engine.states[bundle] = "already"
            return state
    if cancelled() or b.frontmost() != front or b.window(front) != window:
        return "changed"
    acknowledged = b.focus(binding.element)
    engine.log("voice_focus_request", bundle=front.bundle, pid=front.pid, acknowledged=bool(acknowledged))
    status = "restored" if (b.focused(front) == binding.element and b.read(binding.element, "AXFocused") is True) else "confirming"
    engine.states[bundle] = status
    return status

