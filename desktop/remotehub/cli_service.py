"""Configuration commands shared by the agent CLI and the running main program.

The CLI (``python -m remotehub.cli``) lets an AI assistant read and change the
remote configuration from a terminal. Everything here only reads and writes
configuration: no key is sent, no USB device is opened, no audio route or
system permission is touched.

Two sessions carry out the same commands:

* :class:`OfflineSession` - the main program is not running. The CLI loads
  ``remotes.json`` through ``RemoteLibraryStore`` under a file lock, checks that
  nobody else wrote the file in between, backs it up and writes it atomically.
* ``cli_bridge.OnlineSession`` - the main program is running. The request comes
  over its single-instance local channel and is carried out by ``AppController``
  so the change takes effect at once and the window refreshes.

Both validate with the main program's own rules: ``Action.validate``,
``Profile.validate`` and ``Remote.validate`` (through ``RemoteLibraryStore``),
plus the fixed-key rules the controller enforces (the bundled model's voice key
stays a voice key; blocked keys are never configured).

A key may carry several codes (one per receiver, C3): ``code`` is the primary one and
``codes`` lists them all.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from .model import (
    GESTURE_FIELDS, KINDS, MAX_MACRO_STEPS, MODIFIERS, MOUSE_BUTTONS, SPECIAL_KEYS, Action, AppLayer, Profile,
    atomic_write, normalize_app_layer_id, validate_app_layer_id,
)

PROTOCOL = b"10remotehub-cli/1\n"
MAX_REQUEST = 1 << 20
BACKUP_DIR = "cli-backups"
BACKUPS_KEPT = 10
LOCK_NAME = ".remotes.json.cli-lock"
LOCK_WAIT_S = 10.0

# CLI gesture names -> model gesture names.
GESTURES = {"tap": "single", "double": "double", "hold": "long"}
GESTURE_NAMES = {"single": "单击", "double": "双击", "long": "长按"}
CLI_GESTURE = {model: cli for cli, model in GESTURES.items()}

# Exit codes. argparse already uses 2 for usage errors.
OK = 0
UNEXPECTED = 1
USAGE = 2
NOT_FOUND = 3
REJECTED = 4
STORAGE = 5
CHANNEL = 6
OTHER_DIRECTORY = 7      # internal: the running program uses another configuration directory


class CliError(Exception):
    """A user-facing failure: what went wrong and how to fix it."""

    def __init__(self, message, hint="", code=REJECTED):
        super().__init__(message)
        self.message = str(message)
        self.hint = hint
        self.code = code

    def payload(self):
        return {"ok": False, "error": {"code": self.code, "message": self.message, "hint": self.hint}}


# -- arguments ------------------------------------------------------------------

_ARGPARSE_WORDS = (
    ("the following arguments are required", "缺少参数"), ("unrecognized arguments", "无法识别的参数"),
    ("invalid choice", "无效的选择"), ("choose from", "可选"), ("expected one argument", "需要一个值"),
    ("argument ", "参数 "),
)


class ChineseHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, "用法：" if prefix is None else prefix)


class ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", ChineseHelpFormatter)
        parents = kwargs.pop("parents", [])
        super().__init__(*args, **kwargs)
        self._positionals.title = "参数"
        self._optionals.title = "选项"
        for action in self._actions:
            if action.dest == "help":
                action.help = "显示帮助并退出"
        # Added after renaming so shared options land in the same "选项" group.
        for parent in parents:
            self._add_container_actions(parent)
            self._defaults.update(parent._defaults)

    def error(self, message):
        for english, chinese in _ARGPARSE_WORDS:
            message = message.replace(english, chinese)
        raise CliError(f"参数有误：{message}", f"运行「{self.prog} --help」查看用法。", USAGE)


def build_parser(prog="10remotehub-cli"):
    common = ChineseArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出 JSON，供 AI 或脚本解析")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="只显示会改什么，不真改")
    common.add_argument("--config-dir", default=argparse.SUPPRESS, metavar="目录",
                        help="配置目录（默认与主程序相同；一般不用填）")

    parser = ChineseArgumentParser(
        prog=prog, parents=[common],
        description="10RemoteHub 配置命令行：查看和修改遥控器、配置方案与按键动作。只读写配置，不发送按键。",
        epilog="先用 status 和 keys list 看清现状，写操作加 --dry-run 给用户确认后再执行。",
    )
    commands = parser.add_subparsers(dest="command", metavar="命令", parser_class=ChineseArgumentParser)
    commands.required = True

    commands.add_parser("status", parents=[common], help="当前遥控器、配置方案、连接状态、读取路径")

    remotes = commands.add_parser("remotes", parents=[common], help="列出、切换遥控器")
    sub = remotes.add_subparsers(dest="op", metavar="操作", parser_class=ChineseArgumentParser)
    sub.required = True
    sub.add_parser("list", parents=[common], help="列出遥控器")
    use = sub.add_parser("use", parents=[common], help="切换到另一只遥控器")
    use.add_argument("remote", help="遥控器 id 或名称")

    profiles = commands.add_parser("profiles", parents=[common], help="列出、切换配置方案")
    sub = profiles.add_subparsers(dest="op", metavar="操作", parser_class=ChineseArgumentParser)
    sub.required = True
    sub.add_parser("list", parents=[common], help="列出当前遥控器的配置方案")
    use = sub.add_parser("use", parents=[common], help="切换配置方案")
    use.add_argument("profile", help="配置方案 id 或名称")

    keys = commands.add_parser("keys", parents=[common], help="查看、设置、清除按键动作")
    sub = keys.add_subparsers(dest="op", metavar="操作", parser_class=ChineseArgumentParser)
    sub.required = True
    sub.add_parser("list", parents=[common], help="当前方案的所有键")
    for name, text in (("set", "设置动作"), ("clear", "清除动作")):
        command = sub.add_parser(name, parents=[common], help=text)
        command.add_argument("key", help="键 id 或名称（keys list 里查）")
        command.add_argument("--gesture", choices=tuple(GESTURES), default="tap", help="tap 单击 / double 双击 / hold 长按")
        if name == "set":
            command.add_argument("--action", required=True, help='动作 JSON，如 \'{"kind":"shortcut","keys":["cmd","s"]}\'')

    actions = commands.add_parser("actions", parents=[common], help="可用的动作类型")
    sub = actions.add_subparsers(dest="op", metavar="操作", parser_class=ChineseArgumentParser)
    sub.required = True
    sub.add_parser("kinds", parents=[common], help="动作类型和每种需要的参数")

    layers = commands.add_parser("layers", parents=[common], help="按应用切换的按键层")
    sub = layers.add_subparsers(dest="op", metavar="操作", parser_class=ChineseArgumentParser)
    sub.required = True
    sub.add_parser("list", parents=[common], help="当前方案的应用层")
    for name, text in (("set", "设置某个应用里某颗键的动作"), ("clear", "清除应用层覆盖，恢复继承全局")):
        command = sub.add_parser(name, parents=[common], help=text)
        command.add_argument("app", help="应用标识：macOS 用 Bundle ID（如 com.microsoft.VSCode），Windows 用 exe:程序名.exe")
        command.add_argument("key", help="键 id 或名称")
        command.add_argument("--gesture", choices=tuple(GESTURES), default="tap", help="tap 单击 / double 双击 / hold 长按")
        if name == "set":
            command.add_argument("--action", required=True, help="动作 JSON；kind 为 none 表示在这个应用里不做任何事")
            command.add_argument("--name", default="", help="新建应用层时的显示名称（默认用应用标识）")

    export = commands.add_parser("export", parents=[common], help="导出当前配置方案")
    export.add_argument("file", help="导出到的 .json 文件")
    imported = commands.add_parser("import", parents=[common], help="导入配置方案")
    imported.add_argument("file", help="之前导出的 .json 文件")
    imported.add_argument("--replace", action="store_true", help="替换当前方案（默认新增一个方案）")
    return parser


def parse(argv, prog="10remotehub-cli"):
    args = build_parser(prog).parse_args(list(argv))
    for name in ("json", "dry_run"):
        if not hasattr(args, name):
            setattr(args, name, False)
    if not hasattr(args, "config_dir"):
        args.config_dir = None
    return args


WRITE_COMMANDS = {("remotes", "use"), ("profiles", "use"), ("keys", "set"), ("keys", "clear"),
                  ("layers", "set"), ("layers", "clear"), ("import", None)}


def is_write(args):
    return (args.command, getattr(args, "op", None)) in WRITE_COMMANDS


# -- views ------------------------------------------------------------------------

def action_view(action):
    """Compact action dict: kind plus the fields that are set, and a Chinese label."""
    from .presentation import action_text

    if action is None:
        return None
    data = {key: value for key, value in asdict(action).items() if value not in ("", [], {}, False) or key == "kind"}
    data["label"] = action_text(action)
    return data


def connection_id(connection):
    return f"{connection.vendor:04X}:{connection.product:04X}"


def remote_view(remote, active_id):
    return {
        "id": remote.id, "name": remote.name, "active": remote.id == active_id,
        "model_id": remote.model_id, "fixed_model": remote.is_fixed_model,
        # Unit keys are serial digests: never shown.
        "connections": [{"transport": c.transport, "id": connection_id(c), "label": c.label()}
                        for c in remote.connections],
        "profiles": len(remote.profiles),
    }


def is_voice_key(remote, profile, button):
    """Same test as ``AppController._is_voice_button``."""
    from .photo_template import is_reserved_voice_key

    return is_reserved_voice_key(remote, profile, button)


def key_view(remote, profile, button):
    fixed = ""
    if not button.configurable:
        fixed = "禁配键，不能配置"
    elif is_voice_key(remote, profile, button):
        fixed = "配套型号的语音键，固定用于语音输入"
    layers = []
    for layer in profile.app_layers:
        overrides = layer.overrides.get(button.id) or {}
        if overrides:
            layers.append({"app": layer.app_id, "layer": layer.name, "enabled": layer.enabled,
                           **{CLI_GESTURE[g]: action_view(a) for g, a in overrides.items()}})
    view = {"id": button.id, "name": button.name, "code": button.trigger, "codes": button.all_triggers, "configurable": not fixed, "fixed": fixed}
    for gesture, name in GESTURE_FIELDS.items():
        action = getattr(button, name)
        view[CLI_GESTURE[gesture]] = action_view(action) if action.kind != "none" else None
    view["layers"] = layers
    return view


def layer_view(profile, layer):
    names = {b.id: b.name for b in profile.buttons}
    return {
        "id": layer.id, "app": layer.app_id, "name": layer.name, "enabled": layer.enabled,
        "overrides": [{"key": key, "key_name": names.get(key, key), "gesture": CLI_GESTURE[gesture],
                       "action": action_view(action)}
                      for key, gestures in layer.overrides.items() for gesture, action in gestures.items()],
    }


KIND_PARAMS = {
    "none": ([], "不做任何事"),
    "shortcut": ([("keys", "按键列表：一个主键加可选修饰键 ctrl/alt/shift/cmd，如 [\"cmd\",\"s\"]", True)], ""),
    "common": ([("target", "常见操作的标识，见 common_targets", True)], ""),
    "hold": ([("keys", "一个修饰键，如 [\"shift\"]", True), ("target", "hold 按住期间保持 / latch 按一下锁定再按取消", True)], ""),
    "toggle_app": ([("target", "App 的完整路径（macOS .app，Windows .exe）", True)], "打开或收起这个 App"),
    "app": ([("target", "App 的完整路径", True)], "旧配置；新配置请用 toggle_app"),
    "file": ([("target", "文件的完整路径", True), ("application", "可选：用哪个 App 打开", False)], ""),
    "minimize_window": ([], ""), "close_window": ([], ""), "quit_app": ([], ""),
    "voice": ([], "只能用于单击；配套型号的语音键固定为此动作"),
    "text": ([("text", "要输入的文字，1–10000 字", True), ("enter", "true 时输入后再按回车", False)], ""),
    "grid": ([("target", "面板名称", False), ("cells", "9 个格位，每格 {\"title\":..,\"action\":{..}}", True)],
             "建议在主程序里编辑"),
    "nine_key": ([("spec", "九键绑定，见主程序九键设置", True)], "建议在主程序里设置"),
    "cheat_sheet": ([], ""),
    "macro": ([("target", "动作链名称", False),
               ("steps", f"1–{MAX_MACRO_STEPS} 步，每步 {{\"action\":{{..}},\"delay_ms\":120}}", True)], ""),
    "cycle_profile": ([], "切换到下一个配置方案；会同步到同型号其他方案中这颗键的空位"),
    "ui_element": ([("application", "应用标识", True), ("target", "press 或 focus", True),
                    ("spec", "控件定位信息（需在主程序里拾取）", True)], "需要在主程序里拾取控件"),
    "app_menu": ([("application", "可选：应用标识", False), ("spec", "{\"path\":[\"菜单\",\"命令\"]}", True)], ""),
    "mouse": ([("target", "单击位置用 left/right/middle", True)], "独占整颗键：只能用于单击，同时清除双击和长按"),
}


def kinds_view():
    from .common_actions import COMMON_ACTIONS
    from .presentation import KIND_TITLES

    kinds = []
    for kind in KINDS:
        params, note = KIND_PARAMS.get(kind, ([], ""))
        kinds.append({
            "kind": kind, "name": KIND_TITLES.get(kind, KINDS[kind]),
            "params": [{"name": n, "description": d, "required": r} for n, d, r in params],
            "gestures": ["tap"] if kind in ("voice", "mouse") else ["tap", "double", "hold"],
            "note": note,
        })
    return {
        "kinds": kinds,
        "common_targets": [{"target": key, "name": name} for key, name in COMMON_ACTIONS.items()],
        "keys": sorted(SPECIAL_KEYS) + [f"f{i}" for i in range(1, 25)] + ["a–z", "0–9", "标点 -=[]\\;',./`"],
        "modifiers": list(MODIFIERS),
        "mouse_targets": list(MOUSE_BUTTONS),
        "example": {"kind": "shortcut", "keys": ["cmd", "s"]},
    }


# -- lookups ------------------------------------------------------------------------

def find_remote(store, ref):
    remotes = list(store.remotes)
    match = next((r for r in remotes if r.id == ref), None) or _unique(
        [r for r in remotes if r.name == ref], ref, "遥控器", lambda r: r.id)
    if match is None:
        raise CliError(f"找不到遥控器「{ref}」。", "用 remotes list 查看遥控器的 id。", NOT_FOUND)
    return match


def current(store):
    remote = store.remote
    if remote is None:
        raise CliError("还没有添加遥控器。", "先在主程序里添加遥控器。", NOT_FOUND)
    profile = next((p for p in remote.profiles if p.id == store.settings.get("active_profile_id")), remote.profiles[0])
    return remote, profile


def find_profile(remote, ref):
    match = next((p for p in remote.profiles if p.id == ref), None) or _unique(
        [p for p in remote.profiles if p.name == ref], ref, "配置方案", lambda p: p.id)
    if match is None:
        raise CliError(f"当前遥控器没有配置方案「{ref}」。", "用 profiles list 查看配置方案的 id。", NOT_FOUND)
    return match


def find_key(profile, ref):
    match = next((b for b in profile.buttons if b.id == ref), None) or _unique(
        [b for b in profile.buttons if b.name == ref], ref, "键", lambda b: b.id)
    if match is None:
        raise CliError(f"当前方案里没有键「{ref}」。", "用 keys list 查看键的 id 或准确名称。", NOT_FOUND)
    return match


def find_layer(profile, app):
    norm = normalize_app_layer_id(app).casefold()
    return next((layer for layer in profile.app_layers
                 if layer.id == app or layer.app_id.casefold() == norm or layer.name == app), None)


def _unique(matches, ref, what, ident):
    if len(matches) > 1:
        raise CliError(f"有 {len(matches)} 个{what}都叫「{ref}」。", "请改用 id：" + "、".join(ident(m) for m in matches),
                       NOT_FOUND)
    return matches[0] if matches else None


def parse_action(text):
    try:
        raw = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CliError(f"动作不是有效的 JSON：{exc}", '例：--action \'{"kind":"shortcut","keys":["cmd","s"]}\'', USAGE)
    if isinstance(raw, str):
        raw = {"kind": raw}
    try:
        action = Action.from_dict(raw)
    except (TypeError, ValueError, AttributeError) as exc:
        raise CliError(f"动作字段无效：{exc}", "用 actions kinds 查看每种动作需要的字段。", USAGE)
    try:
        action.validate(sys.platform)
    except (TypeError, ValueError) as exc:
        raise CliError(f"动作无效：{exc}", "用 actions kinds 查看每种动作需要的字段。", REJECTED)
    return action


# -- changes on a copy of the configuration ------------------------------------------

@dataclass
class Plan:
    """The result of a write command, computed on the configuration objects given."""

    message: str
    changes: list = field(default_factory=list)
    data: dict = field(default_factory=dict)


def check_key_rules(remote, profile, button, gesture, action, layer=None):
    """The fixed-key rules the controller enforces, so the CLI cannot get around them."""
    if not button.configurable:
        raise CliError(f"「{button.name}」是禁配键，不能配置。", "换一颗 keys list 里 configurable 为 true 的键。")
    if is_voice_key(remote, profile, button):
        if layer is not None:
            raise CliError(f"「{button.name}」是配套型号的语音键，不支持应用层覆盖。", "换一颗普通键。")
        if gesture != "single" or action != Action("voice"):
            raise CliError(f"「{button.name}」是配套型号的语音键，固定用于语音输入，不能改成普通动作。",
                           "语音方式在主程序「语音设置」里调整；要配置动作请换一颗普通键。")
    if gesture != "single" and action.kind in ("voice", "mouse"):
        raise CliError("双击和长按不支持" + ("语音动作。" if action.kind == "voice" else "鼠标按键。"),
                       "这类动作只能用 --gesture tap。")
    if action.kind == "mouse" and action.target not in MOUSE_BUTTONS:
        raise CliError("实体按键只支持绑定鼠标左键、右键或中键。", 'target 用 "left"、"right" 或 "middle"。')


def plan_key_action(remote, profile, button, gesture, action):
    """Set (or with a ``none`` action clear) one gesture of a key in the base profile."""
    check_key_rules(remote, profile, button, gesture, action)
    name = GESTURE_FIELDS[gesture]
    before = getattr(button, name)
    if gesture != "single" and button.action.kind == "mouse" and action.kind != "none":
        raise CliError("这颗键的单击已独占模拟鼠标，不能再设置双击或长按。", "先把单击改成其他动作。")
    changes = []
    if before != action:
        changes.append(_change(button, gesture, before, action))
    setattr(button, name, action)
    if action.kind == "mouse":
        for other in ("double", "long"):
            old = getattr(button, GESTURE_FIELDS[other])
            if old.kind != "none":
                changes.append(_change(button, other, old, Action(), note="鼠标按键独占整颗键"))
                setattr(button, GESTURE_FIELDS[other], Action())
    if action.kind == "cycle_profile":
        changes.extend(_sync_cycle(remote, profile, button, gesture))
    verb = "清除" if action.kind == "none" else "设置"
    return Plan(f"「{button.name}」{GESTURE_NAMES[gesture]}已{verb}" + ("" if changes else "（没有变化）") + "。", changes)


def _sync_cycle(remote, profile, button, gesture):
    """Mirror the controller: other same-hardware profiles get cycle_profile on an empty gesture."""
    changes = []
    name = GESTURE_FIELDS[gesture]
    for other in remote.profiles:
        if other is profile or (other.vendor, other.product) != (profile.vendor, profile.product):
            continue
        target = next((b for b in other.buttons if b.configurable and not is_voice_key(remote, other, b)
                       and (b.id == button.id or any(b.matches_trigger(code) for code in button.all_triggers))), None)
        if target is not None and getattr(target, name).kind == "none":
            changes.append(_change(target, gesture, Action(), Action("cycle_profile"), profile=other.name))
            setattr(target, name, Action("cycle_profile"))
    return changes


def plan_layer_action(remote, profile, button, gesture, action, app, layer_name="", *, clear=False):
    """Override (or with ``clear`` restore inheritance of) one gesture of a key inside an app layer."""
    layer = find_layer(profile, app)
    created = None
    if layer is None:
        if clear:
            raise CliError(f"当前方案没有「{app}」的应用层。", "用 layers list 查看已有的应用层。", NOT_FOUND)
        try:
            app_id = validate_app_layer_id(app)
        except ValueError as exc:
            raise CliError(f"应用标识「{app}」无效：{exc}。",
                           "macOS 用 Bundle ID（如 com.microsoft.VSCode），Windows 用 exe:程序名.exe。", REJECTED)
        layer = AppLayer(app_id=app_id, name=(layer_name or app_id).strip()[:100] or app_id)
        profile.app_layers.append(layer)
        created = layer
    if not clear:
        check_key_rules(remote, profile, button, gesture, action, layer)
    elif not button.configurable or is_voice_key(remote, profile, button):
        raise CliError(f"「{button.name}」是固定按键，没有应用层覆盖。", "换一颗普通键。")
    overrides = layer.overrides.setdefault(button.id, {})
    changes = []
    if created is not None:
        changes.append({"layer": layer.app_id, "change": "新建应用层", "name": layer.name})
    before = overrides.get(gesture)
    if clear:
        if before is not None:
            changes.append(_change(button, gesture, before, None, layer=layer.app_id))
            overrides.pop(gesture)
    else:
        effective_single = overrides.get("single", button.action)
        if gesture != "single" and effective_single.kind == "mouse" and action.kind != "none":
            raise CliError("这颗键在该应用里已独占模拟鼠标，不能再设置双击或长按。", "先改单击动作。")
        if before != action:
            changes.append(_change(button, gesture, before, action, layer=layer.app_id))
        overrides[gesture] = action
        if action.kind == "mouse":
            for other in ("double", "long"):
                if other in overrides:
                    changes.append(_change(button, other, overrides.pop(other), None, layer=layer.app_id,
                                           note="鼠标按键独占整颗键"))
    if not overrides:
        layer.overrides.pop(button.id, None)
    if clear:
        text = f"「{button.name}」{GESTURE_NAMES[gesture]}在「{layer.name}」中恢复继承全局"
    else:
        text = f"「{button.name}」{GESTURE_NAMES[gesture]}在「{layer.name}」中已设置"
    return Plan(text + ("" if changes else "（没有变化）") + "。", changes, {"layer": layer.id, "created": created is not None})


def plan_import(remote, profile_list, text, current_profile, *, replace=False):
    """Mirror ``AppController.import_profile`` on a copy: returns the plan and the new profile."""
    from .photo_template import apply_fixed_keys
    from .remote_library import RECEIVER_MODEL
    from .store import MAX_PROFILES

    try:
        imported = Profile.loads(text)
    except ValueError as exc:
        raise CliError(f"导入的文件不是有效的配置方案：{exc}", "请导入用 export 或主程序导出的 .json 文件。", REJECTED)
    accepted = {(c.vendor, c.product) for c in remote.connections}
    if accepted and (imported.vendor, imported.product) not in accepted:
        raise CliError(
            f"这份按键配置对应型号 {imported.vendor:04X}:{imported.product:04X}，与当前遥控器「{remote.name}」不匹配。",
            "先用 remotes use 切换到对应遥控器再导入。", REJECTED)
    apply_fixed_keys(imported, model_id=RECEIVER_MODEL if remote.is_fixed_model else "")
    try:
        imported.validate()
    except ValueError as exc:
        raise CliError(f"导入的配置方案无效：{exc}", "请检查文件内容。", REJECTED)
    if replace:
        imported.id = current_profile.id
        index = profile_list.index(current_profile)
        profile_list[index] = imported
        message = f"已用导入的配置替换方案「{current_profile.name}」。"
    else:
        if len(profile_list) >= MAX_PROFILES:
            raise CliError(f"最多保存 {MAX_PROFILES} 个配置方案。", "先在主程序里删掉不用的方案。")
        imported.id = uuid.uuid4().hex
        names = {p.name for p in profile_list}
        name, number = imported.name, 2
        while name in names:
            name = f"{imported.name[:95]} {number}"
            number += 1
        imported.name = name
        profile_list.append(imported)
        message = f"已导入配置方案「{imported.name}」并切换到它。"
    change = {"profile": imported.name, "change": "替换" if replace else "新增", "keys": len(imported.buttons)}
    return Plan(message, [change], {"profile": imported.id}), imported


def _change(button, gesture, before, after, *, layer="", note="", profile=""):
    change = {"key": button.id, "key_name": button.name, "gesture": CLI_GESTURE[gesture],
              "before": action_view(before) if before is not None and before.kind != "none" else None,
              "after": action_view(after) if after is not None and after.kind != "none" else None}
    if after is not None and after.kind == "none" and layer:
        change["after"] = action_view(after)        # explicit "do nothing" in this app
    if layer:
        change["layer"] = layer
    if profile:
        change["profile"] = profile
    if note:
        change["note"] = note
    return change


# -- executing a parsed command ------------------------------------------------------

def execute(args, session):
    """Run one parsed command in ``session``; returns (exit code, payload)."""
    try:
        payload = _dispatch(args, session)
        payload.setdefault("ok", True)
        payload["mode"] = session.mode
        if is_write(args):
            payload["dry_run"] = bool(args.dry_run)
        return OK, payload
    except CliError as exc:
        payload = exc.payload()
        payload["mode"] = session.mode
        return exc.code, payload


def _dispatch(args, session):
    store = session.store
    command, op = args.command, getattr(args, "op", None)
    if command == "status":
        return session.status()
    if command == "actions":
        return kinds_view()
    if command == "remotes" and op == "list":
        return {"remotes": [remote_view(r, store.active) for r in store.remotes]}
    if command == "remotes":
        target = find_remote(store, args.remote)
        plan = Plan(f"已切换到遥控器「{target.name}」。" if target.id != store.active else f"「{target.name}」已是当前遥控器。",
                    [] if target.id == store.active else [{"remote": target.id, "name": target.name, "change": "切换"}])
        return session.commit_remote(target, plan, args.dry_run)
    remote, profile = current(store)
    if command == "profiles" and op == "list":
        return {"remote": remote.name, "profiles": [
            {"id": p.id, "name": p.name, "active": p.id == profile.id, "keys": len(p.buttons),
             "layers": len(p.app_layers)} for p in remote.profiles]}
    if command == "profiles":
        target = find_profile(remote, args.profile)
        plan = Plan(f"已切换到配置方案「{target.name}」。" if target is not profile else f"「{target.name}」已是当前方案。",
                    [] if target is profile else [{"profile": target.id, "name": target.name, "change": "切换"}])
        return session.commit_profile(remote, target, plan, args.dry_run)
    if command == "keys" and op == "list":
        return {"remote": remote.name, "profile": {"id": profile.id, "name": profile.name},
                "keys": [key_view(remote, profile, b) for b in profile.buttons]}
    if command == "layers" and op == "list":
        return {"remote": remote.name, "profile": {"id": profile.id, "name": profile.name},
                "layers": [layer_view(profile, layer) for layer in profile.app_layers]}
    if command == "export":
        data = deepcopy(profile)
        data.serial = ""                         # never export a serial number
        return {"profile": {"id": profile.id, "name": profile.name}, "text": data.dumps(),
                "message": f"已导出配置方案「{profile.name}」。"}
    if command == "import":
        return session.commit_import(remote, profile, args.file, args.replace, args.dry_run)
    gesture = GESTURES[args.gesture]
    if command == "keys":
        button = find_key(profile, args.key)
        new = parse_action(args.action) if op == "set" else Action()
        return session.commit_key(remote, profile, button, gesture, new, args.dry_run)
    if command == "layers":
        button = find_key(profile, args.key)
        new = parse_action(args.action) if op == "set" else None
        return session.commit_layer(remote, profile, button, gesture, new, args.app,
                                    getattr(args, "name", ""), args.dry_run)
    raise CliError("未知命令。", "运行 --help 查看用法。", USAGE)


def plan_payload(plan, *, extra=None):
    payload = {"message": plan.message, "changes": plan.changes, "changed": bool(plan.changes)}
    payload.update(plan.data)
    payload.update(extra or {})
    return payload


def read_import(path):
    path = Path(path)
    try:
        if path.stat().st_size > 14_000_000:
            raise CliError("配置文件过大（超过 14 MB）。", "请导入用 export 导出的文件。", REJECTED)
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CliError(f"无法读取要导入的文件：{exc.__class__.__name__}", "检查文件路径和读取权限。", STORAGE)


def input_path_offline(remote):
    """Which reading path the main program would use; presence is unknown offline."""
    if remote is None:
        return ""
    others = [c for c in remote.connections if not c.receiver]
    if remote.uses_receiver and not others:
        return "receiver"
    if remote.uses_receiver:
        return "receiver/hid"
    return "hid"


INPUT_PATH_TEXT = {
    "receiver": "配套接收器专用读取",
    "hid": "共享 USB／蓝牙 HID 读取",
    "receiver/hid": "配套接收器在场时专用读取，否则共享 HID 读取",
    "": "无",
}


def status_payload(store, *, connection, input_path):
    remote = store.remote
    profile = None
    if remote is not None:
        profile = next((p for p in remote.profiles if p.id == store.settings.get("active_profile_id")),
                       remote.profiles[0])
    return {
        "remote": None if remote is None else {
            "id": remote.id, "name": remote.name, "model_id": remote.model_id, "fixed_model": remote.is_fixed_model,
            "connections": [{"transport": c.transport, "id": connection_id(c)} for c in remote.connections]},
        "profile": None if profile is None else {"id": profile.id, "name": profile.name, "keys": len(profile.buttons)},
        "connection": connection,
        "input_path": {"id": input_path, "text": INPUT_PATH_TEXT.get(input_path, input_path)},
        "remotes": len(store.remotes),
    }


# -- offline session --------------------------------------------------------------

def default_directory():
    from platformdirs import user_config_path

    return user_config_path("RemoteHub", appauthor=False)


class FileLock:
    """Exclusive advisory lock between CLI processes writing the same configuration."""

    def __init__(self, path, wait=LOCK_WAIT_S):
        self.path = Path(path)
        self.wait = wait
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                self._lock()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise CliError("另一个命令正在修改配置，等待超时。", "稍后再试。", STORAGE)
                time.sleep(.05)

    def _lock(self):
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, *_):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _fingerprint(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _guarded_store_class():
    from .remote_library import RemoteLibraryStore

    class GuardedStore(RemoteLibraryStore):
        """``RemoteLibraryStore`` whose saves go through the CLI session (lock, check, backup)."""

        session = None

        def save(self):
            if self.load_error:
                raise ValueError("遥控器配置读取失败，本次不会覆盖原文件")
            text = self.serialize()            # validates every remote and profile
            self.session.write(text)

    return GuardedStore


class OfflineSession:
    """Read and write ``remotes.json`` directly while the main program is not running."""

    mode = "offline"

    def __init__(self, directory, *, write=False, dry_run=False):
        self.directory = Path(directory)
        self.write_enabled = write and not dry_run
        self.lock = FileLock(self.directory / LOCK_NAME) if self.write_enabled else None
        self.store = None
        self.loaded = None
        self.backup = None

    @property
    def path(self):
        return self.directory / "remotes.json"

    def __enter__(self):
        if self.lock is not None:
            self.lock.__enter__()
        try:
            self.loaded = _fingerprint(self.path)
            store = _guarded_store_class()(self.directory)
            store.session = self
            store.load()
            if store.load_error:
                raise CliError(store.load_error, "先在主程序里恢复配置，或从 cli-backups 里找回备份。", STORAGE)
            self.store = store
            after_load(self)
        except BaseException:
            if self.lock is not None:
                self.lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        if self.lock is not None:
            self.lock.__exit__(*exc)

    def write(self, text):
        """Atomic replace after a conflict check and a backup; nothing at all in dry-run."""
        if not self.write_enabled:
            return
        if _fingerprint(self.path) != self.loaded:
            raise CliError("配置文件在读取后被其他程序改过（可能主程序刚刚保存），这次没有写入。",
                           "重新执行同一条命令即可。", STORAGE)
        try:
            if self.path.exists():
                folder = self.directory / BACKUP_DIR
                folder.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                self.backup = folder / f"remotes-{stamp}-{uuid.uuid4().hex[:6]}.json"
                self.backup.write_bytes(self.path.read_bytes())
                for old in sorted(folder.glob("remotes-*.json"))[:-BACKUPS_KEPT]:
                    old.unlink(missing_ok=True)
            atomic_write(self.path, text)
        except OSError as exc:
            raise CliError(f"写入配置失败：{exc.__class__.__name__}", "检查配置目录的写入权限和磁盘空间。", STORAGE)
        self.loaded = _fingerprint(self.path)

    def _commit(self):
        self.store.save()        # write() is a no-op in dry-run, but validation always runs

    # -- views
    def status(self):
        return status_payload(self.store, connection={"state": "主程序未运行", "detail": "无法读取连接状态。", "tone": "neutral"},
                              input_path=input_path_offline(self.store.remote))

    # -- writes
    def commit_remote(self, target, plan, dry_run):
        if plan.changes:
            try:
                self.store.activate(target.id)
            except (OSError, ValueError) as exc:
                raise CliError(f"切换遥控器失败：{exc}", "检查配置后重试。", REJECTED)
        return plan_payload(plan)

    def commit_profile(self, remote, target, plan, dry_run):
        if plan.changes:
            self.store.settings["active_profile_id"] = target.id
            self._validated_commit()
        return plan_payload(plan)

    def commit_key(self, remote, profile, button, gesture, action, dry_run):
        plan = plan_key_action(remote, profile, button, gesture, action)
        if plan.changes:
            self._validated_commit()
        return plan_payload(plan)

    def commit_layer(self, remote, profile, button, gesture, action, app, name, dry_run):
        plan = plan_layer_action(remote, profile, button, gesture, action, app, name, clear=action is None)
        if plan.changes:
            self._validated_commit()
        return plan_payload(plan)

    def commit_import(self, remote, profile, path, replace, dry_run):
        plan, imported = plan_import(remote, remote.profiles, read_import(path), profile, replace=replace)
        self.store.settings["active_profile_id"] = imported.id
        self._validated_commit()
        return plan_payload(plan)

    def _validated_commit(self):
        try:
            self._commit()
        except ValueError as exc:
            raise CliError(f"改动没有通过校验：{exc}", "按提示修改动作后重试；用 actions kinds 查看字段。", REJECTED)


def after_load(session):
    """Test hook: runs right after an offline session has loaded the configuration."""


@contextmanager
def offline(directory, args):
    with OfflineSession(directory or default_directory(), write=is_write(args), dry_run=args.dry_run) as session:
        yield session
