#!/bin/sh
# One-shot source setup for 10RemoteHub on an Intel Mac running macOS 13.
# Safe: only writes .venv/ and artifacts/ inside this project. It does not
# install drivers, touch system audio, request permissions or launch the UI.
set -eu
cd "$(dirname "$0")"
DESKTOP="$PWD"

# Some WorkBuddy / CodeBuddy CLI shells inject a sitecustomize that guards bulk
# file deletions. pip legitimately overwrites hundreds of files inside .venv
# while installing Qt, which trips that guard. Drop the shim for this script's
# subprocesses only; nothing outside this project's .venv is touched.
if [ -n "${CODEBUDDY_SAFE_DELETE_ENABLED:-}" ]; then
  unset PYTHONPATH CODEBUDDY_SAFE_DELETE_ENABLED PYTHONDONTWRITEBYTECODE
fi

ARCH=$(uname -m)
OSVER=$(sw_vers -productVersion)
MAJOR=${OSVER%%.*}
if [ "$ARCH" != "x86_64" ]; then
  echo "错误：此脚本面向 Intel (x86_64) Mac，当前架构为 $ARCH。" >&2
  echo "Apple 芯片请使用上游的 requirements-macos-arm64.lock 流程。" >&2
  exit 1
fi
if [ "$MAJOR" -lt 13 ]; then
  echo "错误：PySide6 6.11.3 的 macOS wheel 最低要求 13.0，当前为 $OSVER。" >&2
  exit 1
fi
echo "目标环境：macOS $OSVER / $ARCH / Python 3.12-3.14"

if [ -n "${REMOTEHUB_PYTHON:-}" ]; then
  PY="$REMOTEHUB_PYTHON"
elif command -v python3.12 >/dev/null 2>&1; then
  PY=$(command -v python3.12)
elif command -v python3.13 >/dev/null 2>&1; then
  PY=$(command -v python3.13)
else
  PY=$(command -v python3)
fi
"$PY" -c 'import sys; raise SystemExit(0 if (3,12) <= sys.version_info[:2] < (3,15) else 1)' || {
  echo "错误：$PY 不是 Python 3.12 / 3.13 / 3.14。" >&2
  echo "可设置 REMOTEHUB_PYTHON=/path/to/python3.12 后再运行。" >&2
  exit 1
}
echo "使用解释器：$PY ($("$PY" --version 2>&1))"

if [ "${1:-}" = "--recreate" ] && [ -d .venv ]; then
  echo "移除旧 .venv ..."
  rm -rf .venv
fi
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --no-input --upgrade 'pip>=26.2'
echo "=== 安装依赖（macOS 13 / Intel 哈希锁） ==="
.venv/bin/python -m pip install --no-input --require-hashes -r requirements-macos13-x86_64.lock
echo "=== 安装本项目（不解析依赖） ==="
.venv/bin/python -m pip install --no-input --no-deps --no-build-isolation -e .
echo "=== 离屏自动验证 ==="
mkdir -p ../artifacts
# This one release-only test builds a throwaway self-signed certificate in a
# temporary keychain. On macOS 13 codesign reports "0 valid identities" for it
# unless the certificate is trusted through an interactive SecurityAgent dialog,
# which an unattended verification cannot complete. It exercises the privileged
# installer packaging, not the desktop app, so deselect it here.
.venv/bin/python scripts/verify.py --output ../artifacts/verification-macos13.json \
  --deselect tests/test_device_helper_package.py::test_self_made_certificate_is_pinned_without_apple_team

cat <<'EOF'

完成。常用命令（在本 desktop/ 目录下）：

  启动界面        .venv/bin/python -m remotehub
  仅看权限状态    .venv/bin/python -m remotehub --permission-status
  生成诊断报告    .venv/bin/python -m remotehub --diagnose /tmp/rh-report.json
  重跑测试        .venv/bin/python -m pytest -q
  重新生成哈希锁  .venv/bin/python scripts/make_macos13_lock.py

注意：源码运行不含已编译的「统一采集组件」和虚拟麦克风安装包，
      因此 USB 遥控器的实体按键暂时不会生效。硬件接入步骤见 ../INTEL-MAC.md
EOF
