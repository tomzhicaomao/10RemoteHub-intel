"""Plain-language access and microphone levels shared by the add dialog, list and voice page."""
from remotehub.probe_device import ProbeDevice
from remotehub.protocols import (
    MIC_DIRECT, MIC_DONGLE, MIC_NONE, device_mic_level, identify_protocol, mic_level, mic_text,
)


RECEIVER = ProbeDevice(("usb", 1, 0x0416, 0x0602), "0416:0602", "USB", 0x0416, 0x0602, "a" * 64, (1,))
USB_REMOTE = ProbeDevice(("usb", 2, 0x1234, 0x5678), "2.4G", "USB", 0x1234, 0x5678, "b" * 64, (2,))
SYSTEM_BT = ProbeDevice(("hid", 3), "BT", "蓝牙", 0x0416, 0x0300, "c" * 64, (3,))
BT_KEYBOARD = ProbeDevice(("hid", 4), "BT", "蓝牙", 0x620A, 0x0407, "e" * 64, (4,))
DONGLE_PEER = ProbeDevice(("ble-dongle", "AA:BB:CC:DD:EE:FF"), "BLE", "蓝牙", 0x0416, 0x0300, "d" * 64)


def test_readable_connections_are_direct_without_protocol_names():
    for device in (RECEIVER, USB_REMOTE, SYSTEM_BT, DONGLE_PEER):
        lines = identify_protocol(device).access_lines()
        assert lines[0].startswith("✅ 直接接入") and "协议" not in lines[0]
        assert len(lines) == 1          # the microphone is not explained in the add dialog
    assert "0416:0602" in identify_protocol(RECEIVER).access_lines()[0]
    assert "蓝牙棒直连" in identify_protocol(DONGLE_PEER).access_lines()[0]


def test_microphone_levels(monkeypatch):
    monkeypatch.setattr("remotehub.protocols.dongle_route_available", lambda: True)
    def level(device):
        return device_mic_level(device, identify_protocol(device))

    assert level(RECEIVER) == MIC_DIRECT
    assert level(USB_REMOTE) == MIC_NONE
    # A verified voice remote over the system Bluetooth stack can switch to the dongle route ...
    assert level(SYSTEM_BT) == MIC_DONGLE
    # ... an unverified Bluetooth remote is not promised that route ...
    assert level(BT_KEYBOARD) == MIC_NONE
    # ... and a remote the dongle already probed without a voice match has none.
    assert level(DONGLE_PEER) == MIC_NONE
    assert mic_level(supported=False, dongle_voice=True, dongle_probed=True) == MIC_NONE
    # Without the dongle component in this build that route does not exist.
    monkeypatch.setattr("remotehub.protocols.dongle_route_available", lambda: False)
    assert level(SYSTEM_BT) == MIC_NONE


def test_microphone_text_says_current_version_not_impossible():
    dongle = mic_text(MIC_DONGLE, "win32")
    assert dongle.startswith("遥控器麦克风换蓝牙棒直连能用") and "目前版本" in dongle and "Mac" not in dongle
    assert "Mac 上这条路还在验证" in mic_text(MIC_DONGLE, "darwin")
    assert "「遥控器麦克风」" in mic_text(MIC_DIRECT, "win32")
    none = mic_text(MIC_NONE, "darwin")
    assert none.startswith("遥控器麦克风目前用不了") and "电脑麦克风" in none and "复制给 AI 的求助" in none


def test_voice_page_says_a_built_in_voice_remote_microphone_is_usable(qtbot, tmp_path):
    from remotehub.controller import AppController
    from remotehub.model import Button, Profile
    from remotehub.remote_library import Connection, Remote, RemoteLibraryStore
    from remotehub.ui.voice import VoicePage

    remote = Remote(
        name="电视盒遥控器",
        connections=[Connection("蓝牙", 0x0416, 0x0300)],
        profiles=[Profile(name="默认", vendor=0x0416, product=0x0300,
                          buttons=[Button(name="语音", trigger="hid:7:234", role="voice")])],
    )
    store = RemoteLibraryStore(tmp_path)
    store.add(remote)
    ctrl = AppController(tmp_path, store=store, auto_mapping=False)
    try:
        page = VoicePage(ctrl)
        qtbot.addWidget(page)
        text = page.route.text()
        assert "麦克风可以直接用" in text and "协议" not in text
    finally:
        ctrl.shutdown()


def test_new_remote_is_named_by_its_hardware_id_not_by_the_reported_device_name(qtbot):
    from remotehub.ui.probe_photo import ProbePhotoPane

    device = ProbeDevice(("usb", 9), "某厂商出品 仿冒必究", "USB", 0x0416, 0x0602, "f" * 64, (9,))
    assert device.default_remote_name == "遥控器 0416:0602"
    pane = ProbePhotoPane(device)
    qtbot.addWidget(pane)
    assert pane.profile.name == "遥控器 0416:0602"
    assert ProbeDevice(("preview",), "示例遥控器", "模拟", 0, 0).default_remote_name == "遥控器"
