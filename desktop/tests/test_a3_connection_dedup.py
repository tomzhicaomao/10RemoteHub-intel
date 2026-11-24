"""Tests for A3: Connection deduplication in Remote.validate() includes vendor_id_source."""
import pytest

from remotehub.model import Button, Profile
from remotehub.photo_template import television_profile
from remotehub.probe_device import ProbeDevice
from remotehub.remote_library import Connection, Remote, RemoteLibraryStore, remote_for_device, with_connection

BT_SIG = ProbeDevice(("hid", 21), "蓝牙 620A:0407 (1)", "蓝牙", 0x620A, 0x0407, connection=(21,), vendor_id_source=1)
BT_USBIF = ProbeDevice(("hid", 22), "蓝牙 620A:0407 (2)", "蓝牙", 0x620A, 0x0407, connection=(22,), vendor_id_source=2)
BT_NONE = ProbeDevice(("hid", 23), "蓝牙 620A:0407 (无来源)", "蓝牙", 0x620A, 0x0407, connection=(23,), vendor_id_source=None)


def _bt_profile(name="蓝牙 620A:0407"):
    return Profile(
        name=name,
        vendor=0x620A,
        product=0x0407,
        image=television_profile().image,
        buttons=[Button(id="ok", name="确定", trigger="hid:7:40", rect=[0.1, 0.1, 0.2, 0.1])],
    )


@pytest.mark.fresh_config
def test_remote_validate_allows_same_vid_pid_with_distinct_vendor_id_source(tmp_path):
    remote = remote_for_device("蓝牙 620A:0407", BT_SIG, _bt_profile())
    with_connection(remote, BT_USBIF)
    assert len(remote.connections) == 2
    remote.validate()

    store = RemoteLibraryStore(tmp_path)
    store.load()
    store.add(remote)

    reloaded = RemoteLibraryStore(tmp_path)
    reloaded.load()
    assert not reloaded.load_error
    assert len(reloaded.remotes) == 1
    assert [c.vendor_id_source for c in reloaded.remotes[0].connections] == [1, 2]
    assert reloaded.remotes[0].accepts(BT_SIG)
    assert reloaded.remotes[0].accepts(BT_USBIF)
    assert not reloaded.remotes[0].accepts(BT_NONE)


def test_remote_validate_rejects_duplicate_connection_with_same_vendor_id_source():
    dup_sig = Remote(
        name="重复 SIG",
        connections=[
            Connection("蓝牙", 0x620A, 0x0407, vendor_id_source=1),
            Connection("蓝牙", 0x620A, 0x0407, vendor_id_source=1),
        ],
        profiles=[_bt_profile()],
    )
    with pytest.raises(ValueError, match="同一种接收端只能登记一次"):
        dup_sig.validate()

    dup_none = Remote(
        name="重复 None",
        connections=[
            Connection("蓝牙", 0x620A, 0x0407, vendor_id_source=None),
            Connection("蓝牙", 0x620A, 0x0407, vendor_id_source=None),
        ],
        profiles=[_bt_profile()],
    )
    with pytest.raises(ValueError, match="同一种接收端只能登记一次"):
        dup_none.validate()
