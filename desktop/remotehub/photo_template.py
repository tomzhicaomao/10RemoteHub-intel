"""User photo and fixed key codes verified from the real receiver's reports."""
import base64
import json
from pathlib import Path
import re
from .model import Action, Button, Profile
from .remote_library import RECEIVER_MODEL, TRIGGER

PACKAGE_DIRECTORY = Path(__file__).parent
KEYMAP=json.loads((PACKAGE_DIRECTORY/"assets/tv-keymap.json").read_text(encoding="utf-8"))


def is_fixed_model(target, *, model_id=None):
    """The adapted model (usb-0416-0602) ships a verified key table; its keys are not learned."""
    if target is None:
        return False
    if model_id is not None:
        return model_id == RECEIVER_MODEL
    if hasattr(target, "model_id"):
        return target.model_id == RECEIVER_MODEL
    return (
        (target.vendor, target.product) == (KEYMAP["vendor"], KEYMAP["product"])
        and any(b.id in KEYMAP["keys"] or b.id in KEYMAP["excluded"] for b in getattr(target, "buttons", ()))
    )


def apply_fixed_keys(profile, *, model_id=None):
    """Refresh fixed identities; the physical voice key always controls voice."""
    if not is_fixed_model(profile, model_id=model_id):
        return False
    changed = False
    for button in profile.buttons:
        if button.id in KEYMAP["keys"] or button.id in KEYMAP["excluded"]:
            trigger = KEYMAP["keys"].get(button.id, {}).get("trigger", "")
            if button.trigger != trigger:
                button.trigger = trigger
                changed = True
        if is_voice_button(profile, button, model_id=model_id):
            if button.action != Action("voice"):
                button.action = Action("voice")
                changed = True
            for name in ("double_action", "long_action"):
                if getattr(button, name).kind != "none":
                    setattr(button, name, Action())
                    changed = True
    for layer in getattr(profile, "app_layers", ()):
        for button in profile.buttons:
            if (not button.configurable or is_voice_button(profile, button, model_id=model_id)) and button.id in layer.overrides:
                layer.overrides.pop(button.id, None)
                changed = True
    return changed


def is_voice_button(profile, button, *, model_id=None):
    return bool(
        button
        and is_fixed_model(profile, model_id=model_id)
        and (button.id == "tv-voice" or button.matches_trigger(KEYMAP["keys"]["tv-voice"]["trigger"]))
    )

def is_reserved_voice_key(remote, profile, button):
    """The key that starts and ends the handset microphone, so it cannot be remapped.

    The bundled model reserves its fixed voice key on its own receiver pair. A photo-learned
    remote with a built-in voice protocol (``Remote.voice_protocol``) reserves the key the user
    marked as 「语音」 on the photo.
    """
    from .remote_library import RECEIVER_PAIR

    if button is None or remote is None:
        return False
    if is_fixed_model(remote):
        return ((profile.vendor, profile.product) == RECEIVER_PAIR
                and is_voice_button(profile, button, model_id=RECEIVER_MODEL))
    return bool(getattr(remote, "voice_protocol", "") and button.role == "voice")


def bind_protocol_voice_key(profile, voice_protocol):
    """Give the marked 「语音」 key the receiver's voice key code; return the voice key or None.

    The ``usb-0416-0602`` receiver always reports the handset voice key as one fixed code, so
    the user never has to press it while learning (on shared HID that press re-enumerates the
    receiver). Bluetooth voice protocols start the microphone from their own voice events and
    keep the marked key as a photo position only.
    """
    voice = next((b for b in profile.buttons if b.role == "voice"), None)
    if voice is None:
        return None
    if voice_protocol == RECEIVER_MODEL and not voice.trigger:
        code = KEYMAP["keys"]["tv-voice"]["trigger"]
        learned = next((b for b in profile.buttons if b is not voice and b.matches_trigger(code)), None)
        if learned is not None:
            # The voice key was pressed while learning another position: that frame is the voice key.
            profile.buttons.remove(voice)
            learned.role = "voice"
            voice = learned
        else:
            voice.trigger = code
    if voice.trigger:
        voice.action = Action("voice")
        voice.double_action = Action()
        voice.long_action = Action()
    return voice


def _model_profile(model):
    width, height = model.canvas
    if not width or not height or not model.regions:
        raise ValueError("遥控器预设缺少有效的照片键位")
    layout = json.loads((PACKAGE_DIRECTORY / model.keymap).read_text(encoding="utf-8"))
    if (layout.get("vendor", model.vendor), layout.get("product", model.product)) != model.pair:
        raise ValueError("遥控器预设的键表与型号不匹配")
    keys = layout["keys"]
    profile = Profile(name=model.name, vendor=model.vendor, product=model.product)
    profile.image = base64.b64encode((PACKAGE_DIRECTORY / model.image).read_bytes()).decode("ascii")
    voice_button = model.voice.get("button")
    for key, name, x, y, w, h in model.regions:
        identity = model.button_id_prefix + key
        entry = keys.get(identity, {})
        trigger = entry.get("trigger", "")
        raw_extra = entry.get("extra_triggers", [])
        if not isinstance(raw_extra, list) or any(not isinstance(code, str) for code in raw_extra):
            raise ValueError(f"遥控器预设「{name}」的附加键码格式无效")
        from .key_roles import DEFAULT_ID_TO_ROLE

        role = "voice" if voice_button == identity else DEFAULT_ID_TO_ROLE.get(identity, "")
        button = Button(
            id=identity,
            name=name,
            trigger=trigger,
            extra_triggers=list(raw_extra),
            rect=[x / width, y / height, w / width, h / height],
            role=role,
        )
        if button.configurable and (
            not TRIGGER.fullmatch(trigger) or any(not TRIGGER.fullmatch(code) for code in raw_extra)
        ):
            raise ValueError(f"遥控器预设缺少「{name}」的实体键码")
        if button.configurable and voice_button == identity:
            button.action = Action("voice")
        profile.buttons.append(button)
    if voice_button and not any(key.action.kind == "voice" for key in profile.buttons):
        raise ValueError("遥控器预设缺少语音键")
    profile.validate()
    return profile


def television_profile():
    from . import models

    model = models.by_pair(KEYMAP["vendor"], KEYMAP["product"])
    if model is None or not model.regions:
        raise RuntimeError("缺少这款遥控器的模型定义 remotehub/models/")
    profile = _model_profile(model)
    profile.id = "tv-photo"
    profile.name = "USB 0416:0602"
    apply_fixed_keys(profile, model_id=RECEIVER_MODEL)
    return profile
