#!/bin/bash
# 双击运行：检查 0416:0602 USB 接收器是否已插好、型号是否买对。
# 只读系统 USB 枚举信息，不修改任何设置。
cd "$(dirname "$0")" || exit 1

/usr/bin/python3 - <<'PY'
import re
import subprocess

TARGET_VENDOR = 1046   # 0x0416
TARGET_PRODUCT = 1538  # 0x0602

print("=" * 56)
print("  10RemoteHub · 遥控器接收器检测")
print("=" * 56)
print("正在读取 USB 设备列表…\n")

try:
    raw = subprocess.check_output(
        ["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
        text=True, timeout=30, stderr=subprocess.DEVNULL,
    )
except Exception as exc:
    print("读取失败：%s" % exc)
    raise SystemExit(1)

blocks = re.split(r"\n(?=.*\+\-o )", raw)
found = []
seen = []
for block in blocks:
    v = re.search(r'"idVendor"\s*=\s*(\d+)', block)
    p = re.search(r'"idProduct"\s*=\s*(\d+)', block)
    if not (v and p):
        continue
    vid, pid = int(v.group(1)), int(p.group(1))
    name = re.search(r'"USB Product Name"\s*=\s*"([^"]+)"', block)
    label = name.group(1) if name else "未命名 USB 设备"
    seen.append((vid, pid, label))
    if vid == TARGET_VENDOR and pid == TARGET_PRODUCT:
        found.append(label)

print("当前 USB 设备：")
if not seen:
    print("  （没有检测到外接 USB 设备）")
for vid, pid, label in seen:
    mark = "  <== 就是这个" if (vid, pid) == (TARGET_VENDOR, TARGET_PRODUCT) else ""
    print("  %04x:%04x  %s%s" % (vid, pid, label, mark))

print()
if found:
    print("✅ 找到 0416:0602 接收器，型号正确。")
    print("   现在回到 10RemoteHub 界面点「重新检测」即可。")
    print("   若仍无反应：换一个 USB 口，或确认遥控器已装电池。")
else:
    print("❌ 没有找到 0416:0602。可能原因：")
    print("   1. 接收器没插 / 没插紧，或插在了 USB Hub 上；")
    print("   2. 买到的是纯蓝牙款、飞鼠款，或另一家芯片的 2.4G 接收器；")
    print("   3. 只买了遥控器，没有买「带 USB 接收器」的规格。")
    print("   上方列出的 VID:PID 可以直接发给卖家核对。")
print()
print("（本窗口可以直接关闭）")
PY

chmod +x "$0" 2>/dev/null || true
