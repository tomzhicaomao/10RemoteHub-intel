"""Named operations resolved on the machine executing the mapping."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .system_hotkeys import OPERATIONS as MAC_SYSTEM_ACTIONS

COMMON_ACTIONS = {
    "copy": "复制",
    "paste": "粘贴",
    "cut": "剪切",
    "undo": "撤销",
    "select_all": "全选",
    "find": "页面内查找",
    "save": "保存",
    "switch_app": "切换应用",
    "app_switcher": "显示所有应用并选择",
    "overview": "展示所有窗口",
    "screenshot": "截图",
    "next_tab": "下个标签页",
    "previous_app": "反向切换应用",
    "previous_tab": "上个标签页",
    "next_window": "应用下一窗口",
    "previous_window": "应用上一窗口",
    "dock": "聚焦 Dock",
    "system_search": "打开系统搜索",
    "system_settings": "打开系统设置",
    "show_desktop": "显示桌面",
    "scroll_up": "向上滚动",
    "scroll_down": "向下滚动",
    "page_up": "向上翻页",
    "page_down": "向下翻页",
    "volume_up": "音量 +",
    "volume_down": "音量 −",
    "mute": "静音",
    "play_pause": "播放 / 暂停",
    "previous_track": "上一首",
    "next_track": "下一首",
    "lock_screen": "锁屏",
    "fullscreen": "全屏",
    "hide_current_app": "隐藏当前应用",
}

WIN_ONLY_ACTIONS = {
    "file_explorer": "打开文件资源管理器",
    "clipboard_history": "剪贴板历史",
    "win_input_switch": "切换输入法",
    "win_desktop_left": "切换到左侧虚拟桌面",
    "win_desktop_right": "切换到右侧虚拟桌面",
    "snap_left": "窗口靠左分屏",
    "snap_right": "窗口靠右分屏",
    "maximize_window": "最大化当前窗口",
    "task_manager": "打开任务管理器",
}

LINUX_EXCLUDED_ACTIONS = {
    "overview",
    "dock",
    "system_search",
    "show_desktop",
    "hide_current_app",
}

# Existing cross-platform operations keep their IDs and Windows labels.
MAC_ONLY_ACTIONS = set(MAC_SYSTEM_ACTIONS) - set(COMMON_ACTIONS)
MAC_ONLY_ACTIONS.update(("next_window", "previous_window"))
COMMON_ACTIONS.update({
    name: title
    for name, (_, title) in MAC_SYSTEM_ACTIONS.items()
    if name not in COMMON_ACTIONS
})
COMMON_ACTIONS.update(WIN_ONLY_ACTIONS)

MEDIA_ACTIONS = {"volume_up", "volume_down", "mute", "play_pause", "previous_track", "next_track"}
SCROLL_ACTIONS = {"scroll_up", "scroll_down"}


def available_actions(platform=None):
    platform = platform or sys.platform
    excluded = set()
    if platform != "darwin":
        excluded.update(MAC_ONLY_ACTIONS)
    if platform != "win32":
        excluded.update(WIN_ONLY_ACTIONS)
    if str(platform).startswith("linux"):
        excluded.update(LINUX_EXCLUDED_ACTIONS)
    windows_titles = {
        "dock": "聚焦任务栏",
        "overview": "任务视图",
        "screenshot": "截图选区",
        "fullscreen": "全屏（应用支持时）",
        "hide_current_app": "还原 / 最小化当前窗口",
    }
    return {
        key: (
            MAC_SYSTEM_ACTIONS[key][1]
            if platform == "darwin" and key in MAC_SYSTEM_ACTIONS
            else windows_titles.get(key, title)
            if platform == "win32"
            else title
        )
        for key, title in COMMON_ACTIONS.items()
        if key not in excluded
    }


def open_system_settings(platform=None):
    platform = platform or sys.platform
    if platform == "darwin":
        subprocess.Popen(["/usr/bin/open", "-b", "com.apple.systempreferences"])
        return
    if platform == "win32":
        os.startfile("ms-settings:")
        return
    if str(platform).startswith("linux"):
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        preferred: tuple[str, ...] = ()
        if "kde" in desktop:
            preferred = ("systemsettings", "systemsettings6", "systemsettings5")
        elif "gnome" in desktop or "ubuntu" in desktop:
            preferred = ("gnome-control-center",)
        elif "xfce" in desktop:
            preferred = ("xfce4-settings-manager",)
        elif "cinnamon" in desktop:
            preferred = ("cinnamon-settings",)
        elif "mate" in desktop:
            preferred = ("mate-control-center",)
        fallbacks = (
            "gnome-control-center",
            "systemsettings",
            "systemsettings6",
            "systemsettings5",
            "xfce4-settings-manager",
            "cinnamon-settings",
            "mate-control-center",
        )
        candidates = preferred + tuple(x for x in fallbacks if x not in preferred)
        command = next((path for name in candidates if (path := shutil.which(name))), None)
        if command is None:
            raise RuntimeError("未找到当前桌面的设置程序，请用“打开 / 收起 App”指定设置应用")
        subprocess.Popen([command], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    raise RuntimeError("当前版本仅支持 macOS、Windows 与 Linux")


def common_keys(operation, platform=None):
    platform = platform or sys.platform
    is_linux = str(platform).startswith("linux")
    if platform == "darwin" and operation in MAC_SYSTEM_ACTIONS:
        raise RuntimeError("此动作使用本机系统快捷键缓存，请通过动作执行器调用")
    if platform != "darwin" and operation in MAC_ONLY_ACTIONS:
        raise RuntimeError("此操作仅适用于 macOS")
    if platform != "win32" and operation in WIN_ONLY_ACTIONS:
        raise RuntimeError("此操作仅适用于 Windows")
    if is_linux and operation in LINUX_EXCLUDED_ACTIONS:
        if operation == "overview":
            raise RuntimeError("Linux 的窗口总览快捷键因桌面环境而异，请用“按键 / 组合键”设置本机快捷键")
        raise RuntimeError("当前系统不支持这项操作，请重新选择本机动作")
    modifier = "cmd" if platform == "darwin" else "ctrl"
    letters = {
        "copy": "c",
        "paste": "v",
        "cut": "x",
        "undo": "z",
        "select_all": "a",
        "find": "f",
        "save": "s",
    }
    if operation in letters:
        return [modifier, letters[operation]]
    if operation in ("switch_app", "app_switcher"):
        return ["cmd" if platform == "darwin" else "alt", "tab"]
    if operation == "next_tab":
        return ["ctrl", "tab"]
    if operation == "previous_tab":
        return ["ctrl", "shift", "tab"]
    if operation == "previous_app":
        return ["cmd" if platform == "darwin" else "alt", "shift", "tab"]
    if operation in ("next_window", "previous_window") and platform == "darwin":
        return ["cmd", "`"] if operation == "next_window" else ["cmd", "shift", "`"]
    if platform == "win32":
        win_shortcuts = {
            "file_explorer": ["cmd", "e"],
            "clipboard_history": ["cmd", "v"],
            "win_input_switch": ["cmd", "space"],
            "win_desktop_left": ["ctrl", "cmd", "left"],
            "win_desktop_right": ["ctrl", "cmd", "right"],
            "snap_left": ["cmd", "left"],
            "snap_right": ["cmd", "right"],
            "maximize_window": ["cmd", "up"],
            "task_manager": ["ctrl", "shift", "esc"],
        }
        if operation in win_shortcuts:
            return win_shortcuts[operation]
    if operation == "dock" and platform in ("darwin", "win32"):
        return ["ctrl", "f3"] if platform == "darwin" else ["cmd", "t"]
    if operation == "system_search" and platform in ("darwin", "win32"):
        return ["cmd", "space"] if platform == "darwin" else ["cmd", "s"]
    if operation == "show_desktop" and platform in ("darwin", "win32"):
        return ["cmd", "f3"] if platform == "darwin" else ["cmd", "d"]
    if operation == "screenshot":
        if platform == "darwin":
            return ["cmd", "shift", "5"]
        if is_linux:
            return ["print_screen"]
        return ["cmd", "shift", "s"]
    if operation == "page_up":
        return ["pageup"]
    if operation == "page_down":
        return ["pagedown"]
    if operation == "lock_screen":
        return ["ctrl", "cmd", "q"] if platform == "darwin" else ["cmd", "l"]
    if operation == "fullscreen":
        return ["ctrl", "cmd", "f"] if platform == "darwin" else ["f11"]
    if operation == "hide_current_app":
        return ["cmd", "h"] if platform == "darwin" else ["cmd", "down"]
    if operation == "overview":
        if platform == "darwin":
            return ["ctrl", "up"]
        if platform == "win32":
            return ["cmd", "tab"]
        raise RuntimeError("当前系统不支持这项操作，请重新选择本机动作")
    if operation in COMMON_ACTIONS:
        raise RuntimeError("当前系统不支持这项操作，请重新选择本机动作")
    raise ValueError("未知常见操作")
