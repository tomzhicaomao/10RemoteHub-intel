"""Tests for per-device macOS hidutil key suppression and lifecycle."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

from remotehub.mac_hid_suppress import (
    APPLE_VENDOR,
    NULL_USAGE,
    MacHIDSuppressor,
    build_matching,
    trigger_to_hid_usage,
    usages_from_triggers,
)
from remotehub.model import Button, Profile
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import Connection, Remote


def _hidutil_output(usages):
    if not usages:
        return "RegistryID Key Value\n100f11232 UserKeyMapping (\n)\n"
    entries = "\n".join(
        f"    {{\n        HIDKeyboardModifierMappingDst = {NULL_USAGE};\n        HIDKeyboardModifierMappingSrc = {u};\n    }},"
        for u in usages
    )
    return f"RegistryID Key Value\n100f11232 UserKeyMapping (\n{entries}\n)\n"


def test_trigger_conversion_only_accepts_keyboard_and_consumer_pages():
    assert trigger_to_hid_usage("hid:7:82") == 0x700000052
    assert trigger_to_hid_usage("hid:12:233") == 0xC000000E9
    assert trigger_to_hid_usage("hid:7:0") is None
    assert trigger_to_hid_usage("hid:1:128") is None
    assert trigger_to_hid_usage("hid:9:1") is None
    assert trigger_to_hid_usage("win:30:0") is None
    assert trigger_to_hid_usage("hid:7:70000") is None
    assert usages_from_triggers(["hid:12:233", "hid:7:82", "hid:12:233", "win:10:0"]) == (
        0x700000052,
        0xC000000E9,
    )


def test_build_matching_refuses_apple_and_built_in_devices():
    assert build_matching(APPLE_VENDOR, 0x027B) is None
    assert build_matching(0x620A, 0x0407, built_in=True) is None
    assert build_matching(0, 0x0407) is None
    assert build_matching(0x620A, 0) is None
    assert build_matching(0x620A, 0x0407, transport="Bluetooth Low Energy", location_id=0x13C72B43) == {
        "VendorID": 0x620A,
        "ProductID": 0x0407,
        "Transport": "Bluetooth Low Energy",
        "LocationID": 0x13C72B43,
    }


def test_apply_verify_ensure_and_clear_lifecycle(tmp_path):
    calls = []
    state = {"active": []}

    def runner(args):
        calls.append(args)
        if "--set" in args:
            payload = json.loads(args[args.index("--set") + 1])
            state["active"] = [item["HIDKeyboardModifierMappingSrc"] for item in payload["UserKeyMapping"]]
            return 0, _hidutil_output(state["active"]), ""
        if "--get" in args:
            return 0, _hidutil_output(state["active"]), ""
        return 1, "", "unexpected"

    lease = tmp_path / "mac-hid-takeover.json"
    suppressor = MacHIDSuppressor(state_path=lease, runner=runner)
    assert suppressor.apply(
        vendor=0x620A,
        product=0x0407,
        triggers=("hid:7:82", "hid:12:233"),
        transport="Bluetooth Low Energy",
        location_id=0x13C72B43,
    )
    assert suppressor.active and suppressor.active_usages == (0x700000052, 0xC000000E9)
    assert lease.exists()
    assert json.loads(lease.read_text(encoding="utf-8"))["matching"]["LocationID"] == 0x13C72B43

    # Still intact -> ensure() only runs --get.
    before = len(calls)
    assert suppressor.ensure() is True
    assert len(calls) == before + 1 and "--get" in calls[-1]

    # Simulate macOS clearing UserKeyMapping after sleep/wake -> ensure() re-applies.
    state["active"] = []
    assert suppressor.ensure() is True
    assert state["active"] == [0x700000052, 0xC000000E9]

    # Clean release clears UserKeyMapping and removes lease file.
    suppressor.clear()
    assert not suppressor.active and state["active"] == [] and not lease.exists()


def test_apply_falls_back_when_location_id_matches_no_service():
    matched_filters = []

    def runner(args):
        matching = json.loads(args[args.index("--matching") + 1])
        if "--set" in args:
            matched_filters.append(matching)
            if "LocationID" in matching:
                return 0, "", ""
            payload = json.loads(args[args.index("--set") + 1])
            usages = [item["HIDKeyboardModifierMappingSrc"] for item in payload["UserKeyMapping"]]
            return 0, _hidutil_output(usages), ""
        return 0, "", ""

    suppressor = MacHIDSuppressor(runner=runner)
    assert suppressor.apply(
        vendor=0x1915,
        product=0x1025,
        triggers=("hid:12:233",),
        transport="USB",
        location_id=0x20334400,
    )
    assert matched_filters == [
        {"VendorID": 0x1915, "ProductID": 0x1025, "Transport": "USB", "LocationID": 0x20334400},
        {"VendorID": 0x1915, "ProductID": 0x1025, "Transport": "USB"},
    ]


def test_cleanup_stale_clears_dead_pid_lease_and_respects_live_pid(tmp_path, monkeypatch):
    from remotehub import mac_hid_suppress

    cleared = []

    def runner(args):
        if "--set" in args:
            cleared.append(json.loads(args[args.index("--matching") + 1]))
        return 0, "", ""

    lease = tmp_path / "mac-hid-takeover.json"
    lease.write_text(
        json.dumps({"pid": 999999, "matching": {"VendorID": 0x620A, "ProductID": 0x0407, "LocationID": 123}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mac_hid_suppress, "_pid_alive", lambda pid: pid == 424242)

    # Dead PID: cleaned up immediately on startup.
    MacHIDSuppressor(state_path=lease, runner=runner)
    assert not lease.exists()
    assert {"VendorID": 0x620A, "ProductID": 0x0407, "LocationID": 123} in cleared
    assert {"VendorID": 0x620A, "ProductID": 0x0407} in cleared

    # Live other PID: untouched.
    cleared.clear()
    lease.write_text(
        json.dumps({"pid": 424242, "matching": {"VendorID": 0x620A, "ProductID": 0x0407}}),
        encoding="utf-8",
    )
    MacHIDSuppressor(state_path=lease, runner=runner)
    assert lease.exists() and not cleared

    # Tampered Apple VID in dead lease: refused, never calls hidutil, still unlinks file.
    lease.write_text(
        json.dumps({"pid": 999999, "matching": {"VendorID": APPLE_VENDOR, "ProductID": 0x027B}}),
        encoding="utf-8",
    )
    MacHIDSuppressor(state_path=lease, runner=runner)
    assert not lease.exists() and not cleared


def test_remote_view_distinguishes_full_partial_failed_and_empty_takeover(tmp_path, qapp):
    from remotehub.controller import AppController
    from remotehub.remote_library import RemoteLibraryStore

    remote = Remote(
        name="蓝牙遥控器",
        connections=[Connection("蓝牙", 0x620A, 0x0407)],
        profiles=[Profile(name="默认", buttons=[Button(name="音量+", trigger="hid:12:233")])],
    )
    store = RemoteLibraryStore(tmp_path)
    store.add(remote)
    controller = AppController(tmp_path, store=store, auto_mapping=False)
    try:
        device = ProbeDevice(("hid", 1), "遥控器 620A:0407", "蓝牙", 0x620A, 0x0407)
        reader = SimpleNamespace(take_over=False, seized=1, seize_error="", scope="")
        controller._runtimes["hid"] = SimpleNamespace(reader=reader, device=device)

        assert "已接管原按键" in controller.remote_view().detail

        reader.seize_error = "部分失败"
        assert "仅部分接管原按键" in controller.remote_view().detail

        reader.seized = 0
        assert "系统不允许接管" in controller.remote_view().detail and "部分失败" in controller.remote_view().detail

        reader.seize_error = ""
        assert "尚未学入可接管的按键" in controller.remote_view().detail
    finally:
        controller._runtimes.clear()
        controller.shutdown()


def test_scan_hid_passes_remote_triggers_including_extra_and_syncs_updates(tmp_path, qapp):
    from remotehub.controller import AppController
    from remotehub.remote_library import RemoteLibraryStore

    device = ProbeDevice(("hid", 1), "遥控器 620A:0407", "蓝牙", 0x620A, 0x0407)
    remote = Remote(
        name="蓝牙遥控器",
        connections=[Connection("蓝牙", 0x620A, 0x0407)],
        profiles=[
            Profile(
                name="默认",
                buttons=[
                    Button(name="上", trigger="hid:7:82", extra_triggers=["hid:7:81"]),
                    Button(name="音量+", trigger="hid:12:233"),
                ],
            )
        ],
    )

    class FakeHIDReader:
        take_over = False
        take_over_triggers = ()
        seized = 0
        seize_error = ""

        def __init__(self):
            self.started_triggers = []
            self.synced_triggers = []

        def enumerate(self):
            return [device]

        def peek(self):
            return [device]

        def start(self, identity, on_key, on_removed, *, expected=None):
            self.started_triggers.append((self.take_over, self.take_over_triggers))
            self.seized = len(self.take_over_triggers)
            return 1, 1

        def sync_takeover(self, triggers):
            self.synced_triggers.append(tuple(triggers))
            self.seized = len(triggers)
            return True

        def ensure_takeover(self):
            return False

        def stop(self):
            pass

        def close(self):
            pass

    store = RemoteLibraryStore(tmp_path)
    store.add(remote)
    controller = AppController(tmp_path, store=store, auto_mapping=False)
    reader = FakeHIDReader()
    controller.hid_reader = reader
    try:
        controller.scan_hid()
        assert reader.started_triggers == [(True, ("hid:7:82", "hid:7:81", "hid:12:233"))]
        assert "已接管原按键" in controller.remote_view().detail

        # Adding a key while connected syncs suppression on next scan_hid().
        controller.remote.profiles[0].buttons.append(Button(name="音量-", trigger="hid:12:234"))
        controller.scan_hid()
        assert reader.synced_triggers[-1] == ("hid:7:82", "hid:7:81", "hid:12:233", "hid:12:234")
    finally:
        controller.shutdown()
