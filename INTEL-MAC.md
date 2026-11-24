# 10RemoteHub · Intel Mac / macOS 13 适配说明

这份目录是 [yizhixiaoheigou/10RemoteHub](https://github.com/yizhixiaoheigou/10RemoteHub)
源码（已同步上游 **v0.3.1**，2026-11-24）的 **Intel Mac 改造版**，目标机器是 2017 款 MacBook Pro
（MacBookPro14,1，i5-7360U，8 GB，**macOS 13.7.8 Ventura，x86_64**）。

> **v0.3.1 同步说明**：上游 0.3.x 是大重写——新增照片学键、九键拼音输入浮层、
> **浏览器网页输入框语音输入**、多遥控器管理、蓝牙直连（BLE）、按设备屏蔽原按键、
> 「还差几步」设置清单，并新增 `10remotehub-cli` 命令行工具。
> 本仓库在保留全部上游功能的前提下，重新打上 Intel / macOS 13 适配补丁
> （PySide6 6.11.3、动态最低系统版本、架构检测、一键脚本与 `.app` 启动壳）。
>
> **浏览器语音输入的边界**：上游「浏览器遥控器麦克风语音输入 + 九键拼音上屏」只在
> **macOS arm64** 实机验证过；Intel 路径的代码已同步，但上游从未在 Intel 真机测过，
> 需你本人用 0416:0602 接收器实测。语音键要出字仍依赖你配置的识别软件（见下）。

## 结论先说

| 能力 | 状态 |
| --- | --- |
| 桌面程序启动、按键映射界面、配置读写 | ✅ 已实测跑通 |
| 自动化测试 / 离屏启动 | ✅ 699 passed, 5 skipped, 1 deselected（v0.2.0）；UI smoke 0 |
| 虚拟麦克风（CoreAudio 插件）编译 | ✅ 已实测，x86_64 + arm64 双架构，隔离探针通过 |
| USB 采集组件编译 | ✅ 已实测 x86_64 编译通过 |
| 实体遥控器按键 / 语音 | ⚠️ 一键安装脚本已备好；需一次管理员密码 + 0416:0602 接收器，未做实机验收 |
| 组件签名 / 签名宿主 | ✅ 自签证书 + 签名版采集组件 + 签名启动器，XPC 互校规则实测通过 |
| 可双击启动的 `10RemoteHub.app` | ✅ 已打包（启动壳式），签名有效、x86_64、离屏启动 0；见下文 |
| 可对外分发的自包含 `.app` | ⚠️ 需补第三方许可材料，且 PySide6 6.11 无法冻结，见下文 |

## 为什么原版在这台机器上跑不起来

三个卡点，按严重程度：

1. **macOS 14 硬门槛（真正的拦路虎）**
   `PySide6==6.12.0` 的官方 macOS wheel 只有 `macosx_14_0_universal2`，
   pip 在 macOS 13 上会直接判定平台不兼容；就算强行装上，Qt 二进制也按
   14.0 部署目标编译。上游 `RELEASE.md` / `INSTALL-macOS.md` 因此写明
   「macOS 14 或更新」。这台机器是 2017 款，官方最高只能到 Ventura 13，
   升级系统这条路走不通，只能改依赖。

2. **没有 Intel 发布包**
   上游 Releases 只提供 `macOS arm64` 与 `Windows x64`。源码本身其实已经
   预留了 Intel 支持：`requirements-macos-x86_64.lock`、universal2 的 C 组件、
   `make_update_manifest.py` 里的 `arm64/x86_64` 双架构，但维护者明确写了
   「没有 Intel Mac，不做真机测试，不承诺兼容性」。

3. **`LSMinimumSystemVersion` 写死 14.0**
   `scripts/build.py` 给 `.app` 盖的是 14.0，即使依赖降级，打出来的包在
   13 上也会被系统拦下。

## 改了什么

| 文件 | 改动 |
| --- | --- |
| `desktop/pyproject.toml` | Intel Mac 用 `PySide6==6.11.3`，其它平台保持 6.12.0（PEP 508 `platform_machine` marker） |
| `desktop/requirements-release-macos13.txt` | 新增：macOS 13 / x86_64 依赖约束源 |
| `desktop/requirements-macos13-x86_64.lock` | 新增：带 SHA-256 哈希的完整锁（32 项），由上游 x86_64 锁派生 |
| `desktop/scripts/make_macos13_lock.py` | 新增：可复现地重生成上面的锁（从 PyPI 拉取 4 个 Qt 包哈希） |
| `desktop/scripts/build.py` | `LSMinimumSystemVersion` 按实际 PySide6 版本动态取值：6.11.x → `13.0`，6.12+ → `14.0` |
| `desktop/remotehub/updates.py` | 更新检查的 `arch` 默认值不再硬编码 `arm64`，改为读取 `platform.machine()` |
| `desktop/scripts/verify.py` | 新增 `--deselect` 参数（用于跳过需要交互钥匙串的发布流程测试） |
| `desktop/setup-intel-mac.sh` | 新增：一键建环境 + 装依赖 + 跑验证 |
| `desktop/run-intel-mac.command` | 新增：Finder 双击启动；检测到签名宿主时自动改用它 |
| `desktop/安装遥控器组件.command` | 新增：造自签证书 → 编译签名采集组件 → 签名 Python 宿主 → 安装系统服务 → 自检 → 启动 |
| `desktop/卸载遥控器组件.command` | 新增：完整撤销上述所有系统级改动 |
| `README.md` / `desktop/INSTALL-macOS.md` / `desktop/RELEASE.md` | 加上指向本文档的适配说明 |

**为什么是 6.11.3**：PySide6 各版本的 macOS wheel 标签依次是
6.10/6.11 → `macosx_13_0_universal2`，6.12 → `macosx_14_0_universal2`。
6.11.3 是最后一个官方支持 macOS 13 的版本，其 universal2 wheel 同时含
x86_64 与 arm64 切片。项目用到的 `Qt.ColorScheme`、
`QStyleHints.colorScheme()` / `setColorScheme()` 在 6.11 均已存在，实测可用。
6.12 才拆出的 `PySide6-PDF` / `PySide6-WebEngine` 两个包在 6.11 里属于
Addons，且本项目本来就显式排除这些模块，所以从锁里去掉即可。

改动只涉及依赖版本、打包元信息和两处架构默认值，**没有动业务逻辑，
没有放宽签名校验或权限检查**。

## 一键安装与启动

```sh
cd 10RemoteHub/desktop
sh setup-intel-mac.sh
```

脚本只在本项目内写 `.venv/` 和 `../artifacts/`，不改系统设置、不装驱动、
不申请权限。它要求 Python 3.12 / 3.13 / 3.14，本机自动找到
`/Users/thomas/.local/bin/python3.12`（**3.12.13，正是上游验证基线**）；
也可用 `REMOTEHUB_PYTHON=/path/to/python sh setup-intel-mac.sh` 指定。
要重建环境加 `--recreate`。

启动方式（在 `desktop/` 下）：

```sh
.venv/bin/python -m remotehub                 # 启动界面
.venv/bin/python -m remotehub --permission-status
.venv/bin/python -m remotehub --diagnose /tmp/rh-report.json
.venv/bin/python -m pytest -q                # 重跑测试
```

或者直接在 Finder 里双击 `10RemoteHub.app`（无终端窗口、可固定到 Dock，推荐）；
先跑一次 `打包成app.command` 生成它。也可双击 `run-intel-mac.command`
（会开一个终端窗口，关掉窗口即退出程序）。

常用验证命令：

```sh
.venv/bin/python scripts/verify.py \
  --output ../artifacts/verification-macos13.json \
  --deselect tests/test_device_helper_package.py::test_self_made_certificate_is_pinned_without_apple_team
```

## 实测记录（2026-11-15 初版，2026-11-21 同步 v0.2.0 后复测）

- 环境：`macOS-13.7.8-x86_64`，**Python 3.12.13（上游基线）**，
  `PySide6 6.11.3 / Qt 6.11.3`，Qt Multimedia 自带 FFmpeg 7.1.5 (LGPL)。
- `sh setup-intel-mac.sh` 端到端跑通（退出码 0）：哈希锁 32 个包全部命中。
- 回归测试：**699 passed / 5 skipped / 1 deselected**（v0.2.0），离屏 UI 启动返回 0。
  初版（0.1.0）为 501 passed / 2 skipped / 1 deselected。
- 旧版曾在 managed Python 3.13.12 上单独验过一遍（500 passed / 3 skipped /
  1 deselected，UI smoke 0）；v0.2.0 以 3.12 基线为准。
- 主窗口离屏渲染截图正常（中文字体、侧栏、27 键遥控器示意图、动作面板）。
- `sh scripts/build_macos_audio.sh`：loopback 测试通过，
  `ISOLATED_PLUGIN_PASS`，生成 `RemoteHubAudio.pkg`（43 KB），
  插件为 `x86_64 + arm64` fat binary。
- `scripts/build_device_helper.py --adhoc`：x86_64 原生采集组件编译通过。
- 追加验证（组件安装链路）：自签证书 + `--codesign-identity <SHA1>` 编译出两个
  pkg；`codesign -v --strict --requirement='identifier … and certificate leaf = H"…"'`
  对三个采集二进制和签名宿主全部返回 0；签名宿主离屏启动返回 0。

### 那一个被跳过的测试

`test_self_made_certificate_is_pinned_without_apple_team` 会在临时钥匙串里
造一张自签名代码签名证书再 `codesign`。在 macOS 13 上
`security find-identity -v -p codesigning` 对这张证书返回 `0 valid identities`，
`codesign` 报 `The specified item could not be found in the keychain`；
补齐信任需要 `security add-trusted-cert` 触发交互式 SecurityAgent 授权，
无人值守跑不了。它验证的是**特权安装包**的证书锁定，与桌面程序、Qt、
x86_64 都无关，所以 Intel 验证流程显式 deselect，其它 699 项照常跑。
想完整跑这一项，在有图形会话的终端里手动执行并允许钥匙串授权即可。

## 三层可用性，别混为一谈

**第一层：源码运行（已完成，即上面的「一键安装」）**
界面、按键映射配置、窗口/媒体动作、快捷键、语音设置都能用。
但源码树里没有编译好的 `native/libRemoteHubDevice.dylib` 和
`components/*.pkg`，所以界面左侧会显示「统一采集组件未安装」，
**实体按键不会生效**。上游发布包之所以开箱即用，是因为这些产物已经
随签名 `.app` 一起打包好了。

**第二层：可双击的 `10RemoteHub.app`（双击 `打包成app.command` 生成）**
在 Finder 里双击 `desktop/10RemoteHub.app` 即可启动，有正式图标、能固定在 Dock。
它是「启动壳」式 `.app`：`Contents/MacOS/10RemoteHub` 是一个 x86_64 的 C 启动器，
`exec` 项目 `.venv/bin/python-remotehub` 并把参数转发给 `-m remotehub`。
这样最终进程的签名身份仍是 `org.remotehub.desktop`，特权采集服务的证书互校照常通过。

- 打包脚本：`desktop/打包成app.command`，幂等，可重复运行；生成的包在
  `desktop/10RemoteHub.app`。前置条件是已跑过 `安装遥控器组件.command`
  （需要其中的自签证书与签名宿主）。
- 实测：`codesign --deep --strict` 通过（`valid on disk` +
  `satisfies its Designated Requirement`），架构 x86_64，离屏启动返回 0、
  参数转发正确、界面渲染正常、XPC 同款 requirement 返回 0。
- **边界**：这个 `.app` 依赖项目目录里的 `.venv`（1.3 GB），**不能单独拷走**
  或当作可分发的独立应用。它解决的是「在这台 Mac 上双击就能开」，不是分发。

上游 `scripts/build.py` 那种**自包含** `.app` 仍未做：它需要 `--license-supplement`
（第三方许可材料）和 `--codesign-identity`，且 `prepare_macos_licenses.py` 把
Qt 6.12.0 / Python 3.12.13 写死成校验基线，要出正式包必须同步更新许可清单与
源码归档哈希（LGPL 合规）。更关键的是 PySide6 6.11.3 无法被 PyInstaller 冻结
（见下文），所以自包含路线在本机目前无解。

**第三层：实体遥控器 / 语音（双击 `安装遥控器组件.command`）**

前面那串手工步骤已经全部封装进脚本。它会自动完成：

1. 生成一张**自签名代码签名证书**（RSA 2048，10 年），放进项目内的专用钥匙串
   `build/local-signing/10RemoteHub.keychain-db` 并加入用户钥匙串搜索列表。
   不需要 Apple Developer 账号、不需要改登录钥匙串密码、不需要 sudo。
2. 用该证书的 SHA-1 重编采集组件
   （`--codesign-identity <SHA1>` + `RH_ARCH_FLAGS="-arch x86_64"`），
   产出 `RemoteHubDevice.pkg` / `RemoteHubDevice-Uninstall.pkg`。
3. 复制 venv 的 Python 为 `.venv/bin/python-remotehub`，补一个
   `.venv/lib/libpython3.12.dylib` 链接解决 `@executable_path/../lib` 的 rpath，
   再用同一证书签成 `identifier org.remotehub.desktop`。
4. 把 `libRemoteHubDevice.dylib` 与所有 pkg 放到源码运行模式查找的
   `native/` 与 `components/`，然后 `sudo installer` 安装并注册 LaunchDaemon。
5. 自检 `installation_status()` 应为 0，最后通过 `run-intel-mac.command` 启动。

**为什么最终是「签名宿主 + 启动壳 .app」，而不是 PyInstaller 自包含 .app**：
`Daemon.m` 要求连接方满足
`identifier "org.remotehub.desktop" and certificate leaf = H"<SHA1>"`，
所以源码方式直接跑的 Python 连不上特权服务。先试过 PyInstaller 冻结成
自包含签名 `.app`，但 **PySide6 6.11.3 在冻结环境里会陷入
`feature_import` / `lazy_import` 无限递归**（`sample` 抓栈确认，进程 100%
卡在 shiboken 导入），而能避开它的 6.12 在这台 macOS 13 上根本装不上。
于是给 Python 宿主重签（满足 XPC 身份）+ 外面套一个极简启动壳 `.app`
（满足「双击就有图标」），两者组合完整复用已跑通 699 项测试（v0.2.0）的源码版本。
已弃用的 PyInstaller 产物（含 `dist/`）已删除。

钥匙串维护提示：自签证书放在专用钥匙串
`build/local-signing/10RemoteHub.keychain-db`，密码固定为 `remotehub`，
与你的登录钥匙串密码无关。若哪天 `codesign` 报 `errSecInternalComponent`
或 `find-identity` 归零，多半是该钥匙串被锁定/私钥 partition 失效；
`打包成app.command` 与 `安装遥控器组件.command` 都已内置「解锁 + 重设
partition list + 用固定参数重建搜索列表」的幂等修复（切勿用 word splitting
重建含中文路径的搜索列表）。

实测（未安装系统服务前）：签名宿主的 `codesign -v --strict --requirement=…`
退出码 0，Python 3.12.13 / PySide6 6.11.3 正常加载，离屏 `--smoke` 返回 0
并渲染出界面；`installation_status()` 返回 `(1, "尚未安装")`，符合预期。

仍需你本人完成：双击脚本并输入一次管理员密码、插上 0416:0602 接收器、
必要时在「系统设置 → 隐私与安全性 → 辅助功能」打开开关（macOS 不允许脚本
代改 TCC）。虚拟麦克风是可选的 `build/RemoteHubAudio.pkg`，安装会短暂重启
系统音频服务。**这些系统级动作我没有自动执行，也没有实体硬件实机验收**；
上游自己也注明「实体按键与语音沿用此前实机结论，本轮不重复实测」。
不想用了双击 `卸载遥控器组件.command` 可完整撤销。

**手头没有接收器时**：它是讯飞给运营商 IPTV 机顶盒配的 2.4G 语音遥控器
的随附 USB 小接收头，不单独零售，要买「带 USB 接收器」的整套（约 ¥17–40）。
选购链接、外形图与避坑清单见 `outputs/10RemoteHub-接收器购买指引.html`。
买回来先双击 `desktop/检查接收器.command`，它会列出所有 USB 设备的
VID:PID 并判断是否命中 `0416:0602`（十进制 1046:1538），避免买错型号。

## macOS 13 上的使用差异

- 权限面板叫「系统设置 → 隐私与安全性」（Ventura 起的新版设置），
  上游截图/文案按 macOS 14 写，个别按钮文字会略有差别，功能路径一致。
- 首次打开自签名 `.app` 要在「隐私与安全性」里点「仍要打开」；
  源码运行（`.venv/bin/python`）不受 Gatekeeper 这一层限制。
- 上游 `INSTALL-macOS.md` 里「macOS 14 或更新」的要求，对本改造版
  改为 **macOS 13.0+ / x86_64**；Apple 芯片与 Windows 仍按上游流程。

## 已知限制

- 本改造只在 macOS 13.7.8 / x86_64 上实测（Python 3.12.13 与 3.13.12 各一遍），
  没有在 12.x 或 Apple 芯片上验证。
- 自动更新通道默认未配置；`updates.py` 的默认架构已修正为按本机判断，
  但上游发布清单里没有 Intel 资产，检查更新不会找到可用包。
- 自签证书与签名宿主只在这台机器上有效，重建环境或换机器必须重新运行安装脚本；
  卸载后重装会换一张新证书，辅助功能授权可能要重新点一次。
- 本机没有走 PyInstaller 打包（PySide6 6.11.3 冻结导入递归），因此没有
  可分发的 `.app`；这不影响源码运行，但也就没有 Gatekeeper/公证那一层。
- 许可材料（`licenses/vendor/SOURCES.json`、`macos-sources.json`、
  `prepare_macos_licenses.py`）仍指向 Qt 6.12.0。**源码运行不受影响**；
  若要对外分发二进制包，必须先把这些材料更新到 6.11.3，否则不符合
  上游的发布门禁与 LGPL 归属要求。
