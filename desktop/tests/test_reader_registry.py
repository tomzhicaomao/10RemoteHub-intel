"""Proof tests for KeyReader protocol, ReaderRegistry, CompositeReader, and mutual exclusion."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from remotehub.controller import AppController
from remotehub.device_probe import DeviceProbeDialog
from remotehub.inventory import USBDevice
from remotehub.model import Action, Button, Profile
from remotehub.photo_template import television_profile
from remotehub.probe_device import ProbeDevice
from remotehub.readers import (
    CompositeReader, KeyReader, ReaderEntry, ReceiverReader, default_registry, peek_devices,
)
from remotehub.remote_library import Connection, RECEIVER_MODEL, Remote, RemoteLibraryStore, remote_for_device


class FakeRegisteredReader:
    """In-memory KeyReader test double with peek, scope, and mutual-exclusion telemetry."""

    def __init__(self, scope_text: str, *devices: ProbeDevice, events: list[str] | None = None):
        self.scope = scope_text
        self.devices = list(devices)
        self.peek_devices = list(devices)
        self.expected = None
        self.on_key = None
        self.on_removed = None
        self.active = False
        self.closed = False
        self.events = events if events is not None else []
        self.name = scope_text

    def enumerate(self):
        self.stop()
        return list(self.devices)

    def peek(self):
        return list(self.peek_devices)

    def start(self, identity, on_key, on_removed, *, expected=None):
        match = next((d for d in self.devices if d.id == identity), None)
        if match is None or (expected is not None and match != expected):
            raise RuntimeError("设备不在列表中")
        self.expected = match
        self.on_key = on_key
        self.on_removed = on_removed
        self.active = True
        self.events.append(f"start:{self.name}:{match.name}")
        return 1, 1

    def tap(self, trigger: str):
        assert self.on_key is not None and self.active
        self.on_key(("ch", 0), trigger, True)
        self.on_key(("ch", 0), trigger, False)

    def stop(self):
        if self.active:
            self.events.append(f"stop:{self.name}")
        self.active = False
        self.expected = None
        self.on_key = None
        self.on_removed = None

    def close(self):
        self.stop()
        self.closed = True


def _make_remote(name: str, dev: ProbeDevice, trigger: str, *, model_id: str = "", mic: bool = False) -> Remote:
    prof = Profile(
        name=name,
        vendor=dev.vendor,
        product=dev.product,
        image=television_profile().image,
        buttons=[
            Button(
                id="k1",
                name="确认",
                trigger=trigger,
                action=Action("text", text=f"fired:{name}"),
                rect=[0.1, 0.2, 0.2, 0.1],
            ),
        ],
    )
    return Remote(
        name=name,
        connections=[Connection(dev.transport, dev.vendor, dev.product)],
        profiles=[prof],
        model_id=model_id,
        voice={"voice_source": "remote"} if mic else {},
        active_profile_id=prof.id,
    )


def test_builtin_registry_declares_receiver_and_hid_and_satisfies_key_reader_protocol():
    reg = default_registry()
    entries = reg.entries()
    ids = [entry.id for entry in entries]
    assert "receiver" in ids and "hid" in ids
    assert ids.index("receiver") < ids.index("hid")

    rec_conn = Connection("USB", 0x0416, 0x0602)
    generic_bt = Connection("蓝牙", 0x620A, 0x0407)
    assert reg.primary_entry_for_connection(rec_conn).id == "receiver"
    assert reg.primary_entry_for_connection(generic_bt).id == "hid"

    fixed_remote = Remote("配套", [rec_conn], [television_profile()], model_id=RECEIVER_MODEL)
    other_remote = Remote("非配套", [rec_conn], [Profile(name="自定义", vendor=0x0416, product=0x0602)])
    bt_remote = Remote("蓝牙", [generic_bt], [Profile(name="蓝牙", vendor=0x620A, product=0x0407)])
    assert reg.supports_remote_mic(fixed_remote) is True
    assert reg.supports_remote_mic(other_remote) is False
    assert reg.supports_remote_mic(bt_remote) is False

    # ReceiverReader satisfies KeyReader protocol and forwards Collector keys cleanly.
    class FakeCollector(QObject):
        key = Signal(str, bool)
        stopped = Signal(int)

        def __init__(self):
            super().__init__()
            self.generation = 7
            self.started = 0
            self.stop_count = 0

        def start(self, profile, host=None):
            self.started += 1

        def stop(self):
            self.stop_count += 1
            return True

    collector = FakeCollector()
    usb_item = USBDevice(0x0416, 0x0602, "SER123", "USB 0416:0602", "port-1")
    monitor = SimpleNamespace(devices=[usb_item])
    reader = ReceiverReader(collector, monitor)
    assert isinstance(reader, KeyReader)
    listed = reader.enumerate()
    assert len(listed) == 1 and (listed[0].vendor, listed[0].product) == (0x0416, 0x0602)
    assert peek_devices(reader) == listed

    events = []
    removed = []
    assert reader.start(listed[0].id, lambda *a: events.append(a), lambda: removed.append(True), expected=listed[0]) == (1, 1)
    assert collector.started == 1
    collector.key.emit("hid:7:40", True)
    assert events == [(("receiver", 0), "hid:7:40", True)]
    collector.stopped.emit(7)
    assert removed == [True]
    reader.close()
    assert collector.stop_count >= 1


@pytest.mark.parametrize("with_remote_mic", [False, True])
def test_custom_fake_reader_registration_drives_controller_without_controller_changes(qtbot, tmp_path, with_remote_mic):
    gatt_dev = ProbeDevice(("gatt", "CAFE:0001"), "BLE FIBER0D", "蓝牙", 0xCAFE, 0x0001, "g" * 64, (1,))
    alt_bt = ProbeDevice(("hid", 9), "蓝牙 HID 1234:9999", "蓝牙", 0x1234, 0x9999, "", (2,))
    fake_reader = FakeRegisteredReader("私有蓝牙协议", gatt_dev)
    assert isinstance(fake_reader, KeyReader)

    registry = default_registry().clone()
    registry.register(
        ReaderEntry(
            id="fake-gatt",
            name="私有蓝牙协议",
            handles=lambda conn: conn.transport == "蓝牙" and (conn.vendor, conn.product) == (0xCAFE, 0x0001),
            factory=lambda: fake_reader,
            remote_mic=with_remote_mic,
        )
    )

    c = AppController(tmp_path, auto_mapping=False, readers=registry)
    remote = _make_remote("FIBER0D", gatt_dev, "hid:12:65", mic=with_remote_mic)
    assert c.add_remote(remote, gatt_dev)
    assert c.input_path() == "fake-gatt"
    assert c.uses_dedicated_reader() is False
    assert c.supports_remote_mic is with_remote_mic
    assert c.hid_device == gatt_dev
    assert fake_reader.active is True

    # Remote status reflects custom reader metadata & scope cleanly.
    view = c.remote_view()
    assert view.tone == "ok"
    assert "私有蓝牙协议" in view.detail

    # Key presses arriving from the custom registered reader dispatch through AppController.
    sent = []
    c.keys.input_text = sent.append
    fake_reader.tap("hid:12:65")
    qtbot.waitUntil(lambda: sent == ["fired:FIBER0D"])

    # Read-only peek() while active refreshes controller.discovered without interrupting active reader.
    fake_reader.peek_devices = [gatt_dev, alt_bt]
    c.scan_hid()
    assert fake_reader.active is True
    assert alt_bt in c.discovered

    c.shutdown()
    assert fake_reader.closed is True


def test_mutual_exclusion_single_active_reader_path_across_switches(qtbot, tmp_path, monkeypatch):
    rec_usb = USBDevice(0x0416, 0x0602, "REC1", "USB 0416:0602", "port-rec")
    hid_dev = ProbeDevice(("usb", 2), "USB 1234:5678", "USB", 0x1234, 0x5678, "h" * 64, (1,))
    gatt_dev = ProbeDevice(("gatt", "CAFE:0002"), "BLE CAFE:0002", "蓝牙", 0xCAFE, 0x0002, "g" * 64, (2,))
    events: list[str] = []

    hid_reader = FakeRegisteredReader("HID", hid_dev, events=events)
    gatt_reader = FakeRegisteredReader("GATT", gatt_dev, events=events)
    registry = default_registry().clone()
    registry.register(
        ReaderEntry(
            id="fake-gatt",
            name="GATT",
            handles=lambda conn: (conn.vendor, conn.product) == (0xCAFE, 0x0002),
            factory=lambda: gatt_reader,
        )
    )

    store = RemoteLibraryStore(tmp_path)
    r_rec = _make_remote("配套", ProbeDevice(("rec",), "0416:0602", "USB", 0x0416, 0x0602), "hid:7:40", model_id=RECEIVER_MODEL)
    r_hid = _make_remote("通用 HID", hid_dev, "hid:7:82")
    r_gatt = _make_remote("私有蓝牙", gatt_dev, "hid:12:65")
    store.remotes = [r_rec, r_hid, r_gatt]
    store.active = r_rec.id
    store.save()

    c = AppController(tmp_path, auto_mapping=False, readers=registry)
    c.hid_reader = hid_reader
    rec_active = [False]
    monkeypatch.setattr(
        c.device, "start",
        lambda profile, host=None: (rec_active.__setitem__(0, True), events.append("start:receiver")),
    )
    monkeypatch.setattr(
        c.device, "stop",
        lambda: (rec_active.__setitem__(0, False), events.append("stop:receiver"), True)[2],
    )
    monkeypatch.setattr(c.monitor, "refresh", lambda: c.monitor.apply([rec_usb]))
    c.auto_mapping = True
    c.monitor.apply([rec_usb])

    def active_count():
        return int(rec_active[0]) + int(hid_reader.active) + int(gatt_reader.active)

    assert c.input_path() == "receiver" and c.enabled and active_count() == 1

    # Switch to HID -> dedicated collector stops before HID opens.
    assert c.select_remote(r_hid.id)
    assert c.input_path() == "hid" and hid_reader.active and active_count() == 1

    # Switch to custom fake-gatt -> HID stops before GATT opens.
    assert c.select_remote(r_gatt.id)
    assert c.input_path() == "fake-gatt" and gatt_reader.active and active_count() == 1

    # Switch back to dedicated receiver -> GATT stops before collector starts.
    assert c.select_remote(r_rec.id)
    assert c.input_path() == "receiver" and c.enabled and active_count() == 1

    # Pausing stops whichever path is active; resuming re-arms only the active path.
    assert c.pause_remote_input() is True
    assert active_count() == 0
    c.resume_remote_input()
    assert c.enabled and active_count() == 1

    c.shutdown()
    assert hid_reader.closed and gatt_reader.closed and active_count() == 0


def test_composite_reader_merges_multiple_registered_readers_for_enrollment(qtbot, tmp_path):
    hid_dev = ProbeDevice(("usb", 3), "USB 1234:5678", "USB", 0x1234, 0x5678, "h" * 64, (1,))
    gatt_dev = ProbeDevice(("gatt", "CAFE:0003"), "BLE CAFE:0003", "蓝牙", 0xCAFE, 0x0003, "g" * 64, (2,))
    hid_reader = FakeRegisteredReader("HID 通道", hid_dev)
    gatt_reader = FakeRegisteredReader("GATT 通道", gatt_dev)

    registry = default_registry().clone()
    registry.register(
        ReaderEntry(
            id="fake-gatt",
            name="GATT",
            handles=lambda conn: (conn.vendor, conn.product) == (0xCAFE, 0x0003),
            factory=lambda: gatt_reader,
        )
    )
    c = AppController(tmp_path, auto_mapping=False, readers=registry)
    c.hid_reader = hid_reader

    reader = c.enrollment_reader()
    assert isinstance(reader, CompositeReader)
    listed = reader.enumerate()
    assert {d.id for d in listed} == {gatt_dev.id, hid_dev.id}
    assert {d.id for d in peek_devices(reader)} == {gatt_dev.id, hid_dev.id}

    reader.start(hid_dev.id, lambda *_: None, lambda: None, expected=hid_dev)
    assert hid_reader.active and not gatt_reader.active
    reader.start(gatt_dev.id, lambda *_: None, lambda: None, expected=gatt_dev)
    assert gatt_reader.active and not hid_reader.active

    dialog = DeviceProbeDialog(reader, install=lambda *_args: True)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.picker.count() == 2
    dialog.close()
    assert not hid_reader.active and not gatt_reader.active and gatt_reader.closed and hid_reader.closed
    c.shutdown()


def test_multi_connection_remote_prefers_present_receiver_and_falls_back_to_registered_reader(tmp_path):
    gatt_dev = ProbeDevice(("gatt", "CAFE:0004"), "BLE CAFE:0004", "蓝牙", 0xCAFE, 0x0004, "g" * 64, (2,))
    fake_reader = FakeRegisteredReader("GATT 通道", gatt_dev)
    registry = default_registry().clone()
    registry.register(
        ReaderEntry(
            id="fake-gatt",
            name="GATT",
            handles=lambda conn: conn.transport == "蓝牙" and (conn.vendor, conn.product) == (0xCAFE, 0x0004),
            factory=lambda: fake_reader,
        )
    )
    c = AppController(tmp_path, auto_mapping=False, readers=registry)
    remote = Remote(
        name="双模扩展",
        connections=[Connection("USB", 0x0416, 0x0602), Connection("蓝牙", 0xCAFE, 0x0004)],
        profiles=[Profile(name="默认", vendor=0xCAFE, product=0x0004)],
    )
    assert c.add_remote(remote, gatt_dev)
    # Receiver absent -> falls back to fake-gatt.
    assert c.input_path() == "fake-gatt" and not c.uses_dedicated_reader()
    # Receiver plugged in -> receiver takes priority.
    c.monitor.devices = [USBDevice(0x0416, 0x0602, "REC", "USB 0416:0602", "p1")]
    assert c.input_path() == "receiver" and c.uses_dedicated_reader()
    c.shutdown()
