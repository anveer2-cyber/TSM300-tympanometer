"""One hearing result: the tests received from the audiometer that the operator put on one sheet.

The audiometer sends each test on its own, without the patient or the date (see
docs/hearing/통신_절차.md §4.2). The operator chooses which of the received tests belong to the
patient in the program, and they become one ``HearingResult``: a pure tone test, a speech test
and a high-frequency test, each at most once per ear and transducer. A test that holds only
soundfield curves may be marked as done with hearing aids; its soundfield curves then go on the
sheet as the aided ones (SPKR-A), beside the unaided curves of another test. That is how the
clinic measures before and after the hearing aid: two soundfield tests, the second one with
the aid in, which used to need two sheets.

The values on the sheet may be corrected by hand, and a result may be typed in by hand with
no device at all. Corrections lie over the decoded values and never change the packets: a
result keeps the tests as the device sent them, and a list of ``Correction`` that says, value
by value, what the sheet shows instead. A result typed in by hand (``manual``) has no tests,
and every value on it is such a correction. The sheet shows the corrected values as if they
had come that way; only the saved JSON tells them from the device's.

``record`` turns a result into the part of the saved JSON that is particular to hearing tests,
and ``from_saved`` reads it back from the raw packets and that JSON. The layout is the contract
in docs/hearing/원자료_형식.md.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from . import protocol as p
from .pta import METHODS, Average, average
from .session import fingerprint

EXAM = "hearing"
"""What kind of test the saved result holds, by the name 상쾌한캡처 gives pure tone audiometry."""
FORMAT = 1
"""The version of the hearing record's layout. Fields may be added without changing it;
a field that is removed or changes its meaning gives a new number."""

TONE, SPEECH, HIGH_FREQUENCY = "tone", "speech", "high_frequency"
SRT = "srt"
"""The section of a correction to a speech test's SRT."""
SECTIONS = (TONE, HIGH_FREQUENCY, SPEECH, SRT)
DEVICE, MANUAL = "device", "manual"
"""Where a result's values came from: the audiometer, or typed in by hand."""
SPEECH_POINTS = 10

AIDED = {"Spkr": "SpkrA", "Spkr1": "SpkrA1"}
"""Where a soundfield curve goes when its test was done with hearing aids."""
AVERAGED = ("Ac", "Bc", "Spkr", "SpkrA")
"""The curves whose averages are worked out: air, bone, and soundfield without and with aids."""

KIND_NAMES = {
    "Ac": "기도", "Bc": "골도", "Spkr": "음장", "SpkrA": "음장 보청기", "AcU": "불쾌역치", "AcM": "쾌적역치",
    "Ac1": "기도(둘째 목록)", "Bc1": "골도(둘째 목록)", "Spkr1": "음장(둘째 목록)", "SpkrA1": "음장 보청기(둘째 목록)",
}
EAR_NAMES = {"R": "오른쪽", "L": "왼쪽"}
TYPE_NAMES = {TONE: "순음", SPEECH: "어음", HIGH_FREQUENCY: "고주파 순음"}


TINNITUS_ITEMS = ("pitch_hz", "loudness_db", "threshold_db", "masking_noise_db", "minimum_masking_db_hl",
                  "residual_inhibition_s", "inhibition")
"""The tinnitus tests, as the maker's Korean report lists them (TINNITUS GRAM): pitch matching,
loudness matching, the hearing threshold at that pitch, the masking noise threshold, the
minimum masking level, residual inhibition and its kind."""
TINNITUS_NAMES = {
    "pitch_hz": "이명 주파수 (Hz)", "loudness_db": "이명 크기 (dB)", "threshold_db": "청력역치 (dB)",
    "masking_noise_db": "차폐 잡음 역치 (dB)", "minimum_masking_db_hl": "최소 차폐 크기 (dB HL)",
    "residual_inhibition_s": "잔류 억제 (초)", "inhibition": "억제 특성",
}
TINNITUS_TEXT = 40
"""The most characters a tinnitus value keeps."""
_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)\s*(k(?:hz)?(?![a-z]))?", re.IGNORECASE)


class MergeError(ValueError):
    """The chosen tests cannot go on one sheet. The message says why, for the operator."""


def type_of(test) -> str:
    """``tone``, ``speech``, ``high_frequency``, or ``""`` for a test this program does not draw."""
    if isinstance(test, p.ToneAudiogram):
        return HIGH_FREQUENCY if test.high_frequency else TONE
    if isinstance(test, p.SpeechAudiogram):
        return SPEECH
    return ""


def can_be_aided(test) -> bool:
    """Whether a test holds soundfield curves only, the kind that may be marked as done with hearing aids."""
    if type_of(test) not in (TONE, SPEECH) or not test.curves:
        return False
    return all(curve.kind in AIDED for curve in test.curves)


def describe(test) -> str:
    """What a received test holds, in a few words: 순음 — 기도 R·L, 골도 R."""
    kind = type_of(test)
    if not kind:
        return f"{test.packet.name} (이 프로그램은 결과지로 만들지 않음)"
    by_kind: dict[str, list[str]] = {}
    for curve in test.curves:
        by_kind.setdefault(curve.kind, []).append(curve.ear)
    parts = [f"{KIND_NAMES.get(kind_, kind_)} {'·'.join(ears)}" for kind_, ears in by_kind.items()]
    return f"{TYPE_NAMES[kind]} — {', '.join(parts) if parts else '값 없음'}"


@dataclass(frozen=True)
class Part:
    """One received test on the sheet, with the device's patient it came under."""

    test: object
    """A ``ToneAudiogram`` or ``SpeechAudiogram``."""
    patient: p.DevicePatient | None = None
    aided: bool = False
    """Done with hearing aids: its soundfield curves are the aided ones."""

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.test)

    @property
    def type(self) -> str:
        return type_of(self.test)


@dataclass(frozen=True)
class Placed:
    """A curve where the sheet shows it: ``kind`` is the row it goes in, which for a test done
    with hearing aids is not the one the device gave it."""

    kind: str
    ear: str
    curve: object
    part: int | None
    """Which of the result's parts it came from; None for a curve put in by hand."""


@dataclass(frozen=True)
class PlacedSrt:
    kind: str
    ear: str
    level: int | None
    device_kind: str | None
    part: int | None
    """None for an SRT put in by hand where the device sent none."""


@dataclass(frozen=True)
class TypedSpeechPoint(p.SpeechPoint):
    """A speech point as corrected or typed in by hand. The number of words given is a value of
    its own here, where the device's is worked out from the words repeated and the score."""

    given_words: int | None = None

    @property
    def words(self) -> int | None:
        return self.given_words


@dataclass(frozen=True)
class Correction:
    """One value of the sheet put in by hand, in place of what the device sent or where it sent nothing."""

    section: str
    """``tone``, ``high_frequency``, ``speech`` (a point of a speech curve) or ``srt``."""
    ear: str
    kind: str
    """The row the value is in on the sheet (``SpkrA`` for a soundfield test done with hearing aids)."""
    key: int
    """The frequency of a tone value, the number (from 1, in the order measured) of a speech point; 0 for an SRT."""
    value: object
    """What the sheet shows: a ``TonePoint``, a speech point or an SRT in dB HL. None: nothing."""
    device: object = None
    """What the device sent for it, in the same form; None when it sent nothing there."""
    at: str = ""
    """When it was put in, ``YYYY-MM-DDTHH:MM:SS``."""

    @property
    def cell(self) -> tuple[str, str, str, int]:
        return (self.section, self.ear, self.kind, self.key)


@dataclass(frozen=True)
class Tinnitus:
    """The tinnitus tests, typed in by hand: the device's tinnitus test is not decoded (nor is it by
    the maker's program, whose report has them typed in too). Each value is free text, as the clinic
    writes it: ``1500``, ``28 dB``, ``85 dB (o)``, ``Mo51dB``, ``완전 억제``."""

    entries: tuple[tuple[str, str, str], ...] = ()
    """(ear, item, text) for every value given, in the order of the sheet."""

    def __post_init__(self) -> None:
        kept = {}
        for ear, item, value in self.entries:
            if ear not in EAR_NAMES or item not in TINNITUS_ITEMS:
                raise ValueError(f"이명 값의 자리를 알 수 없습니다 ({ear} {item})")
            cleaned = " ".join(str(value).split())[:TINNITUS_TEXT]
            if cleaned:
                kept[(ear, item)] = cleaned
        ordered = sorted(kept.items(), key=lambda entry: (entry[0][0] != "R", TINNITUS_ITEMS.index(entry[0][1])))
        object.__setattr__(self, "entries", tuple((ear, item, value) for (ear, item), value in ordered))

    @classmethod
    def of(cls, texts: dict) -> Tinnitus:
        """From (ear, item) -> text."""
        return cls(tuple((ear, item, value) for (ear, item), value in texts.items()))

    def text(self, ear: str, item: str) -> str:
        return next((value for e, i, value in self.entries if (e, i) == (ear, item)), "")

    def __bool__(self) -> bool:
        return bool(self.entries)


TINNITUS_GRAPH_HZ = (100, 10000)
TINNITUS_GRAPH_DB = (-10, 120)
"""Where a tinnitus match can go on the audiogram; one outside it is only in the table."""


@dataclass(frozen=True)
class TinnitusPoint:
    """Where the tinnitus match goes on an ear's audiogram."""

    frequency: int | float
    level: int | float
    """dB HL."""
    source: str
    """``HL`` when the loudness was given in dB HL, ``SL`` when above the ear's threshold."""


def tinnitus_value(text: str) -> int | float | None:
    """The number in a tinnitus value as typed: the first one in it, ``1.5k`` read as 1500; None when there is none."""
    found = _NUMBER.search(text or "")
    if found is None:
        return None
    number = float(found.group(1)) * (1000 if found.group(2) else 1)
    return int(number) if number == int(number) else number


@dataclass(frozen=True)
class HearingResult:
    parts: tuple[Part, ...] = ()
    corrections: tuple[Correction, ...] = ()
    """The values put in by hand, over what the parts hold. Settled when the result is made:
    one for each value, each saying what the device sent, none that changes nothing."""
    manual: bool = False
    """Typed in by hand, without the device: no parts, every value a correction."""
    tinnitus: Tinnitus = Tinnitus()
    """The tinnitus tests, always typed in by hand."""
    tone: dict = field(init=False, repr=False, compare=False)
    """(kind, ear) -> ``Placed`` for the pure tone curves, as the sheet shows them."""
    speech: dict = field(init=False, repr=False, compare=False)
    srt: dict = field(init=False, repr=False, compare=False)
    high_frequency: dict = field(init=False, repr=False, compare=False)
    device_values: dict = field(init=False, repr=False, compare=False)
    """Every value as the device sent it, by cell (see ``cells``)."""
    _device: dict = field(init=False, repr=False, compare=False)
    _numbers: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        if not isinstance(self.tinnitus, Tinnitus):
            object.__setattr__(self, "tinnitus", Tinnitus.of(dict(self.tinnitus or {})))
        if not self.parts and not self.manual:
            raise MergeError("고른 검사가 없습니다.")
        if self.parts and self.manual:
            raise MergeError("직접 입력한 결과에는 기기의 검사를 넣지 않습니다.")
        places: dict[str, dict] = {TONE: {}, SPEECH: {}, HIGH_FREQUENCY: {}}
        srt: dict = {}
        for index, part in enumerate(self.parts):
            kind = part.type
            if not kind:
                raise MergeError(f"{part.test.packet.name} 검사는 결과지로 만들 수 없습니다.")
            if part.aided and not can_be_aided(part.test):
                raise MergeError("보청기 착용은 음장만 있는 검사에만 표시할 수 있습니다.")
            for curve in part.test.curves:
                shown = AIDED[curve.kind] if part.aided else curve.kind
                _place(places[kind], (shown, curve.ear), Placed(shown, curve.ear, curve, index), kind)
            if kind == SPEECH:
                for entry in part.test.srt:
                    shown = AIDED.get(entry.kind, entry.kind) if part.aided else entry.kind
                    if (shown, entry.ear) in srt:
                        raise MergeError(f"어음 {KIND_NAMES.get(shown, shown)} {EAR_NAMES[entry.ear]} SRT 가 "
                                         f"두 검사에 들어 있습니다. 하나만 고르세요.{_hint(shown)}")
                    srt[(shown, entry.ear)] = PlacedSrt(shown, entry.ear, entry.level, entry.kind, index)
        places[SRT] = srt
        numbers = {key: tuple(range(1, len(placed.curve.points) + 1)) for key, placed in places[SPEECH].items()}
        device = _cells(places, numbers)
        corrections = _settled(self.corrections, device)
        object.__setattr__(self, "corrections", corrections)
        object.__setattr__(self, "device_values", device)
        object.__setattr__(self, "_device", places)
        shown = _corrected(places, numbers, corrections) if corrections else (places, numbers)
        object.__setattr__(self, "tone", shown[0][TONE])
        object.__setattr__(self, "speech", shown[0][SPEECH])
        object.__setattr__(self, "high_frequency", shown[0][HIGH_FREQUENCY])
        object.__setattr__(self, "srt", shown[0][SRT])
        object.__setattr__(self, "_numbers", shown[1])

    # --- values by hand ----------------------------------------------------------------------

    def cells(self) -> dict:
        """Every value on the sheet, by cell: (section, ear, kind, key) -> value, as in ``Correction``.
        A cell with nothing in it is not there."""
        return _cells({TONE: self.tone, HIGH_FREQUENCY: self.high_frequency, SPEECH: self.speech, SRT: self.srt},
                      self._numbers)

    def with_cells(self, changes: dict, at: datetime) -> HearingResult:
        """The same result with the values in ``changes`` (cell -> value, None for an emptied cell)
        put in by hand at ``at``. Cells left out keep what they show now. A value put back as the
        device sent it stops being a correction; one that is not changed keeps its time."""
        wanted = self.cells()
        wanted.update(changes)
        when = at.isoformat(timespec="seconds")
        earlier = {correction.cell: correction for correction in self.corrections}
        corrections = []
        for cell in set(wanted) | set(self.device_values) | set(earlier):
            value = wanted.get(cell)
            if _same(cell[0], value, self.device_values.get(cell)):
                continue
            before = earlier.get(cell)
            kept = before is not None and _same(cell[0], before.value, value)
            corrections.append(Correction(*cell, value=value, at=before.at if kept else when))
        return HearingResult(self.parts, tuple(corrections), self.manual, self.tinnitus)

    def with_tinnitus(self, tinnitus: Tinnitus) -> HearingResult:
        return HearingResult(self.parts, self.corrections, self.manual, tinnitus)

    def speech_numbers(self, kind: str, ear: str) -> tuple[int, ...]:
        """The numbers (from 1, in the order measured) of the points of a speech curve on the sheet."""
        return self._numbers.get((kind, ear), ())

    def device_curve(self, section: str, kind: str, ear: str):
        """The curve the device sent for a row, before any correction; None when it sent none."""
        placed = self._device[section].get((kind, ear))
        return placed.curve if placed else None

    @property
    def is_empty(self) -> bool:
        """Nothing at all on the sheet."""
        return not (self.tone or self.speech or self.srt or self.high_frequency or self.tinnitus)

    @property
    def has_tinnitus(self) -> bool:
        return bool(self.tinnitus)

    def tinnitus_point(self, ear: str) -> TinnitusPoint | None:
        """The tinnitus match of an ear on its audiogram: at the pitch, as loud as matched in dB HL.

        A loudness in dB SL is put above the ear's air conduction threshold at the pitch, read
        between the two nearest frequencies measured when the pitch itself was not. A loudness
        in dB SPL is not put on the audiogram: there is no conversion to trust. Nor is anything
        that cannot be read, or that falls outside the graph. None then."""
        frequency = tinnitus_value(self.tinnitus.text(ear, "pitch_hz"))
        loudness_text = self.tinnitus.text(ear, "loudness_db")
        loudness = tinnitus_value(loudness_text)
        if frequency is None or loudness is None or not TINNITUS_GRAPH_HZ[0] <= frequency <= TINNITUS_GRAPH_HZ[1]:
            return None
        unit = loudness_text.upper().replace(" ", "")
        if "SPL" in unit:
            return None
        source = "SL" if "SL" in unit else "HL"
        level = loudness
        if source == "SL":
            threshold = _threshold_at(self.tone_curve("Ac", ear), frequency)
            if threshold is None:
                return None
            level = round(threshold + loudness, 1)
            level = int(level) if level == int(level) else level
        if not TINNITUS_GRAPH_DB[0] <= level <= TINNITUS_GRAPH_DB[1]:
            return None
        return TinnitusPoint(frequency, level, source)

    # --- what it holds ---------------------------------------------------------------------

    @property
    def has_tone(self) -> bool:
        return bool(self.tone) or self._has_part(TONE)

    @property
    def has_speech(self) -> bool:
        return bool(self.speech or self.srt) or self._has_part(SPEECH)

    @property
    def has_high_frequency(self) -> bool:
        return bool(self.high_frequency) or self._has_part(HIGH_FREQUENCY)

    def _has_part(self, kind: str) -> bool:
        """A test of this kind was received. Once values are corrected, the sheet goes by what it shows."""
        return not self.corrections and any(part.type == kind for part in self.parts)

    @property
    def has_soundfield(self) -> bool:
        return any(kind.startswith("Spkr") for kind, _ in list(self.tone) + list(self.speech) + list(self.srt))

    @property
    def has_aided(self) -> bool:
        return any(kind.startswith("SpkrA") for kind, _ in list(self.tone) + list(self.speech) + list(self.srt))

    def tone_curve(self, kind: str, ear: str) -> p.ToneCurve | None:
        placed = self.tone.get((kind, ear))
        return placed.curve if placed else None

    def speech_curve(self, kind: str, ear: str) -> p.SpeechCurve | None:
        placed = self.speech.get((kind, ear))
        return placed.curve if placed else None

    def high_frequency_curve(self, kind: str, ear: str) -> p.ToneCurve | None:
        placed = self.high_frequency.get((kind, ear))
        return placed.curve if placed else None

    def srt_of(self, kind: str, ear: str) -> int | None:
        placed = self.srt.get((kind, ear))
        return placed.level if placed else None

    def average(self, kind: str, ear: str, method: str = "6분법") -> Average:
        return average(self.tone_curve(kind, ear), method)

    @property
    def serials(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(part.test.packet.serial for part in self.parts))

    @property
    def device_patients(self) -> tuple[p.DevicePatient, ...]:
        """The device's patients the tests came under, each once, in the order received."""
        seen: dict[bytes, p.DevicePatient] = {}
        for part in self.parts:
            if part.patient is not None:
                seen.setdefault(part.patient.raw, part.patient)
        return tuple(seen.values())

    @property
    def raw(self) -> bytes:
        """The packets as the device sent them: each device patient (``KA``) once, ahead of its tests (``KT``)."""
        out = bytearray()
        current = None
        for part in self.parts:
            if part.patient is not None and part.patient.raw != current:
                out += part.patient.raw
                current = part.patient.raw
            out += part.test.packet.raw
        return bytes(out)

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(part.fingerprint for part in self.parts)


def _place(places: dict, key: tuple[str, str], placed: Placed, kind: str) -> None:
    if key in places:
        shown, ear = key
        what = f"{TYPE_NAMES[kind]} {KIND_NAMES.get(shown, shown)} {EAR_NAMES[ear]}"
        raise MergeError(f"{what} 결과가 두 검사에 들어 있습니다. 하나만 고르세요.{_hint(shown)}")
    places[key] = placed


def _hint(kind: str) -> str:
    if kind not in AIDED:
        return ""
    return " 뒤의 검사가 보청기를 끼고 한 음장검사면 그 검사의 [보청기 착용]에 체크하세요."


# --- corrections ---------------------------------------------------------------------------------

_TONE_LIKE = (TONE, HIGH_FREQUENCY)


def _cells(places: dict, numbers: dict) -> dict:
    """Every value in ``places`` (section -> (kind, ear) -> placed) by cell."""
    cells: dict = {}
    for section in _TONE_LIKE:
        for (kind, ear), placed in places[section].items():
            for point in placed.curve.points:
                cells[(section, ear, kind, point.frequency)] = point
    for (kind, ear), placed in places[SPEECH].items():
        for number, point in zip(numbers.get((kind, ear), ()), placed.curve.points):
            cells[(SPEECH, ear, kind, number)] = point
    for (kind, ear), entry in places[SRT].items():
        if entry.level is not None:
            cells[(SRT, ear, kind, 0)] = entry.level
    return cells


def value_to_dict(section: str, value) -> dict | int | None:
    """A cell's value as the saved record writes it."""
    if value is None:
        return None
    if section in _TONE_LIKE:
        return {"level_db_hl": value.level, "masking_db": value.masking, "masked": value.masked,
                "no_response": value.no_response}
    if section == SPEECH:
        return _speech_point(value)
    return int(value)


def _same(section: str, one, other) -> bool:
    return value_to_dict(section, one) == value_to_dict(section, other)


def _checked(correction: Correction) -> Correction:
    """The correction, or ValueError when it cannot be one: a cell that does not exist, a value of the wrong form."""
    section, ear, kind, key, value = (correction.section, correction.ear, correction.kind, correction.key,
                                      correction.value)
    if section not in SECTIONS or ear not in EAR_NAMES or kind not in KIND_NAMES:
        raise ValueError(f"고친 값의 자리를 알 수 없습니다 ({section} {ear} {kind})")
    if section in _TONE_LIKE:
        if value is not None and not (isinstance(value, p.TonePoint) and value.frequency == key):
            raise ValueError(f"고친 순음 값의 형식이 맞지 않습니다 ({ear} {kind} {key} Hz)")
    elif section == SPEECH:
        if not 1 <= key <= SPEECH_POINTS or (value is not None and not isinstance(value, p.SpeechPoint)):
            raise ValueError(f"고친 어음 값의 형식이 맞지 않습니다 ({ear} {kind} {key})")
    elif value is not None and not isinstance(value, int):
        raise ValueError(f"고친 SRT 의 형식이 맞지 않습니다 ({ear} {kind})")
    return correction


def _settled(corrections, device: dict) -> tuple[Correction, ...]:
    """One correction per cell (the last one given), each with what the device sent there, without
    those that change nothing, in the order of the sheet."""
    latest = {}
    for correction in corrections:
        latest[_checked(correction).cell] = correction
    settled = []
    for cell, correction in latest.items():
        sent = device.get(cell)
        if not _same(cell[0], correction.value, sent):
            settled.append(replace(correction, value=_typed(correction.value), device=sent))
    return tuple(sorted(settled, key=lambda correction: _cell_rank(correction.cell)))


def _typed(value):
    """A speech point given by hand always counts its words itself, however it was made."""
    if isinstance(value, p.SpeechPoint) and not isinstance(value, TypedSpeechPoint):
        return TypedSpeechPoint(value.level, value.score, value.masking, value.masked, value.correct,
                                given_words=value.words)
    return value


def _cell_rank(cell: tuple) -> tuple:
    section, ear, kind, key = cell
    return (SECTIONS.index(section), ear != "R", _rank(kind), key)


def _corrected(places: dict, numbers: dict, corrections: tuple[Correction, ...]) -> tuple[dict, dict]:
    """``places`` and the speech points' numbers as the sheet shows them once ``corrections`` are made."""
    by_row: dict = {}
    for correction in corrections:
        by_row.setdefault((correction.section, correction.kind, correction.ear), []).append(correction)
    shown = {section: dict(rows) for section, rows in places.items()}
    shown_numbers = dict(numbers)
    for (section, kind, ear), changes in by_row.items():
        row = (kind, ear)
        placed = places[section].get(row)
        if section == SRT:
            if changes[-1].value is None:
                shown[SRT].pop(row, None)
            else:
                shown[SRT][row] = PlacedSrt(kind, ear, changes[-1].value, placed.device_kind if placed else None,
                                            placed.part if placed else None)
            continue
        if section == SPEECH:
            values = dict(zip(numbers.get(row, ()), placed.curve.points)) if placed else {}
        else:
            values = {point.frequency: point for point in placed.curve.points} if placed else {}
        for change in changes:
            if change.value is None:
                values.pop(change.key, None)
            else:
                values[change.key] = change.value
        if not values:
            shown[section].pop(row, None)
            shown_numbers.pop(row, None)
            continue
        keys = sorted(values)
        points = tuple(values[key] for key in keys)
        if placed is not None:
            curve = replace(placed.curve, points=points)
        elif section == SPEECH:
            curve = p.SpeechCurve(name=f"{kind} {ear}", ear=ear, kind=kind, stimulus="", masking_signal="",
                                  material=0, points=points)
        else:
            curve = p.ToneCurve(name=f"{kind} {ear}", ear=ear, kind=kind, stimulus="", masking_signal="",
                                pulsed=False, points=points)
        shown[section][row] = Placed(kind, ear, curve, placed.part if placed else None)
        if section == SPEECH:
            shown_numbers[row] = tuple(keys)
    return shown, shown_numbers


def correction_to_dict(correction: Correction) -> dict:
    entry = {"at": correction.at, "section": correction.section, "ear": correction.ear, "kind": correction.kind}
    if correction.section in _TONE_LIKE:
        entry["frequency_hz"] = correction.key
    elif correction.section == SPEECH:
        entry["point"] = correction.key
    entry["device_value"] = value_to_dict(correction.section, correction.device)
    entry["value"] = value_to_dict(correction.section, correction.value)
    return entry


def correction_from_dict(entry: dict) -> Correction:
    """Reads back ``correction_to_dict``. Raises ValueError when it cannot be read."""
    try:
        section = str(entry["section"])
        key = (int(entry["frequency_hz"]) if section in _TONE_LIKE else
               int(entry["point"]) if section == SPEECH else 0)
        value = _value_from_dict(section, key, entry.get("value"))
        return _checked(Correction(section, str(entry["ear"]), str(entry["kind"]), key, value,
                                   at=str(entry.get("at") or "")))
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f"손으로 고친 값을 읽지 못했습니다 ({error})") from None


def _value_from_dict(section: str, key: int, saved):
    if saved is None:
        return None
    if section in _TONE_LIKE:
        return p.TonePoint(key, int(saved["level_db_hl"]), _optional(saved.get("masking_db")),
                           bool(saved.get("masked")), bool(saved.get("no_response")))
    if section == SPEECH:
        return TypedSpeechPoint(int(saved["level_db_hl"]), _optional(saved.get("score_percent")),
                                _optional(saved.get("masking_db")), bool(saved.get("masked")),
                                _optional(saved.get("correct")), given_words=_optional(saved.get("words")))
    if isinstance(saved, bool) or not isinstance(saved, int):
        raise ValueError("SRT 는 정수여야 합니다")
    return saved


def _optional(value) -> int | None:
    return None if value is None else int(value)


# --- the saved record ---------------------------------------------------------------------------

def record(result: HearingResult) -> dict:
    """The hearing part of the saved JSON: every value as the sheet shows it, each value put in by
    hand marked with what the device sent, the corrections themselves, and the packets."""
    patients = list(result.device_patients)
    patient_index = {patient.raw: number for number, patient in enumerate(patients)}
    serials = result.serials
    corrected = {correction.cell: correction for correction in result.corrections}
    return {
        "source": MANUAL if result.manual else DEVICE,
        "device": ({"maker": "MRS", "name": "Resonance", "model": serials[0][:4], "serial": serials[0],
                    "serials": list(serials)} if serials else None),
        "device_patients": [_device_patient(patient) for patient in patients],
        "tests": [_test(index, part, patient_index) for index, part in enumerate(result.parts)],
        "tone": _tone(result, corrected) if result.tone else None,
        "speech": _speech(result, corrected) if result.speech or result.srt else None,
        "high_frequency": ({"curves": [_tone_curve(placed, HIGH_FREQUENCY, corrected)
                                       for placed in _ordered(result.high_frequency)]}
                           if result.high_frequency else None),
        "tinnitus": _tinnitus(result) if result.tinnitus else None,
        "corrections": [correction_to_dict(correction) for correction in result.corrections],
    }


def from_saved(raw: bytes, saved: dict) -> HearingResult:
    """Reads a result back from its raw packets (the .bin) and its record (the .json).

    The packets hold every value the device sent; the record adds what the operator decided:
    which of the tests were done with hearing aids, and the values put in by hand, which are
    made again over the packets. A result typed in by hand has no packets. Raises ValueError
    when the packets or the corrections cannot be read.
    """
    manual = saved.get("source") == MANUAL
    entries = saved.get("corrections") or []
    if not isinstance(entries, list):
        raise ValueError("손으로 고친 값을 읽지 못했습니다")
    corrections = tuple(correction_from_dict(entry) for entry in entries)
    tinnitus = tinnitus_from_record(saved.get("tinnitus"))
    if manual:
        return HearingResult((), corrections, manual=True, tinnitus=tinnitus)
    aided = {str(test.get("fingerprint")) for test in saved.get("tests") or [] if isinstance(test, dict)
             and test.get("aided")}
    decoder = p.StreamDecoder()
    events = decoder.feed(raw) + decoder.flush()
    parts = []
    current = None
    for event in events:
        if isinstance(event, p.Corrupt):
            raise ValueError(f"청력검사 원본 자료가 손상되었습니다 ({event.reason})")
        if isinstance(event, p.Frame) and event.kind == p.KIND_PATIENT:
            current = p.device_patient(event)
        elif isinstance(event, (p.ToneAudiogram, p.SpeechAudiogram)):
            parts.append(Part(event, current, fingerprint(event) in aided))
    if not parts:
        raise ValueError("청력검사 원본 자료에 검사가 없습니다")
    return HearingResult(tuple(parts), corrections, tinnitus=tinnitus)


def device_birth_date(patient: p.DevicePatient | None) -> date | None:
    """The birth date typed on the device (dd/MM/yyyy), or None when there is none or it cannot be read."""
    if patient is None:
        return None
    pieces = patient.birth_date.replace(".", "/").replace("-", "/").split("/")
    try:
        day, month, year = (int(piece) for piece in pieces)
        born = date(year, month, day)
    except (TypeError, ValueError):
        return None
    return born if born.year > 1800 else None


def _device_patient(patient: p.DevicePatient) -> dict:
    born = device_birth_date(patient)
    return {
        "number": patient.number, "pc_number": patient.pc_number, "first_name": patient.first_name,
        "surname": patient.surname, "name": patient.name, "birth_date": patient.birth_date,
        "birth_date_iso": born.isoformat() if born else None, "sex": patient.sex, "packet": patient.raw.hex(),
    }


def _test(index: int, part: Part, patient_index: dict) -> dict:
    packet = part.test.packet
    return {
        "index": index, "type": part.type, "name": packet.name, "mode": packet.mode, "test": packet.test,
        "serial": packet.serial, "model": packet.model, "aided": part.aided,
        "device_patient": patient_index.get(part.patient.raw) if part.patient is not None else None,
        "fingerprint": part.fingerprint, "length": len(packet.raw), "packet": packet.raw.hex(),
    }


_ORDER = ("Ac", "Bc", "Spkr", "SpkrA", "AcU", "AcM", "Ac1", "Bc1", "Spkr1", "SpkrA1")


def _ordered(places: dict) -> list[Placed]:
    """Right ear first, then left; within an ear in the order of the sheet's rows."""
    return sorted(places.values(), key=lambda placed: (placed.ear != "R", _rank(placed.kind)))


def _rank(kind: str) -> int:
    return _ORDER.index(kind) if kind in _ORDER else len(_ORDER)


def _marked(entry: dict, correction: Correction | None) -> dict:
    """A value as printed, with what the device sent when it was put in by hand."""
    if correction is not None:
        entry.update(corrected=True, device_value=value_to_dict(correction.section, correction.device))
    return entry


def _row_marks(entry: dict, section: str, placed, corrected: dict, point_of) -> dict:
    """Marks a curve with a value put in by hand, and lists the device's values that were taken off it."""
    changes = [correction for cell, correction in corrected.items()
               if cell[:3] == (section, placed.ear, placed.kind)]
    if changes:
        entry["corrected"] = True
    removed = [point_of(change) for change in changes if change.value is None and change.device is not None]
    if removed:
        entry["removed_points"] = removed
    return entry


def _tone_curve(placed: Placed, section: str, corrected: dict) -> dict:
    curve = placed.curve
    by_hand = placed.part is None
    points = [_marked({"frequency_hz": point.frequency, "level_db_hl": point.level, "masking_db": point.masking,
                       "masked": point.masked, "no_response": point.no_response},
                      corrected.get((section, placed.ear, placed.kind, point.frequency)))
              for point in curve.points]
    entry = {
        "ear": placed.ear, "kind": placed.kind, "device_kind": None if by_hand else curve.kind, "test": placed.part,
        "stimulus": None if by_hand else curve.stimulus,
        "masking_signal": None if by_hand else curve.masking_signal, "pulsed": None if by_hand else curve.pulsed,
        "points": points,
    }
    return _row_marks(entry, section, placed, corrected,
                      lambda change: dict(frequency_hz=change.key, **value_to_dict(section, change.device)))


def _tone(result: HearingResult, corrected: dict) -> dict:
    averages = []
    for placed in _ordered(result.tone):
        if placed.kind not in AVERAGED:
            continue
        changed = any(cell[:3] == (TONE, placed.ear, placed.kind) for cell in corrected)
        sent = result.device_curve(TONE, placed.kind, placed.ear)
        for method in METHODS:
            entry = dict(ear=placed.ear, kind=placed.kind, **_average(average(placed.curve, method)))
            if changed:
                before = average(sent, method)
                entry.update(corrected=True, device_value=None if sent is None else _average(before))
            averages.append(entry)
    return {"curves": [_tone_curve(placed, TONE, corrected) for placed in _ordered(result.tone)],
            "averages": averages}


def _average(value: Average) -> dict:
    return {
        "method": value.method, "weights": {str(frequency): weight for frequency, weight in METHODS[value.method]},
        "value": None if value.value is None else round(value.value, 4), "shown": value.shown,
        "text": value.text(), "at_least": value.at_least, "missing_hz": list(value.missing),
    }


def _threshold_at(curve, frequency) -> float | None:
    """An air conduction threshold at ``frequency``: measured there, or read on the log-frequency axis
    between the nearest frequencies measured below and above it. Points without a response do not count."""
    if curve is None:
        return None
    heard = sorted((point.frequency, point.level) for point in curve.points if not point.no_response)
    for measured, level in heard:
        if measured == frequency:
            return float(level)
    below = [(measured, level) for measured, level in heard if measured < frequency]
    above = [(measured, level) for measured, level in heard if measured > frequency]
    if not below or not above:
        return None
    (low, low_level), (high, high_level) = below[-1], above[0]
    share = (math.log(frequency) - math.log(low)) / (math.log(high) - math.log(low))
    return low_level + share * (high_level - low_level)


def _tinnitus(result: HearingResult) -> dict:
    """Per ear, every item: the text as typed and the number read from it; and the match on the audiogram."""
    tinnitus = result.tinnitus
    section = {}
    for ear in EAR_NAMES:
        entry = {item: {"text": tinnitus.text(ear, item), "value": tinnitus_value(tinnitus.text(ear, item))}
                 for item in TINNITUS_ITEMS}
        point = result.tinnitus_point(ear)
        entry["plotted"] = (None if point is None else
                            {"frequency_hz": point.frequency, "level_db_hl": point.level, "from": point.source})
        section[ear] = entry
    return section


def tinnitus_from_record(saved) -> Tinnitus:
    """Reads back ``_tinnitus``. Raises ValueError when it cannot be read."""
    if saved is None:
        return Tinnitus()
    try:
        return Tinnitus(tuple((ear, item, str(saved[ear][item]["text"] or ""))
                              for ear in saved for item in TINNITUS_ITEMS if item in saved[ear]))
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f"이명 값을 읽지 못했습니다 ({error})") from None


def _speech_point(point: p.SpeechPoint) -> dict:
    return {"level_db_hl": point.level, "score_percent": point.score, "masking_db": point.masking,
            "masked": point.masked, "correct": point.correct, "words": point.words}


def _speech(result: HearingResult, corrected: dict) -> dict:
    curves = []
    for placed in _ordered(result.speech):
        curve = placed.curve
        best = curve.best
        by_hand = placed.part is None
        numbers = result.speech_numbers(placed.kind, placed.ear)
        entry = {
            "ear": placed.ear, "kind": placed.kind, "device_kind": None if by_hand else curve.kind, "test": placed.part,
            "stimulus": None if by_hand else curve.stimulus,
            "masking_signal": None if by_hand else curve.masking_signal,
            "material": None if by_hand else curve.material,
            "points": [_marked(_speech_point(point), corrected.get((SPEECH, placed.ear, placed.kind, number)))
                       for number, point in zip(numbers, curve.points)],
            "best": _speech_point(best) if best is not None else None,
            "srt_db_hl": result.srt_of(placed.kind, placed.ear),
        }
        curves.append(_row_marks(entry, SPEECH, placed, corrected,
                                 lambda change: dict(point=change.key, **value_to_dict(SPEECH, change.device))))
    srt = [_marked({"ear": entry.ear, "kind": entry.kind, "device_kind": entry.device_kind, "test": entry.part,
                    "level_db_hl": entry.level}, corrected.get((SRT, entry.ear, entry.kind, 0)))
           for entry in sorted(result.srt.values(), key=lambda entry: (entry.ear != "R", _rank(entry.kind)))]
    return {"curves": curves, "srt": srt}
