#!/bin/bash
# 10RemoteHub · Intel Mac 一键安装「遥控器读取组件」
# 双击运行。自动完成：自签证书 → 编译签名采集组件 → 签名启动器 → 安装系统服务 → 自检 → 启动。
# 你只需要输入一次管理员密码，再按需点一下「辅助功能」开关。
set -euo pipefail
cd "$(dirname "$0")"
DESKTOP="$PWD"
SIGN="$DESKTOP/build/local-signing"
KC="$SIGN/10RemoteHub.keychain-db"
CN="10RemoteHub Local Dev"
COMPONENTS="$DESKTOP/build/device-helper-components"
PY="$DESKTOP/.venv/bin/python"
HOST="$DESKTOP/.venv/bin/python-remotehub"

line(){ printf '\n\033[1;36m── %s ──────────────────────────────────\033[0m\n' "$1"; }
ok(){ printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
warn(){ printf '  \033[1;33m!\033[0m %s\n' "$1"; }
die(){ printf '\n\033[1;31m✗ %s\033[0m\n\n' "$1" >&2; exit 1; }

clear
printf '\033[1m10RemoteHub · 遥控器读取组件安装\033[0m\n'
printf '本机：%s / %s\n' "$(sw_vers -productVersion)" "$(uname -m)"
printf '全程自动；只会在中途向你索要一次管理员密码。\n'

line "0 / 6 前置检查"
[ "$(uname -s)" = "Darwin" ] || die "只能在 macOS 上运行"
[ -x "$PY" ] || die "虚拟环境不存在，请先运行 sh setup-intel-mac.sh"
ok "虚拟环境就绪：$("$PY" --version 2>&1)"
command -v brew >/dev/null 2>&1 || die "未找到 Homebrew，见 INTEL-MAC.md"
brew list --versions libusb >/dev/null 2>&1 || die "未安装 libusb，请先执行 brew install libusb"
ok "libusb 就绪"

# 接收器没插也能装组件，只是装完没法验证按键；提前告知，避免白忙。
if system_profiler SPUSBDataType 2>/dev/null | grep -qi '0x0416'; then
  ok "已检测到 0416:0602 USB 接收器"
else
  warn "目前没检测到 0416:0602 接收器（不影响安装，装完插上即可生效）"
fi

line "1 / 6 本机代码签名证书（不需要 Apple 开发者账号）"
mkdir -p "$SIGN"
if [ ! -f "$SIGN/cert.pem" ]; then
  printf '[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=e\n[dn]\nCN=%s\n[e]\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=critical,codeSigning\nbasicConstraints=critical,CA:TRUE\nsubjectKeyIdentifier=hash\n' "$CN" > "$SIGN/cert.cnf"
  /usr/bin/openssl req -new -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
      -config "$SIGN/cert.cnf" -keyout "$SIGN/key.pem" -out "$SIGN/cert.pem" 2>/dev/null
  /usr/bin/openssl pkcs12 -export -inkey "$SIGN/key.pem" -in "$SIGN/cert.pem" \
      -out "$SIGN/cert.p12" -passout pass:remotehub -name "$CN" 2>/dev/null
  ok "已生成自签名代码签名证书（仅本机有效）"
else
  ok "复用已有证书"
fi
if [ ! -f "$KC" ]; then
  security create-keychain -p remotehub "$KC" >/dev/null 2>&1
  security unlock-keychain -p remotehub "$KC" >/dev/null 2>&1
  security set-keychain-settings -lut 3600 "$KC" >/dev/null 2>&1
  security import "$SIGN/cert.p12" -k "$KC" -P remotehub -A -T /usr/bin/codesign >/dev/null 2>&1
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k remotehub "$KC" >/dev/null 2>&1 || true
  ok "已建立专用钥匙串（不改动你的登录钥匙串密码）"
fi
CUR=$(security list-keychains -d user | tr -d '"' | tr '\n' ' ')
if [ ! -f "$SIGN/original-keychains.txt" ]; then printf '%s\n' "$CUR" > "$SIGN/original-keychains.txt"; fi
case "$CUR" in
  *"$KC"*) : ;;
  *) security list-keychains -d user -s "$KC" $CUR >/dev/null 2>&1 ;;
esac
SHA1=$(/usr/bin/openssl x509 -in "$SIGN/cert.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d ':')
printf '%s\n' "$SHA1" > "$SIGN/cert-sha1.txt"
ok "证书 SHA-1：$SHA1"

line "2 / 6 编译带证书锁定的采集组件"
if [ ! -f "$COMPONENTS/RemoteHubDevice.pkg" ] || \
   ! strings "$COMPONENTS/device-helper" 2>/dev/null | grep -q "$SHA1"; then
  ( cd "$DESKTOP" && CODESIGN_IDENTITY="$SHA1" RH_ARCH_FLAGS="-arch x86_64" \
      "$PY" scripts/build_device_helper.py --codesign-identity "$SHA1" )
fi
ok "采集程序 + 安装/卸载包已就绪"

line "3 / 6 准备签名版启动器与运行时资源"
# 特权服务只接受 identifier=org.remotehub.desktop 且证书指纹匹配的客户端。
# 把 venv 的 Python 复制一份，用同一张证书重签；这样源码运行也能通过 XPC 互校。
REAL=$("$PY" -c 'import sys,os;print(os.path.join(sys.base_prefix,"bin","python%d.%d"%sys.version_info[:2]))')
[ -x "$REAL" ] || REAL=$("$PY" -c 'import sys;print(sys._base_executable)')
LIBDIR=$("$PY" -c 'import sysconfig;print(sysconfig.get_config_var("LIBDIR"))')
LDLIB=$("$PY" -c 'import sysconfig;print(sysconfig.get_config_var("LDLIBRARY"))')
mkdir -p "$DESKTOP/.venv/lib"
# 复制后的宿主按 @executable_path/../lib 找 libpython，补一个链接即可。
ln -sfn "$LIBDIR/$LDLIB" "$DESKTOP/.venv/lib/$LDLIB"
if [ -x "$HOST" ] && codesign -d --requirements - "$HOST" 2>/dev/null | grep -qi "$SHA1"; then
  ok "启动器已用当前证书签名，跳过重签（辅助功能授权不会失效）"
else
  cp -pL "$REAL" "$HOST"
  chmod +w "$HOST"
  codesign --force --sign "$SHA1" --identifier org.remotehub.desktop "$HOST"
  ok "启动器已签名：org.remotehub.desktop"
fi
codesign -v --strict \
  --requirement="identifier \"org.remotehub.desktop\" and certificate leaf = H\"$SHA1\"" "$HOST"
mkdir -p "$DESKTOP/native" "$DESKTOP/components"
cp -p "$COMPONENTS/libRemoteHubDevice.dylib" "$DESKTOP/native/"
cp -p "$COMPONENTS/RemoteHubDevice.pkg" "$COMPONENTS/RemoteHubDevice-Uninstall.pkg" "$DESKTOP/components/"
for f in RemoteHubAudio.pkg RemoteHubAudioComponent.pkg RemoteHub-Uninstall.pkg; do
  if [ -f "$DESKTOP/build/$f" ]; then cp -p "$DESKTOP/build/$f" "$DESKTOP/components/"; fi
done
ok "运行时资源已放到 native/ 与 components/"

line "4 / 6 安装系统采集服务（这里会要管理员密码）"
printf '  即将写入 /Library/PrivilegedHelperTools/10RemoteHub 并注册一个 LaunchDaemon。\n'
printf '  这是本项目读取 USB 接收器所必需的；卸载脚本可完整撤销。\n\n'
sudo -v || die "未取得管理员授权，已取消（系统未被修改）"
sudo /usr/sbin/installer -pkg "$COMPONENTS/RemoteHubDevice.pkg" -target / >/dev/null
ok "采集服务已安装"

line "5 / 6 自检"
( cd "$DESKTOP" && "$HOST" - <<'PY'
from remotehub.mac_device_helper import installation_status
code, message = installation_status()
print(f"  采集组件状态码：{code} — {message}")
raise SystemExit(0 if code == 0 else 1)
PY
)
ok "签名互校通过，特权服务可以被启动器连接"
if sudo /bin/launchctl print system/org.remotehub.device-helper >/dev/null 2>&1; then
  ok "LaunchDaemon 已注册"
else
  warn "LaunchDaemon 尚未注册（首次连接时会自动拉起，可先继续）"
fi

line "6 / 6 启动"
open "$DESKTOP/run-intel-mac.command"
printf '  已通过签名启动器打开 10RemoteHub。\n'
printf '  正在打开「辅助功能」设置页……\n'
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" >/dev/null 2>&1 || true

printf '\n\033[1m装完了。还剩两件事：\033[0m\n'
printf '  1) 把 0416:0602 的 USB 接收器插到 Mac 上，回 App 点「重新检测」。\n'
printf '  2) 如果 App 提示「辅助功能未授权」，在刚打开的设置页把\n'
printf '     \033[1mTerminal\033[0m 或 \033[1mpython-remotehub\033[0m 的开关打开；\n'
printf '     列表里没有就点「+」选 %s\n' "$HOST"
printf '\n语音功能（可选）：双击安装 build/RemoteHubAudio.pkg，'
printf '安装时会短暂重启系统音频服务。\n'
printf '不想用了：双击「卸载遥控器组件.command」即可完整撤销。\n\n'
printf '按回车键关闭本窗口…'; read -r _
