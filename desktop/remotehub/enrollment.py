"""Observed steps in generic remote enrollment and a safe support summary."""
from __future__ import annotations

import json
import re
import sys


_RECOGNIZED_KEY = re.compile(r"(?:hid:(?:7|12):[0-9]{1,5}|win:[0-9]{1,5}:[0246])\Z")

KEY_GUIDANCE = {
    "not_visible": "先插拔 USB 接收器，或在系统蓝牙设置确认已连接，再重新检测。系统已连接但此处仍看不到时，可复制接入诊断。",
    "select_device": "选择这只遥控器，再按下并松开一个键。",
    "waiting": "只按所选遥控器上的一个键，松开后等候确认。",
    "down_only": "已经收到按下。请松开；若仍卡在这一步，可换一个普通键再试。",
    "no_signal": "这次没收到所选设备的键。先检查连接、权限或占用，再试数字键或方向键。",
    "missing_release": "收到按下后没有收到松开。换一个键试试；其他键可以正常学习。",
    "multiple_keys": "这次收到多个信号。请只按一个键。如果同一个实体键反复发出多个信号，可复制接入诊断。",
    "duplicate_key": "这个信号已绑定到现有位置。若按的是另一颗实体键，可以复制接入诊断检查原始报告能否区分。",
    "confirmed": "已收到一对按下和松开。可以带入匹配的预设，或导图学习并保存。",
    "open_failed": "找到设备，但暂时无法打开按键接口。检查输入监控、设备占用或驱动状态后重试。",
    "enumeration_failed": "系统设备列表暂时读不到。重新检测；如果持续失败，可以复制接入诊断。",
    "disconnected": "这次试按已结束。重新连接并确认新的连接后再学习。",
}


def diagnostic_text(*, step, device=None, trigger="", opened=None, total=None, details=None):
    """Copy model and observations without device names, IDs, paths or packet contents."""
    report = {"schema": 1, "platform": sys.platform, "step": step}
    if device is not None:
        report["device"] = {
            "transport": device.transport, "vendor": f"{device.vendor:04X}", "product": f"{device.product:04X}",
            "stable_identity_available": bool(device.unit_key),
        }
    if _RECOGNIZED_KEY.fullmatch(trigger):
        report["tested_key"] = trigger
    if opened is not None:
        report["input_interfaces"] = {"opened": opened, "available": total}
    if details:
        report["observations"] = details
    return json.dumps(report, ensure_ascii=False, indent=2)


# -- capability boundaries and help requests for AI -------------------------------
#
# Each entry separates what this project actually did (with where the evidence
# lives) from directions worth trying. Nothing here is a fixed recipe: a new
# device may need a different route, and every conclusion needs a reproducible
# sample before code is written.

SKILL_URL = "https://raw.githubusercontent.com/yizhixiaoheigou/10RemoteHub/main/SKILL.md"

BOUNDARIES = {
    "private_usb": {
        "title": "私有 USB 接口",
        "signs": "系统 USB 列表里有这个设备，但它没有可读的键盘/媒体键接口；或接口被系统驱动独占、普通方式打不开。",
        "user": "这只设备的按键或声音走厂商自己的 USB 接口，普通方式读不到，需要协助适配。",
        "user_try": [
            "拔插接收器，换一个 USB 口，再点「重新检测」。",
            "在 Windows 电脑上，选中它后可以点「让 App 接管这个接收器」再试。",
        ],
        "proven": [
            "接收器 0416:0602：读 USB 配置描述符发现 5 个接口都是 HID class，但接口 3 有 0x84 interrupt IN 和 0x05 bulk OUT；接口 class 与端点类型要分开判断。",
            "系统 HID 驱动独占接口：Mac 上 libusb claim 返回 ACCESS、非特权 IOHID 独占返回 0xe00002c1。最终 Mac 用特权读取组件接管，Windows 用 Zadig 把 Interface 3/4 绑 WinUSB，Linux 用 udev + libusb。",
            "HID SetReport 返回成功但设备不应答，同样的命令走 bulk OUT 才有回包；之后逐条验证出最小录音流程（批准 01 82 01 00、同步 01 08、启动 01 01、每秒保活 01 0a）。",
            "Windows 的 # 键在 Raw Input 路径丢失，WinUSB 读接口 4 原始报文后按 usage 0xCC 解码恢复。",
        ],
        "try": [
            "先读设备与配置描述符，列出每个接口的 class、端点地址、方向和传输类型。",
            "确认是谁占着接口：Mac 看 ioreg，Windows 看设备管理器里的驱动；先只读监听，再决定是否接管。",
            "如果有厂商盒子、App 或 Android 设备能用这只遥控器，在那边抓 USB 包（usbmon、adb）对照命令序列。",
            "输出命令两条路都试：HID SetReport 和 bulk/interrupt OUT。",
            "Windows 可试 WinUSB 或 libusbK（Zadig 替换前备份原驱动，只改目标接口）；Mac 需要特权组件或 DriverKit。",
            "找公开资料：同芯片方案的开源驱动、Linux 内核 quirk、论坛抓包。",
        ],
        "deliver": "接口与端点清单、谁占用接口、最小可复现的读取或命令样本、读取端方案和回放测试。",
        "caution": "替换驱动、接管设备都会让原功能暂时失效，必须先征得用户同意，只改目标接口，并写清恢复方法。",
    },
    "vendor_page": {
        "title": "厂商私有报告",
        "signs": "设备声明的报告在厂商自定义页（0xFF00 以上），或按键/语音用私有 Report ID 和前缀发送。",
        "user": "这只设备的按键或语音用厂商自己的报告格式，需要先采样分析再写解析规则。",
        "user_try": [
            "拔插后点「重新检测」，排除暂时读不到的情况。",
            "这类设备要先采样它的按键格式，AI 可以一步步带你做。",
        ],
        "proven": [
            "0416:0602：语音键发 01 82 03 厂商请求，会话状态 01 86，音频 01 81；按前缀识别，40 字节一帧、50 帧每秒，用 G.722.1 解码器还原 PCM。",
            "BLE 遥控器 FIBER0D：F8 是语音键事件，FB 是写入的录音开关，FC 是音频——三个通知按序号拼成一帧，松手后还会到最后一帧，停止命令和音频结束要分开处理。",
            "验收方式：同链路连续两次录音（753 个通知重组 251 帧）完整生成 WAV 且无错误隐藏，再分别核对松手尾帧和断线清理。",
        ],
        "try": [
            "只读监听所选设备的全部报告，对照每次按键和说话的时间。",
            "同一颗键多按几次、不同键各按一次，找变化的字节：Report ID、前缀、序号、长度。",
            "音频看帧长和速率推断编码，先试项目已有的解码器（G.722.1、ICO）。",
            "搜公开研究和同型号抓包（FIBER0D 用过公开的同型号抓包做离线对照）。",
            "厂商 App 或库只用于本机对照线索，不分发、不打包。",
        ],
        "deliver": "按键/报告对照表、解析规则（参考 ble_remote.py）、样本回放测试；语音分段验收结果。",
        "caution": "采集只针对目标设备和约定时段，原始音频和抓包留在本机，不上传。",
    },
    "gatt_only": {
        "title": "只有 GATT 的蓝牙",
        "signs": "系统蓝牙显示已连接，但系统里没有这只遥控器的可用 HID 键盘设备（常见于电视盒 BLE 遥控器）。",
        "user": "这只电视盒蓝牙遥控器在 Windows 自带蓝牙下无法当键盘使用。插一只普通外接 USB 蓝牙棒，在上方选好后点「切换为 BLE 直连模式」即可接入（不影响电脑自带蓝牙）。",
        "user_try": [
            "插一只普通外接 USB 蓝牙棒，让遥控器直连 App（不影响电脑自带蓝牙）。",
        ],
        "proven": [
            "BLE 遥控器 FIBER0D 在 Windows 自带蓝牙下因非标准 HID 描述表被系统驱动报 Code 10 拒载；改用外接 USB 蓝牙棒（如 0BDA:8771）走 BLE 直连模式后连接稳定，所有按键可正常读取。",
            "外接适配器 33FA:0001 在 WinUSB、libusbK 下都启动失败（Code 10）；外接适配器 0BDA:8771 走 WinUSB + Bumble 能建立稳定的 GATT 会话并保持 120 秒以上。",
            "读 0x180A 设备信息和 0x1812 HID 服务的 Report Map（0x2A4B），订阅按键和语音通知。",
        ],
        "try": [
            "先换系统试：同一只遥控器在 Mac 或手机上是否被识别成键盘（HID over GATT）。",
            "用手机 nRF Connect 等工具看服务和特征：0x1812 HID、0x180A 设备信息、厂商自定义 UUID，订阅通知看按键。",
            "Windows 原生 GATT 不稳定时，试外接蓝牙适配器 + 用户态协议栈（Bumble），注意适配器芯片兼容性。",
            "检查配对加密、连接参数和重连：短连接断开可能是加密或参数问题。",
        ],
        "deliver": "服务与特征清单、Report Map、按键通知样本、连接稳定性记录、读取端方案。",
        "caution": "给外接适配器换驱动前备份原驱动；不改电脑内置蓝牙的驱动。",
    },
    "system_control": {
        "title": "电源/睡眠类按键",
        "signs": "设备声明了系统控制集合（Generic Desktop 0x80：电源、睡眠、唤醒），或某颗键按下后没有任何 HID 输出。",
        "user": "电源、睡眠这类键通常由系统直接处理，有的只发红外，App 可能读不到或不该拦截。",
        "user_try": [
            "电源、睡眠键由系统直接处理，跳过这颗键，其他键照常学。",
        ],
        "proven": [
            "这个项目还没遇到走系统控制页的键。",
            "BLE 遥控器 FIBER0D 的红色电源键实际发的是键盘页 usage 102（Power），能正常读取。",
            "BLE 遥控器 FIBER0D 的 TV 键两次试按都没有 HID 输出（其他厂商自定义服务与红外均未测），不能据此断定只走红外。",
            "0416:0602 的电源、TV 键按用户决定禁配，从未采集，不虚构键码。",
        ],
        "try": [
            "先只读监听全部页，看这颗键到底发了什么：键盘页 Power(102) 可直接学；系统控制页另行处理。",
            "系统控制页的键系统会直接执行睡眠/关机：可评估只读不拦截，或记为限制。",
            "完全没有输出时，查是否另有厂商自定义服务或只走红外；红外电脑收不到，记为限制。",
            "长按、组合键可能发不同报告，分别试。",
        ],
        "deliver": "每颗键实际发出的报告、能否读取、是否会触发系统行为的结论。",
        "caution": "不要为了读取电源键去拦截或改动系统电源行为。",
    },
    "gamepad_buttons": {
        "title": "手柄按钮页",
        "signs": "设备声明的是手柄/摇杆集合，或按键在 HID 按钮页（Usage Page 9）。",
        "user": "这只设备把按键报成手柄按钮，当前版本只读键盘和媒体键，需要放宽读取范围。",
        "user_try": [
            "如果遥控器能在键盘模式和手柄模式之间切换，切到键盘模式后点「重新检测」。",
        ],
        "proven": [
            "这个项目还没遇到这类遥控器。",
            "现有解析器已经能把按钮页解成 hid:9:编号，但三处没放开：枚举只收键盘页和媒体键页；键码校验只认 hid:7 / hid:12（enrollment.py、learned_remotes.py、remote_library.py 各一处）；Windows Raw Input 只注册了键盘和消费者控制。",
        ],
        "try": [
            "先确认报告描述：按钮个数、是否有摇杆或方向帽（Hat switch）、按下/松开是否成对。",
            "放宽上面三处，补按钮页的样本回放测试；方向帽需要单独把角度值映射成四个方向键。",
            "Windows 可注册 Generic Desktop 的 Gamepad(5)/Joystick(4) Raw Input，或改用 HID API 读取。",
        ],
        "deliver": "报告描述、按钮编号对照表、读取端和校验的改动、回放测试。",
        "caution": "手柄类设备常同时被游戏或系统手柄服务占用，先确认读取不会打断它们。",
    },
    "voice_private": {
        "title": "遥控器私有语音通道",
        "signs": "系统声音输入列表里找不到这只遥控器的麦克风（它不是标准 USB Audio / 蓝牙 HFP 麦克风），语音通过厂商私有 USB 接口或 BLE GATT 特征回传。",
        "user": "当前软件仅内置配套 0416:0602 遥控器的私有收音。其他遥控器推荐先用电脑麦克风「仅触发识别」；要接入这只遥控器自带的私有麦克风，需要先采样分析控制命令、分片格式与编码。",
        "user_try": [
            "先用电脑麦克风：在「语音」页选「仅触发识别」。",
        ],
        "proven": [
            "0416:0602（USB 2.4G）：通过 Interface 3（bulk OUT 控制 + interrupt IN 音频）发送批准/同步/启动/保活命令，每 20ms 一帧 40 字节 G.722.1 解码为 PCM，经本地虚拟麦克风送入识别软件。",
            "FIBER0D（BLE 语音遥控器）：通过厂商 GATT 特征 F8（语音键事件）、FB（录音开关）、FC（音频通知）三包拼一帧解码，松手后保留尾帧。",
        ],
        "try": [
            "先确认系统声音设置 → 输入里有没有出现这只遥控器：若已出现，直接在识别软件里选它并配合「仅触发识别」即可，无需任何逆向。",
            "如系统完全没有音频输入设备，先看 USB 接口列表（厂商自定义 HID 页 >= 0xFF00 / 独立端点）或蓝牙 GATT 服务特征（如 0x1812 / 厂商 UUID）。",
            "对照按下和松开语音键时的原始报文，依次确认：启动/停止控制命令 → 音频包序号与帧长 → 音频编码（常见 mSBC / ADPCM / G.722.1 / Opus）→ 解码成 WAV 验证人声清晰 → 再接入虚拟麦克风实时链路。",
        ],
        "deliver": "语音控制命令与音频分片对照表、离线解码 WAV 验证结果、以及接入 ReaderEntry（remote_mic=True）的方案。",
        "caution": "采样仅针对目标设备与指定录音时段，原始音频与抓包留在用户本机，严禁上传。",
    },
}

# What helps at each failing step of the add dialog, beyond the on-screen hint.
STEP_HELP = {
    "not_visible": ("系统已连接但 App 列表里没有这只设备",
                    ["先排除权限：Mac 的输入监控；Windows 看设备管理器里有没有这只设备。",
                     "在下方「没检测到我的接收器 / 蓝牙设备？」里选出它，App 会判断属于哪类边界。"]),
    "no_signal": ("选中了设备，按键没有任何信号",
                  ["换数字键或方向键再试；确认没有别的 App 独占这只设备。",
                   "接收器 0416:0602在 Windows 上，按键接口被 WinUSB 接管后只能由主程序读取，别同时开两个程序。",
                   "如果系统里有多个同名接口，分别试；按键可能在另一个接口上。"]),
    "down_only": ("只收到按下，没有松开",
                  ["换一颗键；部分遥控器的某些键只发一次脉冲。",
                   "记录这颗键，看是否所有键都如此：都如此时可能需要按时间自动补松开（需要改读取端）。"]),
    "missing_release": ("收到按下后一直没有松开",
                        ["换一颗键确认是不是个别键的问题。",
                         "只读监听原始报告，看松开是否走了另一个报告或另一个接口。"]),
    "multiple_keys": ("按一颗键收到多个信号",
                      ["确认只按了一颗；有的键会同时发键盘码和媒体码（0416:0602 的语音键同时发 hid:7:62 和厂商请求）。",
                       "多个信号稳定一起出现时，可以选其中一个作为这颗键的键码（需要改学习规则）。"]),
    "duplicate_key": ("两颗实体键得到同一个键码",
                      ["比较两颗键的原始报告；字节完全相同时只能当作同一个键（已知边界）。",
                       "原始报告有差异时，可以写归一化规则区分（参考 hid_reports.py 和样本回放测试）。"]),
    "open_failed": ("找到设备但打不开按键接口",
                    ["Mac：输入监控是否已授权；是否有其他程序独占（0416:0602 的读取组件会独占接收器）。",
                     "Windows：接口驱动是否被替换过（Zadig）；被 WinUSB 接管的接口不能再走 Raw Input。"]),
    "enumeration_failed": ("系统设备列表读不到",
                           ["重新检测；持续失败时记录系统版本和报错原文。"]),
    "native_audio_not_available": ("遥控器麦克风在系统/识别软件里找不到",
                                   ["先确认能否接受用电脑麦克风「仅触发识别」（零驱动立即可用）。",
                                    "若确实需要直接收遥控器自带麦克风的声音，请按下方「遥控器私有语音通道」方向分步采样排查。"]),
    "shortcut_sent": ("已发送识别快捷键，但识别软件没有出字",
                      ["核对 10RemoteHub 里的识别快捷键与快捷键方式是否与识别软件完全一致。",
                       "Windows 请确认没有使用豆包输入法（推荐微信输入法），且当前前台窗口不是管理员权限窗口（UIPI 隔离）。",
                       "确认识别软件里选对了正在使用的麦克风。"]),
}


def classify(device) -> str:
    """Name the boundary an UnlistedDevice falls into, or "" when unclear."""
    if not getattr(device, "hid", True):
        return "gatt_only" if getattr(device, "transport", "") == "蓝牙" else "private_usb"
    collections = set(getattr(device, "collections", ()))
    pages = set(getattr(device, "pages", ()))
    if any(page >= 0xFF00 for page, _ in collections) or any(page >= 0xFF00 for page in pages):
        return "vendor_page"
    if (1, 0x80) in collections:
        return "system_control"
    if (1, 4) in collections or (1, 5) in collections or 9 in pages:
        return "gamepad_buttons"
    return ""


def unlisted_access_text(case: str, *, takeover: bool = False, platform: str | None = None,
                         dongle: bool | None = None) -> str:
    """Plain-language access level for a device the readers skipped.

    ``takeover`` is True when the app can take this USB device over on Windows, which
    makes it a one-click case whatever the boundary is. ``dongle`` says whether this build
    can drive an external Bluetooth dongle (defaults to checking for it).
    """
    from .protocols import (
        ACCESS_ONE_CLICK, ACCESS_UNSUPPORTED, DONGLE_MISSING, access_heading, dongle_route_available,
        dongle_route_note,
    )

    target = platform or sys.platform
    if takeover:
        return (f"{access_heading(ACCESS_ONE_CLICK)}：点下方「让 App 接管这个接收器」，"
                "App 就能自己读它的按键。")
    if case == "gatt_only" and not (dongle_route_available() if dongle is None else dongle):
        return (f"{access_heading(ACCESS_UNSUPPORTED)}：电脑自带蓝牙读不了这类电视盒遥控器的按键。{DONGLE_MISSING}\n"
                "点「复制给 AI 的求助」，AI 可以帮你看还有没有别的办法。")
    if case == "gatt_only":
        return (f"{access_heading(ACCESS_ONE_CLICK)}：电脑自带蓝牙读不了这类电视盒遥控器的按键。"
                f"插一只普通外接 USB 蓝牙棒，让遥控器直连 App{dongle_route_note(target)}，不影响电脑自带蓝牙。")
    head = f"{access_heading(ACCESS_UNSUPPORTED)}："
    ask = "点「复制给 AI 的求助」，AI 会按这类问题以往的做法一步步带你排查。"
    if case not in BOUNDARIES:
        return head + "没能从系统信息判断它是哪类设备。\n" + ask
    info = BOUNDARIES[case]
    tries = "".join(f"\n· {item}" for item in info["user_try"])
    return f"{head}可能是「{info['title']}」。{info['user']}\n可以试试：{tries}\n{ask}"


def ble_dongle_help_text(platform: str | None = None) -> str:
    """Offline in-app setup guide for using any external USB Bluetooth dongle with TV-box BLE remotes."""
    target = platform or ("Windows" if sys.platform == "win32" else "macOS")
    if target == "Windows":
        return (
            "BLE 专属蓝牙棒使用说明（Windows）\n"
            "====================================\n\n"
            "1. 什么时候需要外接 USB 蓝牙棒？\n"
            "   • 普通电脑蓝牙遥控器 / 飞鼠蓝牙：直接连电脑自带蓝牙即可，不需要买蓝牙棒。\n"
            "   • 部分电视盒 BLE 语音遥控器（如 FIBER0D）：Windows 自带蓝牙驱动会拒载其非标准\n"
            "     按键描述表（Code 10）或屏蔽普通软件读取 0x1812 按键服务。\n"
            "     此时插一只十几元的普通外接 USB 蓝牙棒（不限具体型号）作为这只遥控器的\n"
            "     「专属接收器」，即可绕开限制，且天然 100% 独占接管原按键。\n\n"
            "2. 一键启用（推荐，无需下载任何第三方软件！）：\n"
            "   ① 在 Windows「设置 → 蓝牙」里先删除之前连不上的这只 BLE 遥控器；\n"
            "   ② 把外接 USB 蓝牙棒插到电脑上（程序已从底层锁死，绝不碰电脑自带蓝牙）；\n"
            "   ③ 回到「添加遥控器」窗口，在外接蓝牙棒卡片里选中它，点「切换为 BLE 直连模式」→\n"
            "      在弹出的 Windows 管理员确认框里点「是」，几秒钟即可自动完成。\n\n"
            "3. 配对与学习遥控器：\n"
            "   ① 把电视盒 BLE 遥控器按进蓝牙配对模式（指示灯快闪）；\n"
            "   ② 点「扫描 BLE 遥控器」，扫到后在主列表选中它 → 点「开始检测」（首次自动配对并保存密钥）\n"
            "      → 按任意键确认后进入照片学键保存；\n"
            "   ③ 保存后日常休眠唤醒会自动重连，无需再次配对。\n\n"
            "4. 以后不想用时，如何恢复普通蓝牙？\n"
            "   插着这只外接蓝牙棒打开「添加遥控器」，点「恢复普通蓝牙」→ 点「是」，\n"
            "   几秒即可自动还原，无需重启电脑，也不用打开设备管理器。\n"
        )
    return (
        "BLE 专属蓝牙棒使用说明（macOS）\n"
        "==================================\n\n"
        "1. macOS 上请先尝试直接在「系统设置 → 蓝牙」配对你的 BLE 遥控器；大多数标准 BLE\n"
        "   遥控器在 macOS 上可直接识别为普通 HID 键盘，无需外接蓝牙棒。\n"
        "2. 如某只电视盒 BLE 遥控器仍无法被 macOS 原生蓝牙识别为键盘，可插一只外接标准\n"
        "   USB 蓝牙棒（非 Apple 内置蓝牙），回到「添加遥控器」将遥控器按进配对模式后点「重新检测」。\n"
    )


def driver_help_text(platform: str | None = None) -> str:
    """Offline in-app setup guide for the bundled 0416:0602 USB receiver."""
    target = platform or ("Windows" if sys.platform == "win32" else "macOS")
    if target == "Windows":
        return (
            "配套接收器（USB 0416:0602）Windows 驱动说明\n"
            "========================================\n\n"
            "1. 为什么要启用专用驱动？\n"
            "   Windows 默认键盘驱动读不到这只接收器的「#」号键，且无法读取遥控器内置麦克风。\n"
            "   启用系统内置 WinUSB 专用驱动后，即可独占接管全部按键和遥控器语音。\n\n"
            "2. 一键接管（推荐，无需下载第三方软件）：\n"
            "   ① 插入 USB 接收器（0416:0602）；\n"
            "   ② 在「添加遥控器」选中它，或在「完成设置」清单 /「通用设置 → 设备连接」中直接点击「让 App 接管这个接收器」；\n"
            "   ③ 在弹出的 Windows 管理员确认框中点「是」，等待几秒自动完成（如按键暂无响应，重新插拔一次接收器即可）。\n\n"
            "3. 遥控器语音（可选）：\n"
            "   如希望使用遥控器内置麦克风听写，请另行安装虚拟声卡 VB-CABLE，并在「语音设置」里选择对应输出。\n\n"
            "4. 如何恢复原系统驱动：\n"
            "   在「添加遥控器」或「通用设置 → 设备连接」里点击「恢复系统驱动」→ 点「是」即可一键还原。\n"
        )
    return (
        "配套接收器（USB 0416:0602）macOS 读取组件说明\n"
        "==========================================\n\n"
        "1. 为什么要安装读取组件？\n"
        "   配套接收器包含专用按键接口和遥控器麦克风通道。安装本软件自带的本地读取组件后，\n"
        "   App 才能独占读取全部 27 个按键（含 # 键）以及按住语音键实时收音。\n\n"
        "2. 安装与授权步骤（只需做一次）：\n"
        "   ① 添加完遥控器后，在首次引导或左侧「通用设置」里点击安装读取组件（需输入本机管理员密码）；\n"
        "   ② 在 macOS「系统设置 → 隐私与安全性 → 辅助功能」中开启 10RemoteHub（用于模拟按键与窗口控制）；\n"
        "   ③ 如同时使用其他普通 USB / 蓝牙遥控器，另在「输入监控」中开启 10RemoteHub；\n"
        "   ④ 如需使用遥控器内置麦克风听写，在「语音设置」中按提示启用本地虚拟音频组件。\n\n"
        "3. 日常使用与卸载：\n"
        "   • 平时插入接收器即可自动连接，不需要重复安装；\n"
        "   • 照片学键时语音键只需在照片上标出位置，不用按；保存后到「语音设置」测试遥控器麦克风；\n"
        "   • 卸载时使用安装包附带的卸载程序，可一并清理后台读取组件。\n"
    )


def windows_0416_needs_winusb_keys(device, reader=None) -> bool:
    """True on Windows when 0416:0602 still uses Raw Input (missing Interface 4 WinUSB)."""
    if sys.platform != "win32" or device is None:
        return False
    if (getattr(device, "transport", ""), getattr(device, "vendor", 0), getattr(device, "product", 0)) != (
        "USB", 0x0416, 0x0602
    ):
        return False
    scope = getattr(reader, "scope", "") or ""
    if "完整键位需 WinUSB" in scope:
        return True
    if "WinUSB 键盘接口已接入" in scope:
        return False
    if reader is not None and hasattr(reader, "winusb_keys"):
        entry = getattr(reader, "devices", {}).get(getattr(device, "id", None))
        if entry is not None and len(entry) >= 2:
            return 4 not in getattr(entry[1], "winusb_interfaces", ())
    if reader is None or hasattr(reader, "winusb_keys"):
        from .windows_driver import binding_detail
        return binding_detail().get("keyboard") != "winusb"
    return False


def ai_request(*, step: str, case: str = "", device=None, diagnostics: str = "") -> str:
    """Plain-text request to paste into any AI assistant. No serials, paths or key logs."""
    lines = [
        "我在用 10RemoteHub（0.3.0 统一版，开源遥控器映射软件）添加一只遥控器，卡住了，请协助判断和适配。",
        f"如有本地仓库，请优先阅读工作区 SKILL.md 第 6 节和 desktop/docs/REMOTE-ADAPTATION.md；若在线读取 {SKILL_URL} 为旧版本（如 0.2.0），请以下方附带的 0.3.0 阶段说明与事实为准。",
        "规则：每次只给我一个能当场核对的步骤；不要让我关闭系统安全保护；替换驱动或抓取原始报文前先说明影响并征得我同意，只针对这只设备；原始音频和抓包留在我本机。",
        "",
    ]
    if step in STEP_HELP:
        title, hints = STEP_HELP[step]
        lines += [f"卡在哪一步：{title}", *[f"- {hint}" for hint in hints], ""]
    if device is not None:
        kind = device.transport or "未知连接"
        lines.append(f"设备：{kind} {device.vendor:04X}:{device.product:04X}")
        collections = ", ".join(f"页 0x{page:X} 用法 0x{usage:X}" for page, usage in sorted(getattr(device, "collections", ())))
        if collections:
            lines.append(f"系统报告的顶层集合：{collections}")
        lines.append("")
    if case in BOUNDARIES:
        info = BOUNDARIES[case]
        lines += [
            f"初步判断：{info['title']}。判断依据：{info['signs']}",
            "这个项目以往的做法（可借鉴，不是固定答案）：",
            *[f"- {item}" for item in info["proven"]],
            "可以尝试的方向：",
            *[f"- {item}" for item in info["try"]],
            f"希望得到：{info['deliver']}",
            f"注意：{info['caution']}",
            "",
        ]
    if diagnostics:
        lines += ["App 生成的脱敏接入诊断：", diagnostics]
    return "\n".join(lines).rstrip() + "\n"
