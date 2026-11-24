"""Regression tests for 0.3.0 audit findings (P1, P2, and unified UX flows)."""
from __future__ import annotations

from dataclasses import asdict
from dataclasses import replace
import json
from types import SimpleNamespace
import threading

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from remotehub import models
from remotehub.controller import AppController
from remotehub.device_probe import DeviceProbeDialog
from remotehub.enrollment import classify, windows_0416_needs_winusb_keys
from remotehub.learned_remotes import LearnedRemote, LearnedRemoteStore
from remotehub.macro_runner import MacroRunner
from remotehub.model import Action, Button, MacroStep, Profile, default_profiles
from remotehub.photo_template import television_profile
from remotehub.probe_device import ProbeDevice, UnlistedDevice
from remotehub.remote_library import (
    Connection, Remote, RemoteLibraryStore, receiver_connection, remote_for_device,
)
from remotehub.store import MAX_PROFILES
from remotehub.ui import MainWindow, general as general_ui
import remotehub.windows_collector as windows_collector_mod
import remotehub.windows_input as windows_input_mod
import remotehub.windows_probe as windows_probe_mod


class SharedReader:
    """In-memory shared-HID probe reader with separate peek() and enumerate() tracking."""

    def __init__(self, *devices):
        self.devices = list(devices)
        self.expected = self.on_key = self.on_removed = None
        self.scope = ""
        self.enumerated = 0
        self.peeked = 0

    def enumerate(self):
        self.enumerated += 1
        self.stop()
        return list(self.devices)

    def peek(self):
        self.peeked += 1
        return list(self.devices)

    def start(self, identity, on_key, on_removed, *, expected=None):
        assert expected is None or (identity == expected.id and expected in self.devices)
        self.expected = expected or next(d for d in self.devices if d.id == identity)
        self.on_key, self.on_removed = on_key, on_removed
        return 1, 1

    def edge(self, device, trigger, down):
        if device == self.expected and self.on_key is not None:
            self.on_key((1, 0), trigger, down)

    def tap(self, device, trigger):
        self.edge(device, trigger, True)
        self.edge(device, trigger, False)

    def stop(self):
        self.expected = self.on_key = self.on_removed = None

    close = stop


def _make_photo(tmp_path, name="photo.png"):
    path = tmp_path / name
    image = QImage(100, 220, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.gray)
    assert image.save(str(path))
    return path


def _generic_remote(name, device, *, trigger="hid:7:82"):
    profile = Profile(
        name=name,
        vendor=device.vendor,
        product=device.product,
        image=television_profile().image,
        buttons=[
            Button(id="k-up", name="上", trigger=trigger, rect=[0.1, 0.1, 0.2, 0.1]),
            Button(id="k-ok", name="确定", trigger="hid:7:40", rect=[0.1, 0.3, 0.2, 0.1]),
        ],
    )
    return remote_for_device(name, device, profile)


def _pass_required_keys(qtbot, dialog, reader, device):
    now = [0.0]
    dialog.verification.clock = lambda: now[0]
    while not dialog.verification.required_done:
        step = dialog.verification.current
        passed = dialog.verification.counts()["passed"]
        reader.edge(device, step.trigger, True)
        qtbot.waitUntil(lambda s=step: s.state == "held")
        now[0] += 0.5
        reader.edge(device, step.trigger, False)
        qtbot.waitUntil(lambda p=passed: dialog.verification.counts()["passed"] == p + 1)


# -- P1-1: Stale queued worker key events and release failure -------------------

@pytest.mark.fresh_config
def test_p1_1_queued_worker_key_ignored_after_remote_switch_and_release_failure_blocks_open(
    qtbot, tmp_path, monkeypatch
):
    collectors = []

    class FakeWinCollector(QObject):
        collected = Signal(object)
        status = Signal(str)
        failed = Signal(str)
        ready = Signal()
        log = Signal(str)
        key = Signal(str, bool)
        keys_ready = Signal(bool)

        def __init__(self, parent=None):
            super().__init__(parent)
            collectors.append(self)

        def start(self):
            pass

        def stop(self):
            return True

    class FakeWinInput:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr("remotehub.devices.sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_collector_mod, "WindowsCollector", FakeWinCollector)
    monkeypatch.setattr(windows_input_mod, "WindowsInput", FakeWinInput)

    # Create remote A (0416:0602 receiver) and remote B (generic with text action on hid:7:30).
    c = AppController(tmp_path, auto_mapping=False)
    try:
        recv_dev = ProbeDevice(("usb", 9), "0416:0602", "USB", 0x0416, 0x0602, "r1", (9,))
        remote_a = Remote(
            name="配套 A",
            connections=[receiver_connection()],
            profiles=default_profiles(),
            model_id="usb-0416-0602",
        )
        usb_dev = ProbeDevice(("usb", 1), "通用遥控器", "USB", 0x1234, 0x5678, "u1", (1,))
        p_b = Profile(
            name="B", vendor=usb_dev.vendor, product=usb_dev.product,
            image=television_profile().image,
            buttons=[Button(id="btn-1", name="确定", trigger="hid:7:30", rect=[0.1, 0.1, 0.2, 0.1],
                            action=Action("text", text="NEW_ACTION"))],
        )
        remote_b = remote_for_device("B", usb_dev, p_b)
        assert c.add_remote(remote_b, usb_dev)
        assert c.add_remote(remote_a, recv_dev)
        c.device.start(c.profile)
        old_collector = collectors[-1]

        sent_texts = []
        monkeypatch.setattr(c.keys, "input_text", sent_texts.append)

        # Emit key from a worker thread (queued into MainThread) and switch remote BEFORE draining Qt queue.
        t = threading.Thread(target=lambda: old_collector.key.emit("hid:7:30", True))
        t.start()
        t.join()
        assert c.select_remote(remote_b.id)
        QApplication.processEvents()
        assert sent_texts == []

        # Simulate failed collector release blocking enrollment dialog and resume.
        assert c.select_remote(remote_a.id)
        c.device.start(c.profile)
        collectors[-1].stop = lambda: False
        assert c.pause_remote_input() is False
        assert c.device.release_confirmed is False
        c.resume_remote_input()
        assert not c.enabled
    finally:
        c.shutdown()


@pytest.mark.fresh_config
def test_p1_1_window_aborts_open_enrollment_when_pause_fails(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    monkeypatch.setattr(window.controller, "pause_remote_input", lambda: False)
    resumed = []
    monkeypatch.setattr(window.controller, "resume_remote_input", lambda: resumed.append(True) or False)
    assert window.add_remote() is None
    assert window.add_dialog is None
    assert resumed == [True]
    window.shutdown()


# -- P1-2: Verification & Photo Enrollment stay open on save failure ------------

@pytest.mark.fresh_config
def test_p1_2_photo_dialog_stays_open_and_shows_error_on_save_failure(qtbot, tmp_path):
    (tmp_path / "remotes.json").write_text("{corrupt", encoding="utf-8")
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    assert c.load_error

    photo = _make_photo(tmp_path)
    unknown = ProbeDevice(("usb", 2), "通用", "USB", 0x1234, 0x5678, "u" * 64, (2,))
    reader2 = SharedReader(unknown)
    probe = DeviceProbeDialog(reader2, device=unknown, install=c.add_remote)
    qtbot.addWidget(probe)
    probe.photo.load_photo(photo)
    probe.learn_position([0.1, 0.1, 0.2, 0.1])
    reader2.tap(unknown, "hid:7:82")
    qtbot.waitUntil(probe.enrollment.save_button.isEnabled)
    probe.enrollment.save()
    assert probe.result() != probe.DialogCode.Accepted
    assert probe.feedback.text()
    assert not c.remotes
    probe.photo.mark_clean()
    probe.close()
    window.shutdown()


# -- P1-3: Migration safety on corrupt dev store or full 24 profiles ------------

@pytest.mark.fresh_config
def test_p1_3_corrupt_dev_store_sets_load_error_and_24_profiles_overflow_to_extra_remote(tmp_path):
    legacy_dir = tmp_path / "corrupt"
    legacy_dir.mkdir()
    (legacy_dir / "learned-remotes.json").write_text("{bad json", encoding="utf-8")
    store = RemoteLibraryStore(legacy_dir)
    store.load()
    assert store.load_error
    assert not (legacy_dir / "remotes.json").exists()
    with pytest.raises(ValueError):
        store.save()

    full_dir = tmp_path / "full"
    full_dir.mkdir()
    profiles = []
    for i in range(MAX_PROFILES):
        p = default_profiles()[0]
        p.id = f"p-{i}"
        p.name = f"方案 {i}"
        profiles.append(p)
    (full_dir / "profiles.json").write_text(
        json.dumps({"version": 1, "profiles": [asdict(p) for p in profiles], "settings": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    extra = television_profile()
    extra.id = "extra-learned"
    extra.name = "额外方案"
    LearnedRemoteStore(full_dir / "learned-remotes.json").save([LearnedRemote(extra, "USB", "")], extra.id)

    full_store = RemoteLibraryStore(full_dir)
    full_store.load()
    assert not full_store.load_error
    assert len(full_store.remotes) == 2
    assert len(full_store.remotes[0].profiles) == MAX_PROFILES
    assert [p.id for p in full_store.remotes[1].profiles] == ["extra-learned"]


# -- P1-4: D5 workflow adds Bluetooth connection to USB remote ------------------

@pytest.mark.fresh_config
def test_p1_4_d5_learn_more_keys_adds_bluetooth_connection_after_usb_unplugged(qtbot, tmp_path, monkeypatch):
    usb_dev = ProbeDevice(("usb", 1), "双模手柄 USB", "USB", 0x1111, 0x2222, "u1", (1,))
    bt_dev = ProbeDevice(("hid", 2), "双模手柄 蓝牙", "蓝牙", 0x1111, 0x3333, "", (2,), vendor_id_source=1)
    reader = SharedReader(usb_dev)
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.hid_reader = reader
    remote = _generic_remote("双模手柄", usb_dev)
    assert c.add_remote(remote, usb_dev)

    # Unplug USB, pair/connect Bluetooth, then open Learn More Keys (safe=False so picker reads BT).
    reader.devices = [bt_dev]
    monkeypatch.setattr(window, "safe", False)
    dialog = window.learn_more_keys()
    assert dialog is not None
    assert dialog.picker.isEnabled()
    assert dialog.selected_device() == bt_dev
    assert dialog.start_button.text() == "试按验证键码"
    dialog.start_button.click()
    reader.tap(bt_dev, "hid:7:82")
    qtbot.waitUntil(lambda: not dialog.active and dialog.photo.selected == "k-up")
    dialog.enrollment.save()
    qtbot.waitUntil(lambda: window.add_dialog is None)

    assert len(c.remote.connections) == 2
    assert c.remote.accepts(usb_dev) and c.remote.accepts(bt_dev)
    window.shutdown()


# -- P1-5: Windows 0416:0602 Interface 4 WinUSB warning & reconnect resume ------

# -- P2-1: Immediate persistence on 0.2.0 migration ----------------------------

@pytest.mark.fresh_config
def test_p2_1_migration_writes_remotes_json_immediately_without_touching_profiles_json(tmp_path):
    legacy = json.dumps({"version": 1, "profiles": [asdict(p) for p in default_profiles()], "settings": {}}, ensure_ascii=False)
    (tmp_path / "profiles.json").write_text(legacy, encoding="utf-8")
    store = RemoteLibraryStore(tmp_path)
    store.load()
    assert (tmp_path / "remotes.json").is_file()
    assert (tmp_path / "profiles.json").read_text(encoding="utf-8") == legacy


# -- P2-2: Live Bluetooth discovery via peek() + vendor_id_source isolation -----

def test_p2_2_vendor_id_source_isolation_rejects_missing_source():
    conn = Connection("蓝牙", 0x620A, 0x0407, vendor_id_source=1)
    bt_sig = ProbeDevice(("hid", 1), "BT", "蓝牙", 0x620A, 0x0407, vendor_id_source=1)
    bt_none = ProbeDevice(("hid", 2), "BT", "蓝牙", 0x620A, 0x0407, vendor_id_source=None)
    bt_other = ProbeDevice(("hid", 3), "BT", "蓝牙", 0x620A, 0x0407, vendor_id_source=2)
    assert conn.accepts(bt_sig)
    assert not conn.accepts(bt_none)
    assert not conn.accepts(bt_other)


@pytest.mark.fresh_config
def test_p2_2_scan_hid_discovers_paired_bluetooth_model_via_peek_while_reading_other_remote(
    qtbot, tmp_path, monkeypatch
):
    usb_dev = ProbeDevice(("usb", 1), "现有遥控器", "USB", 0x1234, 0x5678, "u1", (1,))
    bt_dev = ProbeDevice(("hid", 2), "新蓝牙", "蓝牙", 0x620A, 0x0407, "", (2,), vendor_id_source=1)
    known_usb = models.by_pair(0x0416, 0x0602)
    known_bt = replace(known_usb, id="ble-620a-0407", name="新蓝牙",
                       transport="bluetooth-hid", vendor=0x620A, product=0x0407, vendor_id_source=1, bcd_device=None)
    monkeypatch.setattr(models, "load", lambda directory=None: [known_usb, known_bt])

    reader = SharedReader(usb_dev)
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.hid_reader = reader
    assert c.add_remote(_generic_remote("现有遥控器", usb_dev), usb_dev)
    assert c.hid_device == usb_dev

    reader.devices.append(bt_dev)
    before_enum = reader.enumerated
    c.scan_hid()
    assert reader.enumerated == before_enum
    assert reader.peeked >= 1
    assert bt_dev in c.discovered
    assert window.detected_model == known_bt
    window.shutdown()


# -- P2-3: Invalid zone IDs rejected & empty verification never ready -----------

# -- P2-4: Draft disambiguation and Windows win: trigger round-trip -------------

# -- P2-5: GATT-only classification & composite vendor/BT unlisted entries ------

def test_p2_5_classify_gatt_and_windows_composite_unlisted(qtbot, monkeypatch):
    assert classify(UnlistedDevice("BT", "蓝牙", 0x1234, 0x5678, hid=False)) == "gatt_only"

    class FakeMetadata:
        def winusb_receivers(self):
            return []

        def for_interface(self, path, pair):
            return SimpleNamespace(
                group=("usb", "1", ""), name="复合遥控器", transport="USB",
                vendor=0x1234, product=0x5678, vendor_source=None, serial="",
                revision=None, winusb_interfaces=(),
            )

        def unlisted_bluetooth(self):
            return [UnlistedDevice("BT-GATT", "蓝牙", 0x9999, 0x0001, hid=False)]

    monkeypatch.setattr(windows_probe_mod, "WindowsProbeMetadata", FakeMetadata)
    monkeypatch.setattr(windows_probe_mod.WindowsInput, "__init__", lambda self, *a: setattr(self, "user", SimpleNamespace(
        GetRawInputDeviceList=SimpleNamespace(), GetRawInputDeviceInfoW=None,
    )))
    probe = windows_probe_mod.WindowsHIDProbe()
    raw_list = [SimpleNamespace(handle=1, kind=2), SimpleNamespace(handle=2, kind=2)]
    monkeypatch.setattr(probe, "_raw_devices", lambda: raw_list)
    monkeypatch.setattr(probe, "device_path", lambda h: f"path-{h}")

    def fake_identity(raw, skipped=None):
        if raw.handle == 1:
            return (0x1234, 0x5678)
        target = probe._skipped if skipped is None else skipped
        target.setdefault((0x1234, 0x5678), set()).add((0xFF00, 1))
        return None

    monkeypatch.setattr(probe, "_report_identity", fake_identity)
    listed = probe.enumerate()
    assert len(listed) == 1
    assert any(u.vendor == 0x1234 and u.product == 0x5678 and classify(u) == "vendor_page" for u in probe.unlisted)
    assert any(u.vendor == 0x9999 and u.transport == "蓝牙" and not u.hid for u in probe.unlisted)


# -- Big Picture #3, #4, #5 ----------------------------------------------------

@pytest.mark.fresh_config
def test_big_picture_general_page_input_perm_and_generic_remote_status(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(general_ui, "sys", SimpleNamespace(platform="darwin"))
    usb_dev = ProbeDevice(("usb", 1), "书桌遥控器", "USB", 0x1234, 0x5678, "u1", (1,))
    reader = SharedReader(usb_dev)
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.hid_reader = reader
    assert "input" in window.general.permission_rows
    assert c.add_remote(_generic_remote("书桌遥控器", usb_dev), usb_dev)
    window.general.refresh()
    assert "尚未适配" not in window.general.connection_status.text()
    assert "已连接" in window.general.connection_status.text()
    window.shutdown()


@pytest.mark.fresh_config
def test_big_picture_canonical_nav_works_for_learned_remote_in_overlays_switcher_and_macro(qapp, tmp_path):
    c = AppController(tmp_path, auto_mapping=False)
    try:
        dev = ProbeDevice(("hid", 9), "通用遥控器", "蓝牙", 0x2222, 0x3333, "", (9,), vendor_id_source=1)
        profile = Profile(
            name="通用", vendor=dev.vendor, product=dev.product, image=television_profile().image,
            buttons=[
                Button(id="custom-left", name="左", trigger="hid:7:101", rect=[0.1, 0.1, 0.1, 0.1]),
                Button(id="custom-right", name="右", trigger="hid:7:102", rect=[0.2, 0.1, 0.1, 0.1]),
                Button(id="custom-ok", name="确定", trigger="hid:7:103", rect=[0.3, 0.1, 0.1, 0.1]),
                Button(id="custom-back", name="返回", trigger="hid:7:104", rect=[0.4, 0.1, 0.1, 0.1]),
                Button(id="custom-open", name="菜单", trigger="hid:7:105", rect=[0.5, 0.1, 0.1, 0.1]),
            ],
        )
        assert c.add_remote(remote_for_device("通用", dev, profile), dev)

        # 1. Grid navigation & back cancel with Chinese names and custom IDs
        c.overlays.open_grid(Action("grid"), "hid:7:105")
        assert c.overlays.grid_open and c.overlays.grid_selected == 4
        c.on_key("hid:7:102", True)
        c.on_key("hid:7:102", False)
        assert c.overlays.grid_selected == 5
        c.on_key("hid:7:104", True)
        c.on_key("hid:7:104", False)
        assert not c.overlays.grid_open

        # 2. Cheat Sheet dismiss via Chinese-named back button
        c.overlays.run_cheat_sheet("hid:7:105", True, profile=c.profile)
        assert c.overlays.cheat_open
        c.on_key("hid:7:104", True)
        c.on_key("hid:7:104", False)
        assert not c.overlays.cheat_open

        # 3. App Switcher accepts confirm ("确定") from generic remote
        finished_reasons = []
        c.switcher.owner = "hid:7:105"
        c.switcher.opened = True
        orig_finish = c.switcher.finish

        def fake_finish(reason="idle"):
            finished_reasons.append(reason)
            c.switcher.owner = None
            c.switcher.opened = False

        c.switcher.finish = fake_finish
        c.on_key("hid:7:103", True)
        c.on_key("hid:7:103", False)
        c.switcher.finish = orig_finish
        assert finished_reasons == ["confirm"]

        # 4. Macro cancel via custom-back ("返回")
        macro = Action("macro", steps=[
            MacroStep(Action("shortcut", ["a"]), delay_ms=5000),
            MacroStep(Action("shortcut", ["b"]), delay_ms=0),
        ])
        c.runner.keys.shortcut = lambda *a, **kw: None
        c.macros.start(macro, "hid:7:105")
        assert c.macros.active
        c.on_key("hid:7:104", True)
        c.on_key("hid:7:104", False)
        assert not c.macros.active
    finally:
        c.shutdown()


@pytest.mark.fresh_config
def test_big_picture_voice_manual_mode_hides_and_inline_voice_key_picker_assigns(qtbot, tmp_path):
    usb_dev = ProbeDevice(("usb", 1), "通用遥控器", "USB", 0x1234, 0x5678, "u1", (1,))
    reader = SharedReader(usb_dev)
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    c.hid_reader = reader
    assert c.add_remote(_generic_remote("通用遥控器", usb_dev), usb_dev)
    window.voice_page.refresh()

    manual_idx = window.voice_page.mode.findData("manual")
    assert manual_idx >= 0
    assert not window.voice_page.mode.model().item(manual_idx).isEnabled()
    assert window.voice_page.mode.view().isRowHidden(manual_idx)

    assert c.update_voice(voice_source="trigger")
    assert not window.voice_guide.key_picker.isHidden()
    idx = window.voice_guide.key_picker.findData("k-up")
    assert idx > 0
    window.voice_guide.key_picker.setCurrentIndex(idx)
    window.voice_guide.choose_key(idx)
    btn = next(b for b in c.profile.buttons if b.id == "k-up")
    assert btn.action.kind == "voice"
    window.shutdown()
