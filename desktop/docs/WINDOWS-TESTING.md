# Windows 端说明

本文记录 Windows 实现的结构、0.2.0 专项优化、依赖、验收结果和已知限制。安装步骤见 [INSTALL-Windows.md](../INSTALL-Windows.md)。

## 1. 架构

Windows 与 macOS / Linux 采用同一个模型：**采集程序独占接收器，主程序只处理业务**。

```text
遥控器
  │ 2.4G
接收器（USB 0416:0602）
  ├─ 接口 1 消费者控制 ─┐
  ├─ 接口 2 鼠标       ├─ windows_collector.py：WinUSB 独占，读原始 HID 报文
  ├─ 接口 3 厂商语音   │   用 HIDReports 解析成固定键表的 hid:* 标识
  └─ 接口 4 键盘      ─┘
        │
        ├─ 按键事件 → controller.on_key → 动作（VK 组合键 / 鼠标 / UIA 控件与菜单 / 浮层 / 动作链…）
        └─ 语音会话 → G.722.1 解码 → AudioBridge → CABLE Input → 识别软件
```

核心要点与 `0.2.0` 专项优化：

- **进程内 `<1 ms` SetupAPI 枚举与双接口状态诊断**：`inventory.py` 与 `windows_driver.py` 直接在进程内通过 `setupapi.dll`（`SetupDiGetClassDevsW` + `DEVPKEY_Device_Service`）枚举 `USB\VID_0416&PID_0602` 及 `MI_03` / `MI_04` 服务绑定状态，完全取代旧版每 2 秒拉起 `powershell.exe` 子进程的做法（消除后台 CPU 抖动与控制台开销），并在通用设置分别展示 Interface 3 与 Interface 4 绑定状态。
- **按键来源于原始报文，不是 Raw Input**：Windows 键盘驱动会直接丢弃遥控器 `#` 键（HID 用法 `0xCC`），且 Raw Input 无法抑制物理按键。`windows_collector.py` 独占接口 1/2/3/4 并补齐与 macOS 一致的厂商语音帧（`01 82 03` 按下请求、`01 86 00` 松开结束）；Raw Input 仅在接口 4 未绑定时作为降级路径（失败停止时正确发出 `capture_stopped` 状态信号）。
- **与输入法/Shift 无关的虚拟键码注入与原生滚轮/锁屏**：`actions.py` 在 Windows 下优先按物理虚拟键码（`VK_A`–`VK_Z`、`VK_0`–`VK_9`、OEM 符号键）投递 `KeyCode.from_vk(...)`，避免 `Shift` 组合键或中文输入法激活时字符映射失效；补齐 `KeyOutput.scroll()` 滚轮支持（及 PyInstaller `pynput.mouse._win32` 隐藏导入）；锁屏直接调用 `user32.LockWorkStation()`（消除模拟 `Win+L` 导致的 `Win` 修饰键残留卡死）。
- **纯 `ctypes` Windows UI Automation (`win_uia.py`) 与零轮询前台跟踪**：
  - 无额外第三方依赖，封装 COM `CUIAutomation`（MTA 工作线程）实现 **聊天输入框自动聚焦**（`chat_focus` + `voice_auto_focus`）、**界面控件悬停捕获与点击/聚焦**（`ui_element`）以及 **应用菜单命令**（`app_menu`，同时兼容传统 Win32 `GetMenu` 与现代 UIA `MenuBar`）；
  - 使用 `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` 零轮询监听前台窗口切换，支持解析 UWP `ApplicationFrameHost.exe` 内部的真实 `CoreWindow` 进程、读取 `version.dll` `FileDescription` 应用友好名称，以及纯二进制解析开始菜单 `.lnk` 快捷方式；
  - 检测前台窗口管理员提权状态（UIPI `TokenElevation`），受阻时给出明确诊断提示。
- **非抢焦点屏幕浮层与多显示器 DPI**：启用 Per-Monitor V2 DPI 感知；`ui/osd.py` 为速查浮层、九宫格与方案切换提示应用 Win32 `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST`（纯提示浮层叠加 `WS_EX_TRANSPARENT` 鼠标穿透）与 `SetWindowPos(..., SWP_NOACTIVATE)`，确保弹出与点击不抢夺当前前台窗口焦点。

### 语音协议（与 macOS / Linux 采集一致）

| 方向 | 端点 | 内容 |
|---|---|---|
| 读 | `0x84` 中断 IN | `01 82 03` / `01 86 …` 语音键帧；`01 81 …` 音频帧；`01 08 01 00` / `01 01 01 00` 握手；`01 ff` 停止确认 |
| 写 | `0x05` bulk OUT | `01 08 01 00` 就绪；`01 82 01 00` + `01 08 01 00` 开始；`01 01 01 00` 进入音频；`0a 54 45` 每秒保活；`01 00` 停止 |

### 虚拟麦克风与多声卡路由

Windows 依赖外部 **VB-CABLE** 虚拟声卡：

- 应用把解码后的 PCM 播放到选定的 `CABLE Input`（多条虚拟线时可在语音设置下拉切换）；
- 「录音时切换系统默认麦克风」模式下，`WindowsInputRoute` 通过 `IPolicyConfig` 在录音期间将系统默认录音端（console / multimedia / communications 三个角色）临时切换到同组配对的 `CABLE Output`，结束或异常退出时恢复原设备。

## 2. 依赖与绑定

| 组件 | 作用 | 是否随项目分发 |
|---|---|---|
| Zadig | 把接口 3、4 绑到 WinUSB（一次性） | 否，用户自行下载 |
| VB-CABLE | 虚拟声卡 | 否，用户自行安装 |
| libusb / pyusb | 访问 WinUSB 接口 | 是（Python 依赖） |
| PySide6 | 界面与音频输出 | 是 |
| Win32 / COM UIAutomation | 窗口、快捷键、聊天聚焦、控件与菜单自动化 | 系统自带（`ctypes` 直接调用） |

## 3. 验收记录

### 2026-11-18 `0.2.0` 原生构建与门禁验证

验证环境：Windows 11 x64（`10.0.26200`），Python 3.12.10。

- 完成 SetupAPI 进程内枚举、双接口（`Interface 3` / `Interface 4`）状态胶囊、VK 物理键码投递、`LockWorkStation()`、Win32 非激活浮层（`WS_EX_NOACTIVATE` + `WS_EX_TRANSPARENT`）、Windows UIA 聊天聚焦 / 控件悬停捕获（含自动识别任意前台 `.exe` 模式、Electron `Chrome_RenderWidgetHostHWND` 唤醒与多进程/WebView2 归属匹配）/ 菜单动作、官网 `.exe` 与 MSIX 自动发现及 9 项 Windows 专属常见动作优化；
- Windows 11 原生全量自动化回归 **642 passed / 63 skipped**、离屏 `--smoke` 通过；
- PyInstaller 原生打包生成 `10RemoteHub-0.2.0-win-x64.zip`，通过 `release_policy.py` 门禁、651 个文件 + 756 个冻结模块字节码零个人标识隐私扫描、干净目录解包启动与 `--diagnose` 脱敏核验，原有用户配置保持不变。

### 2026-11-12 发布文件

Windows 已实机验证基础链路。原生自动化 442 passed / 57 skipped；ZIP 在独立目录通过解压启动、旧版替换、回滚、卸载和配置保留，诊断报告完成脱敏核对。

### 2026-11-09 端到端实机验证

验证环境：Windows x64，Python 3.12.10。接口 1/2/3/4 独占、`#` = `hid:7:204`、语音键按下/松开、16 kHz 单声道回录峰值 RMS 0.30、微信输入法端到端出字全部通过。

## 4. 已知限制

1. **豆包输入法无法唤醒**：过滤程序模拟按键；微信输入法可用。
2. **管理员权限窗口（UIPI）**：普通权限进程无法向管理员窗口注入输入或读取可写 UIA 焦点。
3. **Windows 语音输入（Win+H）**：能唤醒但识别不到虚拟声卡音频。
4. 应用未运行时遥控器不产生按键（WinUSB 完整重映射预期行为）。

## 5. 排障

| 现象 | 检查 |
|---|---|
| 左侧不显示 `USB 0416:0602` | 接收器是否插好；`10RemoteHub.exe --diagnose report.json` 的 `usb` 字段 |
| 按键无反应或 `#` 键无效 | 通用设置查看 `Interface 4` 是否显示「已绑定」；前台是否为管理员权限窗口（查看 UIPI 提示） |
| 语音不可用或灯常亮 | 通用设置查看 `Interface 3` 是否显示「已绑定」；检查 `connection.log` 的 `request stop` 与 `01ff` |
| 输入法有电平但不出字 | 识别快捷键是否与输入法一致（微信输入法为 `Alt`） |
| 输入法没有电平 | 连接方式选「录音时切换系统默认麦克风」并把输入法设为系统默认；多虚拟声卡时核对选中的 `CABLE Input` |

## 6. 构建与调试

```powershell
cd <项目>\desktop
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -LicenseSupplement <材料清单绝对路径>
```

国内网络直连 PyPI 失败时脚本会自动回退镜像，也可显式传 `-IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple`。PyInstaller 不能交叉编译，Windows 包必须在 Windows x64 主机上原生构建。
