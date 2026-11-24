"""Tests for protocol-first RemoteListPage: shows built-in Protocol Library, never unplugged product cards."""
import pytest

from remotehub.controller import AppController
from remotehub.ui.remote_list import RemoteListPage


@pytest.mark.fresh_config
def test_remote_list_shows_protocol_library_without_unplugged_product_cards(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("remotehub.protocols.dongle_route_available", lambda: True)
    ctl = AppController(tmp_path, auto_mapping=False)
    try:
        page = RemoteListPage(ctl)
        qtbot.addWidget(page)

        assert not hasattr(page, "catalog_cards")
        # Same plain access levels as the add dialog, not protocol names.
        access = page.access_label.text()
        assert all(level in access for level in ("直接接入", "点一下就能接入", "目前无法自动接入"))
        assert "协议" not in access
        mic = page.mic_label.text()
        assert all(level in mic for level in ("直接能用", "换蓝牙棒直连能用", "目前用不了"))
        assert "FIBER0D" in mic and "Android TV" in mic
    finally:
        ctl.shutdown()


@pytest.mark.fresh_config
def test_remote_list_does_not_promise_the_dongle_route_without_its_component(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("remotehub.protocols.dongle_route_available", lambda: False)
    ctl = AppController(tmp_path, auto_mapping=False)
    try:
        page = RemoteListPage(ctl)
        qtbot.addWidget(page)
        assert "蓝牙棒" not in page.access_label.text() and "蓝牙棒" not in page.mic_label.text()
        assert "直接能用" in page.mic_label.text() and "目前用不了" in page.mic_label.text()
    finally:
        ctl.shutdown()
