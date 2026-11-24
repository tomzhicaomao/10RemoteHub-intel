"""Saving must survive a reader holding the file for a moment (Windows refuses the replace then)."""
from pathlib import Path

import pytest

from remotehub import model


def test_replace_refused_while_a_reader_holds_the_file_is_retried(tmp_path, monkeypatch):
    target = tmp_path / "remotes.json"
    target.write_text("old", encoding="utf-8")
    real_replace, refusals = Path.replace, []

    def replace(self, destination):
        if len(refusals) < 3:                   # e.g. the agent CLI reading remotes.json
            refusals.append(destination)
            raise PermissionError(5, "拒绝访问")
        return real_replace(self, destination)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(model.time, "sleep", lambda _s: None)
    model.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new" and len(refusals) == 3
    assert not list(tmp_path.glob("*.tmp-*"))     # no temp file left


def test_a_file_that_stays_locked_still_fails_without_leaving_a_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "remotes.json"
    target.write_text("old", encoding="utf-8")

    def replace(self, destination):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(model.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        model.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.tmp-*"))
