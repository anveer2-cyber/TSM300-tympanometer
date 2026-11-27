"""Saving a result to disk and reading it back: the raw message, the decoded values and the printable sheet."""

from __future__ import annotations

import glob
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .clinic import Clinic
from .hearing import record as hearing_record
from .hearing import sheet as hearing_sheet
from .hearing.record import HearingResult
from .patient import Patient
from .protocol import EarResult, Measurement, ProtocolError, parse_message, tympanogram_pressures
from .report import render

_SUFFIXES = (".bin", ".json", ".jpg")
_IN_ORDER_OF_RISK = (".jpg", ".json", ".bin")
_UNFINISHED = "~"
_REPLACE_TRIES = 5
_REPLACE_PAUSE = 0.2

EXAM_KIND = "tymp"
"""What kind of test a saved result holds, by the name 상쾌한캡처 gives it. Tympanometry is
``tymp`` and the audiometer's pure tone, speech and soundfield results are ``hearing``
(``hearing.record.EXAM``); both share the folder. A file saved before this was written down
holds a tympanogram."""
FORMAT = 1
HEARING = hearing_record.EXAM


@dataclass(frozen=True)
class SavedResult:
    """One result as it lies on disk."""

    sheet: Path
    measurement: Measurement | HearingResult
    """A tympanogram (``exam`` tymp) or a hearing result (``exam`` hearing)."""
    received_at: datetime
    """When the device sent it. The files are named after this, so it never changes."""
    tested_at: datetime
    """The date and time of the test as printed on the sheet, which the operator may correct."""
    patient: Patient
    note: str
    clinic: Clinic | None
    """The clinic printed on the sheet; None for a result saved before that was written down."""
    capture: tuple[dict, ...]
    """What happened each time the sheet was sent to 상쾌한캡처, oldest first."""
    filed: str = ""
    """Where the result's tidy copy is: its full path without a suffix; empty when it has none."""
    filed_from: Path | None = None
    """For a tidy copy itself: the sheet it is a copy of."""


def result_to_dict(measurement: Measurement | HearingResult, received_at: datetime, patient: Patient, note: str = "",
                   capture: tuple[dict, ...] = (), tested_at: datetime | None = None,
                   clinic: Clinic = Clinic(), filed: str = "") -> dict:
    hearing = isinstance(measurement, HearingResult)
    common = {
        "format": hearing_record.FORMAT if hearing else FORMAT,
        "exam": HEARING if hearing else EXAM_KIND,
        "received_at": received_at.isoformat(timespec="seconds"),
        "tested_at": (tested_at or received_at).isoformat(timespec="seconds"),
        "clinic": clinic.to_dict(),
        "patient": patient.to_dict(),
        "note": note,
    }
    if hearing:
        values = hearing_record.record(measurement)
    else:
        values = {
            "device": {"name": measurement.device_name, "code": measurement.device_code},
            "memory_id": measurement.memory_id,
            "right": _ear_to_dict(measurement.right),
            "left": _ear_to_dict(measurement.left),
        }
    return {**common, **values, "capture": list(capture), "filed": filed}


def exam_of_result(measurement: Measurement | HearingResult) -> str:
    """The kind of test a result in memory holds, as its record will say."""
    return HEARING if isinstance(measurement, HearingResult) else EXAM_KIND


def draw(measurement: Measurement | HearingResult, path: Path, tested_at: datetime, patient: Patient = Patient(),
         note: str = "", clinic: Clinic = Clinic()) -> None:
    """Draws the sheet of either kind of result."""
    if isinstance(measurement, HearingResult):
        hearing_sheet.render(measurement, path, tested_at, patient, note, clinic)
    else:
        render(measurement, path, tested_at, patient, note, clinic)


def save_result(measurement: Measurement | HearingResult, directory: Path, received_at: datetime,
                patient: Patient = Patient(), note: str = "", capture: tuple[dict, ...] = (), *,
                tested_at: datetime | None = None, clinic: Clinic = Clinic(),
                replacing: Path | None = None, filed: str = "") -> Path:
    """Writes the three files of one result and returns the path of the sheet (the .jpg).

    ``replacing`` is the sheet of an earlier save of the same result, whose files these take
    the place of. Raises OSError when the files cannot be written, and then the folder is as
    it was before: either all three files are put in place or none is.
    """
    directory.mkdir(parents=True, exist_ok=True)
    wanted = "_".join(part for part in (received_at.strftime("%Y%m%d_%H%M%S"), patient.file_tag()) if part)
    replaced = replacing.stem if replacing is not None and replacing.parent == directory else None
    stem = free_stem(directory, wanted, replaced)
    unfinished = {suffix: directory / f"{_UNFINISHED}{stem}{suffix}" for suffix in _IN_ORDER_OF_RISK}
    record = result_to_dict(measurement, received_at, patient, note, capture, tested_at, clinic, filed)
    try:
        unfinished[".bin"].write_bytes(measurement.raw)
        unfinished[".json"].write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        draw(measurement, unfinished[".jpg"], tested_at or received_at, patient, note, clinic)
        renamed = replacing is not None and replacing.with_suffix("") != directory / stem
        put_in_place(unfinished, {suffix: directory / f"{stem}{suffix}" for suffix in unfinished},
                     replacing if renamed else None)
    finally:
        for path in unfinished.values():
            _remove(path)
    return directory / f"{stem}.jpg"


def put_in_place(unfinished: dict[str, Path], final: dict[str, Path], renamed_from: Path | None) -> None:
    """Gives the finished files their names, undoing every step taken if one of them fails.

    A file already under a name is moved aside first and moved back on failure. The sheet
    goes first: it is the one file another program may be holding open, and that program
    refusing to let go is then found out before anything else has been touched. The same
    goes for the earlier sheet when the result is being given another name.
    """
    aside: dict[Path, Path] = {}
    placed: list[Path] = []
    try:
        for suffix, source in unfinished.items():
            target = final[suffix]
            if target.exists():
                holder = target.with_name(f"{_UNFINISHED}earlier {target.name}")
                _replace(target, holder)
                aside[target] = holder
            _replace(source, target)
            placed.append(target)
        if renamed_from is not None:
            renamed_from.unlink(missing_ok=True)
    except OSError:
        for target in placed:
            _remove(target)
        for target, holder in aside.items():
            try:
                os.replace(holder, target)
            except OSError:
                pass
        raise
    for holder in aside.values():
        _remove(holder)
    if renamed_from is not None:
        delete_result(renamed_from)


def load_result(path: Path) -> SavedResult:
    """Reads a saved result back from any of its three files. Raises ValueError when it cannot be read."""
    sheet = path.with_suffix(".jpg")
    try:
        record = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        if exam_of(record) == HEARING:
            measurement = hearing_record.from_saved(path.with_suffix(".bin").read_bytes(), record)
        elif exam_of(record) == EXAM_KIND:
            measurement = parse_message(path.with_suffix(".bin").read_bytes())
        else:
            raise ValueError(f"이 프로그램이 열 수 없는 종류의 검사 결과입니다 ({exam_of(record)})")
        received_at = datetime.fromisoformat(record["received_at"])
        tested_at = datetime.fromisoformat(record.get("tested_at") or record["received_at"])
    except FileNotFoundError as error:
        raise ValueError(f"결과 파일이 없습니다: {Path(error.filename).name}") from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ProtocolError) as error:
        raise ValueError(f"결과 파일을 읽지 못했습니다: {error}") from None
    capture = tuple(entry for entry in record.get("capture") or [] if isinstance(entry, dict))
    clinic = Clinic.from_dict(record["clinic"]) if isinstance(record.get("clinic"), dict) else None
    filed_from = Path(str(record["filed_from"])) if record.get("filed_from") else None
    return SavedResult(sheet, measurement, received_at, tested_at, Patient.from_dict(_dict(record.get("patient"))),
                       str(record.get("note") or ""), clinic, capture, str(record.get("filed") or ""), filed_from)


def note_in_record(sheet: Path, **fields) -> None:
    """Writes ``fields`` into the result's JSON record, leaving the rest of it as it is."""
    record_file = sheet.with_suffix(".json")
    record = json.loads(record_file.read_text(encoding="utf-8"))
    record.update(fields)
    unfinished = record_file.with_name(_UNFINISHED + record_file.name)
    try:
        unfinished.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        _replace(unfinished, record_file)
    finally:
        _remove(unfinished)


def exam_of(record: dict) -> str:
    """The kind of test in a result's JSON record."""
    return str(record.get("exam") or EXAM_KIND)


def delete_result(sheet: Path) -> None:
    """Removes the files written by ``save_result`` for the result whose sheet is ``sheet``."""
    for suffix in _SUFFIXES:
        _remove(sheet.with_suffix(suffix))


def free_stem(directory: Path, stem: str, ignoring: str | None = None) -> str:
    """Returns ``stem``, or ``stem_2``, ``stem_3``... when files with that name already exist.

    Files named ``ignoring`` do not count: they are about to be replaced.
    """
    candidate, number = stem, 1
    while candidate != ignoring and any(directory.glob(f"{glob.escape(candidate)}.*")):
        number += 1
        candidate = f"{stem}_{number}"
    return candidate


def _replace(source: Path, target: Path) -> None:
    """Moves a finished file into place. Virus scanners and indexers hold a file for a moment
    just after it is written, which on Windows makes the move fail, so it is tried a few times."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(_REPLACE_PAUSE)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _remove(path: Path) -> None:
    """Removes a file if it can be removed; what cannot be is left for the operator to see."""
    try:
        path.unlink()
    except OSError:
        pass


def _ear_to_dict(ear: EarResult | None) -> dict | None:
    if ear is None:
        return None
    return {
        "middle_ear_pressure_dapa": ear.middle_ear_pressure,
        "ear_volume_ml": ear.ear_volume,
        "compliance_ml": ear.compliance,
        "width_dapa": ear.width,
        "tympanogram": {
            "pressure_dapa": tympanogram_pressures(),
            "compliance_ml": [round(value, 4) for value in ear.compliance_curve],
        },
        "reflex_ml": {
            str(frequency): [round(value, 4) for value in ear.reflex_curve(frequency)]
            for frequency in sorted(ear.reflexes)
        },
    }
