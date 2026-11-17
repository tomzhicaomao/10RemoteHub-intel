#!/bin/sh
# Double-click in Finder to start 10RemoteHub on an Intel Mac.
# Run setup-intel-mac.sh once first. Closing the Terminal window quits the app.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "尚未安装运行环境。请先在本目录执行： sh setup-intel-mac.sh"
  echo
  read -r _ </dev/null 2>/dev/null || true
  exit 1
fi
# The CLI shim used by WorkBuddy shells guards bulk deletes; it is irrelevant
# for a GUI process and can interfere with Qt plugin loading.
if [ -n "${CODEBUDDY_SAFE_DELETE_ENABLED:-}" ]; then
  unset PYTHONPATH CODEBUDDY_SAFE_DELETE_ENABLED PYTHONDONTWRITEBYTECODE
fi
# After 安装遥控器组件.command has run, the signed host is required: the
# privileged collector only accepts a client signed as org.remotehub.desktop.
# Without the component installed, fall back to the plain interpreter.
PY=.venv/bin/python
if [ -x .venv/bin/python-remotehub ]; then
  PY=.venv/bin/python-remotehub
fi
exec "$PY" -m remotehub "$@"
