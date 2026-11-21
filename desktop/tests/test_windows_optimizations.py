"""Dedicated Windows optimization tests: VK chords, scroll, LockWorkStation, UIPI, SetupAPI, .lnk, UIA, driver & UI."""
from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from remotehub import common_actions, windows_driver
from remotehub.actions import ActionRunner, KeyOutput, launch_command, win32_vk_code
from remotehub.app_layers import LayerApp, layer_app_from_path
from remotehub.ax_actions import execute_app_menu, execute_ui_element, find_interactive_candidate, list_app_menus
from remotehub.chat_focus import App, app_from_path
from remotehub.devices import DeviceInput
from remotehub.inventory import USBDevice, windows_setupapi_inventory
from remotehub.model import Action, Profile
from remotehub.presentation import action_text, chip_hint, palette
from remotehub.ui import MainWindow
from remotehub.ui import onboarding as onboarding_module
from remotehub.ui.osd import (
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    compute_win32_exstyle,
)
from remotehub.win_uia import parse_lnk_bytes, strip_win32_menu_label
from remotehub.windows_collector import WindowsCollector


def test_windows_vk_mapping_prevents_unicode_injection_under_shift_and_ime(monkeypatch):
    assert win32_vk_code("a") == 0x41
    assert win32_vk_code("s") == 0x53
    assert win32_vk_code("z") == 0x5A
    assert win32_vk_code("0") == 0x30
    assert win32_vk_code("9") == 0x39
    assert win32_vk_code("f1") == 0x70
    assert win32_vk_code("f11") == 0x7A
    assert win32_vk_code("f24") == 0x87
    assert win32_vk_code("print_screen") == 0x2C
    assert win32_vk_code("-") == 0xBD
    assert win32_vk_code("`") == 0xC0

    pressed = []
    out = KeyOutput(sleeper=lambda _: None)
    out.controller = SimpleNamespace(
        press=lambda k: pressed.append(("down", k)),
        release=lambda k: pressed.append(("up", k)),
    )
    out.key_enum = SimpleNamespace(cmd="VK_LWIN", shift="VK_SHIFT", esc="VK_ESCAPE", ctrl="VK_CONTROL")
    out.key_code_cls = SimpleNamespace(from_vk=lambda vk: f"VK_{vk:#04x}")
    monkeypatch.setattr("remotehub.actions.sys.platform", "win32")

    out.shortcut(["cmd", "shift", "s"])
    assert ("down", "VK_0x53") in pressed and ("up", "VK_0x53") in pressed


def test_windows_scroll_uses_win32_wheel_instead_of_crashing_keyboard_controller(monkeypatch):
    wheel_calls = []
    out = KeyOutput(sleeper=lambda _: None)
    # Real pynput.keyboard.Controller has press/release ONLY — no .scroll method!
    out.controller = SimpleNamespace(press=lambda k: None, release=lambda k: None)
    monkeypatch.setattr("remotehub.actions.sys.platform", "win32")
    monkeypatch.setattr("remotehub.actions._win32_mouse_wheel", lambda amt: (wheel_calls.append(amt) or True))

    out.scroll("scroll_up")
    out.scroll("scroll_down")
    assert wheel_calls == [3, -3]


def test_windows_lock_screen_calls_lockworkstation_and_uipi_raises_clear_error(qapp, monkeypatch):
    locked = []
    monkeypatch.setattr("remotehub.actions.sys.platform", "win32")
    monkeypatch.setattr("remotehub.actions._lock_windows_workstation", lambda: (locked.append(True) or "已锁屏"))
    runner = ActionRunner(keys=KeyOutput(sleeper=lambda _: None))
    try:
        assert runner.run(Action("common", target="lock_screen")) == "已锁屏"
        assert locked == [True]
    finally:
        runner.switcher.close()

    out = KeyOutput(sleeper=lambda _: None)
    out.controller = SimpleNamespace(press=lambda k: None, release=lambda k: None)
    out._using_native_backend = True
    monkeypatch.setattr("remotehub.win_uia.foreground_uipi_blocked", lambda: True)
    with pytest.raises(PermissionError, match="管理员身份|UIPI"):
        out.shortcut(["ctrl", "c"])


def test_windows_common_actions_and_palette_coverage():
    win_actions = common_actions.available_actions("win32")
    mac_actions = common_actions.available_actions("darwin")
    linux_actions = common_actions.available_actions("linux")

    expected_win_only = {
        "file_explorer": ["cmd", "e"],
        "clipboard_history": ["cmd", "v"],
        "win_input_switch": ["cmd", "space"],
        "win_desktop_left": ["ctrl", "cmd", "left"],
        "win_desktop_right": ["ctrl", "cmd", "right"],
        "snap_left": ["cmd", "left"],
        "snap_right": ["cmd", "right"],
        "maximize_window": ["cmd", "up"],
        "task_manager": ["ctrl", "shift", "esc"],
    }
    for op, keys in expected_win_only.items():
        assert op in win_actions
        assert op not in mac_actions and op not in linux_actions
        assert common_actions.common_keys(op, "win32") == keys
        assert chip_hint(f"common:{op}", "win32")

    win_palette_ids = [ident for _, items in palette("win32") for ident, _ in items]
    assert len(win_palette_ids) == len(set(win_palette_ids))
    assert {x[7:] for x in win_palette_ids if x.startswith("common:")} == set(win_actions)


def test_windows_driver_dual_interface_binding_detail():
    class FakeKey:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeRegistry:
        HKEY_LOCAL_MACHINE = 1

        def __init__(self, mapping):
            self.mapping = mapping

        def OpenKey(self, root, path):
            if isinstance(root, FakeKey):
                sub = root.data.get(path)
                if sub is None:
                    raise FileNotFoundError(path)
                return FakeKey(sub)
            sub = self.mapping.get(path)
            if sub is None:
                raise FileNotFoundError(path)
            return FakeKey(sub)

        def QueryInfoKey(self, key):
            return (len(key.data), 0, 0)

        def EnumKey(self, key, index):
            return list(key.data.keys())[index]

        def QueryValueEx(self, key, name):
            if name not in key.data:
                raise FileNotFoundError(name)
            return (key.data[name], 1)

    both = FakeRegistry({
        windows_driver.ENUM_KEY: {"inst0": {"Service": "WinUSB"}},
        windows_driver.ENUM_KEY_KEYBOARD: {"inst0": {"Service": "WinUSB"}},
    })
    assert windows_driver.binding_detail(registry=both) == {
        "state": "winusb",
        "voice": "winusb",
        "keyboard": "winusb",
    }

    partial = FakeRegistry({
        windows_driver.ENUM_KEY: {"inst0": {"Service": "WinUSB"}},
        windows_driver.ENUM_KEY_KEYBOARD: {"inst0": {"Service": "HidUsb"}},
    })
    assert windows_driver.binding_detail(registry=partial) == {
        "state": "voice_only",
        "voice": "winusb",
        "keyboard": "other",
    }


def test_windows_setupapi_inventory_parses_vid_pid_rev_and_serial():
    records = [
        (r"USB\VID_0416&PID_0602\SN-REMOTE-01", ["USB\\VID_0416&PID_0602&REV_0105"], "China Mobile Remote"),
        (r"USB\VID_0416&PID_0602&MI_03\7&1234&0&0003", ["USB\\VID_0416&PID_0602&REV_0105&MI_03"], "Voice Interface"),
    ]

    class FakeSetupAPI:
        def SetupDiGetClassDevsW(self, *_args):
            return 0x100

        def SetupDiEnumDeviceInfo(self, _handle, index, _devinfo):
            return 1 if index < len(records) else 0

        def SetupDiGetDeviceInstanceIdW(self, _handle, devinfo, buf, _size, _req):
            # Track index via devinfo reserved field in test
            idx = self._cur_idx
            buf.value = records[idx][0]
            return 1

        def SetupDiGetDeviceRegistryPropertyW(self, _handle, _devinfo, prop, _regtype, buf, _size, req):
            import ctypes as c
            idx = self._cur_idx
            if prop == 1:  # SPDRP_HARDWAREID MULTI_SZ
                joined = "\x00".join(records[idx][1]) + "\x00\x00"
                raw = joined.encode("utf-16-le")
                c.memmove(buf, raw, len(raw))
                req._obj.value = len(raw)
                return 1
            if prop in (12, 0):  # FriendlyName / DeviceDesc
                raw = (records[idx][2] + "\x00").encode("utf-16-le")
                c.memmove(buf, raw, len(raw))
                req._obj.value = len(raw)
                return 1
            return 0

        def SetupDiDestroyDeviceInfoList(self, _handle):
            return 1

    fake = FakeSetupAPI()
    orig_enum = fake.SetupDiEnumDeviceInfo

    def tracked_enum(handle, index, devinfo):
        fake._cur_idx = index
        return orig_enum(handle, index, devinfo)

    fake.SetupDiEnumDeviceInfo = tracked_enum
    hid_rows = [{"vendor_id": 0x0416, "product_id": 0x0602, "serial_number": "SN-REMOTE-01"}]
    devices = windows_setupapi_inventory(setupapi=fake, hid_rows=hid_rows)
    assert devices == [
        USBDevice(
            vendor=0x0416,
            product=0x0602,
            serial="SN-REMOTE-01",
            name="China Mobile Remote",
            connection=r"USB\VID_0416&PID_0602\SN-REMOTE-01",
            bcd_device=0x0105,
        )
    ]


def _build_synthetic_lnk(target_path: str) -> bytes:
    header = bytearray(0x4C)
    struct.pack_into("<I", header, 0, 0x4C)
    header[4:20] = bytes.fromhex("0114020000000000c000000000000046")
    # HasLinkInfo (0x02), no LinkTargetIDList (0x01)
    struct.pack_into("<I", header, 0x14, 0x02)

    ansi_bytes = target_path.encode("latin-1", errors="replace") + b"\x00"
    utf16_bytes = target_path.encode("utf-16-le") + b"\x00\x00"
    info_header_size = 0x24
    local_base_off = info_header_size
    local_base_uni_off = local_base_off + len(ansi_bytes)
    total_info_size = local_base_uni_off + len(utf16_bytes)

    link_info = bytearray(total_info_size)
    struct.pack_into("<I", link_info, 0, total_info_size)
    struct.pack_into("<I", link_info, 4, info_header_size)
    struct.pack_into("<I", link_info, 8, 0x01)  # VolumeIDAndLocalBasePath
    struct.pack_into("<I", link_info, 16, local_base_off)
    struct.pack_into("<I", link_info, 28, local_base_uni_off)
    link_info[local_base_off:local_base_off + len(ansi_bytes)] = ansi_bytes
    link_info[local_base_uni_off:local_base_uni_off + len(utf16_bytes)] = utf16_bytes
    return bytes(header + link_info)


def test_windows_lnk_binary_parser_and_launch_command(tmp_path):
    target = r"C:\Program Files\Tencent\WeChat\WeChat.exe"
    raw = _build_synthetic_lnk(target)
    assert parse_lnk_bytes(raw) == target

    lnk_file = tmp_path / "微信.lnk"
    lnk_file.write_bytes(raw)
    assert layer_app_from_path(lnk_file, platform="win32") == LayerApp("exe:wechat.exe", "微信")
    assert app_from_path(lnk_file, platform="win32") == App("exe:wechat.exe", 0, "微信")
    # Launching a .lnk as an app returns None so os.startfile preserves shortcut args/workdir
    assert launch_command(Action("app", target=str(lnk_file)), "win32") is None
    assert action_text(Action("toggle_app", target=str(lnk_file)), "win32") == "打开 / 收起 App · 微信"


def test_windows_win32_menu_label_stripping_and_ax_execution():
    assert strip_win32_menu_label("保存(&S)\tCtrl+S") == "保存"
    assert strip_win32_menu_label("&View") == "View"
    assert strip_win32_menu_label("Zoom (&Z)...") == "Zoom…"
    assert strip_win32_menu_label("A && B\tAlt+A") == "A & B"

    # Verify Windows exe:... app IDs work seamlessly with ax_actions candidate capture, press, focus & menus
    class FakeNode:
        def __init__(self, role, title="", parent=None, children=None, frame=(10, 10, 80, 30), enabled=True):
            self.role = role
            self.title = title
            self.parent = parent
            self.children = list(children or [])
            for ch in self.children:
                ch.parent = self
            self.frame = frame
            self.enabled = enabled
            self.pressed = 0
            self.focused = 0

    win_root = FakeNode("AXWindow", "主窗口", frame=(0, 0, 800, 600))
    send_btn = FakeNode("AXButton", "发送消息", parent=win_root, frame=(600, 520, 90, 32))
    win_root.children.append(send_btn)

    menu_leaf = FakeNode("AXMenuItem", "隐藏侧边栏")
    menu_popup = FakeNode("AXMenu", "", children=[menu_leaf])
    menu_top = FakeNode("AXMenuBarItem", "视图", children=[menu_popup])
    menubar = FakeNode("AXMenuBar", "", children=[menu_top])

    win_app = App("exe:wechat.exe", 2048, "微信")

    class FakeWinAXBackend:
        def frontmost(self):
            return win_app

        def applications(self, *, include_browsers=True):
            return [win_app]

        def prepare(self, app, abort):
            return True

        def window(self, app):
            return win_root

        def modal(self, el):
            return False

        def menubar(self, app):
            return menubar

        def children(self, el):
            return list(el.children)

        def parent(self, el):
            return el.parent

        def read(self, el, attr):
            if attr == "AXRole":
                return el.role
            if attr == "AXTitle":
                return el.title
            if attr == "AXEnabled":
                return el.enabled
            if attr == "AXParent":
                return el.parent
            if attr == "AXWindow":
                return win_root
            return None

        def visible(self, el, window):
            return True

        def frame(self, el):
            return el.frame

        def same(self, a, b):
            return a is b

        def actions(self, el):
            return ["AXPress"] if el.role in ("AXButton", "AXMenuItem", "AXMenuBarItem") else []

        def press(self, el):
            el.pressed += 1
            return True

        def focus(self, el):
            el.focused += 1
            return True

    backend = FakeWinAXBackend()
    found = find_interactive_candidate(backend, send_btn, win_root, "press")
    assert found is not None
    _, role, lbl, sig = found
    act = Action(
        "ui_element",
        target="press",
        application="exe:wechat.exe",
        spec={"app_name": "微信", "role": role, "label": lbl, "signature": sig},
    )
    act.validate("win32")
    msg = execute_ui_element(backend, act, cache={})
    assert send_btn.pressed == 1 and "发送消息" in msg

    app_info, cmds = list_app_menus(backend, "exe:wechat.exe")
    assert app_info.bundle == "exe:wechat.exe"
    assert [c.path for c in cmds] == [("视图", "隐藏侧边栏")]
    menu_act = Action("app_menu", application="exe:wechat.exe", spec={"app_name": "微信", "path": ["视图", "隐藏侧边栏"]})
    menu_act.validate("win32")
    menu_msg = execute_app_menu(backend, menu_act)
    assert menu_leaf.pressed == 1 and "隐藏侧边栏" in menu_msg


def test_windows_collector_immediate_remote_stop_and_device_input_failure(qapp, monkeypatch):
    import time as time_mod
    import libusb_package
    import usb.core
    import usb.util
    from usb.backend import libusb1

    wc = WindowsCollector()
    collected = []
    wc.collected.connect(collected.append)
    voice_packets = iter([
        b"\x01\x82\x03" + bytes(61),
        b"\x01\x08\x01\x00" + bytes(60),
        b"\x01\x01\x01\x00" + bytes(60),
        b"\x01\x86\x00" + bytes(61),
    ])

    class FakeVoiceEP:
        bEndpointAddress = 0x84

        def read(self, *args, **kwargs):
            if not wc.capture_ready:
                time_mod.sleep(0.16)
                raise usb.core.USBTimeoutError("drain")
            try:
                return next(voice_packets)
            except StopIteration:
                time_mod.sleep(0.08)
                wc.halt.set()
                raise usb.core.USBTimeoutError("done")

    class FakeWinDev:
        def get_active_configuration(self):
            return {(3, 0): [FakeVoiceEP()]}

        def write(self, endpoint, data, timeout=1000):
            return len(data)

    monkeypatch.setattr(libusb_package, "find_library", lambda *a: None)
    monkeypatch.setattr(libusb1, "get_backend", lambda **kw: object())
    monkeypatch.setattr(usb.core, "find", lambda **kw: [FakeWinDev()])
    monkeypatch.setattr(usb.util, "claim_interface", lambda dev, num: None)
    monkeypatch.setattr(usb.util, "release_interface", lambda dev, num: None)
    monkeypatch.setattr(usb.util, "dispose_resources", lambda dev: None)
    wc._run()
    qapp.processEvents()
    assert [e["type"] for e in collected] == ["start", "end"]

    dev = DeviceInput()
    stopped_signals = []
    status_signals = []
    dev.capture_stopped.connect(lambda: stopped_signals.append(True))
    dev.status.connect(status_signals.append)
    dev.generation = 7
    dev.capture_ready = True
    dev._win_failed("WinUSB disconnected", 7)
    assert stopped_signals == [True] and status_signals == ["WinUSB disconnected"]


def test_windows_osd_exstyle_and_tray_maximized_restore_and_ui_semantics(qtbot, tmp_path, monkeypatch):
    normal_style = compute_win32_exstyle(0, click_through=False)
    assert normal_style & WS_EX_NOACTIVATE
    assert normal_style & WS_EX_TOOLWINDOW
    assert normal_style & WS_EX_TOPMOST
    assert not (normal_style & WS_EX_TRANSPARENT)

    toast_style = compute_win32_exstyle(normal_style, click_through=True)
    assert (toast_style & WS_EX_TRANSPARENT) and (toast_style & WS_EX_LAYERED)
    cleared = compute_win32_exstyle(toast_style, click_through=False)
    assert not (cleared & WS_EX_TRANSPARENT)

    monkeypatch.setattr("remotehub.ui.general.sys.platform", "win32")
    monkeypatch.setattr("remotehub.ui.voice.sys.platform", "win32")
    monkeypatch.setattr(onboarding_module.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_driver,
        "binding_detail",
        lambda: {"state": "voice_only", "voice": "winusb", "keyboard": "other"},
    )
    w = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(w)

    # GeneralPage displays granular Interface 3 & Interface 4 status
    w.general.refresh_voice_binding()
    assert "Interface 3" in w.general.voice_binding_pill.text()
    assert "WinUSB" in w.general.voice_interface_pill.text()
    assert "未绑定" in w.general.keyboard_interface_pill.text()

    # VoicePage exposes virtual cable playback selector when outputs exist on Windows
    w.controller.outputs = [
        ("CABLE Input (VB-Audio Virtual Cable)", b"cable-a"),
        ("CABLE-B Input (VB-Audio Cable B)", b"cable-b"),
    ]
    w.controller.voice.output_id = b"cable-a"
    w.controller.settings["voice_source"] = "remote"
    w.voice_page.refresh()
    assert not w.voice_page.output_device.isHidden()
    assert w.voice_page.output_device.count() == 2

    # OnboardingDialog modal navigation buttons close the modal first (no navigation trap)
    dialog = onboarding_module.OnboardingDialog(w.controller)
    qtbot.addWidget(dialog)
    dialog.show_page(1)
    nav = []
    dialog.open_voice.connect(lambda: nav.append("voice"))
    btn = next(b for b in dialog.findChildren(QPushButton) if b.text() == "前往语音设置")
    btn.click()
    assert nav == ["voice"] and not w.controller.onboarding_needed()

    # MainWindow.restore_from_tray preserves WindowMaximized state
    w.setWindowState(Qt.WindowState.WindowMaximized)
    w.restore_from_tray()
    assert bool(w.windowState() & Qt.WindowState.WindowMaximized)

    # Confirm no non-ASCII glyph regressions in general settings labels
    assert any("系统托盘" in lbl.text() for lbl in w.general.findChildren(QLabel))
    w.shutdown()


def test_windows_auto_frontmost_exe_picker_and_installed_app_tuple(qtbot):
    from remotehub.ui.ax_pickers import AUTO_FRONT_APP_ID, UiControlPickerDialog
    from remotehub.win_uia import InstalledWinApp

    item = InstalledWinApp("exe:kimi.exe", "Kimi", r"C:\Users\Public\Kimi\Kimi.exe")
    assert item.app_id == "exe:kimi.exe" and item.name == "Kimi" and item[0] == "exe:kimi.exe"

    kimi_app = App("exe:kimi.exe", 4242, "Kimi")

    class FakeKimiBackend:
        def frontmost(self):
            return kimi_app

        def mouse_point(self):
            return (120.0, 240.0)

        def prepare(self, app, abort):
            return None

        def window(self, app):
            return "win_root"

        def element_at_cursor(self, app):
            return "new_chat_btn"

        def read(self, el, attr):
            if el == "win_root" and attr == "AXRole":
                return "AXWindow"
            if el == "new_chat_btn":
                return {
                    "AXRole": "AXButton",
                    "AXTitle": "新建会话",
                    "AXEnabled": True,
                    "AXParent": "win_root",
                    "AXWindow": "win_root",
                }.get(attr)
            return None

        def children(self, el):
            return ["new_chat_btn"] if el == "win_root" else []

        def actions(self, el):
            return {"AXPress"} if el == "new_chat_btn" else set()

        def visible(self, el, win):
            return True

        def modal(self, win):
            return False

    picker = UiControlPickerDialog(
        apps_provider=lambda: [],
        backend_factory=lambda: FakeKimiBackend(),
    )
    qtbot.addWidget(picker)
    assert picker.app_box.currentData() == AUTO_FRONT_APP_ID
    picker.start_capture()
    qtbot.waitUntil(lambda: not picker._capturing and picker.save_button.isEnabled(), timeout=2500)
    picker._accept_action()
    saved = picker.result_action()
    assert saved is not None
    assert saved.application == "exe:kimi.exe"
    assert saved.spec["app_name"] == "Kimi"
    assert saved.spec["label"] == "新建会话"
