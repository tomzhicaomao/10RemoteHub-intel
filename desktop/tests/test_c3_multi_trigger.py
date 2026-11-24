"""Tests for C3: multi-trigger buttons (extra_triggers) across model, learning, verification, mapping, and runtime."""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt

from remotehub import models
from remotehub.device_probe import DeviceProbeDialog
from remotehub.model import Action, AppLayer, Button, Profile, effective_button
from remotehub.photo_template import television_profile
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import Remote, RemoteLibraryStore, remote_for_device, sync_layout
from remotehub.store import ConfigStore
from remotehub.ui import MainWindow
from remotehub.ui.onboarding import OnboardingDialog
from remotehub.ui.probe_photo import ProbePhotoPane


class SharedReader:
    def __init__(self, *devices):
        self.devices = list(devices)
        self.expected = self.on_key = self.on_removed = None
        self.scope = ""

    def enumerate(self):
        return list(self.devices)

    def start(self, identity, on_key, on_removed, *, expected=None):
        self.expected = expected or next(d for d in self.devices if d.id == identity)
        self.on_key, self.on_removed = on_key, on_removed
        return 1, 1

    def tap(self, device, trigger):
        if device == self.expected and self.on_key is not None:
            self.on_key((1, 0), trigger, True)
            self.on_key((1, 0), trigger, False)

    def stop(self):
        self.expected = self.on_key = self.on_removed = None

    close = stop


def test_button_extra_triggers_backward_compat_and_validation(tmp_path):
    legacy_payload = {
        "version": 1,
        "name": "旧版配置",
        "vendor": 0x1234,
        "product": 0x5678,
        "buttons": [
            {"id": "ok", "name": "确定", "trigger": "hid:7:40", "rect": [0.1, 0.1, 0.2, 0.1]},
        ],
    }
    loaded = Profile.loads(json.dumps(legacy_payload, ensure_ascii=False))
    btn = loaded.buttons[0]
    assert btn.extra_triggers == []
    assert btn.all_triggers == ["hid:7:40"]
    assert btn.matches_trigger("hid:7:40") is True
    assert btn.matches_trigger("hid:12:65") is False

    # Backward compatibility: empty extra_triggers is omitted on serialization.
    dumped = json.loads(loaded.dumps())
    assert "extra_triggers" not in dumped["buttons"][0]

    cfg = ConfigStore(tmp_path / "cfg")
    cfg.profiles = [loaded]
    assert "extra_triggers" not in json.loads(cfg.serialize())["profiles"][0]["buttons"][0]

    dev = ProbeDevice(("usb", 1), "1234:5678", "USB", 0x1234, 0x5678, "u1", (1,))
    remote = remote_for_device("通用遥控器", dev, loaded)
    assert "extra_triggers" not in remote.to_json()["profiles"][0]["buttons"][0]

    # Non-empty extra_triggers round-trips and survives effective_button (both normal & mouse layer overrides).
    btn.extra_triggers = ["hid:12:65", "win:28:0"]
    assert btn.all_triggers == ["hid:7:40", "hid:12:65", "win:28:0"]
    assert btn.matches_trigger("hid:12:65") is True

    round_prof = Profile.loads(loaded.dumps())
    assert round_prof.buttons[0].extra_triggers == ["hid:12:65", "win:28:0"]
    round_remote = Remote.from_json(remote.to_json())
    assert round_remote.profiles[0].buttons[0].extra_triggers == ["hid:12:65", "win:28:0"]

    normal_layer = AppLayer(app_id="com.example.one", name="One", overrides={"ok": {"single": Action("text", text="hi")}})
    mouse_layer = AppLayer(app_id="com.example.two", name="Two", overrides={"ok": {"single": Action("mouse", target="left")}})
    assert effective_button(btn, normal_layer).extra_triggers == ["hid:12:65", "win:28:0"]
    assert effective_button(btn, mouse_layer).extra_triggers == ["hid:12:65", "win:28:0"]

    # Validation checks:
    # 1. Duplicate within same button (extra duplicates primary or another extra).
    with pytest.raises(ValueError):
        Profile(buttons=[Button(id="k1", name="键1", trigger="hid:7:40", extra_triggers=["hid:7:40"])]).validate()
    with pytest.raises(ValueError):
        Profile(buttons=[Button(id="k1", name="键1", trigger="hid:7:40", extra_triggers=["hid:12:65", "hid:12:65"])]).validate()
    # 2. Collision across two buttons.
    with pytest.raises(ValueError):
        Profile(buttons=[
            Button(id="k1", name="键1", trigger="hid:7:40", extra_triggers=["hid:12:65"]),
            Button(id="k2", name="键2", trigger="hid:12:65"),
        ]).validate()
    # 3. Extra triggers without primary trigger, or invalid entries / exceeding limit.
    with pytest.raises(ValueError):
        Profile(buttons=[Button(id="k1", name="键1", trigger="", extra_triggers=["hid:12:65"])]).validate()
    with pytest.raises(ValueError):
        Profile(buttons=[Button(id="k1", name="键1", trigger="hid:7:40", extra_triggers=[""])]).validate()
    with pytest.raises(ValueError):
        Profile(buttons=[Button(id="k1", name="键1", trigger="hid:7:40", extra_triggers=[123])]).validate()
    with pytest.raises(ValueError):
        Profile(buttons=[Button(
            id="k1", name="键1", trigger="hid:7:40",
            extra_triggers=[f"hid:7:{100 + i}" for i in range(16)],
        )]).validate()
    # 4. Remote.validate checks extra_triggers format for non-fixed remotes.
    bad_prof = Profile(vendor=0x1234, product=0x5678, buttons=[
        Button(id="k1", name="键1", trigger="hid:7:40", extra_triggers=["bad:code"]),
    ])
    with pytest.raises(ValueError):
        remote_for_device("坏键码", dev, bad_prof).validate()


def test_extra_triggers_in_sync_layout(tmp_path):
    dev = ProbeDevice(("usb", 1), "1234:5678", "USB", 0x1234, 0x5678, "u1", (1,))
    b1 = Button(id="k1", name="确定", trigger="hid:7:40", extra_triggers=["win:28:0"], rect=[0.1, 0.1, 0.2, 0.1])
    b2 = Button(id="k2", name="上", trigger="hid:7:82", rect=[0.1, 0.3, 0.2, 0.1])
    p1 = Profile(id="p1", name="方案 1", vendor=0x1234, product=0x5678, image=television_profile().image, buttons=[b1, b2])
    p2 = Profile(
        id="p2", name="方案 2", vendor=0x1234, product=0x5678, image=television_profile().image,
        buttons=[Button(id="k1", name="确定", trigger="hid:7:40", action=Action("text", text="keep-ok"), rect=[0.1, 0.1, 0.2, 0.1])],
    )
    remote = Remote(name="双模手柄", connections=[], profiles=[p1, p2], active_profile_id="p1")
    sync_layout(remote, p1)

    p2_k1 = next(b for b in p2.buttons if b.id == "k1")
    p2_k2 = next(b for b in p2.buttons if b.id == "k2")
    assert p2_k1.extra_triggers == ["win:28:0"]
    assert p2_k1.action == Action("text", text="keep-ok")
    assert p2_k2.extra_triggers == []



@pytest.mark.fresh_config
def test_extra_triggers_in_runtime_mapping_inspector_window_and_onboarding(qtbot, tmp_path):
    usb_dev = ProbeDevice(("usb", 1), "1234:5678", "USB", 0x1234, 0x5678, "u1", (1,))
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller

    profile = Profile(
        name="双模",
        vendor=usb_dev.vendor,
        product=usb_dev.product,
        image=television_profile().image,
        buttons=[
            Button(
                id="k-ok", name="确定", trigger="hid:7:40", extra_triggers=["hid:12:65"],
                action=Action("text", text="ok-fired"), rect=[0.1, 0.2, 0.2, 0.1],
            ),
            Button(
                id="k-voice", name="语音", trigger="hid:12:207", extra_triggers=["hid:7:68"],
                action=Action("voice"), rect=[0.1, 0.5, 0.2, 0.1],
            ),
        ],
    )
    remote = remote_for_device("双模遥控器", usb_dev, profile)
    assert c.add_remote(remote, usb_dev)
    c.enabled = True

    sent_texts = []
    c.keys.input_text = lambda text: sent_texts.append(text)
    c.keys._send = lambda *_: None

    # 1. Runtime dispatch via extra_trigger.
    c.on_key("hid:12:65", True)
    assert "hid:12:65" in c.pressed
    c.on_key("hid:12:65", False)
    assert sent_texts == ["ok-fired"]
    assert c.selected_id == "k-ok"

    # 2. Voice key lookup, overlay navigation, switcher controls, and _cancel_button_actions handle extra_triggers.
    assert c._voice_key("hid:7:68").id == "k-voice"
    nav = c._navigation_controls()
    assert nav["hid:12:65"] == "tv-ok"
    sw = c._switcher_controls()
    assert sw["hid:12:65"] == "k-ok"
    c.holds.toggle("hid:12:65", ["alt"])
    assert c.holds.owned("hid:12:65")
    c._cancel_button_actions(profile.buttons[0])
    assert not c.holds.owned("hid:12:65")

    # 3. Inspector shows all triggers; MainWindow.show_physical_key recognizes extra_triggers.
    window.show_mapping()
    c.select("k-ok")
    assert window.inspector.trigger.text() == "hid:7:40 / hid:12:65"
    window.show_physical_key("hid:12:65", True)
    assert "确定" in window.feedback.text()

    # 4. OnboardingWizard (OnboardingDialog.verify_key) recognizes extra_triggers.
    onboarding = OnboardingDialog(c, window)
    qtbot.addWidget(onboarding)
    onboarding.show_page(3)
    onboarding.verify_key("hid:12:65", True)
    assert onboarding.verified is True
    assert "确定" in onboarding.verify_detail.text()
    onboarding.close()

    # 5. ProbePhotoPane.bind treats extra_triggers as duplicate rather than rebinding.
    pane = ProbePhotoPane(usb_dev, profile=Profile.loads(profile.dumps()))
    qtbot.addWidget(pane)
    assert pane.begin([0.4, 0.4, 0.2, 0.1])
    assert pane.bind("hid:12:65") is False
    assert pane.selected == "k-ok"
    assert len(pane.profile.buttons) == 2

    window.shutdown()


@pytest.mark.fresh_config
def test_second_connection_trial_press_different_code_prompts_and_merges(qtbot, tmp_path, monkeypatch):
    usb_dev = ProbeDevice(("usb", 1), "双模手柄 USB", "USB", 0x1234, 0x5678, "u1", (1,))
    bt_dev = ProbeDevice(("hid", 2), "双模手柄 蓝牙", "蓝牙", 0x620A, 0x0407, "", (2,), vendor_id_source=1)
    reader = SharedReader(usb_dev)

    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.hid_reader = reader

    p1 = Profile(
        id="p1", name="默认方案", vendor=usb_dev.vendor, product=usb_dev.product,
        image=television_profile().image,
        buttons=[
            Button(id="k-ok", name="确定", trigger="hid:7:40", rect=[0.1, 0.3, 0.2, 0.1]),
            Button(id="k-up", name="上", trigger="hid:7:82", rect=[0.1, 0.1, 0.2, 0.1]),
        ],
    )
    remote = remote_for_device("双模手柄", usb_dev, p1)
    assert c.add_remote(remote, usb_dev)
    assert c.create_profile("方案二")
    c.select("k-ok")
    assert c.assign(Action("text", text="merged-action"))
    c.select_profile(0)

    # Connect Bluetooth adapter and open "学习更多按键".
    reader.devices = [usb_dev, bt_dev]
    monkeypatch.setattr(window, "safe", False)
    dialog = window.learn_more_keys()
    assert dialog is not None
    assert hasattr(dialog, "merge_card")
    assert not dialog.merge_card.isVisible()

    # Select Bluetooth in picker and trial-press a different keycode.
    bt_idx = next(i for i, d in enumerate(dialog.devices) if d == bt_dev)
    dialog.picker.setCurrentIndex(bt_idx)
    assert dialog.start_button.text() == "试按验证键码"
    dialog.start_button.click()
    reader.tap(bt_dev, "hid:12:65")
    qtbot.waitUntil(lambda: not dialog.active)

    assert "这颗键在新接收端上发的码不同，是否归到同一颗键" in dialog.feedback.text()
    assert "这颗键在新接收端上发的码不同，是否归到同一颗键" in dialog.merge_prompt.text()
    assert dialog.merge_card.isVisibleTo(dialog)
    assert dialog.merge_confirm_button.text() == "归到同一颗键"

    ok_idx = dialog.merge_picker.findData("k-ok")
    assert ok_idx >= 0
    dialog.merge_picker.setCurrentIndex(ok_idx)
    dialog.merge_confirm_button.click()

    assert not dialog.merge_card.isVisible()
    assert dialog.photo.dirty is True
    merged_btn = next(b for b in dialog.photo.profile.buttons if b.id == "k-ok")
    assert merged_btn.extra_triggers == ["hid:12:65"]

    dialog.enrollment.save_button.click()
    qtbot.waitUntil(lambda: window.add_dialog is None)

    assert len(c.remote.connections) == 2
    assert c.remote.accepts(usb_dev) and c.remote.accepts(bt_dev)
    for prof in c.remote.profiles:
        btn = next(b for b in prof.buttons if b.id == "k-ok")
        assert btn.extra_triggers == ["hid:12:65"]
    assert next(b for b in c.remote.profiles[1].buttons if b.id == "k-ok").action == Action("text", text="merged-action")

    # Verify runtime pressing hid:12:65 on profile 2 triggers the preserved action.
    sent = []
    c.keys.input_text = lambda text: sent.append(text)
    c.enabled = True
    c.select_profile(1)
    c.on_key("hid:12:65", True)
    c.on_key("hid:12:65", False)
    assert sent == ["merged-action"]

    # Verify persistence on disk.
    reloaded = RemoteLibraryStore(tmp_path)
    reloaded.load()
    assert len(reloaded.remotes[0].connections) == 2
    assert reloaded.remotes[0].profiles[1].buttons[0].extra_triggers == ["hid:12:65"]

    window.shutdown()
