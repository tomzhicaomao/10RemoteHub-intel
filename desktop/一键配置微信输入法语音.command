#!/bin/sh
# 在 Finder 双击即可将 10RemoteHub 配置为「微信输入法」开箱即用。
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "尚未安装运行环境。请先在本目录执行： sh setup-intel-mac.sh"
  echo
  read -r _ </dev/null 2>/dev/null || true
  exit 1
fi
.venv/bin/python -m remotehub.wetype_setup
status=$?
echo
if [ "$status" -eq 0 ]; then
  echo "配置已保存！如果 10RemoteHub 正在运行，请重启应用后即可直接拿起遥控器使用。"
else
  echo "配置未完成（退出码：$status）。"
fi
echo "按回车键关闭窗口…"
read -r _ </dev/null 2>/dev/null || true
exit "$status"
