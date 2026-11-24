import ctypes as c
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage

from remotehub.device_probe import DeviceProbeDialog, PhotoPreviewReader
from remotehub.mac_hid_probe import MacHIDProbe, ProbeDevice, _Interface
from remotehub.photo_template import KEYMAP


class Reader:
    devices = [
        ProbeDevice(("hid", 8), "蓝牙键盘", "蓝牙", 0x620A, 0x0407),
        ProbeDevice(("usb", 4, 0x0416, 0x0602), "0416:0602", "USB", 0x0416, 0x0602),
    ]

    def __init__(self):
        self.started = []
        self.stopped = 0
        self.closed = False

    def enumerate(self):
        return self.devices

    def start(self, identity, on_key, on_removed):
        self.started.append(identity)
        self.on_key = on_key
        self.on_removed = on_removed
        return 1, 1

    def stop(self):
        self.stopped += 1

    def close(self):
        self.closed = True


def test_probe_requires_reported_press_and_release_from_current_session(qtbot):
    reader = Reader()
    dialog = DeviceProbeDialog(reader)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.picker.setCurrentIndex(1)
    dialog.start_button.click()
    assert reader.started == [reader.devices[1].id]
    qtbot.keyClick(dialog.start_button, Qt.Key.Key_Space)
    assert dialog.active and dialog.edges.text() == "等待检测"
    key = (12, 0), "hid:7:204"
    reader.on_key(*key, False)
    reader.on_key(*key, True)
    qtbot.waitUntil(lambda: dialog.edges.text() == "已收到按下，等待松开")
    reader.on_key(*key, True)
    reader.on_key(*key, False)
    qtbot.waitUntil(lambda: not dialog.active)
    assert dialog.edges.text() == "已收到按下与松开"
    assert dialog.key_code.text() == "hid:7:204"
    assert dialog.original.text() == "待验证"
    assert reader.stopped == 1
    dialog.observe.click()
    assert dialog.original.text() == "已观察到原键生效"
    dialog.close()
    assert reader.closed


def test_probe_rejects_cross_interface_pairs_and_discards_old_callbacks(qtbot):
    reader = Reader()
    dialog = DeviceProbeDialog(reader)
    qtbot.addWidget(dialog)
    dialog.start_button.click()
    old_key, old_removed = reader.on_key, reader.on_removed
    reader.on_key((1, 0), "hid:7:82", True)
    reader.on_key((2, 0), "hid:7:82", False)
    qtbot.waitUntil(lambda: bool(dialog.held))
    dialog.timeout()
    assert dialog.edges.text() == "未收到松开"

    dialog.start_button.click()
    old_key((1, 0), "hid:7:82", True)
    old_key((1, 0), "hid:7:82", False)
    old_removed()
    reader.on_key((3, 0), "hid:7:82", True)
    reader.on_key((4, 0), "hid:7:82", True)
    reader.on_key((3, 0), "hid:7:82", False)
    reader.on_key((4, 0), "hid:7:82", False)
    qtbot.waitUntil(lambda: not dialog.active)
    assert dialog.edges.text() == "收到多个信号"
    dialog.start_button.click()
    reader.devices = []
    reader.on_removed()
    qtbot.waitUntil(lambda: not dialog.active)
    assert "重新连接后再检测" in dialog.feedback.text()
    assert not dialog.start_button.isEnabled()


def test_interface_change_rechecks_and_keeps_selected_remote(qtbot):
    reader = Reader()
    dialog = DeviceProbeDialog(reader)
    qtbot.addWidget(dialog)
    dialog.picker.setCurrentIndex(1)
    selected = dialog.selected_device()
    dialog.start_button.click()
    reader.devices = list(reversed(reader.devices))
    reader.on_removed()
    qtbot.waitUntil(lambda: not dialog.active)
    assert "遥控器仍在" in dialog.feedback.text()
    assert dialog.selected_device() == selected
    dialog.start_button.click()
    assert reader.started == [selected.id, selected.id]
    reader.on_key((12, 0), "hid:7:31", True)
    reader.on_key((12, 0), "hid:7:31", False)
    qtbot.waitUntil(lambda: not dialog.active)
    assert dialog.key_code.text() == "hid:7:31"


def test_release_result_remains_visible_at_minimum_width(qtbot):
    reader = Reader()
    dialog = DeviceProbeDialog(reader)
    qtbot.addWidget(dialog)
    dialog.setStyleSheet(dialog.styleSheet() + "\nQLabel, QPushButton { font-size: 22px; }")
    dialog.resize(dialog.minimumWidth(), dialog.sizeHint().height())
    dialog.show()
    dialog.start_button.click()
    reader.on_key((12, 0), "hid:12:65535", True)
    qtbot.waitUntil(lambda: bool(dialog.held))
    positions = (dialog.size(), dialog.edges.mapTo(dialog, dialog.edges.rect().topLeft()),
                 dialog.key_code.mapTo(dialog, dialog.key_code.rect().topLeft()))
    reader.on_key((12, 0), "hid:12:65535", False)
    qtbot.waitUntil(lambda: not dialog.active)
    assert dialog.key_code.text() == "hid:12:65535"
    assert positions == (dialog.size(), dialog.edges.mapTo(dialog, dialog.edges.rect().topLeft()),
                         dialog.key_code.mapTo(dialog, dialog.key_code.rect().topLeft()))
    for field in (dialog.edges, dialog.key_code):
        assert field.isVisible() and not field.wordWrap()
        assert field.fontMetrics().horizontalAdvance(field.text()) <= field.contentsRect().width()


@pytest.mark.parametrize(
    ("device_index", "trigger", "builtin"),
    [(0, "hid:7:82", False), (1, "hid:7:30", False)],
)
def test_verified_device_can_learn_button_on_photo(qtbot, tmp_path, monkeypatch, device_index, trigger, builtin):
    reader = Reader()
    dialog = DeviceProbeDialog(reader)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.picker.setCurrentIndex(device_index)
    assert not dialog.photo_link.isEnabled()
    dialog.start_button.click()
    reader.on_key((12, 0), trigger, True)
    reader.on_key((12, 0), trigger, False)
    qtbot.waitUntil(dialog.photo_link.isEnabled)
    dialog.photo_link.click()
    learning = dialog.learning_dialog
    assert learning.isVisible()
    assert learning.selected_device() == reader.devices[device_index]
    assert not learning.picker.isEnabled()

    photo = learning.photo
    assert bool(photo.profile.image) is builtin
    if not builtin:
        from remotehub.ui.photo_crop import PhotoCropDialog

        def use_original(editor):
            editor.original_button.click()
            return editor.result()

        monkeypatch.setattr(PhotoCropDialog, "exec", use_original)
        image = QImage(160, 400, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.gray)
        path = tmp_path / "remote.png"
        assert image.save(str(path))
        photo.load_photo(path)
    qtbot.waitUntil(photo.canvas.isVisible)
    position = photo.canvas.image_rect().center().toPoint()
    x, y = photo.canvas._normalized(QPointF(position))
    qtbot.mouseClick(photo.canvas, Qt.MouseButton.LeftButton, pos=position)
    assert learning.active and photo.pending is not None
    assert reader.started == [reader.devices[device_index].id] * 2
    assert not photo.profile.buttons

    reader.on_key((12, 0), trigger, True)
    qtbot.waitUntil(lambda: bool(learning.held))
    assert not photo.profile.buttons
    reader.on_key((12, 0), trigger, False)
    qtbot.waitUntil(lambda: len(photo.profile.buttons) == 1)
    bound = photo.profile.buttons[0]
    assert bound.trigger == trigger
    left, top, width, height = bound.rect
    assert (left + width / 2, top + height / 2) == pytest.approx((x, y))
    assert bound.id in photo.canvas.lines
    expected_name = {"hid:7:82": "上", "hid:7:30": "1"}[trigger]
    expected_role = {"hid:7:82": "up", "hid:7:30": "1"}[trigger]
    assert photo.canvas.callouts[bound.id].text() == expected_name
    assert bound.role == expected_role

    area = photo.canvas.image_rect()
    lower = QPointF(area.center().x(), area.top() + .75 * area.height()).toPoint()
    qtbot.mouseClick(photo.canvas, Qt.MouseButton.LeftButton, pos=lower)
    assert learning.active
    reader.on_key((12, 0), trigger, True)
    reader.on_key((12, 0), trigger, False)
    qtbot.waitUntil(lambda: not learning.active)
    assert photo.profile.buttons == [bound]
    assert photo.selected == bound.id and photo.pending is None
    learning.close()


def test_photo_preview_uses_same_binding_flow_without_hardware(qtbot):
    reader = PhotoPreviewReader()
    dialog = DeviceProbeDialog(reader, device=reader.device, preview=True)
    qtbot.addWidget(dialog)
    dialog.show()
    photo = dialog.photo
    assert photo.photo_button.text() == "导入照片"
    assert not photo.profile.image
    assert not dialog.simulate_button.isEnabled()

    photo.example_button.click()
    qtbot.waitUntil(photo.canvas.isVisible)
    qtbot.mouseClick(photo.canvas, Qt.MouseButton.LeftButton, pos=photo.canvas.image_rect().center().toPoint())
    assert dialog.active and dialog.simulate_button.isEnabled()
    assert not photo.profile.buttons
    dialog.simulate_button.click()
    qtbot.waitUntil(lambda: len(photo.profile.buttons) == 1)
    assert dialog.source.text() == "模拟输入"
    assert photo.profile.buttons[0].trigger == "hid:7:30"
    assert not dialog.simulate_button.isEnabled()
    assert photo.profile.buttons[0].id in photo.canvas.lines


def test_photo_hotspot_can_be_dragged_and_resized_before_and_after_binding(qtbot):
    reader = PhotoPreviewReader()
    dialog = DeviceProbeDialog(reader, device=reader.device, preview=True)
    qtbot.addWidget(dialog)
    dialog.show()
    photo = dialog.photo
    photo.example_button.click()
    canvas = photo.canvas
    qtbot.waitUntil(canvas.isVisible)
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas.image_rect().center().toPoint())
    pending = photo.pending
    assert pending is not None and canvas.size_controls.isVisible()

    def drag_to(key, end):
        start = canvas.hotspot_rect(key).center().toPoint()
        qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        qtbot.mouseMove(canvas, pos=end)
        qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)

    old_anchor = canvas.anchors[pending.id]
    end = canvas.hotspot_rect(pending).center().toPoint() + QPoint(25, -24)
    drag_to(pending, end)
    assert canvas.hotspot_rect(pending).contains(end)
    assert canvas.anchors[pending.id] != old_anchor

    center = canvas.hotspot_rect(pending).center()
    original_size = pending.rect[2:]
    qtbot.mouseClick(canvas.enlarge_button, Qt.MouseButton.LeftButton)
    assert pending.rect[2:] == pytest.approx([size * 1.2 for size in original_size])
    assert canvas.hotspot_rect(pending).center().x() == pytest.approx(center.x())
    assert canvas.hotspot_rect(pending).center().y() == pytest.approx(center.y())
    qtbot.mouseClick(canvas.shrink_button, Qt.MouseButton.LeftButton)
    assert pending.rect[2:] == pytest.approx(original_size)

    dialog.simulate_button.click()
    qtbot.waitUntil(lambda: len(photo.profile.buttons) == 1)
    bound = photo.profile.buttons[0]
    assert bound is pending and bound.trigger == "hid:7:30"
    end = QPoint(round(canvas.image_rect().left()) - 12, end.y())
    drag_to(bound, end)
    assert bound.rect[0] == 0
    qtbot.mouseClick(canvas.enlarge_button, Qt.MouseButton.LeftButton)
    assert bound.rect[2:] == pytest.approx([size * 1.2 for size in original_size])
    assert bound.rect[0] == 0 and bound.trigger == "hid:7:30"
    assert not dialog.active


def send_report(listener, report_id, raw):
    data = (c.c_uint8 * len(raw)).from_buffer_copy(raw)
    listener.report(None, 0, None, 0, report_id, data, len(raw))


def test_shared_probe_decodes_raw_usb_hash_using_real_saved_descriptor():
    fixture = Path(__file__).parent / "fixtures/receiver-descriptors.json"
    descriptor = bytes.fromhex(json.loads(fixture.read_text())["descriptors"]["4"])
    key = KEYMAP["keys"]["tv-number-#"]
    events = []
    listener = MacHIDProbe._listener(_Interface(11, 19, descriptor, 8), lambda *event: events.append(event), None)
    send_report(listener, 0, bytes.fromhex(key["press_hex"]))
    send_report(listener, 0, b"\0")  # An incomplete report cannot release a held key.
    send_report(listener, 0, bytes.fromhex(key["release_hex"]))
    assert events == [((19, 0), key["trigger"], True), ((19, 0), key["trigger"], False)]


def test_numbered_input_report_accepts_both_mac_callback_buffer_forms():
    # A synthetic numbered report with one keyboard usage and seven padding bits.
    descriptor = bytes.fromhex("05078502150025010952750195018102750795018101")
    events = []
    listener = MacHIDProbe._listener(_Interface(10, 18, descriptor, 2), lambda *event: events.append(event), None)
    send_report(listener, 2, b"\x01")
    send_report(listener, 9, b"\0")
    send_report(listener, 2, b"\x02\0")
    assert events == [((18, 2), "hid:7:82", True), ((18, 2), "hid:7:82", False)]


def test_native_probe_cleans_up_and_reopens_only_selected_interfaces(monkeypatch):
    from remotehub import mac_hid_probe

    opened, closed, unscheduled = [], [], []
    reports, removals = {}, {}
    snapshots, released = [], []

    def open_device(handle, options):
        opened.append((handle, options))
        return -1 if handle == 12 else 0

    def register(callbacks, handle, callback, context):
        key = (handle, context)
        if callback:
            callbacks.setdefault(key, callback)
        else:
            callbacks.pop(key, None)

    api = SimpleNamespace(run_loop=20, run_loop_mode=21, hid=SimpleNamespace(
        IOHIDDeviceOpen=open_device,
        IOHIDDeviceClose=lambda *args: closed.append(args),
        IOHIDDeviceRegisterInputReportCallback=lambda handle, _buffer, _length, callback, context:
            register(reports, handle, callback, context),
        IOHIDDeviceRegisterRemovalCallback=lambda handle, callback, context:
            register(removals, handle, callback, context),
        IOHIDDeviceScheduleWithRunLoop=lambda *_: None,
        IOHIDDeviceUnscheduleFromRunLoop=lambda *args: unscheduled.append(args),
    ))
    api.cf = SimpleNamespace(CFRelease=released.append)
    monkeypatch.setattr(mac_hid_probe, "_API", lambda: api)
    reader = MacHIDProbe()
    selected, other = ("hid", 1), ("hid", 2)
    fixture = Path(__file__).parent / "fixtures/receiver-descriptors.json"
    descriptor = bytes.fromhex(json.loads(fixture.read_text())["descriptors"]["4"])

    def enumerate_devices():
        snapshots.append(len(snapshots) + 1)
        reader._device_set = snapshots[-1]
        reader._devices = {selected: ProbeDevice(selected, "Receiver", "USB", 1, 2, connection=(1, 3))}
        reader._catalog = {
            selected: [_Interface(11, 1, descriptor, 8), _Interface(12, 3, b"", 8)],
            other: [_Interface(13, 2, b"", 8)],
        }

    monkeypatch.setattr(reader, "enumerate", enumerate_devices)
    for number in (1, 2):
        events = []
        expected = ProbeDevice(selected, "Receiver", "USB", 1, 2, connection=(1, 3))
        assert reader.start(selected, lambda *event: events.append(event), None, expected=expected) == (1, 2)
        (handle, context), callback = next(iter(reports.items()))
        assert handle == 11 and context and set(reports) == set(removals)
        key = KEYMAP["keys"][f"tv-number-{number}"]
        listener = SimpleNamespace(report=callback)
        send_report(listener, 0, bytes.fromhex(key["press_hex"]))
        send_report(listener, 0, bytes.fromhex(key["release_hex"]))
        assert events == [((1, 0), key["trigger"], True), ((1, 0), key["trigger"], False)]
        reader.stop()
        assert not reports and not removals
    assert opened == [(11, 0), (12, 0)] * 2
    assert unscheduled == [(11, 20, 21)] * 2
    assert closed == [(11, 0)] * 2
    assert snapshots == released == [1, 2]

    expected = ProbeDevice(selected, "Receiver", "USB", 1, 2, connection=(1, 2))
    with pytest.raises(RuntimeError, match="设备已更换"):
        reader.start(selected, None, None, expected=expected)
    assert not reader._catalog and snapshots == released == [1, 2, 3]
    assert opened == [(11, 0), (12, 0)] * 2


def _fake_reader(monkeypatch, *, suppress_ok=True, suppress_error="未匹配到系统键盘事件服务"):
    from remotehub import mac_hid_probe

    opened, closed = [], []

    def open_device(handle, options):
        opened.append((handle, options))
        return 0

    class FakeSuppressor:
        def __init__(self):
            self.active = False
            self.error = ""
            self.active_usages = ()
            self.applied = []
            self.cleared = 0
            self.ensured = 0

        def apply(self, *, vendor, product, triggers, transport="", location_id=None, built_in=False):
            from remotehub.mac_hid_suppress import usages_from_triggers

            usages = usages_from_triggers(triggers)
            self.applied.append((vendor, product, usages, transport, location_id))
            if not usages:
                self.active, self.active_usages, self.error = False, (), ""
                return False
            if suppress_ok:
                self.active, self.active_usages, self.error = True, usages, ""
                return True
            self.active, self.active_usages, self.error = False, (), suppress_error
            return False

        def ensure(self):
            self.ensured += 1
            return self.active

        def clear(self):
            if self.active or self.active_usages:
                self.cleared += 1
            self.active = False
            self.active_usages = ()
            self.error = ""

    api = SimpleNamespace(run_loop=20, run_loop_mode=21, hid=SimpleNamespace(
        IOHIDDeviceOpen=open_device,
        IOHIDDeviceClose=lambda *args: closed.append(args),
        IOHIDDeviceRegisterInputReportCallback=lambda *_: None,
        IOHIDDeviceRegisterRemovalCallback=lambda *_: None,
        IOHIDDeviceScheduleWithRunLoop=lambda *_: None,
        IOHIDDeviceUnscheduleFromRunLoop=lambda *_: None,
    ), cf=SimpleNamespace(CFRelease=lambda *_: None))
    monkeypatch.setattr(mac_hid_probe, "_API", lambda: api)
    suppressor = FakeSuppressor()
    reader = MacHIDProbe(suppressor=suppressor)
    selected = ("hid", 1)
    device = ProbeDevice(selected, "遥控器", "蓝牙", 0x620A, 0x0407, connection=(1,))

    def enumerate_devices():
        reader._devices = {selected: device}
        reader._hid_meta = {selected: ("Bluetooth Low Energy", 0x13C72B43)}
        reader._catalog = {selected: [_Interface(11, 1, b"", 8)]}

    monkeypatch.setattr(reader, "enumerate", enumerate_devices)
    return reader, device, opened, closed, suppressor


def test_daily_use_takes_the_remote_over_and_releases_it(monkeypatch):
    reader, device, opened, closed, suppressor = _fake_reader(monkeypatch)
    reader.take_over = True
    reader.take_over_triggers = ("hid:7:82", "hid:12:233")
    assert reader.start(device.id, lambda *_: None, None, expected=device) == (1, 1)
    # Interfaces open shared (preserves air-mouse and works without root); hidutil suppresses keys.
    assert opened == [(11, 0)] and reader.seized == 2 and not reader.seize_error
    assert suppressor.applied == [(0x620A, 0x0407, (0x700000052, 0xC000000E9), "Bluetooth Low Energy", 0x13C72B43)]
    assert reader.sync_takeover(("hid:7:82", "hid:12:233", "hid:12:234"))
    assert reader.seized == 3
    reader.stop()
    assert closed == [(11, 0)] and suppressor.cleared == 1 and reader.seized == 0


def test_refused_takeover_falls_back_to_shared_reading(monkeypatch):
    reader, device, opened, closed, suppressor = _fake_reader(monkeypatch, suppress_ok=False)
    reader.take_over = True
    reader.take_over_triggers = ("hid:12:233",)
    assert reader.start(device.id, lambda *_: None, None, expected=device) == (1, 1)
    assert opened == [(11, 0)] and reader.seized == 0 and reader.seize_error == "未匹配到系统键盘事件服务"
    reader.stop()
    assert closed == [(11, 0)] and not reader.seize_error


def test_the_add_dialog_reads_shared(monkeypatch):
    reader, device, opened, _, suppressor = _fake_reader(monkeypatch)
    assert reader.start(device.id, lambda *_: None, None, expected=device) == (1, 1)
    assert opened == [(11, 0)] and reader.seized == 0 and not suppressor.applied


def test_runtime_turns_takeover_on_only_for_its_own_start(monkeypatch, qapp):
    from remotehub.learned_remotes import LearnedRemoteRuntime

    seen = []

    class Reader:
        take_over = False
        take_over_triggers = ()
        seized = 0

        def start(self, identity, on_key, on_removed, *, expected=None):
            seen.append((self.take_over, self.take_over_triggers))
            self.seized = len(self.take_over_triggers)
            return 1, 1

        def stop(self):
            pass

    reader = Reader()
    runtime = LearnedRemoteRuntime(reader)
    runtime.take_over_triggers = ("hid:7:82", "hid:12:233")
    device = ProbeDevice(("hid", 1), "遥控器", "蓝牙", 0x620A, 0x0407)
    assert runtime.listen(device)
    assert seen == [(True, ("hid:7:82", "hid:12:233"))]
    assert reader.take_over is False and reader.take_over_triggers == ()
