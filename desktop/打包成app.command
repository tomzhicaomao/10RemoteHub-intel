#!/bin/bash
# 把 10RemoteHub 打包成一个可在 Finder / Launchpad 双击的 .app。
#
# 为什么不是 PyInstaller：PySide6 6.11.x 在冻结环境里 feature_import/lazy_import
# 会无限递归；能避开的 6.12 又要求 macOS 14，这台 2017 Intel Mac 装不上。
# 因此 .app 只做「启动壳」：真正的代码仍由项目 .venv 里已签名的 Python 宿主运行，
# 进程身份保持 org.remotehub.desktop，特权采集服务的证书互校照常通过。
set -euo pipefail
cd "$(dirname "$0")"

DESKTOP="$PWD"
SIGN="$DESKTOP/build/local-signing"
KC="$SIGN/10RemoteHub.keychain-db"
SHA1FILE="$SIGN/cert-sha1.txt"
APP="$DESKTOP/10RemoteHub.app"
BUILD="$DESKTOP/build/app-bundle"

echo ""
echo "=================================================="
echo " 打包 10RemoteHub.app · Intel Mac / macOS 13"
echo "=================================================="
echo ""

# ---------- 前置检查 ----------
if [ ! -f "$SHA1FILE" ] || [ ! -f "$SIGN/cert.p12" ]; then
  echo "缺少本地签名证书，请先双击「安装遥控器组件.command」。"
  exit 1
fi
SHA1=$(tr -d '[:space:]' < "$SHA1FILE")
if [ ! -x "$DESKTOP/.venv/bin/python-remotehub" ]; then
  echo "缺少签名 Python 宿主 .venv/bin/python-remotehub，请先双击「安装遥控器组件.command」。"
  exit 1
fi
ICON_SRC="$DESKTOP/remotehub/assets/app-icon.png"
if [ ! -f "$ICON_SRC" ]; then
  echo "找不到图标素材：$ICON_SRC"
  exit 1
fi

# ---------- 确保专用钥匙串在搜索列表（幂等） ----------
# 注意：路径含中文，绝不能靠 word splitting 重建列表（历史上曾把
# login.keychain-db 拆坏成多条，导致 find-identity 归零）。这里显式
# 解锁 + 重设 partition list + 用固定参数重建，避免继承已损坏的旧列表。
if [ -f "$KC" ]; then
  security unlock-keychain -p remotehub "$KC" >/dev/null 2>&1 || true
  security set-key-partition-list -S apple-tool:,apple:,codesign: \
    -s -k remotehub "$KC" >/dev/null 2>&1 || true
  security list-keychains -d user -s "$KC" \
    "$HOME/Library/Keychains/login.keychain-db" \
    "$HOME/Library/Keychains/System.keychain" >/dev/null 2>&1 || true
fi

mkdir -p "$BUILD"

# ---------- 1. 图标 ----------
echo "[1/5] 生成 app 图标..."
ICONSET="$BUILD/app.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for spec in \
  "16:icon_16x16.png" "32:icon_16x16@2x.png" \
  "32:icon_32x32.png" "64:icon_32x32@2x.png" \
  "128:icon_128x128.png" "256:icon_128x128@2x.png" \
  "256:icon_256x256.png" "512:icon_256x256@2x.png" \
  "512:icon_512x512.png" "1024:icon_512x512@2x.png"; do
  sz="${spec%%:*}"; name="${spec##*:}"
  sips -z "$sz" "$sz" "$ICON_SRC" --out "$ICONSET/$name" >/dev/null 2>&1
done
iconutil -c icns "$ICONSET" -o "$BUILD/app-icon.icns"
echo "      -> app-icon.icns"

# ---------- 2. 启动器 ----------
echo "[2/5] 编译 x86_64 启动器..."
cat > "$BUILD/launcher.c" <<'CEOF'
/* Locate the project venv and exec the signed Python host.  After execve the
 * process identity is python-remotehub (org.remotehub.desktop), which is what
 * the privileged device collector verifies. */
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <libgen.h>
#include <sys/stat.h>

int main(int argc, char **argv) {
    char exe[PATH_MAX], exe_copy[PATH_MAX];
    if (!realpath(argv[0], exe)) {
        fprintf(stderr, "10RemoteHub: cannot resolve executable path\n");
        return 1;
    }
    memcpy(exe_copy, exe, sizeof(exe));
    char *macos_dir = dirname(exe_copy);      /* .../10RemoteHub.app/Contents/MacOS */

    char parent[PATH_MAX];
    int n = snprintf(parent, sizeof(parent), "%s/../../..", macos_dir);
    if (n < 0 || n >= (int)sizeof(parent)) return 1;

    char root[PATH_MAX];
    if (!realpath(parent, root)) {
        fprintf(stderr, "10RemoteHub: cannot locate project directory\n");
        return 1;
    }

    char py[PATH_MAX];
    snprintf(py, sizeof(py), "%s/.venv/bin/python-remotehub", root);
    struct stat st;
    if (stat(py, &st) != 0 || !(st.st_mode & S_IXUSR)) {
        snprintf(py, sizeof(py), "%s/.venv/bin/python", root);
    }
    if (access(py, X_OK) != 0) {
        fprintf(stderr, "10RemoteHub: Python environment not found at %s\n", root);
        return 1;
    }

    if (chdir(root) != 0) {
        fprintf(stderr, "10RemoteHub: cannot chdir to %s\n", root);
        return 1;
    }

    char **nargv = (char **)calloc((size_t)argc + 3, sizeof(char *));
    if (!nargv) return 1;
    nargv[0] = py;
    nargv[1] = "-m";
    nargv[2] = "remotehub";
    for (int i = 1; i < argc; ++i) nargv[i + 2] = argv[i];
    nargv[argc + 2] = NULL;

    execv(py, nargv);
    fprintf(stderr, "10RemoteHub: failed to start %s\n", py);
    return 1;
}
CEOF
clang -O2 -arch x86_64 -mmacosx-version-min=13.0 "$BUILD/launcher.c" -o "$BUILD/launcher"
echo "      -> launcher (x86_64)"

# ---------- 3. 组装 bundle ----------
echo "[3/5] 组装 .app..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/launcher" "$APP/Contents/MacOS/10RemoteHub"
cp "$BUILD/app-icon.icns" "$APP/Contents/Resources/app-icon.icns"
cat > "$APP/Contents/Info.plist" <<'PLEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
  <key>CFBundleExecutable</key><string>10RemoteHub</string>
  <key>CFBundleIconFile</key><string>app-icon</string>
  <key>CFBundleIdentifier</key><string>org.remotehub.desktop</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>10RemoteHub</string>
  <key>CFBundleDisplayName</key><string>10RemoteHub</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSUIElement</key><false/>
  <key>NSHumanReadableCopyright</key><string>Intel Mac local build</string>
</dict>
</plist>
PLEOF
# 与上游 VERSION 保持一致（v0.2.0 起）
APP_VERSION="$(tr -d '[:space:]' < "$DESKTOP/VERSION")"
plutil -replace CFBundleShortVersionString -string "$APP_VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$APP_VERSION" "$APP/Contents/Info.plist"

# ---------- 4. 签名 ----------
echo "[4/5] 用本地证书签名..."
codesign --force --sign "$SHA1" \
  --identifier org.remotehub.desktop \
  "$APP/Contents/MacOS/10RemoteHub"
codesign --force --sign "$SHA1" \
  --identifier org.remotehub.desktop \
  "$APP"

# ---------- 5. 校验 ----------
echo "[5/5] 校验签名..."
codesign -vvv --deep --strict "$APP" 2>&1 | tail -5
echo "      架构：$(lipo -info "$APP/Contents/MacOS/10RemoteHub" 2>/dev/null | sed 's/.*is //')"

echo ""
echo "完成：$APP"
echo ""
echo "用法："
echo "  · 在 Finder 里双击 10RemoteHub.app 启动界面"
echo "  · 实体按键仍需先运行「安装遥控器组件.command」并插入 0416:0602 接收器"
echo "  · 此 .app 依赖项目目录内的 .venv，请勿单独移动或拷贝"
echo ""
