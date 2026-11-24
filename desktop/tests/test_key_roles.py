"""End-to-end tests for unified key roles, 3x4 keypad detection, nine-key reuse, and draft zones."""
from __future__ import annotations

import json

from PySide6.QtGui import QColor, QImage, QPainter
import pytest

from remotehub.key_roles import (
    KEY_ROLES,
    ZONE_FUNCTION,
    ZONE_KEYPAD,
    ZONE_NAVIGATION,
    ZONE_VOICE,
    friendly_name_from_trigger,
    infer_role_from_trigger,
    resolve_button_role,
    zone_for_role,
)
from remotehub.model import Action, Button, Profile, effective_button, profile_to_dict
from remotehub.nine_key import BINDING_ROLES
from remotehub.photo_hotspots import detect_hotspots
from remotehub.photo_template import television_profile
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import Connection, Remote, RemoteLibraryStore, sync_layout
from remotehub.ui.probe_photo import ProbePhotoPane


def test_key_roles_boundaries_and_friendly_media_names():
    assert len(KEY_ROLES) == 19
    assert infer_role_from_trigger("hid:7:82") == "up"
    assert infer_role_from_trigger("hid:7:30") == "1"
    assert infer_role_from_trigger("hid:7:39") == "0"
    assert infer_role_from_trigger("hid:12:548") == "back"
    # Function/media keys get friendly default names, NOT rigid roles.
    assert infer_role_from_trigger("hid:12:233") == ""
    assert friendly_name_from_trigger("hid:12:233") == "音量 +"
    assert friendly_name_from_trigger("hid:12:547") == "主页"
    assert zone_for_role("up") == ZONE_NAVIGATION
    assert zone_for_role("back") == ZONE_NAVIGATION
    assert zone_for_role("5") == ZONE_KEYPAD
    assert zone_for_role("voice") == ZONE_VOICE
    assert zone_for_role("") == ZONE_FUNCTION


def test_button_role_serialization_validation_and_sync_layout():
    b_role = Button(name="上一曲", trigger="hid:7:99", role="up")
    b_func = Button(name="音量 +", trigger="hid:12:233")
    profile = Profile(name="主方案", vendor=0x1234, product=0x5678, buttons=[b_role, b_func])
    data = profile_to_dict(profile)
    assert data["buttons"][0]["role"] == "up"
    assert "role" not in data["buttons"][1]

    reloaded = Profile.loads(profile.dumps())
    assert reloaded.buttons[0].role == "up" and reloaded.buttons[1].role == ""
    assert effective_button(reloaded.buttons[0], None).role == "up"

    other = Profile(name="副方案", vendor=0x1234, product=0x5678, buttons=[
        Button(id=b_role.id, name="旧名", trigger="hid:7:99", role=""),
    ])
    remote = Remote(name="测试遥控器", connections=[Connection("蓝牙", 0x1234, 0x5678)], profiles=[profile, other])
    sync_layout(remote, profile)
    assert other.buttons[0].role == "up"

    bad = Profile(name="坏", vendor=1, product=2, buttons=[Button(name="x", role="unknown_role")])
    with pytest.raises(ValueError, match="按钮角色标识无效"):
        bad.validate()


def test_photo_hotspots_detects_dpad_and_3x4_numeric_keypad_without_guessing_back_or_voice():
    image = QImage(300, 760, QImage.Format.Format_RGBA8888)
    image.fill(QColor(28, 30, 34))
    painter = QPainter(image)
    painter.setPen(QColor(28, 30, 34))
    # Top row: 2 ordinary function keys
    painter.setBrush(QColor(180, 185, 195))
    painter.drawRoundedRect(50, 40, 56, 36, 8, 8)
    painter.drawRoundedRect(194, 40, 56, 36, 8, 8)
    # Direction ring + OK hole
    painter.setBrush(QColor(195, 200, 210))
    painter.drawEllipse(65, 110, 170, 170)
    painter.setBrush(QColor(28, 30, 34))
    painter.drawEllipse(112, 157, 76, 76)
    painter.setBrush(QColor(195, 200, 210))
    painter.drawEllipse(122, 167, 56, 56)
    # 3x4 numeric keypad block below
    painter.setBrush(QColor(185, 190, 200))
    for row_idx in range(4):
        for col_idx in range(3):
            x = 42 + col_idx * 80
            y = 350 + row_idx * 78
            painter.drawRoundedRect(x, y, 56, 44, 10, 10)
    painter.end()

    spots = detect_hotspots(image)
    roles = [spot.role for spot in spots if spot.role]
    assert {"up", "down", "left", "right", "ok"} <= set(roles)
    assert {"1", "2", "3", "4", "5", "6", "7", "8", "9", "star", "0", "hash"} <= set(roles)
    # Visual detection must never guess back or voice.
    assert "back" not in roles and "voice" not in roles


def test_probe_photo_pane_infers_hid_roles_friendly_names_and_supports_manual_role_override(qtbot):
    pane = ProbePhotoPane(ProbeDevice(("hid", 1), "遥控器", "蓝牙", 0x620A, 0x0407))
    qtbot.addWidget(pane)
    pane.set_image(television_profile().image)

    # 1. Standard HID digit -> role="1", name="1"
    assert pane.begin([0.2, 0.2, 0.1, 0.05])
    assert pane.bind("hid:7:30")
    assert pane.profile.buttons[-1].role == "1" and pane.profile.buttons[-1].name == "1"

    # 2. Standard HID media volume+ -> friendly name "音量 +", role stays "" (function zone)
    assert pane.begin([0.4, 0.2, 0.1, 0.05])
    assert pane.bind("hid:12:233")
    vol_btn = pane.profile.buttons[-1]
    assert vol_btn.role == "" and vol_btn.name == "音量 +"

    # 3. Non-standard trigger -> manual role override keeps uniqueness
    assert pane.begin([0.6, 0.2, 0.1, 0.05])
    assert pane.bind("hid:7:105")
    custom_btn = pane.profile.buttons[-1]
    assert custom_btn.role == "" and "hid:7:105" in custom_btn.name
    pane.select(custom_btn.id)
    assert pane.set_role_selected("back")
    assert custom_btn.role == "back" and custom_btn.name == "返回"

    # Reassigning "back" to another button clears it from custom_btn.
    pane.select(vol_btn.id)
    assert pane.set_role_selected("back")
    assert vol_btn.role == "back" and custom_btn.role == ""


def test_nine_key_auto_fills_from_roles_handles_compact_remotes_and_syncs_across_profiles(tmp_path, qtbot):
    from remotehub.controller import AppController
    from remotehub.ui.inspector import Inspector

    # Case A: Compact remote (only 10 keys, no numeric keypad) -> friendly insufficient-key state.
    compact_buttons = [
        Button(name="上", trigger="hid:7:82", role="up"),
        Button(name="下", trigger="hid:7:81", role="down"),
        Button(name="左", trigger="hid:7:80", role="left"),
        Button(name="右", trigger="hid:7:79", role="right"),
        Button(name="确定", trigger="hid:7:40", role="ok"),
        Button(name="返回", trigger="hid:12:548", role="back"),
        Button(name="音量 +", trigger="hid:12:233"),
        Button(name="音量 −", trigger="hid:12:234"),
        Button(name="主页", trigger="hid:12:547"),
        Button(name="菜单", trigger="hid:7:101"),
    ]
    compact_remote = Remote(
        name="紧凑遥控器",
        connections=[Connection("蓝牙", 0x620A, 0x0407)],
        profiles=[Profile(name="默认", vendor=0x620A, product=0x0407, buttons=compact_buttons)],
    )
    store_a = RemoteLibraryStore(tmp_path / "a")
    store_a.add(compact_remote)
    ctrl_a = AppController(tmp_path / "a", store=store_a, auto_mapping=False)
    insp_a = Inspector(ctrl_a)
    qtbot.addWidget(insp_a)
    try:
        ctrl_a.select(compact_buttons[0].id)
        insp_a.choose_kind("nine_key")
        assert not insp_a.nine_key_setup.isEnabled()
        assert "10 个可用键" in insp_a.nine_key_status.text()
    finally:
        ctrl_a.shutdown()

    # Case B: Full remote with roles -> nine-key auto-ready, renaming does not break overlay nav,
    # voice assignment sets role="voice".
    full_buttons = [
        Button(name="上一曲", trigger="hid:7:82", role="up"),   # renamed, keeps role="up"
        Button(name="下", trigger="hid:7:81", role="down"),
        Button(name="左", trigger="hid:7:80", role="left"),
        Button(name="右", trigger="hid:7:79", role="right"),
        Button(name="确定", trigger="hid:7:40", role="ok"),
        Button(name="返回", trigger="hid:12:548", role="back"),
        *[Button(name=str(n), trigger=f"hid:7:{30 + n - 1}", role=str(n)) for n in range(1, 10)],
        Button(name="0", trigger="hid:7:39", role="0"),
        Button(name="*", trigger="hid:7:104", role="star"),
        Button(name="#", trigger="hid:7:105", role="hash"),
        Button(name="话筒", trigger="hid:7:106"),
        Button(name="音量 +", trigger="hid:12:233"),
    ]
    p1 = Profile(name="默认", vendor=0x1234, product=0x5678, image=television_profile().image, buttons=full_buttons)
    full_remote = Remote(name="全功能遥控器", connections=[Connection("USB", 0x1234, 0x5678)], profiles=[p1])
    store_b = RemoteLibraryStore(tmp_path / "b")
    store_b.add(full_remote)
    ctrl_b = AppController(tmp_path / "b", store=store_b, auto_mapping=False)
    try:
        assert ctrl_b.create_profile("办公")
        # Renamed "上一曲" still maps to tv-up via explicit role.
        assert ctrl_b._navigation_role("hid:7:82") == "tv-up"
        # Nine-key defaults auto-fill all 15 required keys + back!
        bindings = ctrl_b.nine_key_bindings(Action("nine_key"))
        assert set(BINDING_ROLES) <= set(bindings) and "back" in bindings
        # Assigning voice key sets role="voice" across all profiles.
        mic_id = full_buttons[-2].id
        assert ctrl_b.assign_voice_trigger_button(mic_id)
        assert all(
            next(b for b in prof.buttons if b.id == mic_id).role == "voice"
            for prof in ctrl_b.remote.profiles
        )
    finally:
        ctrl_b.shutdown()
