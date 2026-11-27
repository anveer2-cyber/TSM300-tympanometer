"""One hearing result: the tests received from the audiometer that the operator put on one sheet.

The audiometer sends each test on its own, without the patient or the date (see
docs/hearing/통신_절차.md §6.5). The operator chooses which of the received tests belong to the
patient in the program, and they become one ``HearingResult``: a pure tone test, a speech test
and a high-frequency test, each at most once per ear and transducer. A test that holds only
soundfield curves may be marked as done with hearing aids; its soundfield curves then go on the
sheet as the aided ones (SPKR-A), beside the unaided curves of another test. That is how the
clinic measures before and after the hearing aid: two soundfield tests, the second one with
the aid in, which used to need two sheets.

``record`` turns a result into the part of the saved JSON that is particular to hearing tests,
and ``from_saved`` reads it back from the raw packets and that JSON. The layout is the contract
in docs/hearing/원자료_형식.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import protocol as p
from .pta import METHODS, Average, average
from .session import fingerprint

EXAM = "hearing"
"""What kind of test the saved result holds, by the name 상쾌한캡처 gives pure tone audiometry."""
FORMAT = 1
"""The version of the hearing record's layout. Fields may be added without changing it;
a field that is removed or changes its meaning gives a new number."""

TONE, SPEECH, HIGH_FREQUENCY = "tone", "speech", "high_frequency"

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
    part: int
    """Which of the result's parts it came from."""


@dataclass(frozen=True)
class PlacedSrt:
    kind: str
    ear: str
    level: int | None
    device_kind: str
    part: int


@dataclass(frozen=True)
class HearingResult:
    parts: tuple[Part, ...]
    tone: dict = field(init=False, repr=False, compare=False)
    """(kind, ear) -> ``Placed`` for the pure tone curves."""
    speech: dict = field(init=False, repr=False, compare=False)
    srt: dict = field(init=False, repr=False, compare=False)
    high_frequency: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        if not self.parts:
            raise MergeError("고른 검사가 없습니다.")
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
        object.__setattr__(self, "tone", places[TONE])
        object.__setattr__(self, "speech", places[SPEECH])
        object.__setattr__(self, "high_frequency", places[HIGH_FREQUENCY])
        object.__setattr__(self, "srt", srt)

    # --- what it holds ---------------------------------------------------------------------

    @property
    def has_tone(self) -> bool:
        return any(part.type == TONE for part in self.parts)

    @property
    def has_speech(self) -> bool:
        return any(part.type == SPEECH for part in self.parts)

    @property
    def has_high_frequency(self) -> bool:
        return any(part.type == HIGH_FREQUENCY for part in self.parts)

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


# --- the saved record ---------------------------------------------------------------------------

def record(result: HearingResult) -> dict:
    """The hearing part of the saved JSON: every value decoded, and the packets themselves."""
    patients = list(result.device_patients)
    patient_index = {patient.raw: number for number, patient in enumerate(patients)}
    serials = result.serials
    return {
        "device": {"maker": "MRS", "name": "Resonance", "model": serials[0][:4], "serial": serials[0],
                   "serials": list(serials)},
        "device_patients": [_device_patient(patient) for patient in patients],
        "tests": [_test(index, part, patient_index) for index, part in enumerate(result.parts)],
        "tone": _tone(result) if result.tone else None,
        "speech": _speech(result) if result.speech or result.srt else None,
        "high_frequency": ({"curves": [_tone_curve(placed) for placed in _ordered(result.high_frequency)]}
                           if result.high_frequency else None),
    }


def from_saved(raw: bytes, saved: dict) -> HearingResult:
    """Reads a result back from its raw packets (the .bin) and its record (the .json).

    The packets hold every value; the record adds only what the operator decided, which of
    the tests were done with hearing aids. Raises ValueError when the packets cannot be read.
    """
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
    return HearingResult(tuple(parts))


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


def _tone_curve(placed: Placed) -> dict:
    curve = placed.curve
    return {
        "ear": placed.ear, "kind": placed.kind, "device_kind": curve.kind, "test": placed.part,
        "stimulus": curve.stimulus, "masking_signal": curve.masking_signal, "pulsed": curve.pulsed,
        "points": [{"frequency_hz": point.frequency, "level_db_hl": point.level, "masking_db": point.masking,
                    "masked": point.masked, "no_response": point.no_response} for point in curve.points],
    }


def _tone(result: HearingResult) -> dict:
    averages = []
    for placed in _ordered(result.tone):
        if placed.kind not in AVERAGED:
            continue
        for method in METHODS:
            averages.append(dict(ear=placed.ear, kind=placed.kind, **_average(average(placed.curve, method))))
    return {"curves": [_tone_curve(placed) for placed in _ordered(result.tone)], "averages": averages}


def _average(value: Average) -> dict:
    return {
        "method": value.method, "weights": {str(frequency): weight for frequency, weight in METHODS[value.method]},
        "value": None if value.value is None else round(value.value, 4), "shown": value.shown,
        "text": value.text(), "at_least": value.at_least, "missing_hz": list(value.missing),
    }


def _speech_point(point: p.SpeechPoint) -> dict:
    return {"level_db_hl": point.level, "score_percent": point.score, "masking_db": point.masking,
            "masked": point.masked, "correct": point.correct, "words": point.words}


def _speech(result: HearingResult) -> dict:
    curves = []
    for placed in _ordered(result.speech):
        curve = placed.curve
        best = curve.best
        curves.append({
            "ear": placed.ear, "kind": placed.kind, "device_kind": curve.kind, "test": placed.part,
            "stimulus": curve.stimulus, "masking_signal": curve.masking_signal, "material": curve.material,
            "points": [_speech_point(point) for point in curve.points],
            "best": _speech_point(best) if best is not None else None,
            "srt_db_hl": result.srt_of(placed.kind, placed.ear),
        })
    srt = [{"ear": entry.ear, "kind": entry.kind, "device_kind": entry.device_kind, "test": entry.part,
            "level_db_hl": entry.level}
           for entry in sorted(result.srt.values(), key=lambda entry: (entry.ear != "R", _rank(entry.kind)))]
    return {"curves": curves, "srt": srt}
