# 10RemoteHub

把 `0416:0602` USB 接收器配套的 2.4G 语音遥控器变成 **macOS / Windows** 的按键面板、屏幕浮层入口与语音入口。

- 27 个普通键可设置单击、双击、长按动作（或整键跟手鼠标左/右/中键）；语音键固定用于开始/结束语音。
- 支持快捷键、打开/收起应用、打开文件、输入文本（可选自动回车）、多步动作链、3×3 九宫格面板、键位速查浮层、按前台应用自动切换按键层，以及 macOS / Windows 界面控件点击、应用菜单命令与聊天输入框自动聚焦。
- 遥控器音频在本机解码送入虚拟麦克风，由用户选择的外部输入法或识别软件处理，支持录音期间临时切换系统默认麦克风并自动恢复。
- 默认诊断日志不记录按键内容、预设文本、聊天正文、录音或完整个人路径。

当前源码版本为 `0.2.0`，依赖基线为 Python 3.12 / PySide6 6.12.0。正式发布支持 macOS 14+（Apple 芯片）与 Windows 10/11 x64；Linux 支持已合入源码并通过离屏测试，因尚未完成实体硬件真机验证，本版本暂不分发 Linux 二进制发布包。

本目录是最新版本的独立源码快照，初始提交不包含开发仓库的旧历史。安装前请阅读对应平台说明并核对发布页提供的 `SHA256SUMS`。

## 界面预览

按键映射：选择遥控器上的按键，再设置单击、双击或长按动作。

![按键映射界面，显示未分配的示例配置](desktop/docs/screenshots/mapping.png)

语音设置：选择声音来源、遥控器操作方式和识别软件快捷键。

![macOS 语音设置界面](desktop/docs/screenshots/voice.png)

截图展示 macOS 界面与示例配置；左侧未完成 USB 检测，不代表设备已连接或通过实机验收。Windows 的驱动与虚拟麦克风设置见下方平台说明。

## 安装使用

从 [GitHub Releases](https://github.com/yizhixiaoheigou/10RemoteHub/releases) 下载对应平台的发布包，并核对同页的 `SHA256SUMS`。具体安装、授权和卸载步骤见下方平台说明：

- **macOS**：[安装、授权、语音配置、更新与卸载](desktop/INSTALL-macOS.md)。主程序放入「应用程序」，安装统一采集组件并授权辅助功能；使用遥控器麦克风时再安装虚拟麦克风。
- **Windows**：[解压运行、Zadig 接口绑定、语音配置与卸载](desktop/INSTALL-Windows.md)。完整接入遥控器需配置 WinUSB（Interface 3 / Interface 4）；使用遥控器麦克风时再安装 VB-CABLE。

开发者可按下方步骤安装源码依赖并运行自动验证。真实硬件接入仍需要平台组件和权限，安装 Python 依赖不会自动完成这些步骤。

## 从源码验证

macOS（Apple Silicon；Intel Mac 替换成 `requirements-macos-x86_64.lock`）：

```sh
cd desktop
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade 'pip>=26.2'
.venv/bin/python -m pip install --require-hashes -r requirements-macos-arm64.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python scripts/verify.py --output ../artifacts/verification-macos.json
```

Windows PowerShell：

```powershell
cd desktop
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade "pip>=26.2"
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-windows-x64.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe scripts\verify.py --output ..\artifacts\verification-windows.json
```

Linux 开发环境可使用 `requirements-linux-x86_64.lock` 运行同一套 `scripts/verify.py`（详见 [INSTALL-Linux.md](desktop/INSTALL-Linux.md)）。离屏测试使用模拟设备与临时配置，不安装组件、不接管物理 USB，也不修改系统音频设置。正式构建的签名身份、许可材料与构建流程见 [RELEASE.md](desktop/RELEASE.md)。

硬件验证边界说明：本轮 `0.2.0` 原生自动化验证在 macOS arm64 为 **700 passed / 5 skipped**、在 Windows 11 x64 为 **642 passed / 63 skipped**，两端离屏 `--smoke` 启动、许可门禁与冻结字节码隐私检查均通过。macOS 与 Windows 基础实体按键/语音链路及 macOS 聊天框自动聚焦已完成真机验证；Intel Mac 与 Linux 尚未进行真机测试，本次不提供对应二进制安装包，亦不将离屏自动化结果表述为真机验收。

## 使用与开发资料

- [版本更新日志](CHANGELOG.md)
- [macOS 安装与权限](desktop/INSTALL-macOS.md)
- [Windows 安装与第三方组件](desktop/INSTALL-Windows.md) · [Windows 架构与验收说明](desktop/docs/WINDOWS-TESTING.md)
- [Linux 源码适配说明（待真机验证）](desktop/INSTALL-Linux.md)
- [常见问题](desktop/TROUBLESHOOTING.md)
- [按键表](desktop/KEYMAP.md)、[架构](desktop/ARCHITECTURE.md)、[平台动作矩阵](desktop/docs/PLATFORM-ACTIONS.md)
- [参与开发](CONTRIBUTING.md)、[安全报告](SECURITY.md)

macOS 的设备采集组件与可选虚拟麦克风需要用户通过标准安装器安装。Windows 的 Zadig 和 VB-CABLE 由用户向原提供者获取，本项目不附带这些安装程序。

## 许可

项目原创代码采用 [MIT](LICENSE)。内置 Python 音频解码器沿用 LGPL-2.1-or-later，Apple 示例及其他第三方代码保留各自许可；MIT 不覆盖这些组件。详见 [第三方声明](desktop/THIRD_PARTY_NOTICES.txt)、[内置解码器来源](desktop/remotehub/codec/NOTICE.md) 和 [对应源码/修改版说明](desktop/licenses/SOURCE.md)。

当前遥控器图为程序生成的示意图；源码快照不含旧照片、个人诊断日志、配置或闭源解码器实验。`SOURCE-INVENTORY.json` 记录该快照各文件的 SHA-256。
