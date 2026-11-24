"""Tests for generic USB Bluetooth HCI dongle (WinUSB + Bumble) BLE HOGP reader."""
from __future__ import annotations

import json
import struct
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from remotehub.ble_dongle_reader import (
    BLEDongleReader,
    BLEDongleStore,
    BLEPeerRecord,
    USBBTDongle,
    discover_usb_bluetooth_dongles,
    is_usb_bluetooth_hci,
    parse_adapter_spec,
)
from remotehub.ble_remote import (
    BLE_FALLBACK_VENDOR,
    GenericBLERemoteReports,
    fallback_ble_pair,
    parse_pnp_id,
)
from remotehub.model import Profile
from remotehub.probe_device import ProbeDevice
from remotehub.readers import CompositeReader, KeyReader, default_registry
from remotehub.remote_library import Connection, Remote
from remotehub.windows_probe_metadata import WindowsProbeMetadata


_KEYBOARD_MAP = bytes.fromhex("050715002565190029657508950185018100")


def test_generic_ble_reports_and_pnp_id_parsing():
    reports = GenericBLERemoteReports(_KEYBOARD_MAP)
    assert reports.feed(1, bytes([30])).keys == (("hid:7:30", True),)
    assert reports.feed(1, bytes([0])).keys == (("hid:7:30", False),)
    # Disconnect releases any held key cleanly.
    assert reports.feed(1, bytes([82])).keys == (("hid:7:82", True),)
    assert reports.disconnect().keys == (("hid:7:82", False),)

    raw_pnp = struct.pack("<BHHH", 2, 0x05D6, 0x000A, 0x0102)
    assert parse_pnp_id(raw_pnp) == (2, 0x05D6, 0x000A, 0x0102)
    assert parse_pnp_id(b"short") == (None, 0, 0, None)

    v, p = fallback_ble_pair("0C:F3:DE:FC:C5:C1")
    assert v == BLE_FALLBACK_VENDOR and 1 <= p <= 0xFFFF
    assert fallback_ble_pair("0c:f3:de:fc:c5:c1") == (v, p)


def test_discover_usb_bluetooth_dongles_accepts_any_hci_class_and_excludes_builtin_bthusb():
    # Built-in BTHUSB (0489:E0CC), WinUSB dongles 0BDA:8771 and 0A12:0001 (HCI on interface 0), webcam 0806:0806.
    builtin_bt = SimpleNamespace(
        idVendor=0x0489, idProduct=0xE0CC, bDeviceClass=0xE0, bDeviceSubClass=0x01, bDeviceProtocol=0x01,
        serial_number="", product="Built-in BT",
    )
    dongle_8771 = SimpleNamespace(
        idVendor=0x0BDA, idProduct=0x8771, bDeviceClass=0xE0, bDeviceSubClass=0x01, bDeviceProtocol=0x01,
        serial_number="SN01", product="BT 5.0 dongle",
    )
    # Composite device where Interface 0 carries the HCI triple (0xE0, 0x01, 0x01).
    hci_intf = SimpleNamespace(bInterfaceClass=0xE0, bInterfaceSubClass=0x01, bInterfaceProtocol=0x01)
    dongle_0a12 = SimpleNamespace(
        idVendor=0x0A12, idProduct=0x0001, bDeviceClass=0x00, bDeviceSubClass=0x00, bDeviceProtocol=0x00,
        serial_number="SN02", product="BT dongle",
        get_active_configuration=lambda: [hci_intf],
    )
    webcam = SimpleNamespace(
        idVendor=0x0806, idProduct=0x0806, bDeviceClass=0xEF, bDeviceSubClass=0x02, bDeviceProtocol=0x01,
        serial_number="", product="Webcam",
        get_active_configuration=lambda: [],
    )
    assert is_usb_bluetooth_hci(dongle_8771) is True
    assert is_usb_bluetooth_hci(dongle_0a12) is True
    assert is_usb_bluetooth_hci(webcam) is False

    # On Windows, ONLY dongles explicitly bound to WinUSB are returned (never built-in BTHUSB!).
    found = discover_usb_bluetooth_dongles(
        usb_devices_provider=lambda: [builtin_bt, dongle_8771, dongle_0a12, webcam],
        winusb_pairs_provider=lambda: {(0x0BDA, 0x8771), (0x0A12, 0x0001), (0x0806, 0x0806)},
        platform="win32",
    )
    assert [(d.vendor, d.product, d.spec) for d in found] == [
        (0x0BDA, 0x8771, "usb:0bda:8771"),
        (0x0A12, 0x0001, "usb:0a12:0001"),
    ]

    # parse_adapter_spec resolves auto for any single dongle and accepts arbitrary usb:VID:PID.
    assert parse_adapter_spec("auto", dongles=[found[1]]) == "usb:0a12:0001"
    assert parse_adapter_spec("usb:0a12:0001") == "usb:0a12:0001"
    assert parse_adapter_spec("pyusb:0a5c:21e8/SER1") == "pyusb:0a5c:21e8/SER1"
    with pytest.raises(ValueError, match="多只"):
        parse_adapter_spec("auto", dongles=found)
    with pytest.raises(ValueError, match="格式"):
        parse_adapter_spec("bad-adapter")


def test_ble_dongle_reader_store_enumerate_start_exclusive_seized_and_registry(tmp_path):
    dongle = USBBTDongle(0x0A12, 0x0001, "SN02", "BT dongle", "usb:0a12:0001")
    peer_rec = BLEPeerRecord(address="0C:F3:DE:FC:C5:C1", name="BLE 语音遥控器")
    events_received = []

    def fake_session(*, dongle, peer, taking_over, on_key, on_removed, halt, ready, startup_error):
        ready.set()
        on_key(("ble-dongle", peer.address), "hid:7:82", True)
        on_key(("ble-dongle", peer.address), "hid:7:82", False)
        halt.wait(2.0)

    reader = BLEDongleReader(
        state_dir=tmp_path / "ble-dongle",
        dongle_provider=lambda: [dongle],
        scanner=lambda _d, _dur: [peer_rec],
        session_runner=fake_session,
    )
    assert isinstance(reader, KeyReader)

    # Device config persists static random address cleanly.
    cfg = reader.store.ensure_device_config()
    assert cfg.is_file()

    # Enumerate discovers remote through generic non-8771 dongle.
    listed = reader.enumerate()
    assert len(listed) == 1
    dev = listed[0]
    assert dev.id == ("ble-dongle", "0C:F3:DE:FC:C5:C1")
    assert dev.transport == "蓝牙" and dev.vendor == BLE_FALLBACK_VENDOR and dev.unit_key

    # Save peer & verify registry routes this remote to ble-dongle while normal BT stays on hid!
    reader.store.save_peer(peer_rec)
    reg = default_registry()
    ble_remote = Remote(
        name="电视盒 BLE",
        connections=[Connection("蓝牙", dev.vendor, dev.product, units=[dev.unit_key])],
        profiles=[Profile(name="默认", vendor=dev.vendor, product=dev.product)],
    )
    normal_bt = Remote(
        name="普通蓝牙",
        connections=[Connection("蓝牙", 0x620A, 0x0407)],
        profiles=[Profile(name="默认", vendor=0x620A, product=0x0407)],
    )
    ctx = SimpleNamespace(_reader_for=lambda eid: reader if eid == "ble-dongle" else None, _receiver_present=lambda: False)
    assert reg.select(ble_remote, ctx).id == "ble-dongle"
    assert reg.select(normal_bt, ctx).id == "hid"
    assert reg.select(normal_bt, None).id == "hid"

    # Daily use (take_over=True) reports exclusive seized and emits keys.
    reader.take_over = True
    reader.take_over_triggers = ("hid:7:82", "hid:7:40")
    assert reader.start(dev.id, lambda *a: events_received.append(a), lambda: None, expected=dev) == (1, 1)
    assert reader.seized == 2 and not reader.seize_error and "0A12:0001" in reader.scope
    assert reader.sync_takeover(("hid:7:82", "hid:7:40", "hid:7:30")) is True
    assert reader.seized == 3
    reader.stop()
    assert reader.seized == 0
    assert events_received == [
        (("ble-dongle", "0C:F3:DE:FC:C5:C1"), "hid:7:82", True),
        (("ble-dongle", "0C:F3:DE:FC:C5:C1"), "hid:7:82", False),
    ]

    # CompositeReader merges ble-dongle + OS hid seamlessly for the Add Remote dialog.
    os_dev = ProbeDevice(("hid", 1), "OS 蓝牙键盘", "蓝牙", 0x620A, 0x0407)
    fake_hid = SimpleNamespace(
        enumerate=lambda: [os_dev], peek=lambda: [os_dev], stop=lambda: None, close=lambda: None, scope="", unlisted=[],
    )
    comp = CompositeReader([fake_hid, reader])
    merged = comp.enumerate()
    assert {d.id for d in merged} == {os_dev.id, dev.id}
    comp.close()


def test_windows_unlisted_bluetooth_includes_gatt_only_dev_without_vid_pid():
    meta = WindowsProbeMetadata.__new__(WindowsProbeMetadata)
    buf_text = "BTHLE\\Dev_0cf3defcc5c1\\8&327f0ef6&0&0cf3defcc5c1\0\0"

    def fake_size(size_ptr, enum, _flags):
        size_ptr._obj.value = len(buf_text) if enum == "BTHLE" else 0
        return 0 if enum == "BTHLE" else 1

    def fake_list(enum, buf, _size, _flags):
        if enum == "BTHLE":
            buf.value = buf_text
            return 0
        return 1

    meta.cfg = SimpleNamespace(CM_Get_Device_ID_List_SizeW=fake_size, CM_Get_Device_ID_ListW=fake_list)
    meta._node = lambda _inst: 1
    meta._name = lambda _node: "BLE 语音遥控器"
    unlisted = meta.unlisted_bluetooth()
    assert len(unlisted) == 1
    assert unlisted[0].name == "BLE 语音遥控器" and unlisted[0].transport == "蓝牙" and unlisted[0].hid is False
    # Never leaks raw MAC address into name.
    assert "0cf3" not in unlisted[0].name.lower()


def test_consumer_ble_dongle_guidance_detects_unbound_external_dongle_and_opens_offline_help(qtbot, tmp_path):
    from remotehub.ble_dongle_reader import detect_unbound_windows_bt_dongles
    from remotehub.device_probe import DeviceProbeDialog
    from remotehub.enrollment import ble_dongle_help_text

    # Single BTHUSB adapter = built-in PC Bluetooth -> NEVER suggested for Zadig!
    only_builtin = [(0x0489, 0xE0CC, "BTHUSB", "Built-in Bluetooth Adapter")]
    assert detect_unbound_windows_bt_dongles(adapters_provider=lambda: only_builtin, platform="win32") == []

    # Integrated built-in + external USB dongle -> excludes built-in, flags ONLY external!
    two_bt = [
        (0x0489, 0xE0CC, "BTHUSB", "Built-in Bluetooth Adapter"),
        (0x0BDA, 0x8771, "BTHUSB", "Generic Bluetooth Radio"),
    ]
    unbound = detect_unbound_windows_bt_dongles(adapters_provider=lambda: two_bt, platform="win32")
    assert [(v, p) for v, p, _, _ in unbound] == [(0x0BDA, 0x8771)]

    # Built-in BTHUSB + external libusbK dongle -> flags ONLY libusbK dongle!
    libusbk_case = [
        (0x0489, 0xE0CC, "BTHUSB", "Built-in Bluetooth Adapter"),
        (0x33FA, 0x0001, "libusbK", "USB Bluetooth Adapter"),
    ]
    unbound_k = detect_unbound_windows_bt_dongles(adapters_provider=lambda: libusbk_case, platform="win32")
    assert [(v, p, svc) for v, p, svc, _ in unbound_k] == [(0x33FA, 0x0001, "libusbK")]

    # Multiple ambiguous external dongles -> safe multi-adapter hint instead of guessing one.
    multi_reader = BLEDongleReader(
        state_dir=tmp_path / "ble-multi",
        dongle_provider=lambda: [],
        managed_provider=lambda: [],
        unbound_provider=lambda: [
            (0x0BDA, 0x8771, "BTHUSB", "USB BT A"),
            (0x0A12, 0x0001, "BTHUSB", "USB BT B"),
        ],
    )
    multi_hint = multi_reader.dongle_status_hint()
    assert "2 只外接 USB 蓝牙棒" in multi_hint and "0BDA:8771" in multi_hint and "0A12:0001" in multi_hint

    # Offline Chinese guide explains built-in one-click bind & restore (zero Zadig).
    win_text = ble_dongle_help_text("Windows")
    assert "切换为 BLE 直连模式" in win_text and "恢复普通蓝牙" in win_text and "绝不碰电脑自带蓝牙" in win_text

    # Add Remote dialog surfaces external BLE dongle card inside "没找到我的遥控器？" (unlisted) & opens offline help.
    from remotehub.win_usb_driver import ManagedUSBDevice
    reader = BLEDongleReader(
        state_dir=tmp_path / "ble",
        dongle_provider=lambda: [],
        unbound_provider=lambda: [],
        managed_provider=lambda: [
            ManagedUSBDevice(
                instance_id=r"USB\VID_0BDA&PID_8771\00E04C239987",
                vendor=0x0BDA,
                product=0x8771,
                kind="ble_dongle",
                service="BTHUSB",
                name="Generic Bluetooth Radio",
            )
        ],
    )
    dialog = DeviceProbeDialog(reader, install=lambda *_a: True)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.ble_dongle_card.isVisibleTo(dialog)
    assert dialog.ble_dongle_picker.count() == 1
    assert "0BDA:8771" in dialog.ble_dongle_picker.currentText()
    assert dialog.ble_dongle_help.isVisibleTo(dialog)
    dialog.ble_dongle_help.click()
    assert dialog.ble_dongle_help_dialog is not None and dialog.ble_dongle_help_dialog.isVisible()
    dialog.ble_dongle_help_dialog.accept()
    dialog.close()


def test_dongle_firmware_auto_staged_from_windows_driverstore(tmp_path):
    from remotehub.ble_dongle_reader import ensure_dongle_firmware_staged

    ds = tmp_path / "rtkfilter.inf_amd64_test"
    ds.mkdir()
    fake_fw = b"Realtech" + b"\x00" * 64
    (ds / "rtl8761b_mp_chip_bt40_fw_asic_rom_patch_new.dll").write_bytes(fake_fw)
    (ds / "not_firmware.dll").write_bytes(b"MZ\x90\x00")
    dest = tmp_path / "bumble-fw"

    staged = ensure_dongle_firmware_staged(driver_store_roots=[ds], dest_dir=dest, platform="win32")
    names = sorted(p.name for p in staged)
    assert names == ["rtl8761b_fw.bin", "rtl8761bu_fw.bin"]
    assert (dest / "rtl8761bu_fw.bin").read_bytes() == fake_fw
    # Idempotent on second call (already staged).
    assert ensure_dongle_firmware_staged(driver_store_roots=[ds], dest_dir=dest, platform="win32") == []


def test_ble_peer_preserves_public_and_random_address_type(tmp_path):
    pub = BLEPeerRecord(address="0C:F3:DE:FC:C5:C1/P", name="FIBER0D").normalized()
    assert pub.address == "0C:F3:DE:FC:C5:C1"
    assert pub.address_type == "public"
    assert pub.bumble_address == "0C:F3:DE:FC:C5:C1/P"

    rnd = BLEPeerRecord(address="4E:C6:26:2F:2D:C5", name="RandomRemote", address_type="random").normalized()
    assert rnd.address_type == "random"
    assert rnd.bumble_address == "4E:C6:26:2F:2D:C5/R"

    store = BLEDongleStore(tmp_path / "ble")
    store.save_peer(pub)
    store.save_peer(rnd)
    loaded = store.load_peers()
    assert loaded["0C:F3:DE:FC:C5:C1"].bumble_address == "0C:F3:DE:FC:C5:C1/P"
    assert loaded["4E:C6:26:2F:2D:C5"].bumble_address == "4E:C6:26:2F:2D:C5/R"

    cfg = json.loads(store.ensure_device_config().read_text(encoding="utf-8"))
    assert cfg["identity_address_type"] == 0 and cfg["le_privacy_enabled"] is False

    # Legacy string "public" in device.json auto-migrates to integer 0 (PairingConfig.AddressType.PUBLIC).
    cfg["identity_address_type"] = "public"
    store.device_config_path.write_text(json.dumps(cfg), encoding="utf-8")
    migrated = json.loads(store.ensure_device_config().read_text(encoding="utf-8"))
    assert migrated["identity_address_type"] == 0 and migrated["le_privacy_enabled"] is False


def test_ble_dongle_reader_pause_and_reuse_live_session_without_reconnecting(tmp_path):
    dongle = USBBTDongle(0x0BDA, 0x8771, "SN", "BT", "usb:0bda:8771")
    peer_rec = BLEPeerRecord(address="0C:F3:DE:FC:C5:C1", name="FIBER0D", address_type="public")
    starts = 0
    emit_holder: dict = {}

    def fake_runner(*, on_key, halt, ready, **_kw):
        nonlocal starts
        starts += 1
        emit_holder["on_key"] = on_key
        ready.set()
        halt.wait(timeout=5.0)

    reader = BLEDongleReader(
        state_dir=tmp_path / "ble",
        dongle_provider=lambda: [dongle],
        scanner=lambda _d, _dur: [peer_rec],
        session_runner=fake_runner,
    )
    listed = reader.enumerate()
    assert len(listed) == 1
    dev = listed[0]

    events_1: list = []
    reader.start(dev.id, lambda _ch, trig, down: events_1.append((trig, down)), lambda: None)
    assert starts == 1 and reader.is_connected_to(dev.id)
    emit_holder["on_key"](dev.id, "hid:7:82", True)
    emit_holder["on_key"](dev.id, "hid:7:82", False)
    assert events_1 == [("hid:7:82", True), ("hid:7:82", False)]

    # Pause keeps session alive (0ms detach), subsequent start() hot-swaps callbacks without reconnecting!
    reader.pause()
    assert reader.is_connected_to(dev.id)
    emit_holder["on_key"](dev.id, "hid:7:81", True)  # ignored while paused
    assert len(events_1) == 2

    events_2: list = []
    reader.start(dev.id, lambda _ch, trig, down: events_2.append((trig, down)), lambda: None)
    assert starts == 1  # ZERO reconnect / re-pair!
    emit_holder["on_key"](dev.id, "hid:7:81", True)
    emit_holder["on_key"](dev.id, "hid:7:81", False)
    assert events_2 == [("hid:7:81", True), ("hid:7:81", False)]

    reader.stop()
    assert not reader.is_connected_to(dev.id)


def test_device_probe_ble_warm_session_pause_reuse_and_release(qtbot):
    from remotehub.device_probe import DeviceProbeDialog

    ble_dev = ProbeDevice(
        id=("ble-dongle", "0C:F3:DE:FC:C5:C1"),
        name="FIBER0D",
        vendor=0x0416,
        product=0x0300,
        transport="BLE 蓝牙棒",
    )

    class _FakeWarmBLEReader:
        def __init__(self):
            self.seized = 0
            self.seize_error = ""
            self.scope = ""
            self.take_over = False
            self.connected = False
            self.start_calls = 0
            self.pause_calls = 0
            self.stop_calls = 0
            self.close_calls = 0
            self.on_key = None

        def enumerate(self):
            return [ble_dev]

        def available_dongles(self):
            return [USBBTDongle(0x0BDA, 0x8771, "SN", "BT", "usb:0bda:8771")]

        def managed_dongles(self):
            return []

        def managed_0416_receivers(self):
            return []

        def blocked_ble_remotes(self):
            return []

        def is_connected_to(self, identity):
            return self.connected and identity == ble_dev.id

        def start(self, identity, on_key, on_removed, *, expected=None):
            self.start_calls += 1
            self.connected = True
            self.on_key = on_key
            return 1, 1

        def pause(self):
            self.pause_calls += 1
            self.on_key = None

        def stop(self):
            self.stop_calls += 1
            self.connected = False
            self.on_key = None

        def close(self):
            self.close_calls += 1
            self.stop()

    reader = _FakeWarmBLEReader()
    dialog = DeviceProbeDialog(reader, owns_reader=False)
    qtbot.addWidget(dialog)

    # Simulate initial connected state (e.g. right after scan auto-connect).
    reader.connected = True
    dialog.start_capture()
    assert dialog.active
    assert reader.start_calls == 1
    assert reader.stop_calls == 0

    # Key press + release triggers finish_sample() -> pauses (0ms) instead of stopping BLE!
    dialog.on_key(dialog.generation, ble_dev.id, "hid:7:82", True)
    dialog.on_key(dialog.generation, ble_dev.id, "hid:7:82", False)
    dialog.finish_sample()
    assert not dialog.active
    assert reader.pause_calls == 1
    assert reader.stop_calls == 0
    assert reader.connected

    # Clicking "再测一个键" reuses live BLE session synchronously without reconnect flash!
    dialog.toggle_capture()
    assert dialog.active
    assert "配对" not in dialog.feedback.text()
    assert reader.start_calls == 2
    assert reader.stop_calls == 0

    # Finish key 2 (paused again), then closing the dialog MUST stop the paused BLE reader.
    dialog.on_key(dialog.generation, ble_dev.id, "hid:7:81", True)
    dialog.on_key(dialog.generation, ble_dev.id, "hid:7:81", False)
    dialog.finish_sample()
    assert not dialog.active
    assert reader.pause_calls == 2
    assert reader.stop_calls == 0

    dialog.release_reader()
    assert reader.stop_calls == 1
    assert not reader.connected


def test_photo_learning_keeps_ble_remote_after_gatt_pnp_id_enrichment(qtbot):
    from remotehub.device_probe import DeviceProbeDialog

    # Before GATT PnP ID discovery, scan-time ProbeDevice has MAC-derived fallback VID:PID.
    pre_gatt = ProbeDevice(
        id=("ble-dongle", "0C:F3:DE:FC:C5:C1"),
        name="FIBER0D",
        vendor=BLE_FALLBACK_VENDOR,
        product=0x1234,
        transport="BLE 蓝牙棒",
    )
    # After GATT PnP ID discovery, peers.json & enumerate() expose real 0416:0300.
    post_gatt = ProbeDevice(
        id=("ble-dongle", "0C:F3:DE:FC:C5:C1"),
        name="FIBER0D",
        vendor=0x0416,
        product=0x0300,
        transport="BLE 蓝牙棒",
    )

    class _FakeEnrichedBLEReader:
        seized = 0
        seize_error = ""
        scope = ""
        take_over = False

        def enumerate(self):
            return [post_gatt]

        def available_dongles(self):
            return [USBBTDongle(0x0BDA, 0x8771, "SN", "BT", "usb:0bda:8771")]

        def managed_dongles(self):
            return []

        def managed_0416_receivers(self):
            return []

        def blocked_ble_remotes(self):
            return []

        def is_connected_to(self, identity):
            return identity == post_gatt.id

        def start(self, identity, on_key, on_removed, *, expected=None):
            assert expected is not None and expected.id == post_gatt.id
            return 1, 1

        def pause(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    dialog = DeviceProbeDialog(
        _FakeEnrichedBLEReader(),
        device=pre_gatt,
        install=lambda profile, dev: None,
        owns_reader=False,
    )
    qtbot.addWidget(dialog)

    # Photo learning must NOT drop the remote as "未找到所选遥控器"; it must upgrade photo.device to 0416:0300!
    assert dialog.selected_device() == post_gatt
    assert dialog.photo is not None
    assert dialog.photo.device == post_gatt
    assert "未找到所选遥控器" not in dialog.feedback.text()


def test_bumble_loop_taking_over_retries_after_transient_startup_error(tmp_path):
    import asyncio
    import threading

    dongle = USBBTDongle(0x0BDA, 0x8771, "SN", "BT", "usb:0bda:8771")
    peer_rec = BLEPeerRecord(address="0C:F3:DE:FC:C5:C1", name="FIBER0D", address_type="public")
    reader = BLEDongleReader(state_dir=tmp_path / "ble", dongle_provider=lambda: [dongle])

    attempts = {"open": 0, "connect": 0}
    async_stop = asyncio.Event()
    ready = threading.Event()
    startup_error: list[str] = []

    class _DummyTransport:
        async def close(self):
            pass

    async def fake_open(_dongle, _cfg):
        attempts["open"] += 1
        if attempts["open"] == 1:
            raise OSError("LIBUSB_ERROR_ACCESS [-3]")
        return _DummyTransport(), object()

    async def fake_connect(**_kw):
        attempts["connect"] += 1
        ready.set()
        async_stop.set()
        return True

    reader._open_powered_device = fake_open  # type: ignore[method-assign]
    reader._connect_and_stream_once = fake_connect  # type: ignore[method-assign]

    # Inject minimal stub bumble modules so _bumble_loop runs even on Mac without bumble installed.
    import sys
    stub_core = SimpleNamespace(UUID=SimpleNamespace(from_16_bits=lambda x: x))
    stub_device = SimpleNamespace(Peer=object)
    saved = {k: sys.modules.get(k) for k in ("bumble", "bumble.core", "bumble.device")}
    sys.modules["bumble"] = SimpleNamespace(core=stub_core, device=stub_device)  # type: ignore[assignment]
    sys.modules["bumble.core"] = stub_core  # type: ignore[assignment]
    sys.modules["bumble.device"] = stub_device  # type: ignore[assignment]
    try:
        asyncio.run(
            reader._bumble_loop(
                dongle=dongle,
                peer=peer_rec,
                taking_over=True,
                on_key=lambda *_: None,
                on_removed=lambda: None,
                ready=ready,
                startup_error=startup_error,
                async_stop=async_stop,
            )
        )
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    assert attempts["open"] == 2
    assert attempts["connect"] == 1
    assert ready.is_set()
    assert not startup_error


def test_fiber0d_ble_remote_mic_registry_and_voice_session_routing(tmp_path, qtbot):
    from remotehub.ble_remote import FIBER0D_PAIR, ReportEvents
    from remotehub.controller import AppController
    from remotehub.remote_library import Connection, Remote

    reg = default_registry()
    fiber0d_remote = Remote(
        id="fiber0d",
        name="FIBER0D",
        model_id="",
        connections=[Connection(transport="蓝牙", vendor=FIBER0D_PAIR[0], product=FIBER0D_PAIR[1], vendor_id_source=1)],
        profiles=[Profile(name="默认", vendor=FIBER0D_PAIR[0], product=FIBER0D_PAIR[1], buttons=[])],
    )
    generic_bt = Remote(
        id="other-bt",
        name="Other BT",
        model_id="",
        connections=[Connection(transport="蓝牙", vendor=0x1234, product=0x5678)],
        profiles=[Profile(name="默认", vendor=0x1234, product=0x5678, buttons=[])],
    )

    assert reg.supports_remote_mic(fiber0d_remote) is True
    assert reg.supports_remote_mic(generic_bt) is False

    # Validate saving voice_source="remote" on FIBER0D passes remote.validate().
    fiber0d_remote.voice["voice_source"] = "remote"
    fiber0d_remote.validate(readers=reg)

    # Full AppController BLE voice event -> VoiceSession("ble") -> AudioBridge flow!
    from remotehub.remote_library import RemoteLibraryStore

    store = RemoteLibraryStore(tmp_path / "cfg")
    store.remotes = [fiber0d_remote]
    store.active = fiber0d_remote.id
    store.save()
    controller = AppController(tmp_path / "cfg", auto_mapping=False, live=False)
    try:
        assert controller.supports_remote_mic is True
        assert controller.uses_ble_remote_mic() is True

        # Simulate virtual mic output ready & select remote mic.
        controller.voice.output_id = b"vb-cable"
        started, fed, finished = [], [], []
        controller.audio.start = lambda out, gain: started.append((out, gain))
        controller.audio.feed = lambda pcm: fed.append(pcm)
        controller.audio.finish = lambda: (finished.append(True), controller.audio.ended.emit())
        controller.audio.abort = lambda: controller.audio.ended.emit()
        controller.keys.press = lambda keys: None
        controller.keys.release = lambda: None

        assert controller.update_voice(voice_source="remote", voice_mode="manual") is True

        controller._on_ble_voice_event(ReportEvents(voice=True))
        assert controller.voice.owner == "ble"
        assert controller.voice_view().title == "正在录音"
        assert len(started) == 1 and started[0][0] == b"vb-cable"

        pcm_chunk = b"\x01\x02" * 320
        controller._on_ble_voice_event(ReportEvents(pcm=pcm_chunk))
        assert fed == [pcm_chunk]

        controller._on_ble_voice_event(ReportEvents(voice=False))
        assert finished == [True]
        assert controller.voice.owner is None
        assert controller.voice_view().title == "空闲"
    finally:
        controller.shutdown()


def test_atv_voice_protocol_handshake_and_ima_adpcm_decoding():
    from remotehub.ble_voice_atv import ATVVVoiceSession, decode_ima_adpcm_frame

    samples, pred, idx = decode_ima_adpcm_frame(bytes([0x12, 0x34, 0x9A]), predictor=0, step_index=10)
    assert len(samples) == 6
    assert all(-32768 <= s <= 32767 for s in samples)
    assert 0 <= idx <= 88

    sess = ATVVVoiceSession(use_remote_mic=True)
    assert sess.caps_command()[0] == 0x0A
    # Remote responds with CAPS_RESP v1.0, 16kHz ADPCM supported, 134-byte frames.
    sess.on_ctl(bytes([0x0B, 0x01, 0x00, 0x00, 0x02, 0x00, 134, 0x00, 20]))
    assert sess.frame_size == 134

    # User presses voice key -> remote sends MIC_OPEN_REQ (0x0C) -> host replies MIC_OPEN 16kHz!
    ev_open = sess.on_ctl(bytes([0x0C]))
    assert ev_open.voice is True
    assert ev_open.command == bytes([0x0C, 0x00, 0x02])
    assert sess.active is True

    # Stream one 134-byte ADPCM frame split into 20-byte BLE notifications -> 256 PCM samples (512 bytes).
    frame = struct.pack(">HBhB", 1, 0, 120, 8) + bytes([0x12] * 128)
    pcm_total = bytearray()
    for off in range(0, len(frame), 20):
        ev_rx = sess.on_rx(frame[off : off + 20])
        pcm_total.extend(ev_rx.pcm)
    assert len(pcm_total) == 256 * 2

    ev_stop = sess.on_ctl(bytes([0x00]))
    assert ev_stop.voice is False
    assert sess.active is False


def test_photo_learning_microphone_first_step_and_canvas_live_highlight(tmp_path, qtbot):
    from remotehub.device_probe import DeviceProbeDialog
    from remotehub.ui.mapping import MappingPage
    from remotehub.controller import AppController
    from remotehub.remote_library import RemoteLibraryStore

    ble_dev = ProbeDevice(
        id=("ble-dongle", "0C:F3:DE:FC:C5:C1"),
        name="BLE Remote",
        vendor=0x1234,
        product=0x5678,
        transport="蓝牙",
    )

    class _FakeReader:
        seized = 0
        seize_error = ""
        scope = ""
        take_over = False

        def enumerate(self):
            return [ble_dev]

        def available_dongles(self):
            return []

        def managed_dongles(self):
            return []

        def managed_0416_receivers(self):
            return []

        def blocked_ble_remotes(self):
            return []

        def start(self, *a, **kw):
            return 1, 1

        def stop(self):
            pass

        def close(self):
            pass

    dialog = DeviceProbeDialog(_FakeReader(), device=ble_dev, install=lambda p, d: None, owns_reader=False)
    qtbot.addWidget(dialog)
    assert dialog.photo is not None
    # Import photo & confirm microphone via Step 1 -> prompt appears on RIGHT panel (left photo stays full height!).
    dialog.photo.set_image("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    dialog.photo.start_voice_mark()
    assert dialog.photo.marking_voice_key is True
    assert not dialog.photo.mic_step_card.isHidden()

    # Clicking on LearningCanvas to place a brand-new key must NOT arm drag (never drifts on release!).
    dialog.learn_position([0.4, 0.5, 0.15, 0.08])
    assert dialog.photo.canvas.drag is None
    assert dialog.photo.marking_voice_key is False
    assert dialog.photo.mic_step_card.isHidden()
    voice_btn = next((b for b in dialog.photo.saved_profile().buttons if b.role == "voice"), None)
    assert voice_btn is not None and voice_btn.name == "语音"
    assert abs(voice_btn.rect[1] - 0.5) < 0.001

    # Once the voice key is already marked, learning another key hides the redundant ZONE_VOICE chip!
    from remotehub.key_roles import ZONE_VOICE
    dialog.photo.begin([0.4, 0.7, 0.1, 0.06])
    assert dialog.photo.zone_buttons[ZONE_VOICE].isHidden()
    dialog.photo.cancel()
    dialog.photo.mark_clean()

    # Saved remote passes validation & main MappingPage highlights both physical keys and voice key live!
    saved_remote = Remote(
        id="custom-mic",
        name="Custom Mic",
        model_id="",
        connections=[Connection(transport="蓝牙", vendor=0x1234, product=0x5678)],
        profiles=[dialog.photo.saved_profile()],
    )
    saved_remote.validate()
    store = RemoteLibraryStore(tmp_path / "cfg")
    store.remotes = [saved_remote]
    store.active = saved_remote.id
    store.save()
    ctrl = AppController(tmp_path / "cfg", auto_mapping=False, live=False)
    try:
        page = MappingPage(ctrl)
        qtbot.addWidget(page)

        ctrl.voice_key_active.emit(True)
        assert voice_btn.id in page.canvas.pressed_ids
        ctrl.voice_key_active.emit(False)
        assert voice_btn.id not in page.canvas.pressed_ids

        diag = ctrl.voice_protocol_diagnostic_text()
        assert "物理语音键已标记：是" in diag and "1234:5678" in diag
    finally:
        ctrl.shutdown()


def test_protocol_library_identifies_usb_and_ble_protocols_without_shell_presets(tmp_path, qtbot):
    from remotehub.device_probe import DeviceProbeDialog
    from remotehub.protocols import identify_protocol

    # 1. Dedicated USB 0416:0602 protocol
    usb_0416 = ProbeDevice(id="usb0", name="USB Receiver", vendor=0x0416, product=0x0602, transport="USB")
    m_usb = identify_protocol(usb_0416)
    assert m_usb.key_protocol.id == "usb-0416-0602"
    assert m_usb.voice_protocol is not None and m_usb.voice_protocol.id == "usb-0416-0602"

    # 2. BLE dongle with FIBER0D GATT-probed voice protocol
    reader = BLEDongleReader(state_dir=tmp_path / "ble-proto")
    reader.store.save_peer(
        BLEPeerRecord(
            address="0C:F3:DE:FC:C5:C1",
            name="BLE Remote A",
            vendor=0x0416,
            product=0x0300,
            vendor_id_source=1,
            voice_capable=True,
            voice_protocol="fiber0d",
            gatt_summary="services=1812;reports=in:1,f8,fc|out:fb",
            paired=True,
        )
    )
    # 3. Same 0416:0300 chip on a DIFFERENT shell WITHOUT voice firmware -> must NOT claim voice_supported!
    reader.store.save_peer(
        BLEPeerRecord(
            address="AA:BB:CC:DD:EE:FF",
            name="BLE Remote B (No Mic)",
            vendor=0x0416,
            product=0x0300,
            vendor_id_source=1,
            voice_capable=False,
            voice_protocol="",
            firmware="OTHER_NO_MIC_V1",
            gatt_summary="services=1812;reports=in:1",
            paired=True,
        )
    )
    # 4. Unknown brand/VID:PID remote exposing standard ATV Voice (AB5E0001)
    reader.store.save_peer(
        BLEPeerRecord(
            address="11:22:33:44:55:66",
            name="Generic ATV Remote",
            vendor=0x2717,
            product=0x32B9,
            voice_capable=True,
            voice_protocol="atv_voice",
            gatt_summary="services=1812,ab5e0001",
            paired=True,
        )
    )

    dev_f0d = ProbeDevice(id=("ble-dongle", "0C:F3:DE:FC:C5:C1"), name="A", vendor=0x0416, product=0x0300, transport="蓝牙")
    m_f0d = identify_protocol(dev_f0d, reader)
    assert m_f0d.key_protocol.id == "ble-dongle-hid"
    assert m_f0d.voice_protocol is not None and m_f0d.voice_protocol.id == "fiber0d"

    dev_nomic = ProbeDevice(id=("ble-dongle", "AA:BB:CC:DD:EE:FF"), name="B", vendor=0x0416, product=0x0300, transport="蓝牙")
    m_nomic = identify_protocol(dev_nomic, reader)
    assert m_nomic.key_protocol.id == "ble-dongle-hid"
    assert m_nomic.voice_protocol is None and m_nomic.probed is True

    dev_atv = ProbeDevice(id=("ble-dongle", "11:22:33:44:55:66"), name="ATV", vendor=0x2717, product=0x32B9, transport="蓝牙")
    m_atv = identify_protocol(dev_atv, reader)
    assert m_atv.voice_protocol is not None and m_atv.voice_protocol.id == "atv_voice"

    # 5. Normal Add Remote dialog shows the access level & ZERO machine-photo candidate cards!
    class _StubReader:
        seized = 0
        seize_error = ""
        scope = ""
        take_over = False

        def enumerate(self):
            return [dev_f0d]

        def available_dongles(self):
            return []

        def managed_dongles(self):
            return []

        def managed_0416_receivers(self):
            return []

        def blocked_ble_remotes(self):
            return []

        def peer_voice_info_for_id(self, ident):
            return reader.peer_voice_info_for_id(ident)

        def stop(self):
            pass

        def close(self):
            pass

    dialog = DeviceProbeDialog(_StubReader(), install=lambda *_a: True, owns_reader=False)
    qtbot.addWidget(dialog)
    assert not hasattr(dialog, "candidate_cards")
    # Plain access level only; the microphone is explained in photo learning, not here.
    assert "直接接入" in dialog.protocol_note.text() and "蓝牙棒直连" in dialog.protocol_note.text()
    assert "协议" not in dialog.protocol_note.text()
    assert dialog.photo_link.text() == "导入照片，开始学习"


_CAMERA = BLEPeerRecord(address="0A:00:00:00:00:01", name="Camera-6429")
_REMOTE = BLEPeerRecord(address="0C:F3:DE:FC:C5:C1", name="BLE 语音遥控器")


def _write_bond(store, *addresses):
    store.directory.mkdir(parents=True, exist_ok=True)
    keys = {"11:22:33:44:55:66/P": {f"{a}/P": {"address_type": 0} for a in addresses}}
    store.keys_path.write_text(json.dumps(keys), encoding="utf-8")


def test_scan_picks_the_remote_heard_now_not_a_remembered_device_that_sorts_first(qtbot, tmp_path):
    """Windows 2026-11-21: a camera heard by the first scan was picked again after the remote was found."""
    from remotehub.device_probe import DeviceProbeDialog

    heard = [[_CAMERA], [_REMOTE]]
    dongle = USBBTDongle(0x0BDA, 0x8771, "SN", "BT", "usb:0bda:8771")
    reader = BLEDongleReader(
        state_dir=tmp_path / "ble", dongle_provider=lambda: [dongle], session_runner=lambda **_kw: None,
    )
    reader._scan_with_bumble = lambda _d, _dur: heard.pop(0)  # type: ignore[method-assign]
    dialog = DeviceProbeDialog(reader, owns_reader=False)
    qtbot.addWidget(dialog)
    picked = []
    dialog.start_capture = lambda rect=None: picked.append(dialog.selected_device().id)

    reader.scan_dongles()
    dialog._on_ble_scan_finished(dialog.generation, "")
    reader.scan_dongles()                                   # the camera is still remembered
    assert [d.id for d in reader.peek()] == [("ble-dongle", _CAMERA.address), ("ble-dongle", _REMOTE.address)]
    dialog._on_ble_scan_finished(dialog.generation, "")
    assert picked == [("ble-dongle", _CAMERA.address), ("ble-dongle", _REMOTE.address)]

    heard.append([])                                        # nothing in pairing mode: never fall back to the list
    reader.scan_dongles()
    dialog._on_ble_scan_finished(dialog.generation, "")
    assert len(picked) == 2 and "未发现" in dialog.feedback.text()


def test_a_peer_without_hid_service_is_forgotten_and_skipped_by_later_scans(tmp_path):
    import asyncio
    import sys
    import threading

    from remotehub.ble_dongle_reader import NotARemoteError

    dongle = USBBTDongle(0x0BDA, 0x8771, "SN", "BT", "usb:0bda:8771")
    reader = BLEDongleReader(
        state_dir=tmp_path / "ble", dongle_provider=lambda: [dongle],
        scanner=lambda _d, _dur: [_CAMERA, _REMOTE],
    )
    reader.scan_dongles()
    reader.store.save_peer(_REMOTE)
    _write_bond(reader.store, _CAMERA.address, _REMOTE.address)

    class _Transport:
        async def close(self):
            pass

    async def fake_open(_dongle, _cfg):
        return _Transport(), object()

    async def fake_connect(**_kw):
        raise NotARemoteError("这只 BLE 设备未暴露 0x1812 HID 服务")

    reader._open_powered_device = fake_open  # type: ignore[method-assign]
    reader._connect_and_stream_once = fake_connect  # type: ignore[method-assign]
    stub_core = SimpleNamespace(UUID=SimpleNamespace(from_16_bits=lambda x: x))
    stub_device = SimpleNamespace(Peer=object)
    saved = {k: sys.modules.get(k) for k in ("bumble", "bumble.core", "bumble.device")}
    sys.modules["bumble"] = SimpleNamespace(core=stub_core, device=stub_device)  # type: ignore[assignment]
    sys.modules["bumble.core"] = stub_core  # type: ignore[assignment]
    sys.modules["bumble.device"] = stub_device  # type: ignore[assignment]
    ready, startup_error = threading.Event(), []
    reader._halt_event.clear()                              # start() does this before the session loop
    try:
        asyncio.run(reader._bumble_loop(
            dongle=dongle, peer=_CAMERA, taking_over=False, on_key=lambda *_: None,
            on_removed=lambda: None, ready=ready, startup_error=startup_error, async_stop=asyncio.Event(),
        ))
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    assert ready.is_set() and "不是遥控器" in startup_error[0] and "Camera-6429" in startup_error[0]
    assert [d.id for d in reader.peek()] == [("ble-dongle", _REMOTE.address)]
    keys = reader.store.keys_path.read_text(encoding="utf-8")
    assert _CAMERA.address not in keys and _REMOTE.address in keys
    assert reader.store.has_bond_for(_REMOTE.address) and not reader.store.has_bond_for(_CAMERA.address)

    reader.scan_dongles()                                   # it keeps advertising; still not offered
    assert reader.last_scan_ids == [("ble-dongle", _REMOTE.address)]
    assert [d.id for d in reader.peek()] == [("ble-dongle", _REMOTE.address)]


def test_scanning_with_a_blocked_remote_selected_finds_only_that_remote(qtbot, tmp_path):
    """Windows 2026-11-21: with the system-Bluetooth remote selected, the scan still picked a camera."""
    from remotehub.device_probe import DeviceProbeDialog
    from remotehub.windows_probe_metadata import BlockedBLERemote

    blocked = BlockedBLERemote(name="BLE 语音遥控器", vendor=0x0416, product=0x0300, address=_REMOTE.address)
    heard = [[_CAMERA], [_CAMERA, BLEPeerRecord(address=_REMOTE.address, name="")]]
    dongle = USBBTDongle(0x0BDA, 0x8771, "SN", "BT", "usb:0bda:8771")

    class _Reader(BLEDongleReader):
        def blocked_ble_remotes(self):
            return [blocked]

    reader = _Reader(state_dir=tmp_path / "ble", dongle_provider=lambda: [dongle], session_runner=lambda **_kw: None)
    reader._scan_with_bumble = lambda _d, _dur: [r for r in heard.pop(0) if reader._is_target(r.address, r.name)]  # type: ignore[method-assign]
    dialog = DeviceProbeDialog(reader, owns_reader=False)
    qtbot.addWidget(dialog)
    picked = []
    dialog.start_capture = lambda rect=None: picked.append(dialog.selected_device().id)
    dialog.picker.setCurrentIndex(dialog.picker.findText(blocked.name, Qt.MatchFlag.MatchContains))
    assert dialog.selected_blocked_ble() == blocked

    dialog.scan_ble_dongle()                                # only the camera is advertising
    qtbot.waitUntil(lambda: not dialog._ble_scanning)
    assert picked == [] and "没找到「BLE 语音遥控器」" in dialog.feedback.text()
    assert reader.peek() == []                              # the camera is not even listed

    dialog.picker.setCurrentIndex(dialog.picker.findText(blocked.name, Qt.MatchFlag.MatchContains))
    dialog.scan_ble_dongle()                                # now the remote advertises too
    qtbot.waitUntil(lambda: not dialog._ble_scanning)
    assert picked == [("ble-dongle", _REMOTE.address)]


def test_without_a_selection_a_scan_only_auto_connects_a_sure_remote(qtbot, tmp_path):
    from remotehub.ble_dongle_reader import REMOTE_MAYBE, REMOTE_SURE, remote_confidence
    from remotehub.device_probe import DeviceProbeDialog

    assert remote_confidence("Camera-6429", [], 0x03C1) == REMOTE_MAYBE
    assert remote_confidence("BLE 语音遥控器", [], None) == REMOTE_SURE
    assert remote_confidence("", ["1812"], None) == REMOTE_SURE

    dongle = USBBTDongle(0x0BDA, 0x8771, "SN", "BT", "usb:0bda:8771")
    reader = BLEDongleReader(state_dir=tmp_path / "ble", dongle_provider=lambda: [dongle], session_runner=lambda **_kw: None)

    def scan(_d, _dur):
        reader._scan_confidence = {_CAMERA.address: REMOTE_MAYBE}
        return [_CAMERA]

    reader._scan_with_bumble = scan  # type: ignore[method-assign]
    dialog = DeviceProbeDialog(reader, owns_reader=False)
    qtbot.addWidget(dialog)
    picked = []
    dialog.start_capture = lambda rect=None: picked.append(dialog.selected_device().id)
    reader.scan_dongles()
    dialog._on_ble_scan_finished(dialog.generation, "")
    assert picked == [] and "不像遥控器" in dialog.feedback.text()
    assert dialog.selected_device().id == ("ble-dongle", _CAMERA.address)


def test_voice_asks_for_a_fast_link_only_when_the_remote_slowed_it(tmp_path):
    """Windows 2026-11-21: voice trailed speech by ~3 s on a slow BLE interval."""
    import asyncio

    from remotehub.ble_dongle_reader import _voice_timing_text

    reader = BLEDongleReader(state_dir=tmp_path / "ble")
    requests = []

    class _Link:
        def __init__(self, interval, latency):
            self.parameters = SimpleNamespace(connection_interval=interval, peripheral_latency=latency)

        async def update_parameters(self, *args):
            requests.append(args)

    asyncio.run(reader._request_voice_interval(_Link(11.25, 0)))
    assert requests == []
    asyncio.run(reader._request_voice_interval(_Link(30.0, 4)))
    assert requests == [(7.5, 15.0, 0, 2000)]

    text = _voice_timing_text({"start": 0.0, "first": 0.1, "last": 6.7, "stop": 4.0}, 199)
    assert text == "first_after=0.10s delivered=6.60s audio=3.98s after_release=2.70s"
