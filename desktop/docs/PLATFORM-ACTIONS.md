# macOS / Windows / Linux 平台动作与能力矩阵

2026-11-17 更新（`v0.2.0`）。区分“操作系统支持”与“本软件已提供独立动作”：动作面板仅展示当前运行平台实际可用的常见操作，跨平台导入配置时保留原配置数据不损坏。

## 1. 常见动作与核心操作对照

| 功能 | macOS | Windows | Linux（X11 / Wayland） | 软件处理说明 |
|---|---|---|---|---|
| 复制、粘贴、剪切、撤销、重做、全选、保存、查找 | `⌘` 系列 | `Ctrl` 系列 | `Ctrl` 系列 | 按运行平台自动转换修饰键 |
| 输入文本（剪贴板粘贴 + 可选回车） | `⌘V` | `Ctrl+V` | X11 终端自动改发 `Ctrl+Shift+V`，普通窗口 `Ctrl+V`（Wayland 同步 `wl-copy`） | 纯文本写入剪贴板后粘贴，可选非阻塞补发回车 |
| 整键鼠标左/右/中键 & 宏点击 | CoreGraphics 鼠标事件（含双击连击计数） | Win32 `mouse_event` / `SendInput` | X11 `XTest` / Wayland `/dev/uinput` | 实体键按下/松开跟手保持，断连自动释放 |
| 鼠标滚轮上下滚动 | CoreGraphics 滚轮 | Win32 `pynput.mouse` 滚轮 | X11 `XTest` / Wayland `/dev/uinput` `REL_WHEEL` | 支持按住连发 |
| 应用切换器 | `⌘Tab`（联动 Dock 窗口数跟踪） | `Alt+Tab`（联动任务栏窗口跟踪） | `Alt+Tab` | 连续按键选应用，停顿约 1 秒自动释放修饰键确认 |
| 打开 / 收起 App | NSWorkspace / AppleScript 激活或隐藏 | Win32 前台检测 + 最小化/还原或启动（支持 `.lnk` 与开始菜单目录） | X11 EWMH 激活/最小化或 `.desktop` 启动；Wayland 仅启动 | Windows / Linux 提供已安装应用图形选择器 |
| 收起当前窗口 / 关闭当前窗口 | 最小化 / `⌘W` | Win32 `SW_MINIMIZE` / `WM_CLOSE`（兜底 `Alt+F4`） | X11 EWMH 最小化 / `_NET_CLOSE_WINDOW`；Wayland 发 `Alt+F4` 关闭 | 保留未保存文档提示，不强杀进程 |
| 退出当前应用（`quit_app`） | 正常退出请求 / `⌘Q` | 向同进程顶层窗口发送正常关闭请求 | X11 向同 PID 窗口发送 `_NET_CLOSE_WINDOW`；Wayland 发 `Ctrl+Q` | 尊重保存确认框 |
| 锁屏（`lock_screen`） | `⌃⌘Q` | 原生 `user32.LockWorkStation()` | `loginctl lock-session`（兜底桌面锁屏键） | Windows 不发模拟 `Win+L`，彻底消除 `Win` 键粘滞 |
| 截图 | 区域、全屏、剪贴板、工具栏 4 项独立动作 | `Win+Shift+S` 截图选区 | `PrintScreen` 系统截图 | 按平台提供原生截图入口 |
| 窗口总览 / 显示桌面 / 系统搜索 | 读取本机系统快捷键缓存 | `Win+Tab` / `Win+D` / `Win+S` | `Super` 活动总览 / `Ctrl+Alt+D` | 各用本机名称与标准行为 |
| Windows 专属常见操作（9 项） | — | 资源管理器 `Win+E`、剪贴板历史 `Win+V`、切换输入法 `Win+Space`、左/右虚拟桌面 `Ctrl+Win+←/→`、窗口左/右贴靠 `Win+←/→`、最大化当前窗口 `Win+↑`、任务管理器 `Ctrl+Shift+Esc` | — | `0.2.0` 已纳入 Windows 动作面板常见操作 |

## 2. 进阶自动化与浮层能力对照

| 能力模块 | macOS | Windows | Linux（X11） | Linux（Wayland） |
|---|---|---|---|---|
| 按前台应用自动切换按键层（App Layers） | ✅ `NSWorkspace` 激活通知（`bundle_id`） | ✅ `SetWinEventHook` + 按需缓存（`exe:<name>`，穿透 UWP `CoreWindow`） | ✅ EWMH `_NET_ACTIVE_WINDOW`（`desktop:<id>` / `wm_class`） | ⚠️ 受合成器隔离限制，固定使用全局默认层 |
| 自动聚焦聊天输入框 + 语音前聚焦 | ✅ Accessibility `AXUIElement` | ✅ COM `IUIAutomation`（支持 Electron / Chromium / 原生控件） | — | — |
| 界面控件动作（`ui_element` 悬停捕获与点击/聚焦） | ✅ `AXPress` / `AXFocused` | ✅ UIA `InvokePattern` / `LegacyIAccessible` / `SetFocus` | — | — |
| 应用菜单命令（`app_menu` 搜索与执行） | ✅ `AXMenuBar` | ✅ 传统 Win32 `HMENU` + 现代 UIA `MenuBar` | — | — |
| 3×3 九宫格面板 / 键位速查 / 方案切换提示 | ✅ 非激活全空间悬浮 + 鼠标穿透 | ✅ `WS_EX_NOACTIVATE` + `WS_EX_TRANSPARENT` 不抢焦点 | ✅ `WindowDoesNotAcceptFocus` + `WindowTransparentForInput` | ✅ 不调用跨屏绝对坐标 `move()`，交由合成器居中 |
| 遥控器语音虚拟麦克风与自动切麦 | ✅ `RemoteHubAudio.driver` + CoreAudio 临时切麦 | ✅ VB-CABLE + `IPolicyConfig` 三角色临时切麦 | ✅ `pactl` / `pw-loopback` 免驱虚拟麦 + `pactl` 临时切麦 | ✅ 同左（PipeWire / PulseAudio） |

## 3. 系统快捷键读取与自定义说明

- **macOS**：23 项命名系统动作在启动时读取本机系统设置配置，可在「通用设置 → 系统快捷键」手动刷新；未启用项明确提示原因。
- **Windows / Linux**：常见系统组合键按平台标准执行；若用户在系统设置或桌面环境（GNOME / KDE）中改过默认快捷键，可直接使用「按键 / 组合键」录制实际按键。
- 参考：[Microsoft Windows 快捷键](https://support.microsoft.com/en-gb/accessibility/windows/keyboard-shortcuts-in-windows)、[Apple Mac 快捷键](https://support.apple.com/en-us/102650)。
