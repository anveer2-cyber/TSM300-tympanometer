"""The values of a hearing sheet as the operator types them, for correcting a result or typing one in.

One cell of the editor holds one number. What may be typed:

    threshold   45      90n  90 NR  90↓  (n after the number: no response; ㅜ on a Korean keyboard)
    masking     60      ?   (masked, the level not known)       empty: not masked
    speech      level and masking as above; score 0-100 %; words repeated correctly / words given

This module reads and writes that text and says, in Korean, what is wrong with a cell. It knows
nothing of the window; ``editor`` puts it in tables.
"""

from __future__ import annotations

import re

from .protocol import TonePoint
from .record import TypedSpeechPoint

LEVELS = (-10, 120)
"""dB HL a threshold, a speech level or a masking level may have."""
SCORES = (0, 100)
WORDS = (1, 100)

NO_RESPONSE = "↓"
UNKNOWN_MASKING = "?"
_THRESHOLD = re.compile(r"([+-]?\d+)\s*(↓|nr|n|ㅜㄱ|ㅜ|v)?", re.IGNORECASE)
_UNKNOWN_MASKING = {"?", "m", "ㅡ"}  # ㅡ is the M key on a Korean keyboard


class CellError(ValueError):
    """What is wrong with what was typed in a cell, in words for the operator. ``field`` says which
    of a point's cells it is."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


def number(text: str, low: int, high: int, field: str = "") -> int | None:
    """A whole number between ``low`` and ``high``; None for an empty cell."""
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        value = int(cleaned)
    except ValueError:
        raise CellError(f"'{cleaned}' 는 숫자가 아닙니다", field) from None
    if not low <= value <= high:
        raise CellError(f"{value} 는 {low} ~ {high} 사이가 아닙니다", field)
    return value


def parse_threshold(text: str) -> tuple[int, bool] | None:
    """(dB HL, no response) from ``45`` or ``90n``; None for an empty cell."""
    cleaned = text.strip()
    if not cleaned:
        return None
    found = _THRESHOLD.fullmatch(cleaned)
    if found is None:
        raise CellError(f"'{cleaned}' 를 읽을 수 없습니다. 숫자만, 무반응은 숫자 뒤에 n (예: 90n)", "threshold")
    level = number(found.group(1), *LEVELS, field="threshold")
    return level, found.group(2) is not None


def format_threshold(level: int, no_response: bool) -> str:
    return f"{level}{NO_RESPONSE}" if no_response else str(level)


def parse_masking(text: str, field: str = "masking") -> tuple[bool, int | None]:
    """(masked, masking dB) from ``60`` or ``?``; (False, None) for an empty cell."""
    cleaned = text.strip()
    if not cleaned:
        return False, None
    if cleaned.lower() in _UNKNOWN_MASKING:
        return True, None
    return True, number(cleaned, *LEVELS, field=field)


def format_masking(masked: bool, level: int | None) -> str:
    if not masked:
        return ""
    return UNKNOWN_MASKING if level is None else str(level)


def toggled_no_response(text: str) -> str:
    """The threshold text with no response marked, or unmarked if it was; unchanged when it holds no number."""
    try:
        read = parse_threshold(text)
    except CellError:
        return text
    if read is None:
        return text
    level, silent = read
    return format_threshold(level, not silent)


def tone_point(frequency: int, threshold: str, masking: str) -> TonePoint | None:
    """The point of a threshold cell and the masking cell under it; None when both are empty."""
    read = parse_threshold(threshold)
    masked, level = parse_masking(masking)
    if read is None:
        if masked:
            raise CellError("차폐만 있고 역치가 없습니다", "masking")
        return None
    db, silent = read
    return TonePoint(frequency, db, level, masked, silent)


def tone_texts(point: TonePoint | None) -> tuple[str, str]:
    """The threshold and masking cells of a point."""
    if point is None:
        return "", ""
    return format_threshold(point.level, point.no_response), format_masking(point.masked, point.masking)


SPEECH_FIELDS = ("level", "score", "masking", "correct", "words")
SPEECH_HEADINGS = ("강도 (dB HL)", "정답률 (%)", "차폐 (dB)", "맞힌 단어", "단어 수")


def speech_point(level: str, score: str, masking: str, correct: str, words: str) -> TypedSpeechPoint | None:
    """A speech point from its five cells; None when all are empty. An empty score with the words
    filled in is worked out from them."""
    if not any(text.strip() for text in (level, score, masking, correct, words)):
        return None
    db = number(level, *LEVELS, field="level")
    if db is None:
        raise CellError("강도가 없습니다", "level")
    percent = number(score, *SCORES, field="score")
    masked, masking_db = parse_masking(masking)
    right = number(correct, 0, WORDS[1], field="correct")
    given = number(words, *WORDS, field="words")
    if right is not None and given is None:
        raise CellError("맞힌 단어 수와 단어 수는 함께 넣습니다", "words")
    if given is not None and right is None:
        raise CellError("맞힌 단어 수와 단어 수는 함께 넣습니다", "correct")
    if right is not None and right > given:
        raise CellError(f"맞힌 단어 수({right})가 단어 수({given})보다 많습니다", "correct")
    if percent is None and given:
        percent = round(100 * right / given)
    return TypedSpeechPoint(db, percent, masking_db, masked, right, given_words=given)


def speech_texts(point) -> tuple[str, str, str, str, str]:
    """The five cells of a speech point."""
    if point is None:
        return "", "", "", "", ""

    def shown(value) -> str:
        return "" if value is None else str(value)

    return (str(point.level), shown(point.score), format_masking(point.masked, point.masking), shown(point.correct),
            shown(point.words))


def srt(text: str) -> int | None:
    return number(text, *LEVELS, field="srt")
