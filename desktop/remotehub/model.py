"""Versioned, validated profiles. No OS calls while importing or editing."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import base64
import json
import math
from pathlib import Path
import re
import time
import uuid

from .nine_key import validate_bindings

KINDS = {"none": "无操作", "shortcut": "按键 / 组合键", "common": "常见操作", "hold": "按住 / 锁定修饰键", "app": "仅打开 App（旧配置）", "toggle_app": "打开 / 收起 App", "file": "打开文件", "minimize_window": "收起当前窗口", "close_window": "关闭当前窗口", "quit_app": "退出当前应用", "voice": "语音输入"}
KINDS["text"] = "输入文本"
KINDS["grid"] = "九宫格面板"
KINDS["nine_key"] = "九键输入"
KINDS["cheat_sheet"] = "键位速查浮层"
KINDS["macro"] = "多步动作链"
KINDS["cycle_profile"] = "循环切换方案"
KINDS["ui_element"] = "界面控件动作"
KINDS["app_menu"] = "应用菜单命令"
KINDS["mouse"] = "鼠标按键"
MOUSE_BUTTONS = ("left", "right", "middle")
MOUSE_ACTIONS = ("left", "left_double", "right", "middle")
MAX_TEXT_LENGTH = 10_000
MAX_APP_LAYERS = 32
MAX_EXTRA_TRIGGERS = 15
GRID_SIZE = 9
MAX_MACRO_STEPS = 8
MAX_MACRO_DELAY_MS = 10_000
DEFAULT_MACRO_DELAY_MS = 120
UI_ELEMENT_OPS = ("press", "focus")
UI_ROLE_RE = re.compile(r"^AX[A-Za-z]{2,40}$")
BLOCKED_UI_ROLES = {"AXSheet", "AXDialog", "AXMenu", "AXMenuItem", "AXMenuBar", "AXSecureTextField", "AXApplication", "AXWindow"}
SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_MENU_PATH_DEPTH = 6
FORBIDDEN_GRID_CELL_KINDS = {"grid", "cheat_sheet", "nine_key", "hold", "voice"}
FORBIDDEN_GRID_CELL_COMMON = {"app_switcher"}
FORBIDDEN_MACRO_STEP_KINDS = {"none", "macro", "grid", "cheat_sheet", "nine_key", "hold", "voice", "cycle_profile"}
FORBIDDEN_MACRO_STEP_COMMON = {"app_switcher"}
GESTURE_FIELDS = {"single": "action", "double": "double_action", "long": "long_action"}
SELF_APP_IDS = {
    "org.remotehub.desktop",
    "exe:10remotehub.exe",
    "exe:remotehub.exe",
    "exe:10remotehub",
    "exe:remotehub",
}
APP_LAYER_ID_RE = re.compile(r"^(?:exe:[a-z0-9][a-z0-9._ ()+-]{0,180}|[A-Za-z0-9][A-Za-z0-9._-]{1,199})$")
MODIFIERS = ("ctrl", "alt", "shift", "cmd")
SPECIAL_KEYS = {"enter", "esc", "tab", "space", "backspace", "delete", "insert", "home", "end", "pageup", "pagedown", "up", "down", "left", "right", "print_screen"}
KEYS = set("abcdefghijklmnopqrstuvwxyz0123456789-=[]\\;',./`") | SPECIAL_KEYS | {f"f{i}" for i in range(1, 25)}


def normalize_app_layer_id(app_id: str) -> str:
    if not isinstance(app_id, str):
        return ""
    raw = app_id.strip()
    if not raw:
        return ""
    if raw.casefold().startswith("exe:"):
        exe_name = raw[4:].strip().casefold()
        return f"exe:{exe_name}" if exe_name else ""
    return raw


def validate_app_layer_id(app_id: str) -> str:
    if not isinstance(app_id, str):
        raise ValueError("应用标识无效")
    if "/" in app_id or "\\" in app_id or "\x00" in app_id:
        raise ValueError("应用标识不能包含路径")
    norm = normalize_app_layer_id(app_id)
    rest = norm[4:] if norm.startswith("exe:") else norm
    if not norm or ":" in rest or rest in {".", ".."} or rest.endswith(".") or not APP_LAYER_ID_RE.match(norm):
        raise ValueError("应用标识格式无效")
    if norm.casefold() in SELF_APP_IDS:
        raise ValueError("不能为当前遥控器软件自身创建应用层")
    return norm


def validate_keys(keys: list[str], *, allow_modifier=False, platform=None):
    if not isinstance(keys, list) or not 1 <= len(keys) <= 5 or any(not isinstance(k, str) for k in keys):
        raise ValueError("请选择一个按键或组合键")
    if len(set(keys)) != len(keys) or any(k not in KEYS and k not in MODIFIERS for k in keys):
        raise ValueError("组合键包含重复或不支持的按键")
    main = [k for k in keys if k not in MODIFIERS]
    if len(main) != 1 and not (allow_modifier and not main):
        raise ValueError("组合键需要一个主键；语音触发允许单独修饰键")
    if platform == 'darwin' and any(k in {'f21', 'f22', 'f23', 'f24', 'print_screen'} for k in keys):
        raise ValueError("macOS 不支持这个按键，请选择 F1–F20 或其他按键；截图可使用「截图」动作")


@dataclass
class Action:
    kind: str = "none"
    keys: list[str] = field(default_factory=list)
    target: str = ""
    application: str = ""  # optional app for a file, or bundle id for ui_element/app_menu
    text: str = ""  # literal plain text; never interpreted as keys or a command
    cells: list[GridCell] = field(default_factory=list)
    steps: list[MacroStep] = field(default_factory=list)
    enter: bool = False  # optional trailing Return for kind == "text"
    spec: dict = field(default_factory=dict)  # structured configuration for actions needing it

    def validate(self, platform=None):
        if self.kind not in KINDS:
            raise ValueError("未知动作类型")
        if type(self.enter) is not bool:
            raise ValueError("自动回车设置无效")
        if self.enter and self.kind != "text":
            raise ValueError("只有输入文本支持自动回车")
        if not isinstance(self.spec, dict):
            raise ValueError("动作扩展配置格式无效")
        if self.kind not in ("ui_element", "app_menu", "nine_key") and self.spec:
            raise ValueError("当前动作类型包含多余配置")
        if self.kind == "grid":
            if not isinstance(self.target, str) or len(self.target) > 60 or "\x00" in self.target:
                raise ValueError("面板名称应为 0–60 字")
            if self.keys or self.application or self.text or self.steps:
                raise ValueError("九宫格动作包含多余字段")
            if not isinstance(self.cells, list) or len(self.cells) != GRID_SIZE:
                raise ValueError(f"九宫格面板必须包含 {GRID_SIZE} 个格位")
            for cell in self.cells:
                if not isinstance(cell, GridCell):
                    raise ValueError("九宫格格位格式无效")
                cell.validate(platform=platform)
            if all(cell.action.kind == "none" for cell in self.cells):
                raise ValueError("请至少为一个格位设置动作")
            return
        if self.kind == "macro":
            if not isinstance(self.target, str) or len(self.target) > 60 or "\x00" in self.target:
                raise ValueError("动作链名称应为 0–60 字")
            if self.keys or self.application or self.text or self.cells:
                raise ValueError("动作链包含多余字段")
            if not isinstance(self.steps, list) or not 1 <= len(self.steps) <= MAX_MACRO_STEPS:
                raise ValueError(f"动作链需包含 1–{MAX_MACRO_STEPS} 个步骤")
            for step in self.steps:
                if not isinstance(step, MacroStep):
                    raise ValueError("动作链步骤格式无效")
                step.validate(platform=platform)
            return
        if self.cells:
            raise ValueError("非九宫格动作不能包含格位数据")
        if self.steps:
            raise ValueError("非动作链不能包含步骤数据")
        if self.kind == "cycle_profile" and (self.keys or self.target or self.application or self.text):
            raise ValueError("切换配置方案动作包含多余字段")
        if self.kind == "nine_key":
            if self.keys or self.target or self.application or self.text:
                raise ValueError("九键输入动作包含多余字段")
            validate_bindings(self.spec)
        if self.kind == "ui_element":
            if self.keys or self.text:
                raise ValueError("界面控件动作包含多余字段")
            if self.target not in UI_ELEMENT_OPS:
                raise ValueError("界面控件操作方式无效")
            validate_app_layer_id(self.application)
            allowed = {"app_name", "role", "label", "signature"}
            if not set(self.spec).issubset(allowed) or {"role", "signature"} - set(self.spec):
                raise ValueError("界面控件定位信息不完整")
            role = self.spec.get("role", "")
            if not isinstance(role, str) or not UI_ROLE_RE.match(role) or role in BLOCKED_UI_ROLES:
                raise ValueError("不支持的界面控件类型")
            sig = self.spec.get("signature", "")
            if not isinstance(sig, str) or not SIGNATURE_RE.match(sig):
                raise ValueError("界面控件定位指纹无效")
            app_name = self.spec.get("app_name", "")
            elem_label = self.spec.get("label", "")
            if (not isinstance(app_name, str) or len(app_name) > 100 or "\x00" in app_name
                    or not isinstance(elem_label, str) or len(elem_label) > 80 or "\x00" in elem_label):
                raise ValueError("界面控件名称无效")
            return
        if self.kind == "app_menu":
            if self.keys or self.target or self.text:
                raise ValueError("应用菜单动作包含多余字段")
            if self.application:
                validate_app_layer_id(self.application)
            if not set(self.spec).issubset({"app_name", "path"}) or "path" not in self.spec:
                raise ValueError("应用菜单路径信息不完整")
            path = self.spec.get("path")
            if (not isinstance(path, list) or not 2 <= len(path) <= MAX_MENU_PATH_DEPTH
                    or any(not isinstance(part, str) or not 1 <= len(part.strip()) <= 80 or "\x00" in part for part in path)):
                raise ValueError("应用菜单路径无效")
            app_name = self.spec.get("app_name", "")
            if not isinstance(app_name, str) or len(app_name) > 100 or "\x00" in app_name:
                raise ValueError("应用名称无效")
            return
        if self.kind == "mouse":
            if self.keys or self.application or self.text:
                raise ValueError("鼠标动作包含多余字段")
            if self.target not in MOUSE_ACTIONS:
                raise ValueError("不支持的鼠标动作")
            return
        if self.kind == "shortcut":
            validate_keys(self.keys, platform=platform)
        if self.kind == "hold":
            if self.target not in ("hold", "latch"):
                raise ValueError("修饰键保持方式无效")
            if len(self.keys) != 1 or self.keys[0] not in MODIFIERS:
                raise ValueError("只能选择一个修饰键")
            validate_keys(self.keys, allow_modifier=True, platform=platform)
        if self.kind == "text":
            if not isinstance(self.text, str) or not 1 <= len(self.text) <= MAX_TEXT_LENGTH:
                raise ValueError("请输入 1–10000 个字符")
            if any((ord(c) < 32 and c not in "\n\r\t") or 0xD800 <= ord(c) <= 0xDFFF for c in self.text):
                raise ValueError("文本包含不支持的控制字符")
        if self.kind == "common":
            from .common_actions import COMMON_ACTIONS
            if not isinstance(self.target, str) or self.target not in COMMON_ACTIONS:
                raise ValueError("请选择常见操作")
        if self.kind in ("app", "toggle_app", "file") and (not isinstance(self.target, str) or not self.target or "\x00" in self.target):
            raise ValueError("请选择应用或文件")
        if not isinstance(self.application, str) or "\x00" in self.application:
            raise ValueError("打开方式无效")

    @classmethod
    def from_dict(cls, raw: dict) -> Action:
        if not isinstance(raw, dict):
            raise ValueError("动作配置必须是对象")
        values = dict(raw)
        if "cells" in values:
            raw_cells = values["cells"]
            if not isinstance(raw_cells, list):
                raise ValueError("九宫格格位列表格式无效")
            cells: list[GridCell] = []
            for item in raw_cells:
                if not isinstance(item, dict):
                    raise ValueError("九宫格格位配置必须是对象")
                cell_values = dict(item)
                cell_values["action"] = cls.from_dict(item.get("action", {}))
                cells.append(GridCell(**cell_values))
            values["cells"] = cells
        if "steps" in values:
            raw_steps = values["steps"]
            if not isinstance(raw_steps, list):
                raise ValueError("动作链步骤列表格式无效")
            steps: list[MacroStep] = []
            for item in raw_steps:
                if not isinstance(item, dict):
                    raise ValueError("动作链步骤配置必须是对象")
                step_values = dict(item)
                step_values["action"] = cls.from_dict(item.get("action", {}))
                steps.append(MacroStep(**step_values))
            values["steps"] = steps
        return cls(**values)


@dataclass
class GridCell:
    title: str = ""
    action: Action = field(default_factory=Action)

    def validate(self, platform=None):
        if not isinstance(self.title, str) or len(self.title) > 60 or "\x00" in self.title:
            raise ValueError("格位名称应为 0–60 字")
        if not isinstance(self.action, Action):
            raise ValueError("格位动作无效")
        if self.action.kind in FORBIDDEN_GRID_CELL_KINDS:
            raise ValueError("九宫格格位不支持此动作类型")
        if self.action.kind == "common" and self.action.target in FORBIDDEN_GRID_CELL_COMMON:
            raise ValueError("九宫格格位不支持此常见操作")
        self.action.validate(platform=platform)


@dataclass
class MacroStep:
    action: Action = field(default_factory=Action)
    delay_ms: int = DEFAULT_MACRO_DELAY_MS

    def validate(self, platform=None):
        if not isinstance(self.action, Action):
            raise ValueError("动作链步骤格式无效")
        if self.action.kind in FORBIDDEN_MACRO_STEP_KINDS:
            raise ValueError("动作链步骤不支持此动作类型")
        if self.action.kind == "common" and self.action.target in FORBIDDEN_MACRO_STEP_COMMON:
            raise ValueError("动作链步骤不支持此常见操作")
        if type(self.delay_ms) is not int or not 0 <= self.delay_ms <= MAX_MACRO_DELAY_MS:
            raise ValueError(f"步骤等待时间应为 0–{MAX_MACRO_DELAY_MS} 毫秒")
        self.action.validate(platform=platform)


@dataclass
class Button:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "新按钮"
    trigger: str = ""  # stable source id e.g. hid:7:82 / ev:103
    extra_triggers: list[str] = field(default_factory=list)
    action: Action = field(default_factory=Action)
    double_action: Action = field(default_factory=Action)
    long_action: Action = field(default_factory=Action)
    # Normalized image rect; portable, independent of window DPI/size.
    rect: list[float] = field(default_factory=lambda: [0.35, 0.4, 0.3, 0.12])
    # Optional canonical role (key_roles.KEY_ROLES) for navigation, keypad, or voice.
    role: str = ""

    @property
    def all_triggers(self) -> list[str]:
        seen: list[str] = []
        for code in (self.trigger, *self.extra_triggers):
            if code and code not in seen:
                seen.append(code)
        return seen

    def matches_trigger(self, trigger: str) -> bool:
        return bool(trigger) and (self.trigger == trigger or trigger in self.extra_triggers)

    @property
    def configurable(self):
        # Reserved identities in the fixed TV-remote template. These two
        # physical controls remain visible but are excluded by user request.
        return self.id not in ("tv-power", "tv-tv")


def profile_to_dict(profile: Profile) -> dict:
    """Serialize a Profile to JSON-ready dict, omitting empty optional fields for compatibility."""
    data = asdict(profile)
    for button in data.get("buttons", ()):
        if not button.get("extra_triggers"):
            button.pop("extra_triggers", None)
        if not button.get("role"):
            button.pop("role", None)
    return data


@dataclass
class AppLayer:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    app_id: str = ""
    name: str = ""
    enabled: bool = True
    overrides: dict[str, dict[str, Action]] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.app_id, str):
            self.app_id = normalize_app_layer_id(self.app_id)


def effective_button(button: Button, layer: AppLayer | None) -> Button:
    """Resolve effective per-gesture actions for button under optional AppLayer."""
    if layer is None or not button.configurable:
        return button
    gestures = layer.overrides.get(button.id)
    if not gestures:
        return button
    single_act = gestures.get("single", button.action)
    if single_act.kind == "mouse":
        return Button(
            id=button.id,
            name=button.name,
            trigger=button.trigger,
            extra_triggers=list(button.extra_triggers),
            action=single_act,
            double_action=Action(),
            long_action=Action(),
            rect=list(button.rect),
            role=button.role,
        )
    return Button(
        id=button.id,
        name=button.name,
        trigger=button.trigger,
        extra_triggers=list(button.extra_triggers),
        action=single_act,
        double_action=gestures.get("double", button.double_action),
        long_action=gestures.get("long", button.long_action),
        rect=list(button.rect),
        role=button.role,
    )


@dataclass
class Profile:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "新遥控器"
    vendor: int = 0x0416
    product: int = 0x0602
    serial: str = ""  # Legacy export field; runtime matching uses VID/PID only.
    image: str = ""  # base64 PNG, bounded, self-contained export
    buttons: list[Button] = field(default_factory=list)
    app_layers: list[AppLayer] = field(default_factory=list)
    version: int = 1

    def validate(self):
        if self.version != 1:
            raise ValueError("不支持的配置版本")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 100:
            raise ValueError("设备名称应为 1–100 字")
        if any(type(n) is not int or not 0 <= n <= 65535 for n in (self.vendor, self.product)):
            raise ValueError("设备 VID/PID 无效")
        if not isinstance(self.serial, str) or len(self.serial) > 256:
            raise ValueError("设备序列号无效")
        if len(self.buttons) > 128:
            raise ValueError("每个设备最多 128 个按钮")
        ids, triggers = set(), set()
        button_map: dict[str, Button] = {}
        for button in self.buttons:
            if not isinstance(button.id, str) or not button.id or button.id in ids:
                raise ValueError("按钮标识重复或无效")
            ids.add(button.id)
            button_map[button.id] = button
            if not isinstance(button.name, str) or not button.name.strip() or len(button.name) > 100:
                raise ValueError("按钮名称无效")
            if not isinstance(button.trigger, str) or len(button.trigger) > 200:
                raise ValueError("实体按键标识无效")
            if button.trigger:
                if button.trigger in triggers:
                    raise ValueError("同一实体键不能绑定两个按钮")
                triggers.add(button.trigger)
            if not isinstance(button.extra_triggers, list) or len(button.extra_triggers) > MAX_EXTRA_TRIGGERS:
                raise ValueError("附加实体按键标识无效")
            if button.extra_triggers and not button.trigger:
                raise ValueError("附加实体按键标识缺少主键码")
            for extra in button.extra_triggers:
                if not isinstance(extra, str) or not extra or len(extra) > 200:
                    raise ValueError("实体按键标识无效")
                if extra in triggers:
                    raise ValueError("同一实体键不能绑定两个按钮")
                triggers.add(extra)
            if len(button.rect) != 4 or any(type(x) not in (float, int) or not math.isfinite(x) for x in button.rect):
                raise ValueError("图片按钮区域无效")
            from .key_roles import KEY_ROLES
            if not isinstance(button.role, str) or (button.role and button.role not in KEY_ROLES):
                raise ValueError("按钮角色标识无效")
            x, y, w, h = button.rect
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1-x+1e-9 and 0 < h <= 1-y+1e-9):
                raise ValueError("按钮区域超出图片范围")
            for action in (button.action, button.double_action, button.long_action):
                action.validate()
            if any(action.kind == "voice" for action in (button.double_action, button.long_action)):
                raise ValueError("双击和长按不支持语音动作")
            if any(action.kind == "mouse" for action in (button.double_action, button.long_action)):
                raise ValueError("双击和长按不支持鼠标按键")
            if button.action.kind == "mouse":
                if button.action.target not in MOUSE_BUTTONS:
                    raise ValueError("实体按键只支持绑定鼠标左键、右键或中键")
                if button.double_action.kind != "none" or button.long_action.kind != "none":
                    raise ValueError("绑定鼠标按键后不能同时设置双击或长按")
        if not isinstance(self.app_layers, list) or len(self.app_layers) > MAX_APP_LAYERS:
            raise ValueError(f"每个设备最多 {MAX_APP_LAYERS} 个应用层")
        if self.app_layers:
            from .photo_template import is_voice_button
            layer_ids, layer_apps = set(), set()
            for layer in self.app_layers:
                if not isinstance(layer, AppLayer):
                    raise ValueError("应用层格式无效")
                if not isinstance(layer.id, str) or not layer.id or len(layer.id) > 100 or layer.id in layer_ids:
                    raise ValueError("应用层标识重复或无效")
                layer_ids.add(layer.id)
                norm_app = validate_app_layer_id(layer.app_id)
                app_key = norm_app.casefold()
                if app_key in layer_apps:
                    raise ValueError("同一应用不能重复创建应用层")
                layer_apps.add(app_key)
                if not isinstance(layer.name, str) or not layer.name.strip() or len(layer.name) > 100:
                    raise ValueError("应用层名称应为 1–100 字")
                if type(layer.enabled) is not bool:
                    raise ValueError("应用层启用状态无效")
                if not isinstance(layer.overrides, dict):
                    raise ValueError("应用层按键覆盖无效")
                for button_id, gestures in layer.overrides.items():
                    btn = button_map.get(button_id)
                    if btn is None:
                        raise ValueError("应用层引用了不存在的按钮")
                    if not btn.configurable or is_voice_button(self, btn):
                        raise ValueError("固定按键不支持应用层覆盖")
                    if not isinstance(gestures, dict) or not gestures:
                        raise ValueError("应用层按键覆盖不能为空")
                    for gesture, action in gestures.items():
                        if gesture not in GESTURE_FIELDS or not isinstance(action, Action):
                            raise ValueError("应用层手势动作无效")
                        action.validate()
                        if action.kind == "voice" and gesture != "single":
                            raise ValueError("双击和长按不支持语音动作")
                        if action.kind == "mouse":
                            if gesture != "single":
                                raise ValueError("双击和长按不支持鼠标按键")
                            if action.target not in MOUSE_BUTTONS:
                                raise ValueError("实体按键只支持绑定鼠标左键、右键或中键")
                    if "single" in gestures and gestures["single"].kind == "mouse":
                        if any(gestures.get(g) is not None and gestures[g].kind != "none" for g in ("double", "long")):
                            raise ValueError("绑定鼠标按键后不能同时设置双击或长按")
        if not isinstance(self.image, str) or len(self.image) > 12_000_000:
            raise ValueError("设备图片过大")
        if self.image:
            raw = base64.b64decode(self.image, validate=True)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("设备图片应为 PNG")

    def dumps(self):
        self.validate()
        return json.dumps(profile_to_dict(self), ensure_ascii=False, indent=2)

    @classmethod
    def loads(cls, text: str):
        if len(text) > 14_000_000:
            raise ValueError("配置文件过大")
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("配置必须是对象")
            buttons = []
            for raw in data.get("buttons", []):
                values = dict(raw)
                for name in ("action", "double_action", "long_action"):
                    values[name] = Action.from_dict(raw.get(name, {}))
                buttons.append(Button(**values))
            data["buttons"] = buttons
            raw_layers = data.get("app_layers", [])
            if not isinstance(raw_layers, list):
                raise ValueError("应用层列表格式无效")
            app_layers = []
            for raw_layer in raw_layers:
                if not isinstance(raw_layer, dict):
                    raise ValueError("应用层配置格式无效")
                layer_values = dict(raw_layer)
                raw_overrides = raw_layer.get("overrides", {})
                if not isinstance(raw_overrides, dict):
                    raise ValueError("应用层按键覆盖格式无效")
                overrides: dict[str, dict[str, Action]] = {}
                for button_id, gestures in raw_overrides.items():
                    if not isinstance(button_id, str) or not isinstance(gestures, dict):
                        raise ValueError("应用层按键覆盖格式无效")
                    parsed_gestures: dict[str, Action] = {}
                    for gesture, raw_action in gestures.items():
                        if not isinstance(gesture, str) or not isinstance(raw_action, dict):
                            raise ValueError("应用层动作格式无效")
                        parsed_gestures[gesture] = Action.from_dict(raw_action)
                    overrides[button_id] = parsed_gestures
                layer_values["overrides"] = overrides
                app_layers.append(AppLayer(**layer_values))
            data["app_layers"] = app_layers
            profile = cls(**data)
            profile.validate()
            return profile
        except (TypeError, KeyError, AttributeError) as exc:
            raise ValueError("配置结构不正确") from exc


REPLACE_ATTEMPTS = 10


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        tmp.write_text(text, encoding="utf-8")
        # Windows refuses to replace a file another handle has open (the agent CLI or
        # the app reading it); such a hold lasts milliseconds, so try again briefly.
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if tmp.exists():
            tmp.unlink()


def default_profiles():
    # Geometry and physical signal table are fixed for the user's TV remote.
    from .photo_template import television_profile
    return [television_profile()]
