"""Regression tests for enrollment UX, offline driver help, photo min-size, and universal voice skip."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QPoint
from PySide6.QtGui import QDesktopServices

from remotehub import models
from remotehub.device_probe import DeviceProbeDialog
from remotehub.enrollment import driver_help_text
from remotehub.photo_template import television_profile
from remotehub.probe_device import ProbeDevice


class _DummyReader:
    def __init__(self, *devices):
        self.devices = list(devices)
        self.on_key = None
        self.on_removed = None
        self.started = 0

    def enumerate(self):
        return list(self.devices)

    def start(self, identity, on_key, on_removed, *, expected=None):
        self.started += 1
        self.on_key = on_key
        self.on_removed = on_removed
        return 1, 1

    def stop(self):
        self.on_key = None
        self.on_removed = None

    close = stop


def test_add_dialog_pins_picker_starts_capture_and_shows_offline_driver_help(qtbot, monkeypatch):
    dev = ProbeDevice(("usb", 1, 0x0416, 0x0602), "0416:0602", "USB", 0x0416, 0x0602, "s" * 64, (1,))
    reader = _DummyReader(dev)
    dialog = DeviceProbeDialog(reader, install=lambda *_: True)
    qtbot.addWidget(dialog)
    dialog.show()

    # Picker is pinned outside scroll_area (never scrolled out of view).
    assert not dialog.scroll_area.isAncestorOf(dialog.picker)
    assert "没检测到" in dialog.not_listed.text()
    # The access note says in plain words that the bundled receiver connects directly.
    assert "直接接入" in dialog.protocol_note.text() and "0416:0602" in dialog.protocol_note.text()

    dialog.start_button.click()
    assert dialog.selected_device() == dev
    assert dialog.active and dialog.results_card.isVisibleTo(dialog)
    dialog.stop_capture()

    # Clicking not_listed hides the 4-row debug card to keep the troubleshooting view uncluttered.
    dialog.not_listed.click()
    assert dialog.selected_device() is None
    assert not dialog.results_card.isVisibleTo(dialog)

    # Driver help opens offline local dialog without calling QDesktopServices.openUrl.
    opened_urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened_urls.append(url) or True)
    dialog.picker.setCurrentIndex(0)
    dialog.open_driver_help()
    assert not opened_urls
    assert dialog.driver_help_dialog is not None and dialog.driver_help_dialog.isVisible()
    assert "0416:0602" in driver_help_text("macOS")
    assert "WinUSB" in driver_help_text("Windows")
    dialog.driver_help_dialog.close()
    dialog.close()


def test_photo_learning_dialog_enforces_minimum_size_and_never_overlaps_tips(qtbot):
    dev = ProbeDevice(("hid", 1), "遥控器 620A:0407", "蓝牙", 0x620A, 0x0407, "", (1,))
    reader = _DummyReader(dev)
    dialog = DeviceProbeDialog(reader, device=dev, install=lambda *_: True)
    qtbot.addWidget(dialog)
    dialog.photo.set_image(television_profile().image)
    dialog.show()
    assert dialog.minimumWidth() >= 940 and dialog.minimumHeight() >= 640

    # Resize down to minimum size and verify photo stage (left) never overlaps tips or key bar (right).
    assert dialog.minimumWidth() >= 1040 and dialog.minimumHeight() >= 740
    dialog.resize(dialog.minimumSize())
    dialog.layout().activate()
    dialog.photo.layout().activate()
    stage_right = dialog.photo.stage.mapTo(dialog, QPoint(dialog.photo.stage.width() - 1, 0)).x()
    tips_left = dialog.photo.tips.mapTo(dialog, QPoint(0, 0)).x()
    assert stage_right < tips_left
    dialog.photo.mark_clean()
    dialog.close()
