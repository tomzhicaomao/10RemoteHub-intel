#!/bin/bash
# 10RemoteHub · 一键撤销「遥控器读取组件」的所有系统级改动
# 双击运行。移除 LaunchDaemon、系统采集程序、虚拟麦克风、签名启动器、
# 运行时资源副本，以及本机自建证书与专用钥匙串；用户配置与编译产物保留。
set -euo pipefail
cd "$(dirname "$0")"
DESKTOP="$PWD"
SIGN="$DESKTOP/build/local-signing"
KC="$SIGN/10RemoteHub.keychain-db"
UNINSTALL_PKG="$DESKTOP/build/RemoteHub-Uninstall.pkg"

line(){ printf '\n\033[1;36m── %s ──────────────────────────────────\033[0m\n' "$1"; }
ok(){ printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
warn(){ printf '  \033[1;33m!\033[0m %s\n' "$1"; }

clear
printf '\033[1m10RemoteHub · 卸载遥控器读取组件\033[0m\n\n'
printf '将撤销：系统采集服务、LaunchDaemon、虚拟麦克风、签名启动器、本机签名证书。\n'
printf '会保留：按键映射等用户配置、源码、build/ 里的编译产物。\n'
printf '按回车继续，或按 Ctrl-C 取消…'; read -r _

line "1 / 5 退出正在运行的程序"
pkill -f "python-remotehub -m remotehub" >/dev/null 2>&1 || true
pkill -x 10RemoteHub >/dev/null 2>&1 || true
sleep 1
ok "已退出（或本来就没在运行）"

line "2 / 5 移除系统组件（需要管理员密码）"
if [ -f "$UNINSTALL_PKG" ]; then
  sudo -v
  sudo /usr/sbin/installer -pkg "$UNINSTALL_PKG" -target / >/dev/null || warn "完整卸载包返回非零，继续逐项清理"
  ok "已执行系统卸载"
else
  warn "找不到完整卸载包，改用采集组件自带卸载包"
  if [ -f "$DESKTOP/build/device-helper-components/RemoteHubDevice-Uninstall.pkg" ]; then
    sudo -v
    sudo /usr/sbin/installer -pkg "$DESKTOP/build/device-helper-components/RemoteHubDevice-Uninstall.pkg" -target / >/dev/null || true
  fi
fi

line "3 / 5 移除签名启动器与运行时资源副本"
rm -f "$DESKTOP/.venv/bin/python-remotehub" \
      "$DESKTOP"/.venv/lib/libpython*.dylib \
      "$DESKTOP/native/libRemoteHubDevice.dylib" 2>/dev/null || true
for f in RemoteHubDevice.pkg RemoteHubDevice-Uninstall.pkg RemoteHub-Uninstall.pkg \
         RemoteHubAudio.pkg RemoteHubAudioComponent.pkg; do
  rm -f "$DESKTOP/components/$f" 2>/dev/null || true
done
rmdir "$DESKTOP/components" "$DESKTOP/native" 2>/dev/null || true
ok "启动器回退到普通 Python（run-intel-mac.command 会自动判断）"

line "4 / 5 撤销本机签名证书"
if [ -f "$SIGN/original-keychains.txt" ]; then
  ORIG=$(cat "$SIGN/original-keychains.txt")
  if [ -n "$ORIG" ]; then
    security list-keychains -d user -s $ORIG >/dev/null 2>&1 || warn "钥匙串搜索列表恢复失败，可在「钥匙串访问」里手动调整"
  fi
fi
security delete-keychain "$KC" >/dev/null 2>&1 || true
rm -f "$SIGN/cert.cnf" "$SIGN/cert.pem" "$SIGN/key.pem" "$SIGN/cert.p12" \
      "$SIGN/cert-sha1.txt" "$SIGN/original-keychains.txt" \
      "$SIGN/probe" "$SIGN/probe.c" 2>/dev/null || true
rmdir "$SIGN" 2>/dev/null || true
ok "证书与专用钥匙串已移除"

line "5 / 5 收尾"
printf '  如当初手动加过辅助功能授权，可到「隐私与安全性 → 辅助功能」\n'
printf '  选中对应条目点「−」移除（TCC 隐私数据库不允许脚本修改）。\n\n'
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" >/dev/null 2>&1 || true
printf '  编译产物仍在 build/，可随时重新双击「安装遥控器组件.command」装回来\n'
printf '  （会生成一张新证书，辅助功能可能需要重新授权）。\n\n'
ok "卸载完成"
printf '按回车键关闭本窗口…'; read -r _
