import sys
import pytest
from PySide6.QtCore import Qt
from remotehub.model import validate_keys
from remotehub.presentation import key_label
from remotehub.session import VoiceSession
from remotehub.store import VOICE_MODES, validate_settings
from remotehub.ui import MainWindow
from remotehub.ui.voice import wetype_preset


def test_fn_key_validation_and_labels():
    validate_keys(["fn"], allow_modifier=True, platform="darwin")
    validate_keys(["fn", "space"], platform="darwin")
    with pytest.raises(ValueError):
        validate_keys(["fn"], allow_modifier=True, platform="win32")
    with pytest.raises(ValueError):
        validate_keys(["fn"], allow_modifier=True, platform="linux")
    assert key_label(["fn"], "darwin") == "Fn"
    assert key_label(["fn", "space"], "darwin") == "Fn + Space"
    assert key_label(["ctrl", "cmd"], "win32") == "Ctrl + Win"


def test_double_tap_voice_mode_in_store_and_session():
    assert "double_tap" in VOICE_MODES
    validate_settings({"voice_keys": "fn", "voice_mode": "double_tap", "voice_source": "trigger"})

    class FakeSignal:
        def connect(self, *_):
            pass

    class FakeAudio:
        ended = FakeSignal()
        failed = FakeSignal()

    class FakeKeys:
        def __init__(self):
            self.events = []

        def sleeper(self, seconds):
            self.events.append(("sleep", seconds))

        def press(self, keys):
            self.events.append(("press", list(keys)))

        def release(self):
            self.events.append(("release",))

    audio = FakeAudio()
    keys = FakeKeys()
    session = VoiceSession(audio, keys)
    session.trigger_keys = ["fn"]
    session.trigger_mode = "double_tap"

    session.start("trigger")
    assert keys.events == [
        ("press", ["fn"]),
        ("release",),
        ("sleep", 0.06),
        ("press", ["fn"]),
        ("release",),
    ]
    keys.events.clear()
    session.finish("trigger")
    assert keys.events == [
        ("press", ["fn"]),
        ("release",),
        ("sleep", 0.06),
        ("press", ["fn"]),
        ("release",),
    ]


def test_wetype_preset_updates_voice_settings(qtbot, tmp_path):
    preset = wetype_preset()
    if preset is None:
        pytest.skip("No WeType preset defined for this platform")
    _, expected = preset
    w = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(w)
    w.controller.update_voice(voice_source="trigger", voice_keys="alt", voice_mode="toggle")
    assert w.voice_page.wetype_preset is not None
    w.voice_page.wetype_preset.click()
    s = w.controller.voice_settings()
    assert s["voice_keys"] == expected["voice_keys"]
    assert s["voice_mode"] == expected["voice_mode"]
    assert w.inspector.voice_editor.shortcut.keys == expected["voice_keys"].split("+")
    w.shutdown()


def test_configure_wetype_persists_settings(tmp_path):
    from remotehub.remote_library import RemoteLibraryStore
    from remotehub.wetype_setup import configure_wetype

    res = configure_wetype(tmp_path, platform="darwin", virtual_mic_ready=True)
    assert res["settings"]["voice_keys"] == "fn"
    assert res["settings"]["voice_mode"] == "hold"
    assert res["settings"]["record_mode"] == "hold"
    assert res["settings"]["input_routing"] == "automatic"
    assert res["settings"]["voice_source"] == "remote"

    store = RemoteLibraryStore(tmp_path)
    store.load()
    assert store.settings["voice_keys"] == "fn"
    assert store.settings["voice_source"] == "remote"
