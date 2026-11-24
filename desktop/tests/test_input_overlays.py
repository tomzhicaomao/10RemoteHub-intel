"""Nine-key sessions through receiver events and non-activating display placement."""
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QPalette

from remotehub.model import Action, Button, Profile
from remotehub.nine_key import BINDING_KEY_NAMES, BINDING_ROLES, dictionary
from remotehub.photo_template import KEYMAP
from remotehub.remote_library import RemoteLibraryStore
from remotehub.theme import TOKENS
from remotehub.ui.input_overlays import overlay_position
from remotehub.ui.window import MainWindow


def press(controller, role):
    trigger = KEYMAP["keys"][role]["trigger"]
    controller.on_key(trigger, True)
    controller.on_key(trigger, False)


def type_code(controller, code):
    for digit in code:
        press(controller, f"tv-number-{digit}")


def test_bundled_dictionary_finds_words_and_consumes_parts():
    lexicon = dictionary()
    candidates = lexicon.lookup("64426")
    assert (candidates[0].text, candidates[0].pinyin, candidates[0].consumed) == ("你好", "ni hao", 5)
    assert any(candidate.text == "你" and candidate.consumed == 2 for candidate in candidates)
    assert lexicon.lookup("999999999999999") == ()


def test_remote_nine_key_action_and_custom_confirmation(tmp_path, qtbot, monkeypatch):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    controller = window.controller
    inserted = []
    shortcuts = []
    monkeypatch.setattr(controller.keys, "input_text", inserted.append)
    monkeypatch.setattr(controller.keys, "shortcut", shortcuts.append)
    controller.select("tv-number-6")
    controller.assign(Action("shortcut", ["space"]))
    controller.select("tv-m")
    window.inspector.choose_kind("nine_key")
    assert controller.selected.action.kind == "nine_key"

    press(controller, "tv-m")
    assert window.nine_key_osd.isVisible()
    assert window.nine_key_osd.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    press(controller, "tv-number-6")
    assert window.nine_key_osd.key_labels[5].property("selected") is True
    type_code(controller, "4426")
    assert inserted == [] and shortcuts == []
    assert window.nine_key_osd.candidates[0].text() == "你好"
    # Light card like the other overlays: the chosen candidate is the accent chip, text is ink.
    assert window.nine_key_osd.candidates[0].palette().color(QPalette.ColorRole.WindowText).name() == "#ffffff"
    assert window.nine_key_osd.candidates[1].palette().color(QPalette.ColorRole.WindowText).name() == TOKENS["INK"].lower()
    assert window.nine_key_osd.composition.palette().color(QPalette.ColorRole.WindowText).name() == TOKENS["INK"].lower()
    press(controller, "tv-right")
    assert controller.overlays.nine_key_state.selected == 1
    press(controller, "tv-left")
    press(controller, "tv-ok")
    assert inserted == ["你好"]
    assert controller.overlays.nine_key_state.text == ""

    type_code(controller, "644269")
    press(controller, "tv-back")
    press(controller, "tv-number-*")
    assert window.nine_key_osd.candidates[0].text() == "，"
    press(controller, "tv-right")
    press(controller, "tv-ok")
    assert inserted == ["你好", "你好。"]

    press(controller, "tv-number-#")
    state = controller.overlays.nine_key_state
    assert state.mode == "abc"
    state.clock = lambda: 0.0
    type_code(controller, "223")
    press(controller, "tv-ok")
    assert inserted[-1] == "bd"
    press(controller, "tv-number-#")
    type_code(controller, "102")
    press(controller, "tv-ok")
    assert state.mode == "number" and inserted[-1] == "102"
    press(controller, "tv-number-#")
    assert state.mode == "pinyin"

    panel = window.general.input_overlay_settings
    controls = panel.fields
    controls["nine_compact"].setCurrentIndex(controls["nine_compact"].findData(True))
    panel.opacity.setValue(68)
    panel.opacity.editingFinished.emit()
    assert window.nine_key_osd.keypad.isHidden()
    press(controller, "tv-back")
    assert not window.nine_key_osd.isVisible()

    window.inspector.nine_key_setup.click()
    editor = window.inspector.nine_key_editor
    editor.slots["2"].click()
    assert editor.instruction.text() == "请在左侧遥控器图上点「2」键。"
    editor.slots["confirm"].click()
    assert editor.instruction.text() == "请在左侧遥控器图上点「确定」键。"
    window.canvas.callouts["tv-number-2"].click()
    assert editor.bindings["confirm"] == "tv-ok"
    assert "已登记为「2」" in editor.instruction.text()
    window.canvas.callouts["tv-menu"].click()
    assert controller.selected_id == "tv-m"
    editor.saved.emit(dict(editor.bindings))
    assert controller.selected.action.spec["bindings"]["confirm"] == "tv-menu"

    press(controller, "tv-m")
    type_code(controller, "64426")
    press(controller, "tv-ok")
    assert inserted[-1] == "102"
    press(controller, "tv-menu")
    assert inserted[-1] == "你好"
    press(controller, "tv-back")
    press(controller, "tv-number-6")
    assert shortcuts == [["space"]]
    store = RemoteLibraryStore(tmp_path)
    store.load()
    assert store.settings["input_overlays"] == {"nine_compact": True, "opacity": 68}


def test_generic_editor_uses_one_photo_prompt_for_every_key(tmp_path, qtbot, monkeypatch):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    controller = window.controller
    messages = []
    controller.message.connect(lambda message, error: messages.append(message) if error else None)
    opener = Button(id="launch", name="打开", trigger="hid:7:104")
    learned = {
        role: Button(id=f"learned-{index}", name=f"按键 {index + 30}",
                     trigger=f"hid:7:{105 + index}")
        for index, role in enumerate(BINDING_ROLES)
    }
    profile = Profile(id="learned", name="新遥控器", vendor=1, product=2, buttons=[opener, *learned.values()])
    controller.store.profiles[:] = [profile]
    controller.select(opener.id)
    window.inspector.choose_kind("nine_key")

    def tap(key):
        controller.on_key(key.trigger, True)
        controller.on_key(key.trigger, False)

    tap(opener)
    assert messages and "全部 15 个键" in messages[-1]
    assert not controller.overlays.nine_key_open
    window.inspector.nine_key_setup.click()
    editor = window.inspector.nine_key_editor
    assert editor.active and editor.bindings == {}
    assert not editor.save.isEnabled()
    editor.slots["right"].click()
    assert editor.instruction.text() == "请在左侧遥控器图上点「右」键。"
    window.canvas.callouts[learned["right"].id].click()
    for role in BINDING_ROLES[:-1]:
        assert editor.instruction.text() == f"请在左侧遥控器图上点「{BINDING_KEY_NAMES[role]}」键。"
        if role == "2":
            window.canvas.callouts[learned["1"].id].click()
            assert "2" not in editor.bindings
        window.canvas.callouts[learned[role].id].click()
    assert all(slot.isEnabled() for slot in editor.slots.values())
    assert editor.progress.text() == "已登记 15 / 15"
    editor.save.click()
    assert not editor.active
    assert controller.selected.action.spec["bindings"] == {role: key.id for role, key in learned.items()}
    reloaded = Profile.loads(profile.dumps())
    assert reloaded.buttons[0].action == controller.selected.action

    inserted = []
    monkeypatch.setattr(controller.keys, "input_text", inserted.append)
    for btn in (opener, learned["6"], learned["4"], learned["confirm"]):
        tap(btn)
    assert inserted == ["你"]
    tap(opener)
    assert not controller.overlays.nine_key_open


def test_paste_failure_preserves_composition_and_foreground_change_cancels(tmp_path, qtbot, monkeypatch):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    controller = window.controller
    foreground = "example.chat"
    monkeypatch.setattr(controller.foreground_apps, "current_app_id", lambda: foreground)

    def unavailable(_text):
        raise RuntimeError("paste unavailable")

    monkeypatch.setattr(controller.keys, "input_text", unavailable)
    controller.select("tv-m")
    controller.assign(Action("nine_key"))
    press(controller, "tv-m")
    type_code(controller, "64426")
    press(controller, "tv-ok")
    assert controller.overlays.nine_key_state.text == "64426"
    assert window.nine_key_osd.hint.text() == "无法上屏，请检查输入权限"
    foreground = "example.other"
    press(controller, "tv-ok")
    assert not controller.overlays.nine_key_open


def test_voice_status_uses_only_known_session_state(tmp_path, qtbot):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    controller = window.controller
    hud = window.voice_osd
    assert not hud.isVisible()

    controller.recording_requested = True
    controller.voice_changed.emit()
    assert hud.isVisible()
    assert hud.status.text() == "准备麦克风"
    assert hud.level.isHidden()

    controller.voice.owner = "usb"
    controller.voice_changed.emit()
    controller.level.emit(0.39)
    assert hud.status.text() == "正在录音"
    assert hud.level.value() == 39

    controller.voice.owner = "trigger"
    controller.voice.recognition_started = True
    controller.voice_changed.emit()
    assert hud.status.text() == "已触发识别"
    assert hud.level.isHidden()

    controller.voice.owner = None
    controller.recording_requested = False
    controller.voice_changed.emit()
    assert not hud.isVisible()


def test_placement_fits_next_to_input_or_uses_chosen_corner():
    screen = QRect(-900, 0, 900, 640)
    field = QRect(-295, 210, 280, 36)
    caret = QRect(-63, 215, 1, 24)
    assert overlay_position(screen, 280, 90, "input", "top_left", field, caret) == QPoint(-288, 253)
    field.moveTop(594)
    assert overlay_position(screen, 280, 90, "input", "top_left", field) == QPoint(-295, 496)
    assert overlay_position(screen, 280, 90, "input", "top_left") == QPoint(-892, 8)
    assert overlay_position(screen, 280, 90, "bottom_right", "top_left") == QPoint(-288, 542)
