"""Identify the keys of a remote by selecting their places in its photo."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QVBoxLayout, QWidget

from ..nine_key import BINDING_KEY_NAMES, BINDING_LABELS, BINDING_ROLES, BINDING_ROWS, missing_bindings
from ..theme import repolish
from .widgets import button, label, row


class NineKeyBindingEditor(QWidget):
    saved = Signal(dict)
    canceled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self.bindings = {}
        self.defaults = {}
        self.buttons = {}
        self.selected = ""
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        self.instruction = label("", "caption")
        box.addWidget(self.instruction)
        self.progress = label("", "caption")
        box.addWidget(self.progress)

        grid = QGridLayout()
        grid.setSpacing(4)
        self.slots = {}
        for row_index, roles in enumerate(BINDING_ROWS):
            for col, role in enumerate(roles):
                control = button("", lambda key=role: self.select_role(key), "nineKeySlot")
                control.setAutoDefault(False)
                control.setCheckable(True)
                control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                control.setMinimumHeight(37)
                self.slots[role] = control
                grid.addWidget(control, row_index, col)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        back_control = button("", lambda: self.select_role("back"), "nineKeySlot")
        back_control.setAutoDefault(False)
        back_control.setCheckable(True)
        back_control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        back_control.setMinimumHeight(37)
        self.slots["back"] = back_control
        grid.addWidget(back_control, len(BINDING_ROWS), 0, 1, 3)
        box.addLayout(grid)
        self.clear = button("清除所选", self.clear_role, "quiet")
        self.restore = button("恢复预设", self.restore_defaults, "quiet")
        box.addWidget(row(self.clear, self.restore, None, spacing=4))
        self.save = button("保存键位", lambda: self.saved.emit(dict(self.bindings)), "primary")
        box.addWidget(row(self.save, button("取消", self.canceled.emit), None, spacing=4))
        self.hide()

    def start(self, buttons, bindings, defaults):
        self.buttons = buttons
        all_roles = (*BINDING_ROLES, "back")
        self.bindings = {role: bindings[role] for role in all_roles if role in bindings}
        self.defaults = {role: defaults[role] for role in all_roles if role in defaults}
        self.select_next()
        self.active = True
        self.show()
        self.refresh()
        self.slots[self.selected or "1"].setFocus()

    def stop(self):
        self.active = False
        self.hide()

    def select_next(self):
        remaining = missing_bindings(self.bindings, BINDING_ROLES)
        self.selected = remaining[0] if remaining else ""

    def select_role(self, role):
        self.selected = role
        self.refresh()

    def pick(self, ident):
        if not self.active:
            return False
        if not self.selected:
            self.refresh()
            return True
        if ident not in self.buttons:
            self.refresh("图上的这个键还未学习或不可用。")
            return True
        role = self.selected
        previous = next((key for key, button_id in self.bindings.items()
                         if button_id == ident and key != role), None)
        if previous is not None:
            self.refresh(f"这个键已登记为「{BINDING_KEY_NAMES[previous]}」。"
                         f"请在左侧遥控器图上点「{BINDING_KEY_NAMES[role]}」键。")
            return True
        replacing = role in self.bindings
        self.bindings[role] = ident
        if replacing:
            self.selected = ""
        else:
            self.select_next()
        self.refresh()
        return True

    def clear_role(self):
        self.bindings.pop(self.selected, None)
        self.refresh()

    def restore_defaults(self):
        self.bindings = dict(self.defaults)
        self.select_next()
        self.refresh()

    def refresh(self, message=None):
        if message is None:
            message = (f"请在左侧遥控器图上点「{BINDING_KEY_NAMES[self.selected]}」键。"
                       if self.selected else "选择右侧要修改的键位，再点左侧遥控器图上的对应键。")
        self.instruction.setText(message)
        required_done = sum(1 for role in BINDING_ROLES if role in self.bindings)
        extra = " · 已设返回键" if "back" in self.bindings else ""
        self.progress.setText(f"已登记 {required_done} / {len(BINDING_ROLES)}{extra}")
        for role, control in self.slots.items():
            name = BINDING_LABELS[role]
            empty = role not in self.bindings
            if role == "back":
                status = "可选 · 未设" if empty else "已登记"
            else:
                status = "待登记" if empty else "已登记"
            control.setText(f"{name}\n{status}")
            control.setAccessibleName(f"{name}，{status}")
            if control.property("empty") != empty:
                control.setProperty("empty", empty)
                repolish(control)
            control.setChecked(role == self.selected)
        self.clear.setEnabled(self.selected in self.bindings)
        self.restore.setVisible(bool(self.defaults))
        self.save.setEnabled(not missing_bindings(self.bindings, BINDING_ROLES))
