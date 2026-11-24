"""Devices the readers skip are explained, and the help text for AI carries real history."""
import json

import pytest
from PySide6.QtWidgets import QApplication

from remotehub.device_probe import DeviceProbeDialog
from remotehub.enrollment import BOUNDARIES, STEP_HELP, ai_request, classify, unlisted_access_text
from remotehub.hid_reports import HIDReports
from remotehub.inventory import USBDevice
from remotehub.probe_device import ProbeDevice, UnlistedDevice

GAMEPAD = bytes.fromhex("05010905a101" "05091901290815002501750195088102" "c0")
VENDOR = bytes.fromhex("0600ff0901a101" "150026ff00750895408102" "c0")
SYSTEM = bytes.fromhex("05010980a101" "0981098209831500250175019503810295058101" "c0")


def test_descriptor_declares_collections_and_input_pages():
    pad = HIDReports(GAMEPAD)
    assert pad.collections == {(1, 5)} and pad.input_pages == {9}
    vendor = HIDReports(VENDOR)
    assert vendor.collections == {(0xFF00, 1)} and vendor.input_pages == {0xFF00}
    assert HIDReports(SYSTEM).collections == {(1, 0x80)}


@pytest.mark.parametrize("device, case", [
    (UnlistedDevice("pad", "USB", 1, 2, frozenset({(1, 5)}), frozenset({9})), "gamepad_buttons"),
    (UnlistedDevice("x", "蓝牙", 1, 2, frozenset({(0xFF00, 1)}), frozenset({0xFF00})), "vendor_page"),
    (UnlistedDevice("pwr", "USB", 1, 2, frozenset({(1, 0x80)}), frozenset({1})), "system_control"),
    (UnlistedDevice("raw", "USB", 1, 2, hid=False), "private_usb"),
    (UnlistedDevice("mouse", "USB", 1, 2, frozenset({(1, 2)}), frozenset({1})), ""),
])
def test_classify(device, case):
    assert classify(device) == case


def test_every_boundary_separates_history_from_directions():
    for case, info in BOUNDARIES.items():
        assert info["proven"] and info["try"] and info["deliver"] and info["caution"], case
        assert info["user_try"], case
    text = ai_request(step="not_visible", case="gatt_only",
                      device=UnlistedDevice("遥控器", "蓝牙", 0x1234, 0x5678))
    assert "以往的做法" in text and "可以尝试的方向" in text and "适配器 0BDA:8771" in text
    assert "SKILL.md" in text and "征得我同意" in text and STEP_HELP["not_visible"][0] in text


def test_unlisted_access_levels_in_plain_words():
    # A USB device the app can take over on Windows is one click away, whatever it declares.
    taken = unlisted_access_text("vendor_page", takeover=True, platform="win32")
    assert taken.startswith("🔧 点一下就能接入") and "让 App 接管这个接收器" in taken
    # A TV-box Bluetooth remote reaches the app through an external dongle; Mac is not verified yet.
    win = unlisted_access_text("gatt_only", platform="win32", dongle=True)
    assert win.startswith("🔧 点一下就能接入") and "外接 USB 蓝牙棒" in win and "Mac" not in win
    assert "Mac 上这条路还在验证" in unlisted_access_text("gatt_only", platform="darwin", dongle=True)
    # A build without the dongle component must not promise that route.
    missing = unlisted_access_text("gatt_only", platform="win32", dongle=False)
    assert missing.startswith("⚠️ 目前无法自动接入") and "还没有带上蓝牙棒直连组件" in missing
    # Everything else lists what the user can try and offers the AI request.
    for case in ("private_usb", "vendor_page", "system_control", "gamepad_buttons", ""):
        text = unlisted_access_text(case, platform="win32")
        assert text.startswith("⚠️ 目前无法自动接入") and "复制给 AI 的求助" in text, case
        assert "协议" not in text, case
    assert BOUNDARIES["gamepad_buttons"]["user_try"][0] in unlisted_access_text("gamepad_buttons")


class Reader:
    def __init__(self, devices, unlisted):
        self.devices, self.unlisted = devices, unlisted

    def enumerate(self):
        return list(self.devices)

    def start(self, *args, **kwargs):
        raise RuntimeError("not used")

    def stop(self):
        pass

    close = stop


def test_add_dialog_lists_unreadable_devices_and_copies_help_for_ai(qtbot):
    listed = ProbeDevice(("usb", 1), "接收器", "USB", 0x0416, 0x0602, "a" * 64)
    pad = UnlistedDevice("Game Remote", "蓝牙", 0x045E, 0x02E0, frozenset({(1, 5)}), frozenset({9}))
    inventory = [USBDevice(0x0416, 0x0602, "SERIAL-1", "接收器", "1"),
                 USBDevice(0x1A2B, 0x3C4D, "SERIAL-2", "厂商接收器", "2")]
    dialog = DeviceProbeDialog(Reader([listed], [pad]), install=lambda *a: None, usb_devices=lambda: inventory)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.not_listed.click()
    assert dialog.input_step == "not_visible" and dialog.unlisted_box.isVisible()
    items = [dialog.unlisted_box.itemText(i) for i in range(dialog.unlisted_box.count())]
    assert any("Game Remote" in item and "手柄按钮页" in item for item in items)
    assert any("厂商接收器" in item and "私有 USB 接口" in item for item in items)
    assert not any("0602" in item for item in items)   # already listed
    dialog.unlisted_box.setCurrentIndex(next(i for i, item in enumerate(items) if "Game Remote" in item))
    note = dialog.boundary_note.text()
    assert "目前无法自动接入" in note and "手柄按钮页" in note and "可以试试" in note and "复制给 AI 的求助" in note
    dialog.copy_ai.click()
    text = QApplication.clipboard().text()
    assert "手柄按钮页" in text and "可以尝试的方向" in text and "045E:02E0" in text
    assert "SERIAL" not in text and "a" * 64 not in text
    diagnostics = json.loads(text[text.index("{"):])
    assert diagnostics["observations"]["boundary"] == "gamepad_buttons"
