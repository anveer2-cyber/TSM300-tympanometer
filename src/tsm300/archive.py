"""The tidy copy of finished results on this PC: a folder a month, a folder a day, files named for the patient.

Every result is saved the moment it arrives, under the time it arrived (see ``store``). That
keeps anything from being lost, but a folder of timestamps is no place to look a patient up.
Filing a result puts a copy of its sheet and of its raw data where a person would look:

    보관/2026-11/2026-11-26/00512_홍길동_1430.jpg    the sheet
    보관/2026-11/2026-11-26/00512_홍길동_1430.json   the values and curves
    보관/2026-11/2026-11-26/00512_홍길동_1430.bin    what the device sent
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from .patient import Patient
from .store import put_in_place

FOLDER = "보관"
_SUFFIXES = (".jpg", ".json", ".bin")
_UNFINISHED = "~"


def archive_directory(results: Path, chosen: str = "") -> Path:
    """The archive folder: the one ``chosen`` in the settings, or ``보관`` inside the results folder."""
    return Path(chosen.strip()) if chosen.strip() else results / FOLDER


def file_result(sheet: Path, root: Path, patient: Patient, tested_at: datetime, earlier: str = "") -> str:
    """Copies the result's sheet and raw data into the archive under ``root`` and returns the
    full path of the copy, without a suffix.

    ``earlier`` is where this result was filed before. That copy is replaced, and removed
    when the new copy belongs elsewhere: the patient or the test date has changed, or the
    archive folder itself. Raises OSError when the copy cannot be made; an earlier copy is
    then still as it was.
    """
    folder = Path(os.path.abspath(root)) / f"{tested_at:%Y-%m}" / f"{tested_at:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    before = Path(earlier) if earlier else None
    name = _free_name(folder, f"{patient.filing_name()}_{tested_at:%H%M}", before, sheet.name)
    where = str(folder / name)
    record = json.loads(sheet.with_suffix(".json").read_text(encoding="utf-8"))
    record.update(filed=where, filed_from=os.path.abspath(sheet))
    unfinished = {suffix: folder / f"{_UNFINISHED}{name}{suffix}" for suffix in _SUFFIXES}
    try:
        shutil.copyfile(sheet, unfinished[".jpg"])
        unfinished[".json"].write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copyfile(sheet.with_suffix(".bin"), unfinished[".bin"])
        moved = before is not None and before != folder / name
        put_in_place(unfinished, {suffix: folder / f"{name}{suffix}" for suffix in _SUFFIXES},
                     filed_sheet(earlier) if moved else None)
        if moved:
            _remove_if_empty(before.parent, before.parent.parent)
    finally:
        for path in unfinished.values():
            try:
                path.unlink()
            except OSError:
                pass
    return where


def refresh_copy(sheet: Path) -> None:
    """Brings the tidy copy of a result up to date with the result, going by what its record
    says of where the copy is. Does nothing for a result that has no copy. Raises OSError or
    ValueError when the copy cannot be brought up to date."""
    record = json.loads(sheet.with_suffix(".json").read_text(encoding="utf-8"))
    filed = str(record.get("filed") or "")
    if not filed:
        return
    tested_at = datetime.fromisoformat(str(record.get("tested_at") or record["received_at"]))
    saved = record.get("patient")
    patient = Patient.from_dict(saved if isinstance(saved, dict) else {})
    file_result(sheet, Path(filed).parent.parent.parent, patient, tested_at, filed)


def filed_sheet(where: str) -> Path:
    """The sheet of the tidy copy filed at ``where``."""
    return Path(f"{where}.jpg")


def _free_name(folder: Path, name: str, before: Path | None, origin: str) -> str:
    """``name``, or ``name_2``, ``name_3``... when another result is already filed under it.

    A copy already there is this result's own when it is the one filed ``before``, or when
    its record names a sheet called ``origin`` as the one it was made from: after the
    folders have been moved to another place, that is all there is to know it by.
    """
    candidate, number = name, 1
    while folder / candidate != before and _is_anothers(folder / candidate, origin):
        number += 1
        candidate = f"{name}_{number}"
    return candidate


def _is_anothers(copy: Path, origin: str) -> bool:
    """Whether files are filed at ``copy`` and belong to another result than the one called ``origin``."""
    if not any(Path(f"{copy}{suffix}").exists() for suffix in _SUFFIXES):
        return False
    try:
        made_from = json.loads(Path(f"{copy}.json").read_text(encoding="utf-8")).get("filed_from")
    except (OSError, ValueError, AttributeError):
        return True
    return not made_from or Path(str(made_from).replace("\\", "/")).name != origin


def _remove_if_empty(*folders: Path) -> None:
    """Takes away a day's and a month's folder that a moved copy has left with nothing in them."""
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            return
