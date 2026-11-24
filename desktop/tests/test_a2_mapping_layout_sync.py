"""Tests for A2: hide add-button on generic remotes and sync layout edits across all profiles."""
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from remotehub.model import Action, AppLayer, Button, Profile
from remotehub.photo_template import television_profile
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import RemoteLibraryStore, remote_for_device
from remotehub.ui import MainWindow


def _make_photo(tmp_path, name="new-photo.png", color=Qt.GlobalColor.darkGray):
    path = tmp_path / name
    img = QImage(140, 280, QImage.Format.Format_RGB32)
    img.fill(color)
    assert img.save(str(path))
    return path


def _two_profile_window(qtbot, tmp_path, device):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    c = window.controller
    profile = Profile(
        name="方案 1",
        vendor=device.vendor,
        product=device.product,
        image=television_profile().image,
        buttons=[
            Button(id="k1", name="键一", trigger="hid:7:82", rect=[0.1, 0.1, 0.2, 0.1]),
            Button(id="k2", name="键二", trigger="hid:7:40", rect=[0.1, 0.3, 0.2, 0.1]),
        ],
    )
    remote = remote_for_device("通用遥控器", device, profile)
    assert c.add_remote(remote, device)
    assert c.create_profile("方案 2")
    assert c.index == 1
    c.select("k1")
    assert c.assign(Action("text", text="keep-b1"))
    c.select("k2")
    assert c.assign(Action("text", text="keep-b2"))
    c.profile.app_layers.append(
        AppLayer(id="layer-1", app_id="com.example.app", name="Example", overrides={"k2": {"single": Action("shortcut", ["space"])}})
    )
    assert c.save()
    c.select_profile(0)
    return window, c


@pytest.mark.fresh_config
@pytest.mark.parametrize(
    "device",
    [
        ProbeDevice(("usb", 1), "通用 USB 遥控器", "USB", 0x1234, 0x5678, "a" * 64, (1,)),
        ProbeDevice(("usb", 2), "0416:0602 接收器", "USB", 0x0416, 0x0602, "b" * 64, (2,)),
    ],
)
def test_generic_remote_has_no_add_button_in_mapping_inspector(qtbot, tmp_path, device):
    window, _c = _two_profile_window(qtbot, tmp_path, device)
    window.show()
    window.show_mapping()
    ins = window.inspector
    assert not ins.more.isHidden()
    ins.more.click()
    assert ins.details.isVisible()
    assert not hasattr(ins, "add_button_control")
    assert ins.remove_button_control.isVisible()
    window.shutdown()


@pytest.mark.fresh_config
def test_mapping_layout_edits_sync_across_all_profiles_and_rollback_on_save_failure(qtbot, tmp_path, monkeypatch):
    device = ProbeDevice(("usb", 1), "通用 USB 遥控器", "USB", 0x1234, 0x5678, "a" * 64, (1,))
    window, c = _two_profile_window(qtbot, tmp_path, device)

    c.select("k1")
    c.rename("上键")
    p2 = c.profiles[1]
    assert next(b for b in p2.buttons if b.id == "k1").name == "上键"
    assert next(b for b in p2.buttons if b.id == "k1").action == Action("text", text="keep-b1")

    c.set_rect([0.2, 0.25, 0.3, 0.15])
    assert next(b for b in p2.buttons if b.id == "k1").rect == pytest.approx([0.2, 0.25, 0.3, 0.15])
    assert next(b for b in p2.buttons if b.id == "k1").action == Action("text", text="keep-b1")

    new_photo = _make_photo(tmp_path)
    c.replace_photo(new_photo)
    assert p2.image == c.profile.image
    assert next(b for b in p2.buttons if b.id == "k1").action == Action("text", text="keep-b1")

    c.select("k2")
    c.remove_button()
    assert [b.id for b in p2.buttons] == ["k1"]
    assert "k2" not in p2.app_layers[0].overrides
    assert p2.buttons[0].action == Action("text", text="keep-b1")

    # Persisted on disk across reload.
    reloaded = RemoteLibraryStore(tmp_path)
    reloaded.load()
    assert len(reloaded.remotes[0].profiles) == 2
    rp2 = reloaded.remotes[0].profiles[1]
    assert rp2.image == c.profile.image
    assert [b.id for b in rp2.buttons] == ["k1"]
    assert rp2.buttons[0].name == "上键"
    assert rp2.buttons[0].action == Action("text", text="keep-b1")

    # Failure rollback preserves both current and sibling profiles cleanly.
    c.select("k1")
    snapshot_p1 = c.profiles[0].dumps()
    snapshot_p2 = c.profiles[1].dumps()
    monkeypatch.setattr(c.store, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    alt_photo = _make_photo(tmp_path, "alt.png", Qt.GlobalColor.blue)
    c.rename("不应生效")
    c.set_rect([0.01, 0.02, 0.1, 0.1])
    c.replace_photo(alt_photo)
    c.remove_button()
    assert c.profiles[0].dumps() == snapshot_p1
    assert c.profiles[1].dumps() == snapshot_p2
    window.shutdown()
