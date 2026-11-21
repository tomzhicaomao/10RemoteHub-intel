"""Comprehensive Linux adaptation tests: actions, XDG, inventory, udev, X11/Wayland, collector & audio."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from remotehub import autostart, common_actions, permissions
from remotehub.actions import KeyOutput, LinuxUInputBackend, launch_command
from remotehub.input_codes import LinuxKeys
from remotehub.inventory import USBDevice, linux_inventory
from remotehub.linux_audio_route import LinuxInputRoute
from remotehub.linux_collector import LinuxCollector
from remotehub.linux_desktop import (
    is_wayland_session,
    launch_desktop_entry_command,
    match_x11_window_to_layer_app,
    parse_desktop_file,
    scan_desktop_entries,
)
from remotehub.app_layers import LayerApp
from remotehub.model import Action
from remotehub.photo_template import KEYMAP
from remotehub.presentation import palette
from remotehub.ui import MainWindow
from remotehub.ui import onboarding as onboarding_module
from remotehub.virtual_mic import LINUX_FEED_NODE, LINUX_MIC_NODE, VirtualMic
from remotehub.windows import WindowController, X11Windows


def test_window_operations_that_need_a_desktop_specific_shortcut_are_not_offered():
    available = common_actions.available_actions("linux")
    assert "overview" not in available and "dock" not in available
    assert "hide_current_app" not in available and "file_explorer" not in available
    assert "copy" in available and "screenshot" in available and "lock_screen" in available


def test_linux_palette_covers_all_available_actions_once():
    ids = [ident for _, items in palette("linux") for ident, _ in items]
    assert len(ids) == len(set(ids))
    assert {x[7:] for x in ids if x.startswith("common:")} == set(common_actions.available_actions("linux"))
    assert "ui_element" not in ids and "app_menu" not in ids


def test_common_keys_use_linux_conventions():
    assert common_actions.common_keys("copy", "linux") == ["ctrl", "c"]
    assert common_actions.common_keys("switch_app", "linux") == ["alt", "tab"]
    assert common_actions.common_keys("previous_tab", "linux") == ["ctrl", "shift", "tab"]
    assert common_actions.common_keys("screenshot", "linux") == ["print_screen"]
    assert common_actions.common_keys("fullscreen", "linux") == ["f11"]
    with pytest.raises(RuntimeError, match="Wayland|窗口总览"):
        common_actions.common_keys("overview", "linux")


def test_file_paths_use_xdg_open_without_a_shell(tmp_path):
    file = tmp_path / "合同 $(touch bad); ' &.pdf"
    file.touch()
    assert launch_command(Action("file", target=str(file)), "linux") == ["xdg-open", str(file)]
    assert not (tmp_path / "bad").exists()


def test_desktop_entry_launches_through_gio_and_rejects_open_with_desktop(tmp_path, monkeypatch):
    entry = tmp_path / "Example.desktop"
    entry.write_text("[Desktop Entry]\nType=Application\nName=Example\nExec=/usr/bin/example %U\n", encoding="utf-8")
    assert launch_command(Action("app", target=str(entry)), "linux") == ["gio", "launch", str(entry)]
    monkeypatch.setattr(
        "remotehub.linux_desktop.shutil.which",
        lambda name: None if name in ("gio", "gtk-launch") else f"/usr/bin/{name}",
    )
    assert launch_desktop_entry_command(entry) == ["/usr/bin/example"]

    note = tmp_path / "note.txt"
    note.touch()
    with pytest.raises(ValueError, match=r"\.desktop"):
        launch_command(Action("file", target=str(note), application=str(entry)), "linux")


def test_open_with_uses_the_selected_executable(tmp_path):
    file = tmp_path / "note.txt"
    file.touch()
    editor = tmp_path / "editor"
    editor.touch()
    assert launch_command(Action("file", target=str(file), application=str(editor)), "linux") == [str(editor), str(file)]


def test_missing_target_fails_before_launch(tmp_path):
    with pytest.raises(FileNotFoundError):
        launch_command(Action("file", target=str(tmp_path / "missing")), "linux")


def test_xdg_autostart_registration_round_trip_in_temporary_home(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    args = ["/opt/10RemoteHub/10RemoteHub", "--start-hidden"]
    assert not autostart.is_enabled(home=tmp_path)
    autostart.set_enabled(True, arguments=args, home=tmp_path)
    path = autostart.startup_path(home=tmp_path)
    assert path == tmp_path / ".config/autostart/org.remotehub.desktop.desktop"
    assert 'Exec="/opt/10RemoteHub/10RemoteHub" "--start-hidden"' in path.read_text(encoding="utf-8")
    assert autostart.is_enabled(home=tmp_path)
    autostart.set_enabled(False, arguments=args, home=tmp_path)
    assert not autostart.is_enabled(home=tmp_path)


def test_desktop_exec_escapes_reserved_characters_and_rejects_newlines():
    text = autostart.desktop_exec(['/tmp/a "$`%\\z', "--start-hidden"])
    assert "%%" in text and "\\$" in text and "\\`" in text
    assert "\\\\z" in text
    with pytest.raises(ValueError):
        autostart.desktop_exec(["/tmp/a\nHidden=true"])


def test_linux_inventory_reads_descriptor_files_without_claiming(tmp_path):
    device = tmp_path / "1-2"
    device.mkdir()
    for name, value in {
        "idVendor": "0416",
        "idProduct": "0602",
        "bcdDevice": "0100",
        "serial": "receiver-1",
        "product": "Receiver",
    }.items():
        (device / name).write_text(value, encoding="utf-8")
    (tmp_path / ("1-2_iface" if os.name == "nt" else "1-2:1.0")).mkdir()
    assert linux_inventory(tmp_path) == [USBDevice(0x0416, 0x0602, "receiver-1", "Receiver", "1-2", 0x0100)]


def test_original_hid_scans_match_every_fixed_key():
    normalizer = LinuxKeys()
    for number, evidence in enumerate(KEYMAP["keys"].values(), 100):
        trigger = evidence["trigger"]
        _, page, usage = trigger.split(":")
        assert normalizer.feed(4, 4, int(page) << 16 | int(usage)) == []
        assert normalizer.feed(1, number, 1) == [(trigger, True)]
        assert normalizer.feed(0, 0, 0) == []
        assert normalizer.feed(1, number, 2) == []
        assert normalizer.feed(1, number, 0) == [(trigger, False)]
    assert not normalizer.held


def test_fallback_is_device_scoped_and_dropped_events_release_keys():
    one, two = LinuxKeys(), LinuxKeys()
    one.feed(4, 4, 0x0C00E9)
    assert two.feed(1, 2, 1) == [("hid:7:30", True)]
    assert one.feed(1, 115, 1) == [("hid:12:233", True)]
    assert one.feed(0, 3, 0) == [("hid:12:233", False)]
    one.feed(4, 4, 0x0C00E9)
    one.feed(0, 0, 0)
    assert one.feed(1, 999, 1) == []


def test_linux_udev_rule_uses_least_privilege_uaccess_and_read_only_permission_check(tmp_path):
    rule = permissions.bundled_udev_rule_path()
    assert rule.is_file()
    content = rule.read_text(encoding="utf-8")
    assert 'TAG+="uaccess"' in content
    assert "0416" in content and "0602" in content and "uinput" in content
    assert "0666" not in content

    sysfs = tmp_path / "sysfs"
    dev_bus = tmp_path / "devbus"
    (sysfs / "1-4").mkdir(parents=True)
    for k, v in {"idVendor": "0416", "idProduct": "0602", "busnum": "1", "devnum": "7"}.items():
        (sysfs / "1-4" / k).write_text(v, encoding="utf-8")
    usb_node = dev_bus / "001" / "007"
    usb_node.parent.mkdir(parents=True)
    usb_node.write_bytes(b"")
    uinput = tmp_path / "uinput"
    uinput.write_bytes(b"")
    fake_rule = tmp_path / "70-10remotehub.rules"
    fake_rule.write_text(content, encoding="utf-8")

    st = permissions.linux_access_status(
        sysfs_root=sysfs,
        dev_bus_root=dev_bus,
        uinput_path=uinput,
        rule_paths=(fake_rule,),
    )
    assert st == {"udev_rule": "已安装", "usb": "已授权", "uinput": "已授权"}


def test_linux_desktop_catalog_and_x11_layer_matching(tmp_path):
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    good = apps_dir / "org.mozilla.firefox.desktop"
    good.write_text(
        "[Desktop Entry]\nType=Application\nName=Firefox\nName[zh_CN]=火狐浏览器\n"
        "Exec=/usr/bin/firefox %u\nStartupWMClass=firefox\n",
        encoding="utf-8",
    )
    hidden = apps_dir / "hidden.desktop"
    hidden.write_text("[Desktop Entry]\nType=Application\nName=Hidden\nNoDisplay=true\nExec=/bin/true\n", encoding="utf-8")

    parsed = parse_desktop_file(good)
    assert parsed is not None
    assert parsed.app_id == "org.mozilla.firefox"
    assert parsed.name == "火狐浏览器"
    entries = scan_desktop_entries(dirs=[apps_dir])
    assert [e.app_id for e in entries] == ["org.mozilla.firefox"]

    matched = match_x11_window_to_layer_app("/usr/lib/firefox/firefox", ("Navigator", "firefox"), entries)
    assert matched is not None and LayerApp(*matched) == LayerApp("org.mozilla.firefox", "火狐浏览器")
    fallback = match_x11_window_to_layer_app("/usr/bin/mpv", ("gl", "mpv"), entries)
    assert fallback is not None and LayerApp(*fallback) == LayerApp("exe:mpv", "mpv")


def test_linux_terminal_smart_paste_and_wayland_uinput_backend(qapp, monkeypatch):
    chords = []
    keys = KeyOutput(sleeper=lambda _: None)
    monkeypatch.setattr(keys, "shortcut", lambda seq: chords.append(list(seq)))
    monkeypatch.setattr("remotehub.actions.sys.platform", "linux")
    monkeypatch.setattr("remotehub.actions._use_linux_uinput", lambda: False)

    monkeypatch.setattr("remotehub.linux_desktop.is_x11_foreground_terminal", lambda: True)
    keys.input_text("echo hello")
    assert chords[-1] == ["ctrl", "shift", "v"]

    monkeypatch.setattr("remotehub.linux_desktop.is_x11_foreground_terminal", lambda: False)
    keys.input_text("normal text")
    assert chords[-1] == ["ctrl", "v"]

    events = []

    class FakeDev:
        def write(self, etype, code, value):
            events.append((etype, code, value))

        def syn(self):
            events.append(("syn",))

        def close(self):
            events.append(("close",))

    uinput = LinuxUInputBackend(sleeper=lambda _: None, uinput_factory=lambda **kw: FakeDev())
    uinput.send_key("print_screen", True)
    uinput.send_key("print_screen", False)
    uinput.send_mouse_button("left", True)
    uinput.send_mouse_button("left", False)
    uinput.scroll(-3)
    uinput.close()
    assert ("syn",) in events and ("close",) in events

    broken = LinuxUInputBackend(sleeper=lambda _: None, uinput_factory=lambda **kw: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(PermissionError, match="/dev/uinput"):
        broken.send_key("a", True)


def test_linux_virtual_mic_pactl_and_pw_loopback_lifecycle(qapp, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[1:] == ["list", "short", "modules"]:
            out = f"41\tmodule-null-sink\tsink_name={LINUX_FEED_NODE}\n"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if cmd[1] == "load-module" and cmd[2] == "module-null-sink":
            return SimpleNamespace(returncode=0, stdout="77\n", stderr="")
        if cmd[1] == "load-module" and cmd[2] == "module-remap-source":
            return SimpleNamespace(returncode=0, stdout="78\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("remotehub.virtual_mic.shutil.which", lambda name: f"/usr/bin/{name}" if name == "pactl" else None)
    monkeypatch.setattr("remotehub.virtual_mic.subprocess.run", fake_run)
    mic = VirtualMic()
    mic.start_linux()
    assert ["pactl", "unload-module", "41"] in [[Path(c[0]).name, *c[1:]] for c in calls]
    assert mic._pactl_modules == [77, 78]
    mic.stop()
    normalized = [[Path(c[0]).name, *c[1:]] for c in calls[-2:]]
    assert normalized == [["pactl", "unload-module", "78"], ["pactl", "unload-module", "77"]]


def test_linux_input_route_respects_user_mid_session_override(qapp):
    state = {"default": "alsa_input.pci-0000_00_1f.3.analog-stereo"}

    def fake_runner(args):
        if args == ["list", "short", "sources"]:
            return (
                f"1\talsa_input.pci-0000_00_1f.3.analog-stereo\tPipeWire\n"
                f"2\t{LINUX_MIC_NODE}\tPipeWire\n"
                f"3\tbluez_input.headset\tPipeWire\n"
            )
        if args == ["get-default-source"]:
            return state["default"]
        if args[0] == "set-default-source":
            state["default"] = args[1]
            return ""
        return ""

    route = LinuxInputRoute(runner=fake_runner)
    try:
        ok, msg, _ = route._acquire_sync()
        assert ok and not msg
        assert state["default"] == LINUX_MIC_NODE

        route._restore_sync()
        assert state["default"] == "alsa_input.pci-0000_00_1f.3.analog-stereo"

        # Simulate the user manually switching microphones during an active session:
        route._acquire_sync()
        assert state["default"] == LINUX_MIC_NODE
        state["default"] = "bluez_input.headset"
        route._restore_sync()
        assert state["default"] == "bluez_input.headset"
    finally:
        route.close()


def test_linux_collector_hid_and_voice_protocol_and_reattach(qapp, monkeypatch):
    import time as time_mod
    import libusb_package
    import usb.core
    import usb.util
    from usb.backend import libusb1

    detached = []
    reattached = []
    claimed = []
    released = []
    collected = []
    keys_seen = []
    descriptor = bytes.fromhex("0507190029ff150025ff750895018100")

    key_packets = iter([b"\x1e", b"\x00", b"\xcc", b"\x00"])
    voice_packets = iter([
        b"\x01\x82\x03" + bytes(61),
        b"\x01\x08\x01\x00" + bytes(60),
        b"\x01\x01\x01\x00" + bytes(60),
        b"\x01\x81" + bytes(62),
        b"\x01\x86\x00" + bytes(61),
    ])

    collector = LinuxCollector()

    class FakeKeyEndpoint:
        def __init__(self, address, active=False):
            self.bEndpointAddress = address
            self.active = active

        def read(self, *args, **kwargs):
            if self.active and collector.capture_ready:
                try:
                    return next(key_packets)
                except StopIteration:
                    pass
            time_mod.sleep(0.02)
            raise usb.core.USBTimeoutError("timeout")

    class FakeVoiceEndpoint:
        bEndpointAddress = 0x84

        def read(self, *args, **kwargs):
            if not collector.capture_ready:
                time_mod.sleep(0.16)
                raise usb.core.USBTimeoutError("drain")
            try:
                return next(voice_packets)
            except StopIteration:
                time_mod.sleep(0.08)
                collector.halt.set()
                raise usb.core.USBTimeoutError("done")

    class FakeDevice:
        def is_kernel_driver_active(self, num):
            return True

        def detach_kernel_driver(self, num):
            detached.append(num)

        def attach_kernel_driver(self, num):
            reattached.append(num)

        def get_active_configuration(self):
            return {
                (1, 0): [FakeKeyEndpoint(0x81, active=True)],
                (2, 0): [FakeKeyEndpoint(0x82)],
                (3, 0): [FakeVoiceEndpoint()],
                (4, 0): [FakeKeyEndpoint(0x85)],
            }

        def ctrl_transfer(self, *args, **kwargs):
            return descriptor

        def write(self, endpoint, data, timeout=1000):
            return len(data)

    fake_device = FakeDevice()
    monkeypatch.setattr(libusb_package, "find_library", lambda *a: None)
    monkeypatch.setattr(libusb1, "get_backend", lambda **kw: object())
    monkeypatch.setattr(usb.core, "find", lambda **kw: [fake_device])
    monkeypatch.setattr(usb.util, "claim_interface", lambda dev, num: claimed.append(num))
    monkeypatch.setattr(usb.util, "release_interface", lambda dev, num: released.append(num))
    monkeypatch.setattr(usb.util, "dispose_resources", lambda dev: None)

    collector.key.connect(lambda t, d: keys_seen.append((t, d)))
    collector.collected.connect(collected.append)
    collector._run()
    qapp.processEvents()

    assert set(detached) == {1, 2, 3, 4}
    assert set(claimed) == {1, 2, 3, 4}
    assert set(released) == {1, 2, 3, 4}
    assert set(reattached) == {1, 2, 3, 4}
    assert ("hid:7:30", True) in keys_seen and ("hid:7:204", True) in keys_seen
    types_emitted = [item["type"] for item in collected]
    assert "start" in types_emitted and "audio" in types_emitted and "end" in types_emitted


@pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("Xvfb"), reason="requires Linux Xvfb")
def test_native_x11_foreground_hide_show_close(monkeypatch):
    from Xlib import X, Xatom, display

    read, write = os.pipe()
    process = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write), "-screen", "0", "800x600x24", "-nolisten", "tcp"],
        pass_fds=(write,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    os.close(write)
    try:
        import select
        if not select.select([read], [], [], 5)[0]:
            raise RuntimeError("Xvfb did not start")
        number = os.read(read, 30).decode().strip()
        monkeypatch.setenv("DISPLAY", ":" + number)
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        d = display.Display()
        root = d.screen().root
        root.change_attributes(event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        w = root.create_window(0, 0, 100, 100, 0, d.screen().root_depth, X.InputOutput, X.CopyFromParent)
        w.change_property(d.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()])
        root.change_property(d.intern_atom("_NET_CLIENT_LIST_STACKING"), Xatom.WINDOW, 32, [w.id])
        root.change_property(d.intern_atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW, 32, [w.id])
        d.sync()
        backend = X11Windows()
        active = backend.foreground()
        assert active is not None and active.pid == os.getpid()
        assert backend.find(str(Path(sys.executable).resolve()))
        backend.hide(active)
        backend.show(active)
        backend.close(active)
        backend.d.sync()
        d.sync()
        kinds = []
        while d.pending_events():
            e = d.next_event()
            if e.type == X.ClientMessage:
                kinds.append(d.get_atom_name(e.client_type))
        assert "WM_CHANGE_STATE" in kinds and "_NET_ACTIVE_WINDOW" in kinds and "_NET_CLOSE_WINDOW" in kinds
        backend.d.close()
        d.close()
    finally:
        os.close(read)
        process.terminate()
        process.wait(timeout=5)


def test_wayland_session_refuses_cross_app_control_and_toggle_launches_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert is_wayland_session()
    with pytest.raises(RuntimeError, match="Wayland"):
        X11Windows()

    app_file = tmp_path / "demo.desktop"
    app_file.write_text("[Desktop Entry]\nType=Application\nName=Demo\nExec=/bin/true\n", encoding="utf-8")
    monkeypatch.setattr("remotehub.windows.sys.platform", "linux")
    wc = WindowController()
    launched = []
    msg = wc.toggle(str(app_file), lambda: launched.append(True))
    assert launched == [True] and "Wayland" in msg


def test_linux_onboarding_and_general_udev_copy(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("remotehub.ui.general.sys.platform", "linux")
    monkeypatch.setattr(onboarding_module.sys, "platform", "linux")
    w = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(w)
    w.general.copy_linux_udev_command()
    clip = QApplication.instance().clipboard().text()
    assert "70-10remotehub.rules" in clip and "udevadm" in clip

    dialog = onboarding_module.OnboardingDialog(w.controller)
    qtbot.addWidget(dialog)
    dialog.show_page(1)
    text = " ".join(lbl.text() for lbl in dialog.findChildren(QLabel))
    assert "udev" in text and "PipeWire" in text
    nav = []
    dialog.open_settings.connect(lambda: nav.append("settings"))
    btn = next(b for b in dialog.findChildren(QPushButton) if b.text() == "前往通用设置")
    btn.click()
    assert nav == ["settings"] and not w.controller.onboarding_needed()
    w.shutdown()
