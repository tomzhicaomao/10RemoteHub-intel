"""Agent CLI: ``python -m remotehub.cli`` (packaged as ``10remotehub-cli``).

Lets an AI assistant change the remote configuration from a terminal: when the
main program runs, the request goes over its single-instance local channel and
takes effect at once; otherwise ``remotes.json`` is edited directly under a file
lock with a backup. See ``cli_service`` for the commands and rules.

Exit codes: 0 done, 2 bad arguments, 3 not found, 4 rejected by a rule or by
validation, 5 configuration file could not be read or written (or changed
underneath), 6 the main program did not answer, 1 unexpected failure.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from . import cli_service as service
from .cli_service import CliError

INSTANCE = "org.remotehub.desktop.instance"      # same name as app.INSTANCE
CONNECT_MS = 300
ANSWER_MS = 20_000


def instance_name():
    # Tests and side-by-side development builds use their own channel name.
    return os.environ.get("REMOTEHUB_CLI_INSTANCE") or INSTANCE


def _normalize(args, argv):
    """Absolute paths for file arguments: the main program runs in another directory."""
    argv = list(argv)
    if args.command in ("import",):
        absolute = str(Path(args.file).expanduser().resolve())
        argv[len(argv) - 1 - argv[::-1].index(args.file)] = absolute
        args.file = absolute
    return argv


def ask_main_program(argv, config_dir):
    """Send the request to the running main program: (code, payload), or None if it is not running."""
    from PySide6.QtNetwork import QLocalSocket

    socket = QLocalSocket()
    socket.connectToServer(instance_name())
    if not socket.waitForConnected(CONNECT_MS):
        return None
    request = {"argv": list(argv), "config_dir": str(Path(config_dir).expanduser().resolve()) if config_dir else None}
    socket.write(service.PROTOCOL + json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
    socket.flush()
    data = b""
    deadline = time.monotonic() + ANSWER_MS / 1000
    while b"\n" not in data and time.monotonic() < deadline:
        if not socket.bytesAvailable() and not socket.waitForReadyRead(500):
            if socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
                data += bytes(socket.readAll())
                break
        data += bytes(socket.readAll())
    socket.disconnectFromServer()
    try:
        answer = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
        return int(answer["code"]), answer["payload"]
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise CliError("主程序没有回应命令行请求（可能是不支持命令行的旧版本，或正忙）。",
                       "确认主程序是最新版本后重试；也可以先退出主程序，命令行会直接修改配置文件。", service.CHANNEL)


def run(argv):
    """Parse and execute; returns (exit code, payload, args or None)."""
    args = None
    try:
        args = service.parse(argv)
        argv = _normalize(args, argv)
        answer = ask_main_program(argv, args.config_dir)
        if answer is not None and answer[0] != service.OTHER_DIRECTORY:
            code, payload = answer
        else:
            with service.offline(args.config_dir, args) as session:
                code, payload = service.execute(args, session)
        if code == service.OK and args.command == "export":
            payload = _write_export(args.file, payload)
        return code, payload, args
    except CliError as exc:
        return exc.code, exc.payload(), args


def _write_export(file, payload):
    from .model import atomic_write

    if payload.get("dry_run"):
        return payload
    try:
        atomic_write(Path(file).expanduser(), payload.pop("text"))
    except OSError as exc:
        raise CliError(f"无法写入导出文件：{exc.__class__.__name__}", "检查目标目录是否存在、是否有写入权限。", service.STORAGE)
    payload["file"] = Path(file).name          # never echo a personal path
    return payload


# -- output ---------------------------------------------------------------------

def _action(view):
    return view["label"] if view else "未设置"


def render_text(args, payload):
    if not payload.get("ok", True):
        error = payload["error"]
        lines = [f"错误：{error['message']}"]
        if error.get("hint"):
            lines.append(f"怎么改：{error['hint']}")
        return "\n".join(lines)
    mode = "主程序在运行，改动已即时生效" if payload.get("mode") == "online" else "主程序未运行，已直接读写配置文件"
    command = args.command if args is not None else ""
    lines = []
    if command == "status":
        remote, profile = payload["remote"], payload["profile"]
        lines.append(f"当前遥控器：{remote['name']}（{remote['id']}）" if remote else "当前遥控器：未添加")
        if remote:
            lines.append("接收端：" + "、".join(f"{c['transport']} {c['id']}" for c in remote["connections"]))
        lines.append(f"配置方案：{profile['name']}（{profile['id']}，{profile['keys']} 个键）" if profile else "配置方案：无")
        connection = payload["connection"]
        lines.append(f"连接状态：{connection['state']}" + (f" · {connection['detail']}" if connection.get("detail") else ""))
        lines.append(f"读取路径：{payload['input_path']['text']}")
        lines.append(f"模式：{'在线（主程序在运行）' if payload['mode'] == 'online' else '离线（主程序未运行）'}")
        return "\n".join(lines)
    if command == "remotes" and args.op == "list":
        for remote in payload["remotes"]:
            mark = "●" if remote["active"] else "○"
            ids = "、".join(c["id"] for c in remote["connections"])
            lines.append(f"{mark} {remote['name']}  id={remote['id']}  接收端 {ids}  {remote['profiles']} 个方案")
        return "\n".join(lines) or "还没有添加遥控器。"
    if command == "profiles" and args.op == "list":
        lines.append(f"遥控器：{payload['remote']}")
        for profile in payload["profiles"]:
            mark = "●" if profile["active"] else "○"
            lines.append(f"{mark} {profile['name']}  id={profile['id']}  {profile['keys']} 个键  {profile['layers']} 个应用层")
        return "\n".join(lines)
    if command == "keys" and args.op == "list":
        lines.append(f"遥控器：{payload['remote']} · 方案：{payload['profile']['name']}")
        for key in payload["keys"]:
            head = f"- {key['name']}  id={key['id']}  键码={key['code'] or '未学'}"
            if key["fixed"]:
                lines.append(head + f"  [{key['fixed']}]")
                continue
            lines.append(head + f"  单击={_action(key['tap'])}  双击={_action(key['double'])}  长按={_action(key['hold'])}")
            for layer in key["layers"]:
                parts = [f"{name}={_action(layer.get(gesture))}" for gesture, name in
                         (("tap", "单击"), ("double", "双击"), ("hold", "长按")) if gesture in layer]
                lines.append(f"    在「{layer['layer']}」({layer['app']})：" + "  ".join(parts))
        return "\n".join(lines)
    if command == "layers" and args.op == "list":
        lines.append(f"遥控器：{payload['remote']} · 方案：{payload['profile']['name']}")
        for layer in payload["layers"]:
            state = "启用" if layer["enabled"] else "停用"
            lines.append(f"- {layer['name']}  应用={layer['app']}  id={layer['id']}  {state}")
            for item in layer["overrides"]:
                gesture = {"tap": "单击", "double": "双击", "hold": "长按"}[item["gesture"]]
                lines.append(f"    {item['key_name']} {gesture} → {_action(item['action'])}")
        return "\n".join(lines) or "当前方案没有应用层。"
    if command == "actions":
        for kind in payload["kinds"]:
            params = "，".join(("" if p["required"] else "可选 ") + f"{p['name']}：{p['description']}" for p in kind["params"])
            lines.append(f"- {kind['kind']}（{kind['name']}）" + (f"  参数：{params}" if params else "  无参数")
                         + (f"  注：{kind['note']}" if kind["note"] else ""))
        lines.append("常见操作 target：" + "、".join(f"{t['target']}={t['name']}" for t in payload["common_targets"]))
        lines.append("示例：--action '" + json.dumps(payload["example"], ensure_ascii=False) + "'")
        return "\n".join(lines)
    if command == "export":
        return f"{payload['message']} 文件：{payload.get('file', '')}"
    # Write commands.
    prefix = "【试运行，未修改】" if payload.get("dry_run") else ""
    lines.append(prefix + payload.get("message", "完成。"))
    for change in payload.get("changes", []):
        lines.append("  " + _change_text(change))
    if not payload.get("dry_run") and payload.get("changed"):
        lines.append(mode + "。")
    return "\n".join(lines)


def _change_text(change):
    if "gesture" in change:
        gesture = {"tap": "单击", "double": "双击", "hold": "长按"}[change["gesture"]]
        where = f"（{change['layer']} 应用层）" if change.get("layer") else ""
        where += f"（方案 {change['profile']}）" if change.get("profile") else ""
        before = _action(change["before"]) if change["before"] else ("继承全局" if change.get("layer") else "未设置")
        after = _action(change["after"]) if change["after"] else ("继承全局" if change.get("layer") else "未设置")
        note = f"  [{change['note']}]" if change.get("note") else ""
        return f"{change['key_name']} {gesture}{where}：{before} → {after}{note}"
    kind = change.get("change")
    if kind == "新建应用层":
        return f"新建应用层「{change['name']}」（{change['layer']}）"
    if kind == "切换" and "remote" in change:
        return f"切换遥控器 → 「{change['name']}」"
    if kind == "切换":
        return f"切换配置方案 → 「{change['name']}」"
    if kind in ("新增", "替换"):
        return f"{kind}配置方案「{change['profile']}」（{change['keys']} 个键）"
    return "、".join(f"{key}={value}" for key, value in change.items())


def _utf8_when_piped():
    """Agents read our output through pipes. Those default to the system code page (GBK on
    Chinese Windows), which cannot encode text such as 「音量 −」, and a frozen app ignores
    PYTHONUTF8. A real console keeps its own encoding."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty() and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


def main(argv=None):
    from PySide6.QtCore import QCoreApplication

    _utf8_when_piped()
    argv = sys.argv[1:] if argv is None else list(argv)
    wants_json = "--json" in argv
    app = QCoreApplication.instance() or QCoreApplication([sys.argv[0]])  # the local socket needs one
    try:
        code, payload, args = run(argv)
    except SystemExit:
        raise
    except Exception as exc:  # last resort: a clear message, never a traceback for the AI to parse
        code, args = service.UNEXPECTED, None
        payload = CliError(f"意外错误：{exc.__class__.__name__}", "请把这条命令和输出反馈给维护者。",
                           service.UNEXPECTED).payload()
    del app
    if wants_json or (args is not None and args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif "--help" not in argv and "-h" not in argv:
        stream = sys.stdout if code == service.OK else sys.stderr
        print(render_text(args, payload), file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
