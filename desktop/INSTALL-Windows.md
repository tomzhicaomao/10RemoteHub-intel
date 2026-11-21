# Windows 安装与使用

完整接入遥控器需要用 **Zadig** 配置 USB 驱动；**VB-CABLE** 仅在使用遥控器麦克风时需要，使用电脑麦克风无需安装。它们不随本项目分发，需要向原提供者获取。应用本身解压即用（支持多显示器高分屏 Per-Monitor V2 DPI 缩放）。

> 发布包位于 [GitHub Releases](https://github.com/yizhixiaoheigou/10RemoteHub/releases)。先核对同一版本的 `SHA256SUMS`；源码环境与构建见 [RELEASE.md](RELEASE.md)。

> Windows x64 已实机验证基础按键与语音链路。

## 一、准备两个组件

### 1. Zadig（给遥控器接口换驱动）

遥控器的接收器（`0416:0602`）是复合 USB 设备，其中两个关键接口需要交给本项目通过 WinUSB 独占读取：

- **Interface 3（语音接口）**：负责遥控器麦克风音频流与语音键握手；
- **Interface 4（全键位接口）**：负责完整原始按键报文（含 Raw Input 会丢弃的 `#` 号键，并避免按键向系统键盘重复触发）。

配置步骤：

1. 从 [zadig.akeo.ie](https://zadig.akeo.ie/) 下载 Zadig（本项目不附带；应用里「通用设置 → 前往下载 Zadig」即打开此页面）。
2. 插上遥控器接收器。
3. 打开 Zadig → 菜单 **Options → List All Devices** 打勾。
4. 下拉框里选 **`… (Interface 3)`**，目标驱动选 **WinUSB**，点 **Replace Driver**，等待完成。
5. 再选 **`… (Interface 4)`**，同样选 **WinUSB**，点 **Replace Driver**。
6. 首次换完驱动后，**重新插拔一次 USB 接收器**，在应用「通用设置」点「重新检测」，确认「遥控器语音接口（Interface 3）」与「遥控器全键位接口（Interface 4）」两枚状态胶囊均显示 **已绑定**。

> 只绑这两个即可。Interface 1/2 会由应用自动尝试顺带接管；即使 Interface 4 暂未绑定，应用也会自动降级到 Raw Input 提供除 `#` 键外的普通按键。

### 2. VB-CABLE（虚拟麦克风，可选）

仅在将语音来源设为「遥控器麦克风」时需要安装：

1. 从 [vb-audio.com/Cable](https://vb-audio.com/Cable/) 下载 `VBCABLE_Driver_Pack43.zip`（应用「语音设置 → 前往下载 VB-CABLE」即打开此页面）。
2. 解压，**右键 `VBCABLE_Setup_x64.exe` → 以管理员身份运行** → 点 **Install Driver**。
3. **重启电脑**。

装完后系统里会出现：

- 播放端 `CABLE Input (VB-Audio Virtual Cable)`
- 录音端 `CABLE Output (VB-Audio Virtual Cable)`

> 若您的电脑安装了多条虚拟音频线（如 `VB-Audio Cable A/B` 或带前缀的多实例），可在「语音设置 → 虚拟声卡播放端」下拉框中指定目标播放端，程序在录音临时切换默认麦克风时会自动配对同组的 `CABLE Output` 录音端。

## 二、安装与日常使用

1. 下载发布页的 `10RemoteHub-<版本>-win-x64.zip`，解压到固定目录（例如 `D:\10RemoteHub`）。
2. 双击 `10RemoteHub.exe`。若 SmartScreen 拦截，先核对下载来源与该版本的 SHA-256；确认后按系统提供的选项继续。重复双击 `10RemoteHub.exe` 会自动唤醒已运行的托盘实例（并保持原窗口最大化状态）。
3. 左侧应出现 `USB 0416:0602`；按遥控器任意键，右侧对应按键会高亮。
4. **选择应用与进阶能力**：
   - 在「打开 / 收起 App」、「应用专属层」或「自动聚焦聊天输入框」中选择程序时，系统会自动扫描开始菜单、桌面、`%LOCALAPPDATA%\Programs` 官网 `.exe` 安装目录及 MSIX 应用包（如 Kimi、ChatGPT / Codex、Claude、Cursor、微信、豆包、Windows 终端等），支持通过 **从已安装软件添加…** 搜索点选或 **选择本地 EXE…** 任意指定。
   - Windows 版现已支持 **自动聚焦聊天输入框**（通用设置开启并勾选目标聊天软件）、**界面控件动作**（默认支持「自动识别切到的前台软件」，鼠标悬停约 1 秒自动捕获按钮或滚动区）与 **应用菜单命令**（支持传统 Win32 菜单栏与现代 UI Automation 菜单栏），无需管理员权限。

## 三、配置语音

1. 打开 **语音设置**：
   - 语音来源：**遥控器麦克风**（或 **仅触发识别**）
   - 麦克风连接方式：**录音时切换系统默认麦克风**（推荐）
   - 识别快捷键：设成识别软件里“按住说话”用的键
   - 语音前聚焦（可选）：若在通用设置开启了「自动聚焦聊天输入框」，可联动按语音键前先把光标定位回聊天输入框。
2. 打开 **通用设置**，确认「遥控器语音接口（Interface 3）」显示 **已绑定**。

### 识别软件怎么设

| 识别软件 | 能否用本项目唤醒 | 说明 |
|---|---|---|
| **微信输入法** | ✅ | 识别快捷键设 `Alt`；麦克风选 **系统默认** |
| 豆包输入法 | ❌ | 会过滤所有程序模拟的按键，遥控器无法唤醒 |
| Windows 语音输入（Win+H） | 能唤出，但不识别本项目音频 | 不推荐 |

用「录音时切换系统默认麦克风」时，程序会在按住语音键期间把系统默认麦克风临时切到配对的 `CABLE Output`，松手立刻恢复原设备。因此识别软件的麦克风保持「系统默认」即可。若改为「固定使用虚拟麦克风」，则在识别软件里直接选择 `CABLE Output (VB-Audio Virtual Cable)`。

## 四、已知限制与权限边界

- **遥控器按键不会进入系统键盘**：本项目通过 WinUSB 独占接收器，按键只在应用内触发映射动作（避免与系统默认按键重复触发）；应用未运行时遥控器不产生按键。
- **管理员窗口（UIPI 权限隔离）**：当 `10RemoteHub` 以普通用户权限运行时，Windows 用户界面特权隔离（UIPI）会阻止向“以管理员身份运行”的前台窗口（如管理员终端、任务管理器等）发送模拟按键或执行 UI Automation 操作。此时「通用设置 → 系统输入隔离」会给出明确提示。
- **豆包输入法无法被唤醒**：受其自身反模拟按键策略限制，非本项目故障。

## 五、卸载

1. 右键托盘图标退出程序，删除解压目录（若开启过「登录后自动启动」，先在通用设置中关闭，或删除当前用户 `Run` 注册表项中的 `10RemoteHub`）。
2. 设备管理器 → 找到遥控器接收器对应的 `Interface 3` / `Interface 4` → 右键卸载设备 → 勾选「删除此设备的驱动程序软件」。
3. 拔插接收器，Windows 会自动恢复系统默认 HID 驱动。
4. VB-CABLE 可用其安装目录里的 `VBCABLE_Setup_x64.exe`（管理员）移除，或从系统「已安装的应用」卸载。

## 六、出问题看这里

- 连接日志：`%LOCALAPPDATA%\RemoteHub\logs\connection.log`；聊天聚焦日志：`%LOCALAPPDATA%\RemoteHub\logs\chat-focus.log`
- 只读环境报告：`10RemoteHub.exe --diagnose report.json`
- 最小环境报告：`powershell -ExecutionPolicy Bypass -File scripts\collect_windows_logs.ps1`（源码目录内使用，不含用户名、序列号或个人路径）。
- 更细的架构、协议与验收记录见 [Windows 端说明](docs/WINDOWS-TESTING.md)。
