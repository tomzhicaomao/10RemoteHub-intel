"""Tests for C2: scrollable add-remote body with fixed footer and single primary button."""
from dataclasses import replace

from PySide6.QtWidgets import QPushButton, QScrollArea

from remotehub import models
from remotehub.device_probe import DeviceProbeDialog
from remotehub.probe_device import ProbeDevice

RECEIVER = ProbeDevice(("usb", 1, 0x0416, 0x0602), "USB 0416:0602", "USB", 0x0416, 0x0602, "a" * 64, (1,))
GENERIC = ProbeDevice(("usb", 2, 0x1234, 0x5678), "通用 1234:5678", "USB", 0x1234, 0x5678, "b" * 64, (2,))


class Reader:
    def __init__(self, *devices):
        self.devices = list(devices)
        self.on_key = None

    def enumerate(self):
        return list(self.devices)

    def start(self, _identity, on_key, _on_removed, *, expected=None):
        self.on_key = on_key
        return 1, 1

    def tap(self, trigger):
        self.on_key((1, 0), trigger, True)
        self.on_key((1, 0), trigger, False)

    def stop(self):
        self.on_key = None

    close = stop


def _visible_primary_buttons(dialog):
    return [
        btn
        for btn in dialog.findChildren(QPushButton)
        if btn.isVisibleTo(dialog) and btn.property("role") == "primary"
    ]


def test_add_dialog_uses_scroll_area_with_fixed_footer(qtbot):
    reader = Reader(RECEIVER)
    dialog = DeviceProbeDialog(reader, install=lambda *_args: True)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.resize(520, 420)
    qtbot.waitUntil(dialog.isVisible)

    assert isinstance(dialog.scroll_area, QScrollArea)
    assert dialog.scroll_area.widgetResizable()
    body = dialog.scroll_area.widget()
    assert not dialog.scroll_area.isAncestorOf(dialog.picker)
    assert not dialog.footer.isAncestorOf(dialog.picker)
    for widget in (dialog.feedback, dialog.protocol_note, dialog.guide):
        assert body.isAncestorOf(widget)
        assert not dialog.footer.isAncestorOf(widget)

    for btn in (dialog.close_button, dialog.start_button, dialog.photo_link):
        assert dialog.footer.isAncestorOf(btn)
        assert not dialog.scroll_area.isAncestorOf(btn)
        assert btn.isVisibleTo(dialog)
        assert btn.mapTo(dialog, btn.rect().topLeft()).y() >= dialog.scroll_area.geometry().bottom() - 2


def test_add_dialog_keeps_only_one_primary_button(qtbot):
    # Protocol-first default Add Remote: no product-photo cards; before tap -> start_button, after tap -> photo_link.
    reader = Reader(RECEIVER, GENERIC)
    dialog = DeviceProbeDialog(reader, install=lambda *_args: True)
    qtbot.addWidget(dialog)
    dialog.show()
    assert _visible_primary_buttons(dialog) == [dialog.start_button]
    dialog.start_button.click()
    reader.tap("hid:7:30")
    qtbot.waitUntil(dialog.photo_link.isEnabled)
    assert _visible_primary_buttons(dialog) == [dialog.photo_link]

    # Switching to generic device: before tap -> start_button; after tap -> photo_link.
    dialog.picker.setCurrentIndex(1)
    assert _visible_primary_buttons(dialog) == [dialog.start_button]
    dialog.start_button.click()
    reader.tap("hid:7:82")
    qtbot.waitUntil(dialog.photo_link.isEnabled)
    assert _visible_primary_buttons(dialog) == [dialog.photo_link]
    dialog.close()
