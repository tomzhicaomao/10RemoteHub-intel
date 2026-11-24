"""Live key highlight on the mapping photo: painting it must never raise (a raise mid-paint
leaves the painter open and crashed the macOS backing store flush)."""
import re
from pathlib import Path

from remotehub.canvas import RemoteCanvas
from remotehub.photo_template import television_profile
from remotehub.theme import TOKENS


def test_pressed_key_highlight_paints(qtbot):
    canvas = RemoteCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(420, 640)
    profile = television_profile()
    canvas.set_profile(profile, profile.buttons[0].id)
    key = next(b for b in profile.buttons if b.configurable)
    canvas.set_pressed(key.id, True)
    assert not canvas.grab().isNull()      # pytest-qt fails the test on an exception in paintEvent
    canvas.set_pressed(key.id, False)
    assert not canvas.grab().isNull()


def test_every_color_token_used_in_code_exists():
    root = Path(__file__).resolve().parents[1] / "remotehub"
    used = set()
    for path in root.rglob("*.py"):
        used |= set(re.findall(r'qcolor\(\s*"([A-Z_]+)"', path.read_text(encoding="utf-8")))
    assert used and not (used - set(TOKENS)), sorted(used - set(TOKENS))
