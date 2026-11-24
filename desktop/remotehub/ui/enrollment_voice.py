"""Trigger-only voice check: pick the remote key that starts dictation, then confirm it worked.

The microphone itself is chosen in exactly one place, step 1「语音来源」 of the voice page.
This card only appears for 「仅触发识别」, where the recognition software keeps its own
microphone and the remote only sends the recognition shortcut.
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout

from ..enrollment import ai_request, diagnostic_text
from .widgets import ScrollSafeComboBox, button, label, row


class VoiceEnrollment(QFrame):
    def __init__(self, controller, current_remote, ready_device, show_mapping):
        super().__init__()
        self.c = controller
        self.current_remote = current_remote
        self.ready_device = ready_device
        self.trial_started = None
        self.trial_finished = None
        self.setObjectName("deviceCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addWidget(label("试用仅触发识别", "section"))
        self.key_picker = ScrollSafeComboBox()
        self.key_picker.setAccessibleName("触发听写的遥控器键")
        self.key_picker.activated.connect(self.choose_key)
        self.key_picker_row = row(label("触发听写的遥控器键", wrap=False), self.key_picker, None)
        layout.addWidget(self.key_picker_row)
        self.status = label("", "muted")
        layout.addWidget(self.status)
        self.troubleshoot = label(
            "若仍未出字，按顺序核对：1. 识别快捷键与听写软件一致；2. 听写软件里选对了麦克风；3. 光标已在可输入窗口且非管理员窗口。",
            "caption",
        )
        self.troubleshoot.hide()
        layout.addWidget(self.troubleshoot)
        self.mapping = button("回到按键映射", show_mapping)
        self.confirm = button("确认已听写成功", self.verify, "primary")
        self.copy = button("复制接入诊断", self.copy_diagnostics, "link")
        self.copy_ai = button("复制给 AI 的求助", self.copy_ai_request, "link")
        self.copy_ai.setToolTip("复制当前语音边界说明、以往做法和脱敏诊断，粘贴给任意 AI 助手协助排查。")
        for control in (self.mapping, self.confirm, self.copy, self.copy_ai):
            control.setAutoDefault(False)
        layout.addWidget(row(self.mapping, self.confirm, None, self.copy, self.copy_ai))
        self.c.voice_changed.connect(self.refresh)
        self.c.voice.changed.connect(self.observe_trigger)
        self.c.voice.failed.connect(self.cancel_trial)
        self.c.voice.expired.connect(self.cancel_trial)
        self.c.profiles_changed.connect(self.refresh)
        self.c.mapping_changed.connect(self.refresh)
        self.c.remotes_changed.connect(self.refresh)
        self.refresh()

    def applies(self):
        return self.current_remote() is not None and self.c.voice_settings()["voice_source"] == "trigger"

    def signature(self):
        settings = self.c.voice_settings()
        return (self.c.profile.id, *(
            settings[name] for name in ("voice_source", "voice_keys", "voice_mode", "record_mode")
        ))

    def observe_trigger(self):
        session = self.c.voice
        if session.owner == "trigger" and session.recognition_started and self.c.voice_trigger:
            self.trial_started = self.signature(), self.c.voice_trigger
        self.refresh()

    def observe_key(self, trigger, down):
        signature = self.signature()
        toggle = self.c.voice_settings()["record_mode"] == "toggle"
        if self.trial_started == (signature, trigger) and self.c.voice.owner is None and (not down or toggle):
            self.trial_finished = signature
            self.trial_started = None
            self.refresh()

    def cancel_trial(self, *_):
        self.trial_started = self.trial_finished = None
        self.refresh()

    def evidence(self):
        if not self.applies():
            return "voice_not_trigger", ""
        signature = self.signature()
        if self.c.settings.get("guide_verified_voice") == list(signature):
            return "dictation_confirmed", "你已确认这套设置能听写。更换触发键或快捷键后可重新测试。"
        if self.trial_finished == signature:
            return "shortcut_sent", "已观察到遥控器触发识别快捷键。请核对识别软件是否真的写出文字。"
        return ("voice_waiting_for_key",
                "指定触发键（或到按键映射分配「语音输入」），在下方设置识别快捷键。"
                "打开识别软件，用遥控器开始听写，说一句后结束。")

    def choose_key(self, _index):
        button_id = self.key_picker.currentData()
        if button_id:
            self.c.assign_voice_trigger_button(button_id)

    def refresh(self):
        visible = self.applies()
        self.setVisible(visible)
        show_key_picker = visible and not self.c.fixed_model
        self.key_picker.blockSignals(True)
        self.key_picker.clear()
        self.key_picker.addItem("选择遥控器上的触发键…", "")
        selected_id = ""
        if show_key_picker:
            for item in self.c.configurable_buttons():
                if not item.trigger:
                    continue
                label_text = f"{item.name}（语音区）" if getattr(item, "role", "") == "voice" else item.name
                self.key_picker.addItem(label_text, item.id)
                if not selected_id and item.action.kind == "voice":
                    selected_id = item.id
            self.key_picker.setCurrentIndex(max(0, self.key_picker.findData(selected_id)))
        self.key_picker.blockSignals(False)
        self.key_picker_row.setVisible(show_key_picker)
        self.key_picker.setEnabled(show_key_picker and self.c.voice.owner is None and self.key_picker.count() > 1)
        state, message = self.evidence()
        self.status.setText(message)
        self.troubleshoot.setVisible(state == "shortcut_sent")
        self.confirm.setEnabled(state == "shortcut_sent")

    def verify(self):
        if self.evidence()[0] == "shortcut_sent":
            self.c.update_voice(guide_verified_voice=list(self.signature()))

    def _diagnostics(self):
        remote = self.current_remote()
        device = self.ready_device()
        state, _ = self.evidence()
        text = diagnostic_text(
            step=state, device=device, details={
                "mapping_saved": remote is not None,
                "input_connected": device is not None,
                "voice_source": self.c.voice_settings()["voice_source"],
                "remote_mic_capture_available": self.c.supports_remote_mic,
                "dictation_confirmed_by_user": state == "dictation_confirmed",
            },
        )
        return text, state, device

    def copy_diagnostics(self):
        text, _, _ = self._diagnostics()
        QApplication.clipboard().setText(text)
        self.copy.setText("接入诊断已复制")

    def copy_ai_request(self):
        text, state, device = self._diagnostics()
        # A marked voice key without a built-in protocol: the handset has a microphone we cannot read.
        private = not self.c.supports_remote_mic and self.c.has_marked_voice_button()
        QApplication.clipboard().setText(ai_request(
            step=state, case="voice_private" if private else "", device=device, diagnostics=text))
        self.copy_ai.setText("已复制，可粘贴给 AI")
