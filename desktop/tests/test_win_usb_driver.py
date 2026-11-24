"""Tests for one-click Windows inbox WinUSB bind/restore (zero Zadig)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from PySide6.QtCore import Qt

from remotehub import win_usb_driver as drv
from remotehub.ble_dongle_reader import BLEDongleReader
from remotehub.device_probe import DeviceProbeDialog


def _sample_nodes(*, dongle_service: str = "BTHUSB", dongle_inf: str = "bth.inf", dongle_class: str = "Bluetooth",
                  dongle_name: str = "Generic Bluetooth Radio", include_dongle: bool = True,
                  include_0416: bool = True):
    nodes = [
        # Built-in motherboard Bluetooth (0489:E0CC) -> MUST NEVER be touched!
        {
            "instance_id": r"USB\VID_0489&PID_E0CC\6&3AF0F9CE&0&12",
            "vendor": 0x0489,
            "product": 0xE0CC,
            "service": "BTHUSB",
            "device_class": "Bluetooth",
            "driver_inf": "bth.inf",
            "compat": "USB\\CLASS_E0&SUBCLASS_01&PROT_01",
            "name": "Built-in Bluetooth Adapter",
        },
        # Ordinary USB mouse -> MUST NEVER be touched!
        {
            "instance_id": r"USB\VID_046D&PID_C52B&MI_01\7&1111&0&0001",
            "vendor": 0x046D,
            "product": 0xC52B,
            "service": "HidUsb",
            "device_class": "HIDClass",
            "driver_inf": "input.inf",
            "compat": "USB\\CLASS_03&SUBCLASS_01&PROT_02",
            "name": "USB Mouse",
        },
    ]
    if include_dongle:
        nodes.append(
            {
                "instance_id": r"USB\VID_0BDA&PID_8771\00E04C239987",
                "vendor": 0x0BDA,
                "product": 0x8771,
                "service": dongle_service,
                "device_class": dongle_class,
                "driver_inf": dongle_inf,
                "compat": "USB\\CLASS_E0&SUBCLASS_01&PROT_01",
                "name": dongle_name,
            }
        )
    if include_0416:
        for mi, svc in ((0, "HidUsb"), (1, "HidUsb"), (3, "HidUsb"), (4, "HidUsb")):
            nodes.append(
                {
                    "instance_id": rf"USB\VID_0416&PID_0602&MI_0{mi}\7&2222&0&000{mi}",
                    "vendor": 0x0416,
                    "product": 0x0602,
                    "service": svc,
                    "device_class": "HIDClass",
                    "driver_inf": "input.inf",
                    "compat": "USB\\CLASS_03&SUBCLASS_00&PROT_00",
                    "name": f"USB 0416:0602 MI_0{mi}",
                }
            )
    return nodes


def test_classify_strictly_excludes_builtin_bt_and_non_allowlisted_usb():
    # Built-in only -> empty dongles list.
    dongles, receivers = drv.classify_managed_devices(_sample_nodes(include_dongle=False, include_0416=False), platform="win32")
    assert dongles == [] and receivers == []

    # Single unknown BTHUSB adapter (e.g. desktop with only 1 BT dongle) -> treated as sole system BT, never suggested!
    sole_bt = [
        {
            "instance_id": r"USB\VID_0BDA&PID_8771\00E04C239987",
            "vendor": 0x0BDA,
            "product": 0x8771,
            "service": "BTHUSB",
            "device_class": "Bluetooth",
            "driver_inf": "bth.inf",
            "compat": "USB\\CLASS_E0&SUBCLASS_01&PROT_01",
            "name": "Generic Bluetooth Radio",
        }
    ]
    assert drv.classify_managed_devices(sole_bt, platform="win32") == ([], [])

    # Built-in + external dongle + 0416:0602 -> ONLY external dongle & 0416 MI_03/MI_04!
    dongles, receivers = drv.classify_managed_devices(_sample_nodes(), platform="win32")
    assert [(d.vendor, d.product, d.bound_winusb) for d in dongles] == [(0x0BDA, 0x8771, False)]
    assert [(r.vendor, r.product, r.interface) for r in receivers] == [(0x0416, 0x0602, 3), (0x0416, 0x0602, 4)]

    # After binding to inbox winusb.inf, FriendlyName becomes "WinUSB 设备" — still detected via CompatId E0/01/01!
    dongles_bound, _ = drv.classify_managed_devices(
        _sample_nodes(dongle_service="WinUSB", dongle_inf="winusb.inf", dongle_class="USBDevice", dongle_name="WinUSB 设备"),
        platform="win32",
    )
    assert len(dongles_bound) == 1
    assert dongles_bound[0].bound_winusb is True and dongles_bound[0].restorable is True


def test_validate_and_execute_bind_and_restore_including_legacy_oem_cleanup():
    nodes = _sample_nodes()
    # Refuses built-in Bluetooth even if directly passed on CLI!
    res_refuse = drv.execute_driver_action(
        "bind",
        [r"USB\VID_0489&PID_E0CC\6&3AF0F9CE&0&12"],
        nodes_provider=lambda: nodes,
        require_admin=False,
        platform="win32",
    )
    assert res_refuse["ok"] is False
    assert "绝不修改电脑自带蓝牙" in res_refuse["error"]

    # Bind external dongle calls bind_fn cleanly.
    bound_ids: list[str] = []
    res_bind = drv.execute_driver_action(
        "bind",
        [r"USB\VID_0BDA&PID_8771\00E04C239987"],
        nodes_provider=lambda: nodes,
        bind_fn=lambda dev: (bound_ids.append(dev.instance_id) or False),
        require_admin=False,
        platform="win32",
    )
    assert res_bind["ok"] is True
    assert bound_ids == [r"USB\VID_0BDA&PID_8771\00E04C239987"]

    # Deterministic WinUSB DeviceInterfaceGUIDs per instance.
    g1 = drv.device_interface_guid(r"USB\VID_0BDA&PID_8771\00E04C239987")
    g2 = drv.device_interface_guid(r"usb\vid_0bda&pid_8771\00e04c239987")
    assert g1 == g2 and g1.startswith("{") and g1.endswith("}")

    # Restore legacy Zadig oem42.inf deletes OEM INF + removes device + scans devices.
    zadig_nodes = _sample_nodes(dongle_service="WinUSB", dongle_inf="oem42.inf", dongle_class="USBDevice")
    pnp_calls: list[tuple[str, ...]] = []
    cleared: list[str] = []
    res_restore = drv.execute_driver_action(
        "restore",
        [r"USB\VID_0BDA&PID_8771\00E04C239987"],
        nodes_provider=lambda: zadig_nodes,
        pnputil_fn=lambda *a: (pnp_calls.append(a) or (0, "ok")),
        clear_reg_fn=cleared.append,
        require_admin=False,
        platform="win32",
    )
    assert res_restore["ok"] is True
    assert cleared == [r"USB\VID_0BDA&PID_8771\00E04C239987"]
    assert ("/remove-device", r"USB\VID_0BDA&PID_8771\00E04C239987") in pnp_calls
    assert ("/delete-driver", "oem42.inf", "/uninstall", "/force") in pnp_calls
    assert ("/scan-devices",) in pnp_calls

    # Restore inbox winusb.inf NEVER runs /delete-driver on winusb.inf!
    inbox_nodes = _sample_nodes(dongle_service="WinUSB", dongle_inf="winusb.inf", dongle_class="USBDevice")
    pnp_calls_inbox: list[tuple[str, ...]] = []
    res_restore_inbox = drv.execute_driver_action(
        "restore",
        [r"USB\VID_0BDA&PID_8771\00E04C239987"],
        nodes_provider=lambda: inbox_nodes,
        pnputil_fn=lambda *a: (pnp_calls_inbox.append(a) or (0, "ok")),
        clear_reg_fn=lambda _inst: None,
        require_admin=False,
        platform="win32",
    )
    assert res_restore_inbox["ok"] is True
    assert not any(call[0] == "/delete-driver" for call in pnp_calls_inbox)
    assert ("/scan-devices",) in pnp_calls_inbox


def test_device_probe_one_click_ble_dongle_bind_restore_and_uac_cancel(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("remotehub.device_probe.dongle_route_available", lambda: True)
    unbound_dev = drv.ManagedUSBDevice(
        instance_id=r"USB\VID_0BDA&PID_8771\00E04C239987",
        vendor=0x0BDA,
        product=0x8771,
        kind="ble_dongle",
        service="BTHUSB",
        name="Generic Bluetooth Radio",
        driver_inf="bth.inf",
        device_class="Bluetooth",
    )
    bound_dev = drv.ManagedUSBDevice(
        instance_id=r"USB\VID_0BDA&PID_8771\00E04C239987",
        vendor=0x0BDA,
        product=0x8771,
        kind="ble_dongle",
        service="WinUSB",
        name="WinUSB 设备",
        driver_inf="winusb.inf",
        device_class="USBDevice",
    )
    state = {"dev": unbound_dev}
    reader = BLEDongleReader(
        state_dir=tmp_path / "ble",
        dongle_provider=lambda: [],
        unbound_provider=lambda: [],
        managed_provider=lambda: [state["dev"]],
    )
    dialog = DeviceProbeDialog(reader, install=lambda *_a: True)
    qtbot.addWidget(dialog)
    dialog.show()

    # Initially unbound -> picker lists the dongle, Bind visible, Scan/Restore hidden.
    assert dialog.ble_dongle_card.isVisibleTo(dialog)
    assert dialog.ble_dongle_picker.count() == 1
    assert "0BDA:8771" in dialog.ble_dongle_picker.currentText()
    assert dialog.ble_dongle_bind.isVisibleTo(dialog)
    assert not dialog.ble_dongle_scan.isVisibleTo(dialog)
    assert not dialog.ble_dongle_restore.isVisibleTo(dialog)

    # UAC cancel shows calm feedback without error crash.
    monkeypatch.setattr(
        drv,
        "launch_elevated_action",
        lambda _a, _ids, **_k: (_ for _ in ()).throw(drv.DriverActionCancelled("已取消操作，设备未做任何改动。")),
    )
    dialog.ble_dongle_bind.click()
    assert "已取消操作" in dialog.feedback.text()

    # Successful bind transitions cleanly to Scan + Restore buttons visible.
    res_file = tmp_path / "res.json"
    res_file.write_text(json.dumps({"ok": True, "message": "已切换为 BLE 直连模式！"}), encoding="utf-8")
    launched: list[tuple[str, list[str]]] = []

    def fake_launch(action, ids, **_k):
        launched.append((action, list(ids)))
        state["dev"] = bound_dev
        return 999, res_file

    monkeypatch.setattr(drv, "launch_elevated_action", fake_launch)
    monkeypatch.setattr(drv, "poll_elevated_process", lambda _h: (True, 0))

    dialog.ble_dongle_bind.click()
    dialog._poll_win_driver_action()
    assert launched == [("bind", [r"USB\VID_0BDA&PID_8771\00E04C239987"])]
    assert "BLE 直连模式" in dialog.feedback.text()
    assert not dialog.ble_dongle_bind.isVisibleTo(dialog)
    assert dialog.ble_dongle_scan.isVisibleTo(dialog)
    assert dialog.ble_dongle_restore.isVisibleTo(dialog)
    dialog.close()


def test_device_probe_clean_empty_state_collapsed_diagnostics_and_macos_libusb_only(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("remotehub.device_probe.dongle_route_available", lambda: True)
    # Empty state with NO external dongle: clean empty card, hidden blank picker, hidden dongle card, collapsed drawer.
    empty_reader = BLEDongleReader(
        state_dir=tmp_path / "ble-empty",
        dongle_provider=lambda: [],
        unbound_provider=lambda: [],
        managed_provider=lambda: [],
    )
    dialog = DeviceProbeDialog(empty_reader, install=lambda *_a: True)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.windowTitle() == "添加遥控器"
    assert not dialog.picker_row.isVisibleTo(dialog)
    assert dialog.empty_card.isVisibleTo(dialog)
    assert not dialog.ble_dongle_card.isVisibleTo(dialog)
    assert dialog.diag_toggle.isVisibleTo(dialog)
    assert not dialog.diag_drawer.isVisibleTo(dialog)
    dialog.diag_toggle.click()
    assert dialog.diag_drawer.isVisibleTo(dialog)
    # Verify long unlisted item text never stretches drawer wider than scroll viewport (no right-edge text clipping!).
    dialog.unlisted_box.addItem("测试超长设备名称 · 蓝牙 · 0000:0000 （只有 GATT 的蓝牙）" * 2, ("case", "gatt_only"))
    dialog.unlisted_box.setCurrentIndex(dialog.unlisted_box.count() - 1)
    qtbot.wait(20)
    assert dialog.diag_drawer.width() <= dialog.scroll_area.viewport().width()
    dialog.close()

    # macOS direct libusb dongle: shows ONLY Scan button — never Windows Bind/Restore buttons!
    mac_dongle = drv.ManagedUSBDevice(
        instance_id="usb:0bda:8771:1:4",
        vendor=0x0BDA,
        product=0x8771,
        kind="ble_dongle",
        service="libusb",
        name="USB BT 0BDA:8771",
    )
    mac_reader = BLEDongleReader(
        state_dir=tmp_path / "ble-mac",
        dongle_provider=lambda: [],
        unbound_provider=lambda: [],
        managed_provider=lambda: [mac_dongle],
    )
    mac_dialog = DeviceProbeDialog(mac_reader, install=lambda *_a: True)
    qtbot.addWidget(mac_dialog)
    mac_dialog.show()
    assert mac_dialog.ble_dongle_card.isVisibleTo(mac_dialog)
    assert mac_dialog.ble_dongle_scan.isVisibleTo(mac_dialog)
    assert not mac_dialog.ble_dongle_bind.isVisibleTo(mac_dialog)
    assert not mac_dialog.ble_dongle_restore.isVisibleTo(mac_dialog)
    mac_dialog.close()


def test_blocked_ble_remote_detected_in_unified_picker_and_guides_to_dongle(qtbot, tmp_path):
    from types import SimpleNamespace
    from remotehub.windows_probe_metadata import BlockedBLERemote, WindowsProbeMetadata

    # 1. Precision filter: ONLY 0x1812 with problem != 0 is returned; healthy 0x1812 & audio services ignored.
    meta = object.__new__(WindowsProbeMetadata)
    instances = [
        r"BTHLEDevice\{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&010416_PID&0300_REV&0110_0cf3defcc5c1\9&1&0&0021",
        r"BTHLEDevice\{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&0105ac_PID&0255_112233445566\9&2&0&0021",
        r"BTHLEDevice\{0000180a-0000-1000-8000-00805f9b34fb}_Dev_VID&010416_PID&0300_0cf3defcc5c1\9&1&0&0010",
    ]
    buf_text = "\0".join(instances) + "\0\0"
    node_map = {instances[0]: 101, instances[1]: 102, instances[2]: 103}
    problems = {101: 10, 102: 0, 103: 10}
    names = {201: "BLE 语音遥控器", 202: "Healthy Keyboard"}

    def fake_size(size_ptr, enum, _flags):
        size_ptr._obj.value = len(buf_text) if enum == "BTHLEDEVICE" else 0
        return 0 if enum == "BTHLEDEVICE" else 1

    def fake_list(enum, buf, _size, _flags):
        if enum == "BTHLEDEVICE":
            buf.value = buf_text
            return 0
        return 1

    def fake_status(status_ptr, prob_ptr, node, _flags):
        status_ptr._obj.value = 0x1802400
        prob_ptr._obj.value = problems.get(node, 0)
        return 0

    meta.cfg = SimpleNamespace(
        CM_Get_Device_ID_List_SizeW=fake_size,
        CM_Get_Device_ID_ListW=fake_list,
        CM_Get_DevNode_Status=fake_status,
        CM_Get_Parent=object(),
    )
    meta._node = lambda inst: node_map.get(inst)
    meta._relate = lambda _fn, node: node + 100
    meta._name = lambda node: names.get(node, "")
    blocked = meta.blocked_ble_remotes()
    assert blocked == [BlockedBLERemote(name="BLE 语音遥控器", vendor=0x0416, product=0x0300, problem_code=10,
                                        address="0C:F3:DE:FC:C5:C1")]

    # 2. Unified picker shows the blocked BLE remote directly (never hides it behind "暂未检测到")!
    class _BlockedReader(BLEDongleReader):
        def blocked_ble_remotes(self):
            return list(blocked)

    reader = _BlockedReader(
        state_dir=tmp_path / "ble-blocked",
        dongle_provider=lambda: [],
        unbound_provider=lambda: [],
        managed_provider=lambda: [],
    )
    dialog = DeviceProbeDialog(reader, install=lambda *_a: True)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.picker_row.isVisibleTo(dialog)
    assert dialog.picker.count() == 1
    assert "BLE 语音遥控器" in dialog.picker.currentText() and "自带蓝牙无法读取" in dialog.picker.currentText()
    assert dialog.selected_device() is None
    assert dialog.selected_blocked_ble() == blocked[0]
    assert not dialog.empty_card.isVisibleTo(dialog)
    assert dialog.blocked_ble_card.isVisibleTo(dialog)
    assert not dialog.ble_dongle_card.isVisibleTo(dialog)
    dialog.close()


# -- one-click takeover of any USB receiver the user picks -------------------------------

def _receiver_nodes(*, bound: bool = False):
    """A composite remote receiver 1234:5678: keyboard, mouse, consumer+vendor, audio."""
    service = "WinUSB" if bound else "HidUsb"
    base = r"USB\VID_1234&PID_5678"
    return [
        {"instance_id": base + r"\5&1&0&1", "vendor": 0x1234, "product": 0x5678, "service": "usbccgp",
         "device_class": "USB", "compat": "USB\\DEVCLASS_00&SUBCLASS_00&PROT_00", "name": "USB 复合设备"},
        {"instance_id": base + r"&MI_00\6&1&0&0000", "vendor": 0x1234, "product": 0x5678, "service": service,
         "device_class": "HIDClass", "compat": "USB\\CLASS_03&SUBCLASS_01&PROT_01", "name": "键盘接口",
         "hid_usages": [] if bound else ["0001:0006"]},
        {"instance_id": base + r"&MI_01\6&1&0&0001", "vendor": 0x1234, "product": 0x5678, "service": "HidUsb",
         "device_class": "HIDClass", "compat": "USB\\CLASS_03&SUBCLASS_00&PROT_00", "name": "空鼠接口",
         "hid_usages": ["0001:0002", "000C:0001"]},
        {"instance_id": base + r"&MI_02\6&1&0&0002", "vendor": 0x1234, "product": 0x5678, "service": service,
         "device_class": "HIDClass", "compat": "USB\\CLASS_03&SUBCLASS_00&PROT_00", "name": "媒体与厂商接口",
         "hid_usages": [] if bound else ["000C:0001", "FF00:0001"]},
        {"instance_id": base + r"&MI_03\6&1&0&0003", "vendor": 0x1234, "product": 0x5678, "service": "usbaudio",
         "device_class": "MEDIA", "compat": "USB\\CLASS_01&SUBCLASS_01&PROT_00", "name": "USB 麦克风"},
    ]


def test_takeover_picks_key_and_vendor_interfaces_but_leaves_pointer_and_audio():
    assert drv.hid_usages_from_ids("HID_DEVICE_SYSTEM_MOUSE\0HID_DEVICE_UP:000C_U:0001\0HID_DEVICE") == [
        "0001:0002", "000C:0001"]
    _, receivers = drv.classify_managed_devices(_receiver_nodes() + _sample_nodes(), platform="win32")
    picked = drv.receiver_interfaces(receivers, 0x1234, 0x5678)
    # The pointer interface keeps moving the cursor without the app; the standard microphone stays a
    # system microphone; the composite parent is never switched itself.
    assert [d.interface for d in picked] == [0, 2]
    assert all(d.kind == "receiver" for d in picked)
    # The bundled receiver keeps its proven interfaces only.
    assert [d.interface for d in drv.receiver_interfaces(receivers, 0x0416, 0x0602)] == [3, 4]
    # Interfaces already switched stay listed (USB class IDs survive the switch) so they can be restored.
    _, bound = drv.classify_managed_devices(_receiver_nodes(bound=True), platform="win32")
    assert [(d.interface, d.restorable) for d in bound] == [(0, True), (2, True)]
    # A single-interface receiver without composite parent is taken whole.
    single = {"instance_id": r"USB\VID_AAAA&PID_0001\SERIAL", "vendor": 0xAAAA, "product": 0x0001,
              "service": "HidUsb", "compat": "USB\\CLASS_03&SUBCLASS_00&PROT_00", "hid_usages": ["000C:0001"]}
    _, only = drv.classify_managed_devices([single], platform="win32")
    assert [(d.vendor, d.interface) for d in only] == [(0xAAAA, None)]


def test_takeover_refuses_to_leave_the_computer_without_a_keyboard_and_mixed_receivers():
    nodes = _receiver_nodes() + _sample_nodes()
    ids = [r"USB\VID_1234&PID_5678&MI_00\6&1&0&0000", r"USB\VID_1234&PID_5678&MI_02\6&1&0&0002"]
    bound: list[str] = []
    own_keyboard = [r"HID\VID_1234&PID_5678&MI_00\7&2&0&0000"]
    res = drv.execute_driver_action("bind", ids, nodes_provider=lambda: nodes, bind_fn=lambda d: bound.append(d.instance_id),
                                    keyboards_provider=lambda: own_keyboard, require_admin=False, platform="win32")
    assert res["ok"] is False and "唯一的键盘" in res["error"] and not bound
    mixed = drv.execute_driver_action("bind", [ids[0], r"USB\VID_0416&PID_0602&MI_04\7&2222&0&0004"],
                                      nodes_provider=lambda: nodes, bind_fn=lambda d: False,
                                      keyboards_provider=lambda: ["ACPI\\PNP0303\\4&1"], require_admin=False,
                                      platform="win32")
    assert mixed["ok"] is False and "一次只能接管一种" in mixed["error"]


@pytest.mark.parametrize("answer, kept", [("keep", True), ("revert", False), ("", False)])
def test_takeover_waits_for_confirmation_and_restores_by_itself(tmp_path, answer, kept):
    nodes = _receiver_nodes() + _sample_nodes()
    ids = [r"USB\VID_1234&PID_5678&MI_00\6&1&0&0000", r"USB\VID_1234&PID_5678&MI_02\6&1&0&0002"]
    confirm = tmp_path / "result.json.confirm"
    stages, bound, pnp = [], [], []

    def on_stage(data):
        stages.append(data)
        if answer:
            confirm.write_text(answer, encoding="utf-8")

    res = drv.execute_driver_action(
        "bind", ids, nodes_provider=lambda: nodes, bind_fn=lambda d: bound.append(d.instance_id) or False,
        pnputil_fn=lambda *a: (pnp.append(a) or (0, "ok")), clear_reg_fn=lambda _i: None,
        keyboards_provider=lambda: ["ACPI\\PNP0303\\4&1"], confirm_file=confirm, confirm_seconds=1,
        on_stage=on_stage, sleep_fn=lambda _s: None, require_admin=False, platform="win32",
    )
    assert bound == ids
    assert stages and stages[0]["stage"] == "awaiting_confirm"
    assert res["ok"] is kept
    removed = [call[1] for call in pnp if call[0] == "/remove-device"]
    if kept:
        assert not removed and "已由 App 接管" in res["message"]
    else:
        # Rolled back inside the elevated helper: no second elevation prompt is needed.
        assert removed == ids and res["rolled_back"] is True and ("/scan-devices",) in pnp


def test_add_dialog_offers_takeover_for_any_picked_usb_device(qtbot, monkeypatch):
    from remotehub.probe_device import ProbeDevice, UnlistedDevice
    import remotehub.device_probe as probe_mod

    monkeypatch.setattr(probe_mod.sys, "platform", "win32")
    _, receivers = drv.classify_managed_devices(_receiver_nodes(), platform="win32")
    monkeypatch.setattr(drv, "list_managed_devices", lambda **_: ([], receivers))
    readable = ProbeDevice(("usb", 1), "遥控器接收器", "USB", 0x1234, 0x5678, "u" * 64, (1,))

    class Reader:
        unlisted = [UnlistedDevice("读不了的接收器", "USB", 0x1234, 0x5678, hid=False),
                    UnlistedDevice("U 盘", "USB", 0x0951, 0x1666, hid=False)]

        def enumerate(self):
            return [readable]

        def start(self, *_a, **_k):
            return 1, 1

        def stop(self):
            pass

        close = stop

    dialog = probe_mod.DeviceProbeDialog(Reader(), install=lambda *_a: True)
    qtbot.addWidget(dialog)
    dialog.show()
    # The user decides which device is the remote: no key press is required to offer it.
    assert dialog.selected_device() == readable
    assert dialog.receiver_bind.isVisibleTo(dialog) and "App 没有运行时" in dialog.receiver_note.text()

    launched = []
    monkeypatch.setattr(drv, "launch_elevated_action",
                        lambda action, ids, **kw: launched.append((action, list(ids), kw)) or (0, None))
    monkeypatch.setattr(drv, "poll_elevated_process", lambda _h: (False, 0))
    dialog.receiver_bind.click()
    assert launched == [("bind", [r"USB\VID_1234&PID_5678&MI_00\6&1&0&0000",
                                  r"USB\VID_1234&PID_5678&MI_02\6&1&0&0002"], {"confirm": True})]
    dialog._driver_poll_timer.stop()
    dialog._driver_pending = None

    # Unreadable devices are offered too, picked from the troubleshooting list.
    dialog.open_not_listed()
    dialog.unlisted_box.setCurrentIndex(dialog.unlisted_box.findText("读不了的接收器", Qt.MatchFlag.MatchStartsWith))
    assert dialog.takeover_pair() == (0x1234, 0x5678) and dialog.receiver_bind.isVisibleTo(dialog)
    dialog.unlisted_box.setCurrentIndex(dialog.unlisted_box.findText("U 盘", Qt.MatchFlag.MatchStartsWith))
    assert not dialog.receiver_bind.isVisibleTo(dialog) and "没有 App 能接管" in dialog.receiver_note.text()
    dialog.close()


def test_parse_usb_vid_pid_accepts_composite_mi_interfaces_and_enrollment_help_matches_button():
    from remotehub.enrollment import driver_help_text

    # Regression for v0.3.0 bug: windows_probe_metadata._usb_pair() intentionally rejects &MI_XX,
    # so win_usb_driver must parse VID:PID from composite interfaces directly.
    assert drv._parse_usb_vid_pid(r"USB\VID_0416&PID_0602&MI_03\7&2222&0&0003") == (0x0416, 0x0602)
    assert drv._parse_usb_vid_pid(r"USB\VID_0416&PID_0602&MI_04\7&2222&0&0004") == (0x0416, 0x0602)
    assert drv._parse_usb_vid_pid(r"USB\VID_0416&PID_0602\6&1111&0&1") == (0x0416, 0x0602)
    assert drv._parse_usb_vid_pid(r"USB\VID_0BDA&PID_8771\00E04C239987") == (0x0BDA, 0x8771)
    assert drv._parse_usb_vid_pid(r"HID\VID_0416&PID_0602&MI_04\8&1&0&0000") is None

    win_help = driver_help_text("Windows")
    assert "让 App 接管这个接收器" in win_help
    assert "一键启用专用接收器" not in win_help
