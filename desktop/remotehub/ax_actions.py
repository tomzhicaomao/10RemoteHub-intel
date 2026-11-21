"""macOS Accessibility UI-control and menu-bar action policy and adapter.

Pure policy functions accept an injectable backend for offscreen unit testing.
Native ApplicationServices / AppKit APIs are imported lazily inside MacAXActionBackend.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re
import sys
import time

from .chat_focus import App, BLOCKED_ROLES, attached
from .chat_focus_locator import digest
from .model import Action, SELF_APP_IDS, normalize_app_layer_id
from .presentation import ui_role_label

PRESSABLE_ROLES = {
    "AXButton",
    "AXCheckBox",
    "AXRadioButton",
    "AXPopUpButton",
    "AXTab",
    "AXToggleButton",
    "AXDisclosureTriangle",
    "AXMenuButton",
}

FOCUSABLE_CONTAINER_ROLES = {
    "AXTextArea",
    "AXTextField",
    "AXSearchField",
    "AXComboBox",
    "AXScrollArea",
    "AXList",
    "AXOutline",
    "AXTable",
    "AXWebArea",
}

NON_CANDIDATE_ROLES = BLOCKED_ROLES | {"AXApplication", "AXWindow", "AXMenuBar", "AXMenuBarItem"}
MENUBAR_ROLES = {"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem"}
_TEXT_OR_CONTAINER_ROLES = {
    "AXTextArea",
    "AXTextField",
    "AXSearchField",
    "AXComboBox",
    "AXScrollArea",
    "AXList",
    "AXOutline",
    "AXTable",
    "AXWebArea",
    "AXWindow",
}
_SKIP_DESCEND_ROLES = {
    "AXStaticText",
    "AXImage",
    "AXMenuBar",
    "AXMenu",
    "AXMenuItem",
    "AXScrollBar",
    "AXSplitter",
    "AXValueIndicator",
}


@dataclass(frozen=True)
class MenuCommand:
    path: tuple[str, ...]
    enabled: bool = True


@dataclass(frozen=True)
class CapturedControl:
    app: App
    op: str
    role: str
    label: str
    signature: str


def sanitize_control_label(raw: str | None) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", raw)
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:60]


def normalize_menu_title(title: str | None) -> str:
    if not title or not isinstance(title, str):
        return ""
    text = " ".join(title.split()).strip()
    return text.replace("...", "…")


def _app_matches(front: App | None, expected_id: str) -> bool:
    if front is None:
        return False
    if not expected_id:
        return True
    return normalize_app_layer_id(front.bundle).casefold() == normalize_app_layer_id(expected_id).casefold()


def _is_blocked_node(backend, node) -> str | None:
    role = backend.read(node, "AXRole")
    if not role or role in BLOCKED_ROLES or role in {"AXMenuBar", "AXMenuBarItem"}:
        return None
    subrole = backend.read(node, "AXSubrole")
    if subrole in {"AXSecureTextField", "AXDialog", "AXSystemDialog"}:
        return None
    return str(role)


def is_menubar_element(backend, element) -> bool:
    """Return True when element belongs to the top macOS menu bar or an open menu."""
    node = element
    for _ in range(6):
        if node is None:
            return False
        role = backend.read(node, "AXRole")
        if role in MENUBAR_ROLES:
            return True
        if role == "AXWindow":
            return False
        node = backend.read(node, "AXParent")
    return False


def _control_chrome_label(backend, node, role: str) -> str:
    if role in _TEXT_OR_CONTAINER_ROLES:
        return ""
    for attr in ("AXTitle", "AXDescription", "AXHelp"):
        val = sanitize_control_label(backend.read(node, attr))
        if val:
            return val
    # SwiftUI/WebView buttons often place visible text in an immediate AXStaticText child.
    for child in backend.children(node)[:4]:
        if backend.read(child, "AXRole") == "AXStaticText":
            val = sanitize_control_label(
                backend.read(child, "AXTitle") or backend.read(child, "AXValue")
            )
            if val:
                return val
    return ""


def _sibling_slot(backend, node) -> int | None:
    parent = backend.read(node, "AXParent")
    if parent is None:
        return None
    for idx, child in enumerate(backend.children(parent)[:64]):
        if child == node:
            return idx
    return None


def _ui_control_signature(backend, target, window, cancelled=lambda: False) -> str | None:
    def metadata(node, include_chrome: bool):
        role = backend.read(node, "AXRole")
        if not role or role in BLOCKED_ROLES:
            raise ValueError("Incomplete or modal ancestry")
        subrole = backend.read(node, "AXSubrole")
        if subrole in {"AXSecureTextField", "AXDialog", "AXSystemDialog"}:
            raise ValueError("Protected ancestry")
        ident = backend.read(node, "AXIdentifier")
        if not include_chrome:
            return [role, subrole, ident]
        lbl = _control_chrome_label(backend, node, str(role))
        if ident or lbl:
            return [role, subrole, ident, lbl]
        return [role, subrole, ident, "", _sibling_slot(backend, node)]

    chain, surroundings = [], []
    node = target
    for depth in range(64):
        if node is None or cancelled():
            return None
        chain.append(metadata(node, include_chrome=(depth == 0)))
        if node == window:
            return digest([chain, surroundings])
        if 1 <= depth <= 3:
            children = backend.children(node)
            sibs = []
            for child in children[:24]:
                try:
                    sibs.append(metadata(child, include_chrome=False))
                except (ValueError, RuntimeError, TypeError):
                    continue
            surroundings.append(sibs)
        node = backend.read(node, "AXParent")
    return None


def _visible_in_window(backend, node, window) -> bool:
    rect_fn = getattr(backend, "rect", None)
    if callable(rect_fn):
        if backend.read(node, "AXHidden"):
            return False
        rect = rect_fn(node)
        outer = rect_fn(window)
        if rect is not None and outer is not None:
            x, y, w, h = rect
            left, top, width, height = outer
            tol = 2.0
            return (
                w > 0
                and h > 0
                and x >= left - tol
                and y >= top - tol
                and x + w <= left + width + tol
                and y + h <= top + height + tol
            )
    return bool(backend.visible(node, window))


def _supports_op(backend, node, role: str, op: str) -> bool:
    if role in NON_CANDIDATE_ROLES:
        return False
    if backend.read(node, "AXEnabled") is False:
        return False
    if op == "focus":
        return bool(backend.settable(node))
    if role in PRESSABLE_ROLES:
        return True
    return "AXPress" in backend.actions(node)


def find_interactive_candidate(backend, start_element, window, op: str, *, cancelled=lambda: False):
    """Walk from hit-tested start_element to nearest uniquely identifiable control."""
    if start_element is None or window is None or cancelled():
        return None
    if backend.modal(window):
        return None
    chosen = None
    chosen_role = ""
    node = start_element
    for _ in range(16):
        if node is None or cancelled() or node == window:
            break
        role = _is_blocked_node(backend, node)
        if role is None:
            return None
        if _supports_op(backend, node, role, op):
            chosen = node
            chosen_role = role
            break
        if op == "focus" and role == "AXScrollArea":
            for child in backend.children(node):
                child_role = _is_blocked_node(backend, child)
                if child_role in FOCUSABLE_CONTAINER_ROLES and _supports_op(backend, child, child_role, "focus"):
                    chosen = child
                    chosen_role = child_role
                    break
            if chosen is not None:
                break
        node = backend.read(node, "AXParent")
    if chosen is None or not attached(backend, chosen, window):
        return None
    try:
        sig = _ui_control_signature(backend, chosen, window, cancelled)
    except (ValueError, RuntimeError, TypeError):
        return None
    if not sig:
        return None
    resolved = resolve_ui_element(
        backend, window, sig, chosen_role, op=op, cancelled=cancelled, max_nodes=6000, timeout_s=1.2
    )
    if resolved != chosen:
        return None
    label_text = _control_chrome_label(backend, chosen, chosen_role) or ui_role_label(chosen_role)
    return chosen, chosen_role, label_text, sig


def resolve_ui_element(
    backend,
    window,
    signature_hex: str,
    expected_role: str,
    *,
    op: str = "press",
    cancelled=lambda: False,
    max_nodes: int = 6000,
    timeout_s: float = 1.8,
):
    """Uniquely find a control matching expected_role and control signature inside window."""
    if not window or not signature_hex or not expected_role or backend.modal(window):
        return None
    pending, matches = deque([(window, 0)]), []
    deadline, count = time.monotonic() + timeout_s, 0
    try:
        while pending and count < max_nodes and time.monotonic() < deadline and not cancelled():
            node, depth = pending.popleft()
            count += 1
            role = backend.read(node, "AXRole")
            if role is None or role in BLOCKED_ROLES:
                continue
            if role == expected_role:
                if _supports_op(backend, node, role, op) and _visible_in_window(backend, node, window):
                    try:
                        if _ui_control_signature(backend, node, window, cancelled) == signature_hex:
                            matches.append(node)
                            if len(matches) > 1:
                                return None
                    except (ValueError, RuntimeError, TypeError):
                        pass
                # Even if an outer container matched/didn't match, allow nested controls of same role if group/web
                if role not in {"AXGroup", "AXWebArea", "AXScrollArea"}:
                    continue
            if role in _SKIP_DESCEND_ROLES:
                continue
            if depth >= 64:
                continue
            children = backend.children(node)
            if not children:
                continue
            if count + len(pending) + len(children) > max_nodes:
                if not matches:
                    return None
                continue
            pending.extend((child, depth + 1) for child in children)
    except (ValueError, RuntimeError, TypeError):
        return None
    if cancelled() or len(matches) != 1:
        return None
    return matches[0]


def execute_ui_element(backend, action: Action, *, cache: dict | None = None) -> str:
    action.validate()
    if action.kind != "ui_element":
        raise ValueError("不是界面控件动作")
    spec = action.spec
    op = action.target
    role = spec["role"]
    sig = spec["signature"]
    app_name = (spec.get("app_name") or action.application).strip()
    label_text = (spec.get("label") or "").strip() or ui_role_label(role)

    front = backend.frontmost()
    if not _app_matches(front, action.application):
        raise RuntimeError(f"当前前台应用不是「{app_name}」，未执行控件操作")
    window = backend.window(front)
    if window is None:
        raise RuntimeError(f"「{app_name}」当前没有可用窗口")
    if backend.modal(window):
        raise RuntimeError("当前窗口有弹窗或菜单打开，已跳过控件操作")

    cache_key = (front.pid, normalize_app_layer_id(action.application).casefold(), op, role, sig)
    target = cache.get(cache_key) if cache is not None else None
    if target is not None:
        if (
            backend.read(target, "AXRole") != role
            or not _supports_op(backend, target, role, op)
            or not attached(backend, target, window)
        ):
            target = None
            cache.pop(cache_key, None)

    if target is None:
        backend.prepare(front, lambda: False)
        target = resolve_ui_element(backend, window, sig, role, op=op)
        if target is None:
            raise RuntimeError(f"未能在「{app_name}」当前窗口唯一找到「{label_text}」，请确认页面一致或重新指定")
        if cache is not None:
            cache[cache_key] = target

    ok = backend.focus(target) if op == "focus" else backend.press(target)
    if not ok:
        if cache is not None:
            cache.pop(cache_key, None)
        verb = "聚焦" if op == "focus" else "点击"
        raise RuntimeError(f"无法{verb}控件「{label_text}」（应用未接受操作）")
    return f"已{'聚焦' if op == 'focus' else '点击'}控件「{label_text}」"


def list_app_menus(
    backend,
    app_id: str,
    *,
    max_nodes: int = 1600,
    timeout_s: float = 1.5,
) -> tuple[App, list[MenuCommand]]:
    """Read top-level and nested AXMenuBar items from a running application."""
    norm = normalize_app_layer_id(app_id).casefold()
    app = next((item for item in backend.applications(include_browsers=True)
                if normalize_app_layer_id(item.bundle).casefold() == norm), None)
    if app is None:
        raise RuntimeError("目标应用未运行，请先打开该应用后再读取菜单栏")
    menubar = backend.menubar(app)
    if menubar is None:
        hint = (
            "暂时无法读取该应用的菜单栏，请确认辅助功能权限与窗口状态"
            if sys.platform == "darwin"
            else "暂时无法读取该应用的菜单栏（请确认窗口已展开且该应用包含标准菜单栏）"
        )
        raise RuntimeError(hint)

    commands: list[MenuCommand] = []
    seen_paths: set[tuple[str, ...]] = set()
    deadline = time.monotonic() + timeout_s
    visited = 0

    def walk_menu(container, prefix: tuple[str, ...], depth: int):
        nonlocal visited
        if depth > 6 or visited >= max_nodes or time.monotonic() >= deadline:
            return
        for child in backend.children(container):
            visited += 1
            if visited >= max_nodes or time.monotonic() >= deadline:
                return
            role = backend.read(child, "AXRole")
            title = sanitize_control_label(backend.read(child, "AXTitle"))
            if role == "AXMenu":
                walk_menu(child, prefix, depth)
                continue
            if not title:
                continue
            if depth == 0 and title in {"Apple", "\uf8ff"}:
                continue
            next_path = prefix + (title,)
            subchildren = backend.children(child) if len(next_path) < 6 else []
            submenus = [c for c in subchildren if backend.read(c, "AXRole") == "AXMenu"]
            if submenus:
                for submenu in submenus:
                    walk_menu(submenu, next_path, depth + 1)
            elif len(next_path) >= 2 and next_path not in seen_paths:
                seen_paths.add(next_path)
                enabled = backend.read(child, "AXEnabled") is True
                commands.append(MenuCommand(path=next_path, enabled=enabled))

    walk_menu(menubar, (), 0)
    if not commands:
        raise RuntimeError("未读取到可用菜单命令（可能受系统或应用限制）")
    return app, commands


def _find_menu_child_by_title(backend, container, target_title: str):
    wanted_raw = target_title.strip()
    wanted_norm = normalize_menu_title(wanted_raw)
    fallback = None
    for child in backend.children(container):
        role = backend.read(child, "AXRole")
        if role == "AXMenu":
            hit = _find_menu_child_by_title(backend, child, target_title)
            if hit is not None:
                return hit
            continue
        title = sanitize_control_label(backend.read(child, "AXTitle"))
        if not title:
            continue
        if title == wanted_raw:
            return child
        if fallback is None and normalize_menu_title(title) == wanted_norm:
            fallback = child
    return fallback


def execute_app_menu(backend, action: Action) -> str:
    action.validate()
    if action.kind != "app_menu":
        raise ValueError("不是应用菜单命令")
    spec = action.spec
    path = [str(part).strip() for part in spec["path"]]
    app_name = (spec.get("app_name") or action.application).strip()

    front = backend.frontmost()
    if front is None:
        raise RuntimeError("暂时无法识别当前前台应用")
    if action.application and not _app_matches(front, action.application):
        raise RuntimeError(f"当前前台应用不是「{app_name}」，未执行菜单命令")

    menubar = backend.menubar(front)
    if menubar is None:
        hint = (
            "无法读取当前前台应用的菜单栏，请检查辅助功能权限"
            if sys.platform == "darwin"
            else "无法读取当前前台应用的菜单栏（应用可能不含菜单栏或以管理员权限运行）"
        )
        raise RuntimeError(hint)

    current = menubar
    for idx, title in enumerate(path):
        nxt = _find_menu_child_by_title(backend, current, title)
        if nxt is None:
            raise RuntimeError(f"当前菜单栏中找不到「{' → '.join(path)}」")
        if idx == len(path) - 1:
            current = nxt
            break
        children = backend.children(nxt)
        submenu = next((c for c in children if backend.read(c, "AXRole") == "AXMenu"), None)
        if submenu is None:
            raise RuntimeError(f"无法展开子菜单「{title}」")
        current = submenu

    if backend.read(current, "AXEnabled") is not True:
        raise RuntimeError(f"菜单命令「{' → '.join(path)}」当前处于禁用状态")
    if not backend.press(current):
        raise RuntimeError(f"无法执行菜单命令「{' → '.join(path)}」")
    return f"已执行菜单「{' → '.join(path)}」"


def create_ax_action_backend():
    """Return the native UI-control & menu-bar backend for the running OS."""
    if sys.platform == "darwin":
        return MacAXActionBackend()
    if sys.platform == "win32":
        from .win_uia import WinAXActionBackend
        return WinAXActionBackend()
    raise RuntimeError("当前系统暂不支持界面控件与应用菜单动作")


class MacAXActionBackend:
    """Native macOS Accessibility adapter for UI controls and menu bars."""

    def __init__(self):
        import ApplicationServices as ax
        from AppKit import NSEvent, NSScreen, NSWorkspace
        from .mac_chat_focus import MacFocusBackend

        self._ax = ax
        self._nsevent = NSEvent
        self._nsscreen = NSScreen
        self._workspace = NSWorkspace.sharedWorkspace()
        self._focus_backend = MacFocusBackend()
        ax.AXUIElementSetMessagingTimeout(ax.AXUIElementCreateSystemWide(), 0.08)

    def applications(self, *, include_browsers: bool = True) -> list[App]:
        from .app_layers import running_layer_apps
        if not include_browsers:
            return self._focus_backend.applications()
        layer_apps = running_layer_apps("darwin")
        by_id: dict[str, int] = {}
        for app in self._workspace.runningApplications():
            if app.activationPolicy() != 0:
                continue
            ident = str(app.bundleIdentifier() or "").strip()
            if not ident:
                url = app.executableURL()
                if url and url.path():
                    ident = normalize_app_layer_id("exe:" + str(url.path()).rsplit("/", 1)[-1])
            else:
                ident = normalize_app_layer_id(ident)
            if ident and ident.casefold() not in SELF_APP_IDS:
                by_id.setdefault(ident.casefold(), int(app.processIdentifier()))
        result = []
        for item in layer_apps:
            pid = by_id.get(item.app_id.casefold(), 0)
            if pid > 0:
                result.append(App(item.app_id, pid, item.name))
        return result

    def frontmost(self) -> App | None:
        running = self._workspace.frontmostApplication()
        if running is None:
            return None
        ident = str(running.bundleIdentifier() or "").strip()
        if not ident:
            url = running.executableURL()
            if url and url.path():
                ident = normalize_app_layer_id("exe:" + str(url.path()).rsplit("/", 1)[-1])
        else:
            ident = normalize_app_layer_id(ident)
        if not ident or ident.casefold() in SELF_APP_IDS:
            return None
        name = str(running.localizedName() or ident)
        return App(ident, int(running.processIdentifier()), name)

    def prepare(self, app: App, cancelled):
        self._focus_backend.prepare(app, cancelled)

    def root(self, app: App):
        return self._focus_backend.root(app)

    def read(self, element, attr: str):
        return self._focus_backend.read(element, attr)

    def window(self, app: App):
        root = self.root(app)
        return self.read(root, "AXFocusedWindow") or self.read(root, "AXMainWindow")

    def menubar(self, app: App):
        return self.read(self.root(app), "AXMenuBar")

    def children(self, element):
        error, value = self._ax.AXUIElementCopyAttributeValue(element, "AXChildren", None)
        if error:
            return []
        return list(value or [])

    def settable(self, element) -> bool:
        return self._focus_backend.settable(element)

    def modal(self, window) -> bool:
        return self._focus_backend.modal(window)

    def visible(self, element, window) -> bool:
        return self._focus_backend.visible(element, window)

    def rect(self, element):
        return self._focus_backend.rect(element)

    def actions(self, element) -> set[str]:
        if element is None:
            return set()
        error, names = self._ax.AXUIElementCopyActionNames(element, None)
        if error or not names:
            return set()
        return {str(name) for name in names}

    def press(self, element) -> bool:
        if element is None:
            return False
        error = int(self._ax.AXUIElementPerformAction(element, "AXPress"))
        return error == 0

    def focus(self, element) -> bool:
        return self._focus_backend.focus(element)

    def mouse_point(self) -> tuple[float, float]:
        loc = self._nsevent.mouseLocation()
        return float(loc.x), float(loc.y)

    def element_at_cursor(self, app: App):
        screens = self._nsscreen.screens()
        if not screens:
            return None
        primary_height = float(screens[0].frame().size.height)
        loc = self._nsevent.mouseLocation()
        cg_x = float(loc.x)
        cg_y = primary_height - float(loc.y)
        root = self.root(app)
        error, element = self._ax.AXUIElementCopyElementAtPosition(root, cg_x, cg_y, None)
        if error == 0 and element is not None:
            return element
        sys_wide = self._ax.AXUIElementCreateSystemWide()
        error, element = self._ax.AXUIElementCopyElementAtPosition(sys_wide, cg_x, cg_y, None)
        if error == 0 and element is not None:
            pid_err, pid = self._ax.AXUIElementGetPid(element, None)
            if pid_err == 0 and int(pid) == app.pid:
                return element
        return None
