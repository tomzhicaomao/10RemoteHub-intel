"""Offline Chinese keypad lookup using the bundled Pinyin Simplified dictionary."""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from functools import cache
from heapq import nlargest
from pathlib import Path

KEY_LABELS = ("", "ABC", "DEF", "GHI", "JKL", "MNO", "PQRS", "TUV", "WXYZ")
DIGITS = "0123456789"
KEYPAD_ROWS = (
    ("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"),
    ("symbols", "0", "mode"),
)
KEYPAD_ROLES = tuple(role for row in KEYPAD_ROWS for role in row)
CONTROL_ROLES = ("left", "confirm", "right")
BINDING_ROWS = (*KEYPAD_ROWS, CONTROL_ROLES)
BINDING_ROLES = KEYPAD_ROLES + CONTROL_ROLES
KEYPAD_LABELS = {**{digit: digit for digit in DIGITS}, "symbols": "*", "mode": "#"}
BINDING_KEY_NAMES = {**KEYPAD_LABELS, "left": "左", "right": "右", "confirm": "确定", "back": "返回"}
BINDING_LABELS = {
    **{digit: digit for digit in DIGITS},
    "symbols": "* 标点", "mode": "# 切换", "left": "左选词",
    "confirm": "确定上屏", "right": "右选词", "back": "退格 / 退出",
}
DEFAULT_BUTTON_IDS = {
    **{digit: f"tv-number-{digit}" for digit in DIGITS},
    "symbols": "tv-number-*", "mode": "tv-number-#", "left": "tv-left",
    "confirm": "tv-ok", "right": "tv-right", "back": "tv-back",
}
MODE_LABELS = {"pinyin": "拼音", "abc": "ABC", "number": "123"}
SYMBOLS = ("，", "。", "？", "！", "、", "：", "；", "…", "（", "）", "「", "」",
           ",", ".", "?", "!", ":", "/", "-", "@", "#", "*")
PINYIN_TO_DIGITS = str.maketrans("abcdefghijklmnopqrstuvwxyzü", "222333444555666777788899998", " ")
CANDIDATE_LIMIT = 24
MAX_DIGITS = 48
MULTITAP_SECONDS = 0.7


def validate_bindings(spec):
    if not spec:
        return
    bindings = spec.get("bindings")
    if set(spec) != {"bindings"} or not isinstance(bindings, dict):
        raise ValueError("九键键位配置格式无效")
    if any(role not in BINDING_LABELS or not isinstance(ident, str) or not ident
           for role, ident in bindings.items()):
        raise ValueError("九键键位配置包含无效按键")
    if len(set(bindings.values())) != len(bindings):
        raise ValueError("一个实体键只能绑定一个九键位置")


def missing_bindings(bindings, roles):
    return tuple(role for role in roles if role not in bindings)


@dataclass(frozen=True)
class Candidate:
    text: str
    pinyin: str
    consumed: int
    weight: int


class PinyinDictionary:
    def __init__(self, path: Path):
        by_code: dict[str, list[Candidate]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) != 3 or not fields[2].isdecimal():
                continue
            word, pinyin, weight = fields
            code = pinyin.lower().translate(PINYIN_TO_DIGITS)
            if code and code.isascii() and code.isdigit():
                by_code.setdefault(code, []).append(Candidate(word, pinyin, len(code), int(weight)))
        if not by_code:
            raise ValueError("九键词库为空")
        self.by_code = {code: nlargest(CANDIDATE_LIMIT, entries, key=lambda item: item.weight)
                        for code, entries in by_code.items()}
        self.codes = sorted(self.by_code)

    def lookup(self, digits: str) -> tuple[Candidate, ...]:
        if not digits:
            return ()
        # Prefer full matches. Keep some space for longer words and for committing
        # the first part of a phrase that is missing from the bundled dictionary.
        exact = self.by_code.get(digits, [])
        first = bisect_left(self.codes, digits)
        last = bisect_left(self.codes, digits + ":")
        completions = nlargest(CANDIDATE_LIMIT,
                               (item for code in self.codes[first:last] if code != digits
                                for item in self.by_code[code]),
                               key=lambda item: item.weight)
        partial = []
        for end in range(len(digits) - 1, 0, -1):
            part = self.by_code.get(digits[:end], [])
            if part:
                partial = part[:4]
                break
        unique = {}
        for item in (*exact[:16], *partial, *completions, *exact[16:]):
            candidate = Candidate(item.text, item.pinyin, min(item.consumed, len(digits)), item.weight)
            unique.setdefault(candidate.text, candidate)
            if len(unique) >= CANDIDATE_LIMIT:
                break
        return tuple(unique.values())


@cache
def dictionary() -> PinyinDictionary:
    path = Path(__file__).with_name("assets") / "pinyin-simp-dictionary.txt"
    return PinyinDictionary(path)


class Composition:
    def __init__(self, clock):
        self.clock = clock
        self.modes = tuple(MODE_LABELS)
        self.mode = self.modes[0]
        self.drafts = dict.fromkeys(self.modes, "")
        self.candidates: tuple[Candidate, ...] = ()
        self.selected = 0
        self.symbol_index = None
        self.last_digit = ""
        self.last_tap = 0.0
        self.notice = ""

    @property
    def text(self):
        return self.drafts[self.mode]

    @property
    def selected_candidate(self):
        return self.candidates[self.selected] if self.candidates else None

    @property
    def display_candidates(self):
        if self.symbol_index is not None:
            return SYMBOLS, self.symbol_index
        return tuple(candidate.text for candidate in self.candidates), self.selected

    def update(self, text):
        self.drafts[self.mode] = text
        self.notice = ""
        self.selected = 0
        if self.mode == "pinyin":
            try:
                self.candidates = dictionary().lookup(text) if text else ()
            except (OSError, ValueError):
                self.candidates = ()
                self.notice = "无法加载九键词库"
        else:
            self.candidates = (Candidate(text, "", len(text), 0),) if text else ()

    def cycle_mode(self):
        index = self.modes.index(self.mode)
        self.mode = self.modes[(index + 1) % len(self.modes)]
        self.symbol_index = None
        self.last_digit = ""
        self.update(self.text)

    def move(self, offset):
        if self.symbol_index is not None:
            self.symbol_index = (self.symbol_index + offset) % len(SYMBOLS)
        elif self.candidates:
            self.selected = (self.selected + offset) % len(self.candidates)

    def enter_digit(self, digit):
        if self.mode == "number":
            self.update((self.text + digit)[:MAX_DIGITS])
        elif self.mode == "abc":
            if digit == "0":
                self.last_digit = ""
                self.update((self.text + " ")[:MAX_DIGITS])
            elif digit != "1":
                letters = KEY_LABELS[int(digit) - 1].lower()
                now = self.clock()
                within_multitap = self.last_digit == digit and now - self.last_tap < MULTITAP_SECONDS
                if self.text and within_multitap and self.text[-1] in letters:
                    index = (letters.index(self.text[-1]) + 1) % len(letters)
                    self.update(self.text[:-1] + letters[index])
                else:
                    self.update((self.text + letters[0])[:MAX_DIGITS])
                self.last_digit, self.last_tap = digit, now
        elif digit == "1":
            self.move(1)
        elif digit != "0":
            self.update((self.text + digit)[:MAX_DIGITS])

    def confirmation(self, suffix=""):
        candidate = self.selected_candidate
        if self.symbol_index is not None:
            suffix = SYMBOLS[self.symbol_index]
        if not suffix:
            return candidate
        if candidate is not None and candidate.consumed == len(self.text):
            return Candidate(candidate.text + suffix, candidate.pinyin, candidate.consumed, candidate.weight)
        return Candidate(suffix, "", 0, 0)

    def accept(self, candidate):
        self.symbol_index = None
        self.last_digit = ""
        self.update(self.text[candidate.consumed:])

    def go_back(self):
        if self.symbol_index is not None:
            self.symbol_index = None
        elif self.text:
            self.last_digit = ""
            self.update(self.text[:-1])
        else:
            return False
        return True
