"""All voice settings, audio components and diagnostics; fields save automatically."""
from __future__ import annotations

import base64
import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..branding import BRAND
from ..presentation import key_label
from ..protocols import MIC_NONE, mic_text
from .widgets import Pill, ScrollSafeComboBox, ScrollSafeDoubleSpinBox, VoiceKeyRecorder, button, hbox, label, row


def offer_remote_source(combo, available):
    """The remote microphone exists only on connections that carry audio."""
    index = combo.findData("remote")
    if index >= 0:
        combo.model().item(index).setEnabled(available)
        combo.view().setRowHidden(index, not available)

SOURCES = (("disabled", "未启用"), ("remote", "遥控器麦克风"), ("trigger", "仅触发识别（使用识别软件当前麦克风）"))
RECORD_MODES = (("hold", "按住说话，松开结束"), ("toggle", "按一下开始，再按一下结束"))
INPUT_ROUTING = (("automatic", "录音时切换系统默认麦克风"), ("fixed", "固定使用虚拟麦克风"))
SEND_MODES = (("hold", "按住快捷键录音，松开结束"), ("toggle", "快捷键按一次开始，再按一次结束"),
              ("manual", "不发送快捷键（手动控制识别软件）"))


def virtual_input_name():
    """The microphone the recognizer should read on this platform."""
    return "CABLE Output" if sys.platform == "win32" else BRAND["microphone_name"]


def input_route_hint(mode, *, compact=False):
    if mode == "automatic":
        if compact:
            return "按语音键时切换到虚拟麦克风，录音结束后恢复。识别软件请选择「系统默认」。"
        return (f"按语音键开始录音时，把系统默认麦克风切换到「{virtual_input_name()}」；录音结束后恢复原来的麦克风。\n"
                "请自行到语音识别软件中，将输入设备设为「系统默认」或「跟随系统」，不要固定选择虚拟麦克风或其他设备。")
    if compact:
        return f"请在识别软件中固定选择「{virtual_input_name()}」。只在录音时传声，不切换系统默认输入。"
    return (f"请自行到语音识别软件中，将输入设备固定选择为「{virtual_input_name()}」。\n"
            "按语音键开始收音，录音结束时停止；不会更改系统默认输入设备，也不会替你修改识别软件的设置。")


def step_card(number, title, caption=""):
    card = QWidget()
    card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    card.setObjectName("panel")
    card.setProperty("settingsCard", True)
    card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    box = QVBoxLayout(card)
    box.setContentsMargins(22, 18, 22, 20)
    box.setSpacing(12)
    head = QHBoxLayout()
    head.setSpacing(10)
    if number is not None:
        badge = QLabel(str(number))
        badge.setObjectName("stepNumber")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(badge)
    titles = QVBoxLayout()
    titles.setSpacing(1)
    titles.addWidget(label(title, "section"))
    if caption:
        titles.addWidget(label(caption, "caption"))
    head.addLayout(titles, 1)
    box.addLayout(head)
    return card, box, head


class SettingsForm(QFormLayout):
    """Same label column and field alignment on every desktop platform."""

    def addRow(self, title, field):
        caption = label(title, wrap=False)
        caption.setMinimumWidth(96)
        if isinstance(field, QWidget):
            caption.setBuddy(field)
        super().addRow(caption, field)


def form():
    layout = SettingsForm()
    layout.setVerticalSpacing(10)
    layout.setHorizontalSpacing(14)
    layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return layout


def combo(items, accessible):
    widget = ScrollSafeComboBox()
    widget.setAccessibleName(accessible)
    widget.setSizeAdjustPolicy(ScrollSafeComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    widget.setMinimumContentsLength(8)
    for key, title in items:
        widget.addItem(title, key)
    return widget


def column_page(page):
    page.setProperty("settingsPage", True)
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 4, 0, 0)
    column = QWidget()
    # Preserve the column's height-for-width inside the scrolling page.
    # Shrinking it to its minimum hint clips wrapped labels and button rows.
    column.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    steps = QVBoxLayout(column)
    steps.setContentsMargins(0, 0, 0, 0)
    steps.setSpacing(14)
    outer.addWidget(column)
    outer.addStretch(1)
    return steps


class VoiceKeyEditor(QWidget):
    """Compact controls for the reserved physical voice key, not an action picker."""

    microphone_setup_requested = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        box.addWidget(label("这个键固定控制语音开始和结束，单次最长 60 秒，不能分配其他动作。", "caption"))
        box.addWidget(label("麦克风", "section"))
        # Chosen only in the voice page's step 1 「语音来源」; shown here for reference.
        self.source_summary = label("", "value")
        box.addWidget(self.source_summary)
        self.route = label("", "caption")
        box.addWidget(self.route)
        self.microphone_setup = button("前往安装虚拟麦克风", self.microphone_setup_requested.emit)
        box.addWidget(self.microphone_setup)
        self.controls = QWidget()
        box.addWidget(self.controls)
        box = QVBoxLayout(self.controls)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        box.addWidget(label("识别软件的唤起键", "section"))
        self.shortcut = VoiceKeyRecorder()
        self.shortcut.recorded.connect(lambda keys: self.c.update_voice(voice_keys="+".join(keys)))
        box.addWidget(self.shortcut)
        box.addWidget(label("点击后按下识别软件使用的快捷键，松开保存。", "caption"))
        box.addWidget(label("怎么说话", "section"))
        self.record_mode = combo(RECORD_MODES, "语音键说话方式")
        self.record_mode.activated.connect(lambda: self.c.update_voice(record_mode=self.record_mode.currentData()))
        box.addWidget(self.record_mode)
        controller.voice_changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        s = self.c.voice_settings()
        self.source_summary.setText(dict(SOURCES).get(s["voice_source"], "未启用") + "（在「语音设置」第 1 步修改）")
        self.record_mode.setCurrentIndex(self.record_mode.findData(s["record_mode"]))
        self.shortcut.set_keys(s["voice_keys"].split("+"))
        enabled = self.c.voice_enabled
        self.controls.setVisible(enabled)
        remote = s["voice_source"] == "remote"
        missing = remote and not self.c.voice.output_id
        self.microphone_setup.setVisible(missing)
        installed = self.c.microphone_installed
        if sys.platform.startswith("linux"):
            setup_label = "前往启用虚拟麦克风" if installed else "前往查看音频环境"
        elif sys.platform == "win32":
            setup_label = "前往完成麦克风设置" if installed else "前往下载 VB-CABLE"
        else:
            setup_label = "前往完成麦克风设置" if installed else "前往安装虚拟麦克风"
        self.microphone_setup.setText(setup_label)
        if not enabled:
            self.route.setText("在「语音设置」第 1 步选择麦克风后启用。")
        elif missing:
            state = self.c.microphone_view()
            self.route.setText(f"虚拟麦克风{state.title}。{state.detail}")
        elif remote:
            self.route.setText(input_route_hint(s["input_routing"], compact=True))
        else:
            self.route.setText("使用识别软件当前选定的麦克风，不需要虚拟麦克风。")
        busy = self.c.voice.owner is not None or self.c.recording_requested
        for widget in (self.shortcut, self.record_mode):
            widget.setEnabled(not busy)


class VoicePage(QWidget):
    title = "语音设置"
    subtitle = "集中设置麦克风、语音唤起方式、音频组件与输入电平检测。"

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.loading = False
        self.setObjectName("page")
        steps = column_page(self)
        self.steps = steps

        card, box, _ = step_card(1, "语音来源", "声音从哪里来")
        self.source = combo(SOURCES, "语音来源")
        self.source.activated.connect(self.source_chosen)
        fields = form()
        fields.addRow("来源", self.source)
        box.addLayout(fields)
        self.route = label("", "caption")
        box.addWidget(self.route)
        self.copy_voice_diag = button("复制给 AI 的求助", self._copy_voice_diagnostic, "link")
        self.copy_voice_diag.setAutoDefault(False)
        self.copy_voice_diag.hide()
        box.addWidget(row(self.copy_voice_diag, None))
        self.routing_fields = form()
        self.input_routing = combo(INPUT_ROUTING, "遥控器麦克风连接方式")
        self.input_routing.activated.connect(lambda: self.c.update_voice(
            input_routing=self.input_routing.currentData()))
        self.routing_fields.addRow("连接方式", self.input_routing)
        box.addLayout(self.routing_fields)
        self.routing_hint = label("", "caption")
        box.addWidget(self.routing_hint)
        steps.addWidget(card)

        card, box, _ = step_card(2, "遥控器操作", "怎么用「语音输入」键")
        self.record_card = card
        self.record_mode = combo(RECORD_MODES, "遥控器录音操作")
        self.record_mode.activated.connect(lambda: self.c.update_voice(record_mode=self.record_mode.currentData()))
        fields = form()
        fields.addRow("操作方式", self.record_mode)
        box.addLayout(fields)
        steps.addWidget(card)

        card, box, _ = step_card(3, "识别软件触发", "让识别软件开始和结束听写")
        self.recognition_card = card
        fields = form()
        self.shortcut = VoiceKeyRecorder()
        self.shortcut.recorded.connect(lambda keys: self.c.update_voice(voice_keys="+".join(keys)))
        fields.addRow("识别快捷键", self.shortcut)
        self.mode = combo(SEND_MODES, "识别软件的快捷键方式")
        self.mode.activated.connect(lambda: self.c.update_voice(voice_mode=self.mode.currentData()))
        fields.addRow("快捷键方式", self.mode)
        box.addLayout(fields)
        box.addWidget(label("点击按键框，按下识别软件使用的快捷键，松开保存。录音结束后由识别软件输入文字。", "caption"))
        self.flow_summary = label("", "caption")
        box.addWidget(self.flow_summary)
        steps.addWidget(card)

        # -- remote audio --------------------------------------------------
        self.audio_card, box, head = step_card(4, "遥控器音频连接", "仅在语音来源为遥控器麦克风时需要")
        self.usb_pill = Pill()
        head.addWidget(self.usb_pill, 0, Qt.AlignmentFlag.AlignTop)
        self.audio_fields = form()
        self.microphone_status = label("", "value", wrap=False)
        self.microphone_status.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        default_setup_label = (
            "前往下载 VB-CABLE"
            if sys.platform == "win32"
            else "启用虚拟麦克风"
            if sys.platform.startswith("linux")
            else "安装虚拟麦克风"
        )
        self.microphone_setup = button(
            default_setup_label,
            self.open_vbcable_page if sys.platform == "win32" else self.c.setup_virtual_mic,
        )
        microphone_row = hbox(self.microphone_status)
        microphone_row.addStretch(1)
        microphone_row.addWidget(self.microphone_setup)
        microphone_row.addWidget(button("重新检测", self.c.check_microphone))
        self.audio_fields.addRow("虚拟麦克风", microphone_row)
        self.microphone_detail = label("", "caption")
        self.audio_fields.addRow("", self.microphone_detail)
        self.microphone_help = label("", "caption")
        self.audio_fields.addRow("", self.microphone_help)
        self.output_device = ScrollSafeComboBox()
        self.output_device.setAccessibleName("虚拟声卡播放端")
        self.output_device.setSizeAdjustPolicy(ScrollSafeComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.output_device.setMinimumContentsLength(8)
        self.output_device.activated.connect(self._output_device_chosen)
        self.audio_fields.addRow("播放设备", self.output_device)
        self.gain = ScrollSafeDoubleSpinBox()
        self.gain.setRange(0, 24)
        self.gain.setSuffix(" dB")
        self.gain.setAccessibleName("语音增益")
        self.gain_timer = QTimer(self)
        self.gain_timer.setSingleShot(True)
        self.gain_timer.setInterval(400)
        self.gain_timer.timeout.connect(lambda: self.c.update_voice(gain=self.gain.value()))
        self.gain.valueChanged.connect(lambda: None if self.loading else self.gain_timer.start())
        self.audio_fields.addRow("语音增益", self.gain)
        box.addLayout(self.audio_fields)
        self.usb_start = button("连接遥控器音频", self.start_usb, "primary")
        self.usb_stop = button("断开音频连接", self.c.stop_usb)
        box.addWidget(row(self.usb_start, self.usb_stop, None))
        self.usb_detail = label("", "caption")
        box.addWidget(self.usb_detail)
        steps.addWidget(self.audio_card)

        # -- audio test ----------------------------------------------------
        self.audio_test, box, head = step_card(5, "音频检测", "直接按住遥控器语音键，观察实时电平")
        self.voice_pill = Pill()
        head.addWidget(self.voice_pill, 0, Qt.AlignmentFlag.AlignTop)
        self.meter = QProgressBar()
        self.meter.setObjectName("audioLevel")
        self.meter.setRange(0, 100)
        self.meter.setValue(0)
        self.meter.setTextVisible(False)
        self.meter.setAccessibleName("遥控器音频输入电平")
        self.meter.setAccessibleDescription("随音频变化的电平指示，不是音量调节滑块")
        self.meter.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.meter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.meter_value = label("0%", "value", wrap=False)
        self.meter_value.setMinimumWidth(44)
        self.meter_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fields = form()
        meter_row = hbox(self.meter, self.meter_value, spacing=12)
        meter_row.setStretch(0, 1)
        fields.addRow("实时输入电平", meter_row)
        box.addLayout(fields)
        self.meter_hint = label("", "caption")
        box.addWidget(self.meter_hint)
        self.wav_test = button("高级：选择 WAV 文件测试", self.play_wav, "quiet")
        self.stop = button("停止测试 / 紧急结束录音", self.c.stop_voice)
        box.addWidget(row(self.wav_test, self.stop, None))
        box.addWidget(label("电平有变化不代表识别软件已经收到声音；请在识别软件里再确认一次。", "caption"))
        steps.addWidget(self.audio_test)

        controller.level.connect(self.update_level)
        controller.voice_changed.connect(self.refresh)
        controller.device_changed.connect(self.refresh)
        self.refresh()

    def _output_device_chosen(self, index: int):
        if self.loading or index < 0:
            return
        encoded = self.output_device.itemData(index) or ""
        try:
            ident = base64.b64decode(encoded) if encoded else b""
        except Exception:
            ident = b""
        if ident:
            self.c.set_output(ident)

    def refresh(self):
        self.loading = True
        c = self.c
        s = c.voice_settings()
        remote = s["voice_source"] == "remote"
        offer_remote_source(self.source, c.supports_remote_mic)
        self.source.setCurrentIndex(max(0, self.source.findData(s["voice_source"])))
        self.record_mode.setCurrentIndex(max(0, self.record_mode.findData(s["record_mode"])))
        self.mode.setCurrentIndex(max(0, self.mode.findData(s["voice_mode"])))
        self.input_routing.setCurrentIndex(self.input_routing.findData(s["input_routing"]))
        supports_routing = sys.platform in ("darwin", "win32") or sys.platform.startswith("linux")
        self.routing_fields.setRowVisible(self.input_routing, remote and supports_routing)
        self.routing_hint.setVisible(remote)
        self.routing_hint.setText(input_route_hint(s["input_routing"] if supports_routing else "fixed"))
        # Trigger-only needs the shortcut; "manual" is valid only when remote audio is used on a non-fixed remote.
        manual = self.mode.findData("manual")
        if manual >= 0:
            allow_manual = bool(remote and not c.receiver_voice_key)
            self.mode.model().item(manual).setEnabled(allow_manual)
            self.mode.view().setRowHidden(manual, not allow_manual)
        self.shortcut.set_keys(s["voice_keys"].split("+"))
        chord = key_label(s["voice_keys"].split("+"))
        start = "按住遥控器语音键" if s["record_mode"] == "hold" else "按一次遥控器语音键开始"
        trigger = ("不发送识别快捷键" if s["voice_mode"] == "manual" else
                   f"电脑按住 {chord}" if s["voice_mode"] == "hold" else f"电脑发送一次 {chord}")
        end = "松开遥控器后结束" if s["record_mode"] == "hold" else "再按一次遥控器结束"
        self.flow_summary.setText(f"操作顺序：{start} → {trigger}；{end}。单次最长 60 秒，到点自动结束。")
        if not self.gain_timer.isActive():
            self.gain.setValue(s["gain"])
        self.record_card.setVisible(c.voice_enabled)
        self.recognition_card.setVisible(c.voice_enabled)
        proto_info = c.ble_voice_protocol_info() if c.uses_ble_remote_mic() else {}
        recognized = bool(proto_info.get("protocol_label"))
        # A saved remote does not record whether Bluetooth goes through a dongle, so the page
        # only tells "usable" from "not yet"; the dongle route is offered while learning keys.
        unavailable = "" if c.supports_remote_mic else mic_text(MIC_NONE, sys.platform)
        self.route.setText(
            ("这只遥控器的麦克风可以直接用：选择「遥控器麦克风」即可用遥控器收音。"
             if recognized else
             "选择麦克风后启用语音输入。" if c.supports_remote_mic else
             f"选择麦克风后启用语音输入。{unavailable}")
            if not c.voice_enabled else
            ("按住遥控器实体语音键直接收音（照片上标记的语音键会同步亮起）。"
             if recognized else
             "使用遥控器收音，首次使用请先完成下方设置。") if remote else
            ("不需要虚拟麦克风，也不读取电脑麦克风；只向识别软件发送快捷键，识别软件继续使用当前麦克风。"
             if c.supports_remote_mic else
             f"不需要虚拟麦克风，只向识别软件发送快捷键，由识别软件使用电脑麦克风。{unavailable}")
        )
        self.copy_voice_diag.setVisible(not c.supports_remote_mic)
        self.copy_voice_diag.setText("复制给 AI 的求助")
        busy = c.voice.owner is not None or c.recording_requested
        for widget in (self.source, self.record_mode, self.mode, self.gain, self.input_routing, self.output_device):
            widget.setEnabled(not busy)
        self.shortcut.setEnabled(not busy and s["voice_mode"] != "manual")
        remote = c.remote_audio
        self.audio_card.setVisible(remote)
        self.audio_test.setVisible(remote)

        show_output_combo = bool(remote and ((sys.platform == "win32" and len(c.outputs) >= 1) or len(c.outputs) > 1))
        self.output_device.blockSignals(True)
        self.output_device.clear()
        selected_idx = 0
        for idx, (desc, ident) in enumerate(c.outputs):
            self.output_device.addItem(desc, base64.b64encode(ident).decode())
            if ident == c.voice.output_id:
                selected_idx = idx
        if self.output_device.count() > 0:
            self.output_device.setCurrentIndex(selected_idx)
        self.output_device.blockSignals(False)
        self.audio_fields.setRowVisible(self.output_device, show_output_combo)

        ready = bool(c.voice.output_id)
        microphone = c.microphone_view()
        self.microphone_status.setText(microphone.title)
        self.microphone_detail.setText(microphone.detail)
        self.microphone_setup.setVisible(not ready)
        if sys.platform.startswith("linux"):
            self.microphone_setup.setText("启用虚拟麦克风")
        elif sys.platform == "win32":
            self.microphone_setup.setText("完成设置" if c.microphone_installed else "前往下载 VB-CABLE")
        else:
            self.microphone_setup.setText("重新打开 App" if c.microphone_needs_relaunch
                                          else "完成设置" if c.microphone_installed else "安装虚拟麦克风")
        self.microphone_setup.setEnabled(not busy and (not sys.platform.startswith("linux") or c.microphone_installed))
        needs_win_guide = (sys.platform == "win32" and c.remote_audio and not c.microphone_installed)
        needs_linux_guide = (sys.platform.startswith("linux") and c.remote_audio and not ready)
        self.microphone_help.setVisible(needs_win_guide or needs_linux_guide)
        if needs_win_guide:
            self.microphone_help.setText(
                "Windows 的虚拟麦克风是内核驱动，必须微软签名，本程序不自带，用免费的 VB-CABLE。\n"
                "1. 点右上「前往下载 VB-CABLE」，解压后右键 VBCABLE_Setup_x64.exe 选「以管理员身份运行」。\n"
                "2. 点 Install Driver，完成后重启电脑，回来点「重新检测」。\n"
                "3. 把上面「连接方式」设为「录音时切换系统默认麦克风」，识别软件的麦克风保持「系统默认」。"
            )
        elif needs_linux_guide:
            self.microphone_help.setText(
                f"Linux 通过 PipeWire 或 PulseAudio 在当前用户会话中创建「{BRAND['microphone_name']}」，无需管理员密码或重启。\n"
                "点击右上「启用虚拟麦克风」即可创建，退出应用时会自动清理。"
            )
        else:
            self.microphone_help.setText("")
        unified = c.usb.collector is not None or c.uses_ble_remote_mic()
        self.usb_start.setVisible(not unified)
        self.usb_stop.setVisible(not unified)
        usb = c.usb_view()
        self.usb_pill.show_status(usb)
        self.usb_detail.setText(usb.detail)
        linked = c.usb_connecting or c.usb.server.isListening() or c.usb.authenticated or c.usb.ready or c.ble_voice_ready()
        self.usb_start.setEnabled(not linked)
        self.usb_stop.setEnabled(linked)
        self.voice_pill.show_status(c.voice_view())
        busy = c.voice.owner is not None or c.recording_requested
        self.stop.setEnabled(busy)
        self.wav_test.setEnabled(ready and not busy)
        self.wav_test.setToolTip("选择 WAV 录音并发送到虚拟麦克风" if ready else "请先准备上方虚拟麦克风")
        audio_ready = c.usb.ready or c.ble_voice_ready()
        if busy:
            hint = "电平随收到的音频变化，无需点击。语音大小可在上方调整增益。"
        elif not ready:
            hint = "暂无音频。请先准备上方虚拟麦克风，再连接遥控器音频或选择录音文件测试。"
        elif not audio_ready:
            hint = ("暂无音频。遥控器连接后自动就绪，按语音键即可开始说话。" if unified else
                    "暂无音频。连接遥控器音频后按语音键说话，或选择录音文件测试。")
        else:
            hint = "等待声音。按遥控器语音键说话，电平会随声音变化，无需点击。"
        self.meter_hint.setText(hint)
        if not busy:
            self.update_level(0)

        self.loading = False

    def source_chosen(self):
        source = self.source.currentData()
        changes = {"voice_source": source}
        if source == "trigger" and self.c.voice_settings()["voice_mode"] == "manual":
            changes["voice_mode"] = "hold"
        if self.c.update_voice(**changes) and "voice_mode" in changes:
            self.c.tell("仅触发识别需要发送快捷键，已改为「按住快捷键录音」。")

    def _copy_voice_diagnostic(self):
        from PySide6.QtWidgets import QApplication

        text = self.c.voice_protocol_diagnostic_text()
        QApplication.clipboard().setText(text)
        self.copy_voice_diag.setText("已复制，可粘贴给 AI")
        self.c.tell("已复制语音求助，可粘贴给 AI 协助适配这只遥控器的麦克风。")

    def on_show(self):
        self.c.refresh_outputs()

    def update_level(self, value):
        level = max(0, min(100, int(value * 500)))
        self.meter.setValue(level)
        self.meter_value.setText(f"{level}%")

    def open_vbcable_page(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl("https://vb-audio.com/Cable/"))

    def start_usb(self):
        try:
            profile = self.c.usb_preflight()
        except Exception as exc:
            self.c.fail(str(exc))
            return
        if self.c.usb.collector is not None:
            self.c.start_usb(profile)
            return
        answer = QMessageBox.question(
            self, "连接遥控器音频",
            "本次将暂时接管已识别的 USB 接收器，由本程序处理它的按键和音频。"
            "点「断开音频连接」后恢复；异常时可能需要拔插接收器。是否开始？")
        if answer == QMessageBox.StandardButton.Yes:
            self.c.start_usb(profile)

    def play_wav(self):
        path = QFileDialog.getOpenFileName(self, "选择用于通路验证的录音", "", "WAV 音频 (*.wav)")[0]
        if path:
            self.c.play_wav(path)
