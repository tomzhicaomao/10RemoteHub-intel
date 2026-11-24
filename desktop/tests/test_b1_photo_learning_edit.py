"""Tests for B1: inline key rename/delete and unsaved-close confirmation in photo learning."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMessageBox

from remotehub.device_probe import DeviceProbeDialog
from remotehub.probe_device import ProbeDevice


class SharedReader:
    def __init__(self, device):
        self.device = device
        self.on_key = None

    def enumerate(self):
        return [self.device]

    def start(self, _identity, on_key, _on_removed, *, expected=None):
        self.on_key = on_key
        return 1, 1

    def tap(self, trigger):
        self.on_key((1, 0), trigger, True)
        self.on_key((1, 0), trigger, False)

    def stop(self):
        self.on_key = None

    close = stop


def _make_photo(tmp_path):
    path = tmp_path / "remote.png"
    img = QImage(160, 400, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.lightGray)
    assert img.save(str(path))
    return path


def _learn_key(qtbot, dialog, reader, norm_y, trigger):
    dialog.learn_position([0.2, norm_y, 0.2, 0.1])
    reader.tap(trigger)
    qtbot.waitUntil(lambda: not dialog.active and any(b.trigger == trigger for b in dialog.photo.profile.buttons))


def test_photo_learning_rename_and_delete_selected_key(qtbot, tmp_path):
    dev = ProbeDevice(("usb", 1), "通用遥控器", "USB", 0x1234, 0x5678, "a" * 64, (1,))
    reader = SharedReader(dev)
    dialog = DeviceProbeDialog(reader, device=dev, install=lambda *_args: True)
    qtbot.addWidget(dialog)
    dialog.show()
    photo = dialog.photo

    assert not photo.key_bar.isVisibleTo(dialog)
    photo.load_photo(_make_photo(tmp_path))

    _learn_key(qtbot, dialog, reader, 0.15, "hid:7:82")
    _learn_key(qtbot, dialog, reader, 0.45, "hid:7:40")
    assert len(photo.profile.buttons) == 2

    first, second = photo.profile.buttons
    photo.select(first.id)
    assert photo.key_bar.isVisibleTo(dialog)
    assert photo.key_name_input.text() == first.name

    # Blank name is rejected and restores the previous name.
    photo.key_name_input.setText("   ")
    photo.rename_button.click()
    assert first.name != "   "
    assert photo.key_name_input.text() == first.name
    assert "名称" in dialog.feedback.text()

    # Valid rename updates button, canvas callout and marks dirty.
    photo.key_name_input.setText("上方向")
    photo.rename_button.click()
    assert first.name == "上方向"
    assert "上方向" in photo.canvas.callouts[first.id].text()

    # Select second key directly and delete it.
    photo.select(second.id)
    assert photo.key_name_input.text() == second.name
    photo.delete_button.click()
    assert [b.id for b in photo.profile.buttons] == [first.id]
    assert photo.selected == first.id
    assert dialog.enrollment.save_button.isEnabled()

    # Deleting the last key hides the key bar and disables saving.
    photo.delete_button.click()
    assert not photo.profile.buttons
    assert not photo.key_bar.isVisibleTo(dialog)
    assert not dialog.enrollment.save_button.isEnabled()

    photo.mark_clean()
    dialog.close()


def test_photo_learning_confirms_unsaved_keys_on_close_button_esc_and_window_close(qtbot, tmp_path, monkeypatch):
    dev = ProbeDevice(("usb", 1), "通用遥控器", "USB", 0x1234, 0x5678, "a" * 64, (1,))
    reader = SharedReader(dev)
    prompts = []

    def ask(*args, **kwargs):
        prompts.append(args)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", ask)

    # Opening and closing without learning any keys never prompts.
    clean_dialog = DeviceProbeDialog(reader, device=dev, install=lambda *_args: True)
    qtbot.addWidget(clean_dialog)
    clean_dialog.show()
    clean_dialog.close_button.click()
    assert not prompts
    assert not clean_dialog.isVisible()

    # Learn a key and verify Cancel keeps dialog open on Esc, close button, and close().
    dialog = DeviceProbeDialog(reader, device=dev, install=lambda *_args: True)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.photo.load_photo(_make_photo(tmp_path))
    _learn_key(qtbot, dialog, reader, 0.2, "hid:7:82")
    assert dialog.photo.dirty

    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert len(prompts) == 1 and dialog.isVisible()

    dialog.close_button.click()
    assert len(prompts) == 2 and dialog.isVisible()

    dialog.close()
    assert len(prompts) == 3 and dialog.isVisible()

    # Confirming Discard prompts strictly once and closes cleanly.
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: (prompts.append(args), QMessageBox.StandardButton.Discard)[1])
    dialog.close()
    assert len(prompts) == 4
    assert not dialog.isVisible()

    # Saving clears dirty flag so closing afterwards does not prompt.
    saved = []
    save_dialog = DeviceProbeDialog(reader, device=dev, install=lambda remote, device: saved.append(remote) or True)
    qtbot.addWidget(save_dialog)
    save_dialog.show()
    save_dialog.photo.load_photo(_make_photo(tmp_path))
    _learn_key(qtbot, save_dialog, reader, 0.25, "hid:7:40")
    prompts.clear()
    save_dialog.enrollment.save_button.click()
    assert len(saved) == 1
    assert not save_dialog.photo.dirty
    assert not prompts
    assert not save_dialog.isVisible()
