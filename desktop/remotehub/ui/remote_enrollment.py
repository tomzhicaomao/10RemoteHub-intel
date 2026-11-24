"""Name and save a remote from the photo learning dialog."""
from __future__ import annotations

from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from ..remote_library import remote_for_device, sync_layout, with_connection
from .widgets import button, label, row


class RemoteEnrollment(QWidget):
    """Name and save a remote from the photo learning dialog."""

    def __init__(self, dialog, photo, install, draft):
        super().__init__(dialog)
        self.dialog, self.photo, self.install, self.draft = dialog, photo, install, draft
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.name = QLineEdit(draft.name if draft is not None else photo.profile.name)
        self.name.setAccessibleName("遥控器名称")
        self.name.setMaxLength(100)
        layout.addWidget(row(label("遥控器名称", wrap=False), self.name, stretch_last=True))
        # The dialog places the save button in its footer, next to「关闭」.
        self.save_button = button("保存遥控器", self.save, "primary")
        self.save_button.setAutoDefault(False)
        self.photo.selection_changed.connect(self.refresh)
        self.refresh()

    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        self.refresh()

    def refresh(self):
        # A 「语音」 mark alone is a photo position, not a learned key.
        self.save_button.setEnabled(bool(
            self.isEnabled()
            and self.photo.device is not None and self.photo.profile.image
            and any(key.trigger for key in self.photo.profile.buttons)
        ))

    def voice_protocol(self):
        """The built-in voice protocol of this handset's microphone, or "" when it has none.

        A handset has a microphone only when the user marked its voice key on the photo; the
        protocol comes from the connection (receiver pair, or the Bluetooth peer's GATT probe).
        """
        from ..protocols import identify_protocol

        if not any(key.role == "voice" for key in self.photo.profile.buttons):
            return ""
        match = identify_protocol(self.photo.device, getattr(self.dialog, "reader", None))
        if match.voice_protocol is not None:
            return match.voice_protocol.id
        return self.draft.voice_protocol if self.draft is not None else ""

    def build(self):
        from copy import deepcopy

        from ..model import Action
        from ..photo_template import bind_protocol_voice_key

        name = self.name.text().strip()
        device = self.photo.device
        # Detected frames that never got a key code are not saved.
        profile = self.photo.saved_profile()
        voice_protocol = self.voice_protocol()
        voice = bind_protocol_voice_key(profile, voice_protocol)
        if self.draft is None:
            profile.name = name or profile.name
            return remote_for_device(name, device, profile, voice_protocol=voice_protocol)
        remote = deepcopy(self.draft)
        remote.name = name or remote.name
        remote.voice_protocol = voice_protocol
        sync_layout(remote, profile)
        if voice is not None and voice.trigger:
            for other in remote.profiles:
                for key in other.buttons:
                    if key.id == voice.id:
                        key.action = Action("voice")
        return with_connection(remote, device)

    def save(self):
        try:
            remote = self.build()           # reads the live Bluetooth probe before the reader is released
        except (OSError, ValueError) as exc:
            self.dialog.feedback.setText(str(exc))
            return
        if getattr(self.dialog, "active", False) or getattr(self.dialog, "_ble_paused", False):
            self.dialog.stop_capture()          # release live/paused reader before MainWindow starts Runner
        elif getattr(self.dialog, "reader", None) is not None:
            self.dialog.reader.stop()
        try:
            saved = self.install(remote, self.photo.device)
        except (OSError, ValueError) as exc:
            self.dialog.feedback.setText(str(exc))
            return
        if saved is False:
            owner = getattr(self.install, "__self__", None)
            self.dialog.feedback.setText(getattr(owner, "last_error", "") or "保存遥控器失败，请检查配置文件状态后重试。")
            return
        self.photo.mark_clean()
        self.dialog.accept()
