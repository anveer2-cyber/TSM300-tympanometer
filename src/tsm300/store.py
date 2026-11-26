"""Saving a result to disk and reading it back: the raw message, the decoded values and the printable sheet."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .patient import Patient
from .protocol import EarResult, Measurement, ProtocolError, parse_message, tympanogram_pressures
from .report import render

_SUFFIXES = (".bin", ".json", ".jpg")


@dataclass(frozen=True)
class SavedResult:
    """One result as it lies on disk."""

    sheet: Path
    measurement: Measurement
    received_at: datetime
    patient: Patient
    note: str
    capture: tuple[dict, ...]
    """What happened each time the sheet was sent to 상쾌한캡처, oldest first."""


def result_to_dict(measurement: Measurement, received_at: datetime, patient: Patient,
                   note: str = "", capture: tuple[dict, ...] = ()) -> dict:
    return {
        "received_at": received_at.isoformat(timespec="seconds"),
        "patient": patient.to_dict(),
        "note": note,
        "device": {"name": measurement.device_name, "code": measurement.device_code},
        "memory_id": measurement.memory_id,
        "right": _ear_to_dict(measurement.right),
        "left": _ear_to_dict(measurement.left),
        "capture": list(capture),
    }


def save_result(measurement: Measurement, directory: Path, received_at: datetime,
                patient: Patient = Patient(), note: str = "", capture: tuple[dict, ...] = ()) -> Path:
    """Writes the three files of one result and returns the path of the sheet (the .jpg)."""
    directory.mkdir(parents=True, exist_ok=True)
    wanted = "_".join(part for part in (received_at.strftime("%Y%m%d_%H%M%S"), patient.file_tag()) if part)
    stem = free_stem(directory, wanted)
    (directory / f"{stem}.bin").write_bytes(measurement.raw)
    record = result_to_dict(measurement, received_at, patient, note, capture)
    (directory / f"{stem}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    sheet = directory / f"{stem}.jpg"
    render(measurement, sheet, received_at, patient, note)
    return sheet


def load_result(path: Path) -> SavedResult:
    """Reads a saved result back from any of its three files. Raises ValueError when it cannot be read."""
    sheet = path.with_suffix(".jpg")
    try:
        record = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        measurement = parse_message(path.with_suffix(".bin").read_bytes())
        received_at = datetime.fromisoformat(record["received_at"])
    except FileNotFoundError as error:
        raise ValueError(f"결과 파일이 없습니다: {Path(error.filename).name}") from None
    except (OSError, ValueError, KeyError, TypeError, ProtocolError) as error:
        raise ValueError(f"결과 파일을 읽지 못했습니다: {error}") from None
    capture = tuple(entry for entry in record.get("capture") or [] if isinstance(entry, dict))
    return SavedResult(sheet, measurement, received_at, Patient.from_dict(record.get("patient") or {}),
                       str(record.get("note") or ""), capture)


def delete_result(sheet: Path) -> None:
    """Removes the files written by ``save_result`` for the result whose sheet is ``sheet``."""
    for suffix in _SUFFIXES:
        sheet.with_suffix(suffix).unlink(missing_ok=True)


def free_stem(directory: Path, stem: str) -> str:
    """Returns ``stem``, or ``stem_2``, ``stem_3``... when files with that name already exist."""
    candidate, number = stem, 1
    while any(directory.glob(f"{candidate}.*")):
        number += 1
        candidate = f"{stem}_{number}"
    return candidate


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
