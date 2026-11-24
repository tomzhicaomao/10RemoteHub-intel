"""User-facing text for actions and keys. Pure functions, no Qt widgets."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath
import sys

from .model import KINDS

# Short labels for the inspector and canvas callouts; KINDS keeps the
# longer names used in older exports.
KIND_TITLES = {
    "text": "输入文本",
    "macro": "多步动作链",
    "grid": "九宫格面板",
    "nine_key": "九键输入",
    "cheat_sheet": "键位速查浮层",
    "cycle_profile": "循环切换方案",
    "ui_element": "界面控件动作",
    "app_menu": "应用菜单命令",
    "mouse": "鼠标按键",
    "hold": "按住 Command / Shift 等键",
    "latch": "保持 Command / Shift 等键",
    "shortcut": "按键 / 组合键",
    "app": "打开 App",            # legacy "open only"; no longer offered
    "toggle_app": "打开 / 收起 App",
    "file": "打开文件",
    "minimize_window": "收起当前窗口",
    "close_window": "关闭当前窗口",
    "quit_app": "退出当前应用",
    "voice": "语音输入",
}


def kind_title(ident: str, platform: str = sys.platform) -> str:
    if ident == "hold":
        if platform == "win32":
            return "按住 Win / Ctrl / Shift 等键"
        if str(platform).startswith("linux"):
            return "按住 Super / Ctrl / Shift 等键"
    if ident == "latch":
        if platform == "win32":
            return "保持 Win / Ctrl / Shift 等键"
        if str(platform).startswith("linux"):
            return "保持 Super / Ctrl / Shift 等键"
    return KIND_TITLES.get(ident, KINDS.get(ident, ident))


UI_ROLE_LABELS = {
    "AXButton": "按钮",
    "AXCheckBox": "复选框",
    "AXRadioButton": "单选框",
    "AXPopUpButton": "下拉菜单",
    "AXTab": "标签页",
    "AXToggleButton": "切换开关",
    "AXDisclosureTriangle": "展开按钮",
    "AXMenuButton": "菜单按钮",
    "AXTextArea": "文本区",
    "AXTextField": "输入框",
    "AXSearchField": "搜索框",
    "AXComboBox": "组合框",
    "AXScrollArea": "滚动区",
    "AXList": "列表区",
    "AXOutline": "大纲区",
    "AXTable": "表格区",
    "AXWebArea": "网页区",
    "AXGroup": "区域",
}


MOUSE_TITLES = {
    "left": "鼠标左键",
    "right": "鼠标右键",
    "middle": "鼠标中键",
    "left_double": "鼠标左键双击",
}


def ui_role_label(role: str) -> str:
    return UI_ROLE_LABELS.get(role or "", "控件")


KIND_HINTS = {
    "text": "保存后，按遥控器键粘贴到当前光标位置；勾选「输入后自动按回车」可在粘贴后自动发送。",
    "macro": "按顺序依次执行多步操作（如：聚焦区域 → 翻页，或 粘贴提示词 → 等待 120 ms → 按回车发送）；执行等待期间按返回键可随时中止。",
    "grid": "弹出 3×3 可视化动作面板；按遥控器数字键 1–9 直接执行，或用方向键选择、确定执行、返回收起。",
    "ui_element": "通过系统辅助功能直接点击没有快捷键的按钮，或先聚焦到指定滚动/输入区域（推荐放进应用层或动作链）。",
    "app_menu": "直接调用目标应用顶部菜单栏中的命令（如 显示 → 隐藏侧边栏），不依赖且不占用快捷键。",
    "mouse": "模拟对应鼠标按键（在物理键上会独占整颗按键：短按单击 · 连按双击 · 按住不放即为鼠标长按）。",
    "mouse:left": "当前光标处模拟鼠标左键（独占整颗按键：短按左击 · 连按双击 · 按住不放即鼠标长按，松手释放）。",
    "mouse:right": "当前光标处模拟鼠标右键（独占整颗按键：短按右击 · 按住不放即保持按下，松手释放）。",
    "mouse:middle": "当前光标处模拟鼠标中键（独占整颗按键：短按中键点击 · 按住不放即保持按下，松手释放）。",
    "none": "这个键还没有动作。从下面选一个，点一下即保存。",
    "shortcut": "点录制框，按下一个键或组合键；松手后自动保存。",
    "app": "旧配置：每次按下都打开这个应用。改用「打开 / 收起 App」可在前台时收起。",
    "toggle_app": "未运行时启动；在后台时显示；已在前台时收起。",
    "file": "用系统默认应用打开，也可以指定打开方式。",
    "minimize_window": "只收起按下时的当前窗口，不关闭它，也不收起应用的其他窗口。",
    "close_window": "关闭按下时的前台窗口，保留应用自己的保存提示。",
    "quit_app": "请求退出按下时的前台应用，不强制结束进程。",
    "voice": "录音方式与识别快捷键在「语音设置」页设置。",
    "nine_key": "打开九键浮层。在「设置九键键位」按提示点遥控器图上的对应键。",
    "common": "使用本机系统默认快捷键；若已自行更改，请改用「按键 / 组合键」。",
    "cheat_sheet": "在屏幕中央显示当前生效的遥控器键位图；按住不放松手即收起，轻点可固定查看。",
    "cycle_profile": "按一下切换到同遥控器的下一套配置方案（A → B → C → A），并在屏幕上方弹出当前方案提示。",
    "common:switch_app": "按系统最近使用顺序切换应用；不按图标栏顺序逐个启动。",
    "common:app_switcher": "按一次显示所有应用；继续按切换下一个，停顿 1 秒打开。左右键选择，确定打开，返回取消。",
    "hold": "例如按住 Shift 再按方向键选择文字；松开遥控器按键就放开它。",
    "latch": "例如保持 Command 后按 Tab 选择应用；再按一次取消保持。断连或退出也会取消。",
    "common:previous_app": "按系统应用切换器的反向顺序切换。",
    "common:dock": "把键盘焦点移到 Dock / 任务栏，再用方向键选择、回车打开。",
    "common:system_settings": "打开本机的系统设置。",
    "common:screenshot": "使用本机截图工具；按键可能因系统或应用版本不同。",
}

SUPPORTED_AX_PLATFORMS = {"darwin", "win32"}
AX_KINDS = {"ui_element", "app_menu"}

# Right-hand action palette: every entry is one click. Only shortcut, app and
# file/text/grid/macro/ui_element/app_menu need a value afterwards. Common operations resolve per platform.
PALETTE = (
    ("按键与应用", ("shortcut", "text", "macro", "toggle_app", "file", "voice")),
    ("鼠标", ("mouse:left", "mouse:right", "mouse:middle")),
    ("界面与菜单", ("ui_element", "app_menu")),
    ("浮层与方案", ("grid", "cheat_sheet", "cycle_profile")),

    ("窗口", ("minimize_window", "common:maximize_window", "common:snap_left", "common:snap_right", "close_window", "quit_app")),
    ("滚动与翻页", ("common:scroll_up", "common:scroll_down", "common:page_up", "common:page_down")),
    ("媒体", ("common:volume_up", "common:volume_down", "common:mute", "common:play_pause",
             "common:previous_track", "common:next_track")),
    ("编辑", ("common:copy", "common:paste", "common:cut", "common:undo",
              "common:select_all", "common:find", "common:save")),
    ("切换与浏览", ("common:app_switcher", "common:switch_app", "common:previous_app", "common:next_window", "common:previous_window",
                   "common:next_tab", "common:previous_tab")),
    ("桌面与窗口总览", ("common:overview", "common:app_overview", "common:show_desktop",
                     "common:space_left", "common:space_right",
                     "common:win_desktop_left", "common:win_desktop_right")),
    ("导航与搜索", ("common:file_explorer", "common:clipboard_history",
                   "common:dock", "common:toggle_dock", "common:menu_bar", "common:status_menu",
                   "common:system_search", "common:finder_search")),
    ("输入法", ("nine_key", "common:previous_input", "common:next_input", "common:win_input_switch")),
    ("截图与录屏", ("common:screenshot_region", "common:screenshot", "common:screenshot_full",
                   "common:screenshot_region_clipboard", "common:screenshot_full_clipboard")),
    ("高级导航", ("common:focus_next_window", "common:focus_toolbar", "common:focus_floating")),
    ("系统", ("common:system_settings", "common:task_manager", "common:lock_screen",
              "common:fullscreen", "common:hide_current_app")),
)


def chip_id(action):
    """Palette entry that represents a saved action."""
    if action.kind == "hold":
        return action.target
    if action.kind == "mouse":
        return "mouse:" + action.target
    return "common:" + action.target if action.kind == "common" else action.kind


def palette(platform=sys.platform):
    """[(group, [(chip id, title)])] for this platform; empty groups dropped."""
    from .common_actions import available_actions
    common = available_actions(platform)
    groups = []
    for group, ids in PALETTE:
        items = []
        for ident in ids:
            if ident in AX_KINDS and platform not in SUPPORTED_AX_PLATFORMS:
                continue
            if ident.startswith("common:"):
                key = ident.split(":", 1)[1]
                if key in common:
                    items.append((ident, common[key]))
            elif ident.startswith("mouse:"):
                key = ident.split(":", 1)[1]
                items.append((ident, MOUSE_TITLES[key]))
            else:
                items.append((ident, kind_title(ident, platform)))
        if items:
            groups.append((group, items))
    return groups


def chip_hint(ident, platform=None):
    platform = platform or sys.platform
    if platform == "win32":
        hints = {
            "ui_element": "通过 Windows UI 自动化直接点击界面按钮，或先聚焦到指定滚动/输入区域（推荐放进应用层或动作链）。",
            "app_menu": "直接调用目标窗口顶部菜单栏中的命令（兼容传统菜单栏与现代应用菜单），不占用快捷键。",
            "hold": "例如按住 Shift 再按方向键选择文字，或按住 Ctrl 配合滚轮缩放；松开遥控器按键即放开。",
            "latch": "例如保持 Ctrl 或 Alt 后继续按其他键；再按一次取消保持。断连或退出也会取消。",
            "common:hide_current_app": "发送 Win+↓；最大化窗口先还原，再按可最小化。不是 macOS 的隐藏整个应用。",
            "common:overview": "发送 Win+Tab 打开任务视图。",
            "common:screenshot": "发送 Win+Shift+S 打开截图选区；框选后自动复制或进入截图工具。",
            "common:fullscreen": "发送 F11；是否支持全屏由当前应用决定。",
            "common:file_explorer": "发送 Win+E 打开文件资源管理器。",
            "common:clipboard_history": "发送 Win+V 打开剪贴板历史（需在系统设置 → 剪贴板中开启剪贴板历史记录）。",
            "common:win_input_switch": "发送 Win+Space 切换输入法或键盘布局。",
            "common:win_desktop_left": "发送 Ctrl+Win+← 切换到左侧虚拟桌面。",
            "common:win_desktop_right": "发送 Ctrl+Win+→ 切换到右侧虚拟桌面。",
            "common:snap_left": "发送 Win+← 将当前窗口贴靠到屏幕左半侧。",
            "common:snap_right": "发送 Win+→ 将当前窗口贴靠到屏幕右半侧。",
            "common:maximize_window": "发送 Win+↑ 最大化当前窗口。",
            "common:task_manager": "发送 Ctrl+Shift+Esc 直接打开任务管理器。",
            "common:lock_screen": "直接调用 Windows LockWorkStation 安全锁屏，不受输入法或修饰键状态影响。",
            "quit_app": "请求关闭前台应用同一进程的窗口，保留保存提示；有些应用仍会驻留系统托盘。",
        }
        if ident in hints:
            return hints[ident]
    if str(platform).startswith("linux"):
        linux_hints = {
            "text": "保存后按遥控器键粘贴到光标处；X11 下识别到终端窗口时会自动改用 Ctrl+Shift+V 粘贴。",
            "hold": "例如按住 Shift 再按方向键选择文字，或按住 Ctrl 配合滚轮缩放；松开遥控器按键即放开。",
            "latch": "例如保持 Super 或 Alt 后继续按其他键；再按一次取消保持。断连或退出也会取消。",
            "toggle_app": "X11 下支持启动、切换至前台与收起；Wayland 会话下受协议安全限制仅执行启动。",
            "common:screenshot": "发送 PrintScreen 键调用当前桌面环境的截图工具。",
            "common:lock_screen": "调用 loginctl / xdg-screensaver 锁定当前桌面会话。",
        }
        if ident in linux_hints:
            return linux_hints[ident]
    from .system_hotkeys import OPERATIONS
    operation = ident.removeprefix("common:")
    if platform == "darwin" and ident.startswith("common:") and operation in OPERATIONS:
        details = {
            "screenshot": "打开截图与录屏工具栏，由你选择截图或录屏，不自动开始录制。",
            "screenshot_region": "进入区域选择，需要鼠标框选；按 Esc 取消。截图保存到系统配置的位置。",
            "screenshot_region_clipboard": "进入区域选择，需要鼠标框选；按 Esc 取消。结果复制到剪贴板。",
            "screenshot_full": "立即截取屏幕，保存到系统配置的位置。",
            "screenshot_full_clipboard": "立即截取屏幕并复制到剪贴板。",
            "dock": "将焦点移到 Dock，方向键选择、回车打开、Esc 退出。",
            "toggle_dock": "切换 Dock 的自动隐藏状态，不是将操作焦点移到 Dock。",
            "space_left": "切换到左侧桌面空间或全屏应用；没有相邻空间时不切换。",
            "space_right": "切换到右侧桌面空间或全屏应用；没有相邻空间时不切换。",
            "app_overview": "只展开前台应用的窗口，不是所有应用的窗口总览。",
            "previous_window": "在已读取的下一窗口组合上加 Shift；无法推导时会显示原因。",
            "previous_input": "切换输入法；需先在 macOS 添加多个输入法。",
            "next_input": "切换输入法；需先在 macOS 添加多个输入法。",
        }
        return details.get(operation, "") + "使用启动时读取的系统快捷键；修改后到「通用设置 → 系统快捷键」重新读取。"
    return KIND_HINTS.get(ident) or (KIND_HINTS["common"] if ident.startswith("common:") else "")


_MAC_GLYPHS = {"cmd": "⌘", "ctrl": "⌃", "alt": "⌥", "shift": "⇧"}
_MAC_ORDER = ("ctrl", "alt", "shift", "cmd")  # Apple HIG modifier order
_PC_NAMES = {"cmd": "Win" if sys.platform == "win32" else "Super", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift"}
_SPECIAL = {
    "enter": "↩", "esc": "Esc", "tab": "Tab", "space": "Space", "backspace": "⌫",
    "delete": "Del", "insert": "Ins", "home": "Home", "end": "End",
    "pageup": "PgUp", "pagedown": "PgDn", "up": "↑", "down": "↓", "left": "←", "right": "→",
    "print_screen": "PrtSc",
}


def _clean_app_display_name(target: str) -> str:
    name = PureWindowsPath(target).name if "\\" in target else Path(target).name
    lower = name.lower()
    for suffix in (".app", ".lnk", ".desktop"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name


def key_label(keys, platform=sys.platform):
    """Render a chord the way the host OS writes it: ⌃⇧A on macOS, Ctrl + Shift + A elsewhere."""
    if not keys:
        return "未录制"
    mods = [k for k in _MAC_ORDER if k in keys]
    rest = [k for k in keys if k not in _MAC_ORDER]
    main = [_SPECIAL.get(k, k.upper()) for k in rest]
    if platform == "darwin":
        return "".join(_MAC_GLYPHS[m] for m in mods) + "".join(main)
    names = {**_PC_NAMES, "cmd": "Win" if platform == "win32" else "Super"}
    return " + ".join([names[m] for m in mods] + main)


def action_text(action, platform=sys.platform):
    if action.kind == "none":
        return "未设置"
    if action.kind == "common":
        from .common_actions import COMMON_ACTIONS, available_actions
        return available_actions(platform).get(action.target, COMMON_ACTIONS.get(action.target, "未选择常见操作"))
    if action.kind == "hold":
        return ("按住 " if action.target == "hold" else "保持 ") + key_label(action.keys, platform) + \
            (" 不放" if action.target == "hold" else "（再按取消）")
    if action.kind == "shortcut":
        return key_label(action.keys, platform)
    if action.kind == "mouse":
        return MOUSE_TITLES.get(action.target, "鼠标按键")
    if action.kind == "text":
        preview = " ".join(action.text.split())
        prefix = "输入文本并回车 · " if getattr(action, "enter", False) else "输入文本 · "
        return prefix + (preview[:32] + "…" if len(preview) > 32 else preview or "空白字符")
    if action.kind == "ui_element":
        op = "聚焦" if action.target == "focus" else "点击"
        spec = action.spec or {}
        elem_name = (spec.get("label") or "").strip() or ui_role_label(spec.get("role", ""))
        app_name = (spec.get("app_name") or "").strip()
        return f"{op} · {app_name} / {elem_name}" if app_name else f"{op}控件 · {elem_name}"
    if action.kind == "app_menu":
        spec = action.spec or {}
        parts = [str(p).strip() for p in spec.get("path", []) if str(p).strip()]
        menu_str = " → ".join(parts) if parts else "未选择菜单"
        app_name = (spec.get("app_name") or "").strip()
        return f"菜单 · {app_name}：{menu_str}" if app_name else f"菜单 · {menu_str}"
    if action.kind == "grid":
        title = action.target.strip()
        count = sum(1 for cell in action.cells if cell.action.kind != "none")
        return f"九宫格 · {title}" if title else f"九宫格面板（{count} 格）"
    if action.kind == "macro":
        title = action.target.strip()
        count = len(action.steps)
        if title:
            return f"动作链 · {title}"
        if count == 1:
            return f"动作链 · {action_text(action.steps[0].action, platform)}"
        if count >= 2:
            first = action_text(action.steps[0].action, platform)
            if len(first) > 14:
                first = first[:14] + "…"
            return f"动作链（{count} 步）· {first} → …"
        return "多步动作链"
    if action.kind in ("app", "toggle_app", "file"):
        name = _clean_app_display_name(action.target) if action.kind != "file" else (
            PureWindowsPath(action.target).name if "\\" in action.target else Path(action.target).name
        )
        return KIND_TITLES[action.kind] + " · " + name
    return kind_title(action.kind, platform) if action.kind in KIND_TITLES else KINDS.get(action.kind, "未知动作")


def grid_cell_texts(cell, platform=sys.platform) -> tuple[str, str]:
    """Return (primary_title, secondary_subtitle) for a GridCell."""
    if cell.action.kind == "none":
        return cell.title.strip() or "空槽位", "未设置"
    summary = action_text(cell.action, platform)
    custom = cell.title.strip()
    if custom:
        return custom, summary
    if cell.action.kind == "text":
        preview = " ".join(cell.action.text.split())
        short = preview[:18] + "…" if len(preview) > 18 else (preview or "空白字符")
        return short, "输入文本并回车" if getattr(cell.action, "enter", False) else "输入文本"
    if cell.action.kind == "ui_element":
        spec = cell.action.spec or {}
        op = "聚焦" if cell.action.target == "focus" else "点击"
        elem_name = (spec.get("label") or "").strip() or ui_role_label(spec.get("role", ""))
        app_name = (spec.get("app_name") or "").strip()
        return elem_name, f"{op} · {app_name}" if app_name else KIND_TITLES["ui_element"]
    if cell.action.kind == "app_menu":
        spec = cell.action.spec or {}
        parts = [str(p).strip() for p in spec.get("path", []) if str(p).strip()]
        leaf = parts[-1] if parts else "菜单命令"
        app_name = (spec.get("app_name") or "").strip()
        sub = " → ".join(parts) if parts else KIND_TITLES["app_menu"]
        return leaf, f"{app_name} · {sub}" if app_name else sub
    if cell.action.kind == "macro":
        macro_title = cell.action.target.strip() or f"动作链（{len(cell.action.steps)} 步）"
        sub = f"多步动作链 · {len(cell.action.steps)} 步" if cell.action.target.strip() else summary
        return macro_title, sub
    if cell.action.kind in ("app", "toggle_app", "file"):
        name = _clean_app_display_name(cell.action.target) if cell.action.kind != "file" else (
            PureWindowsPath(cell.action.target).name if "\\" in cell.action.target else Path(cell.action.target).name
        )
        return name or summary, KIND_TITLES[cell.action.kind]
    return summary, kind_title(cell.action.kind, platform) if cell.action.kind in KIND_TITLES else ("常见操作" if cell.action.kind == "common" else "")
