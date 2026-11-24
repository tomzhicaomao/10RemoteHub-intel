"""Shared test setup.

Most tests exercise the mapping, voice and settings flows of an installation
that already has the bundled receiver remote, i.e. a user upgraded from 0.2.0.
Each test's ``tmp_path`` therefore starts with a 0.2.0 ``profiles.json`` holding
the default receiver profile; the remote library migrates it on first load.
Tests about a fresh installation use ``@pytest.mark.fresh_config``.
"""
from dataclasses import asdict
import json

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "fresh_config: start without the upgraded 0.2.0 receiver remote")


@pytest.fixture(autouse=True)
def upgraded_receiver_config(request, tmp_path):
    if request.node.get_closest_marker("fresh_config"):
        return
    path = tmp_path / "profiles.json"
    if not path.exists():
        from remotehub.model import default_profiles

        data = {"version": 1, "profiles": [asdict(p) for p in default_profiles()], "settings": {}}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def photo_import_uses_original(monkeypatch):
    """Importing a photo opens the modal four-corner dialog. Tests that do not
    exercise it answer "使用原图" instead of blocking; tests about the dialog
    patch ``exec`` themselves."""
    from remotehub.ui.photo_crop import PhotoCropDialog

    def use_original(dialog):
        dialog.original_button.click()
        return dialog.result()

    monkeypatch.setattr(PhotoCropDialog, "exec", use_original)
