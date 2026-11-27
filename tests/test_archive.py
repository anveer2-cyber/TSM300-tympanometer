"""The tidy copy of finished results: a folder a month, a folder a day, files named for the patient."""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from tsm300 import archive
from tsm300.archive import archive_directory, file_result, filed_sheet
from tsm300.patient import Patient
from tsm300.protocol import parse_message
from tsm300.store import load_result, save_result

BOTH = parse_message((Path(__file__).parent / "data" / "both_ears.bin").read_bytes())
ARRIVED = datetime(2026, 11, 26, 21, 12, 5)
TESTED = datetime(2026, 11, 25, 14, 30)
HONG = Patient("00512", "홍길동", "남", date(1975, 3, 4))


@pytest.fixture
def results(tmp_path):
    return tmp_path / "results"


def names(folder):
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())


def place(root, where):
    """Where a copy went, as a person reads it: relative to the archive folder."""
    return Path(where).relative_to(root).as_posix()


def test_archive_is_inside_the_results_folder_unless_another_folder_is_chosen(tmp_path):
    assert archive_directory(tmp_path / "results") == tmp_path / "results" / "보관"
    assert archive_directory(tmp_path / "results", "  ") == tmp_path / "results" / "보관"
    assert archive_directory(tmp_path / "results", str(tmp_path / "검사 보관")) == tmp_path / "검사 보관"


def test_a_filed_result_is_found_by_its_test_date_and_its_patient(results):
    sheet = save_result(BOTH, results, ARRIVED, HONG, "메모", tested_at=TESTED)
    root = archive_directory(results)

    where = file_result(sheet, root, HONG, TESTED)

    assert Path(where).is_absolute() and place(root, where) == "2026-11/2026-11-25/00512_홍길동_1430"
    assert names(root) == [f"{place(root, where)}{suffix}" for suffix in (".bin", ".jpg", ".json")]
    assert filed_sheet(where).read_bytes() == sheet.read_bytes()                             # the sheet
    assert Path(f"{where}.bin").read_bytes() == BOTH.raw                                     # what the device sent
    record = json.loads(Path(f"{where}.json").read_text(encoding="utf-8"))                  # the values and curves
    assert (record["note"], record["right"]["compliance_ml"], record["filed"]) == ("메모", BOTH.right.compliance, where)
    assert Path(record["filed_from"]) == sheet.resolve()


def test_a_copy_can_be_read_back_like_any_result_and_names_the_result_it_was_made_from(results):
    sheet = save_result(BOTH, results, ARRIVED, HONG, tested_at=TESTED)
    root = archive_directory(results)

    copy = load_result(filed_sheet(file_result(sheet, root, HONG, TESTED)))

    assert (copy.measurement, copy.patient, copy.tested_at) == (BOTH, HONG, TESTED)
    assert copy.filed_from == sheet.resolve() and load_result(sheet).filed_from is None


def test_results_without_a_patient_and_names_unfit_for_files_are_still_filed(results):
    root = archive_directory(results)
    nobody = save_result(BOTH, results, ARRIVED)
    awkward = Patient("12/34", "홍 길:동")
    other = save_result(BOTH, results, ARRIVED, awkward)

    assert place(root, file_result(nobody, root, Patient(), TESTED)) == "2026-11/2026-11-25/환자정보없음_1430"
    assert place(root, file_result(other, root, awkward, TESTED)) == "2026-11/2026-11-25/1234_홍길동_1430"


def test_filing_a_result_again_replaces_its_copy_and_moves_it_when_the_patient_or_date_changed(results):
    root = archive_directory(results)
    sheet = save_result(BOTH, results, ARRIVED, HONG, tested_at=TESTED)
    first = file_result(sheet, root, HONG, TESTED)

    sheet = save_result(BOTH, results, ARRIVED, HONG, "고친 메모", tested_at=TESTED, replacing=sheet, filed=first)
    again = file_result(sheet, root, HONG, TESTED, first)

    assert again == first and len(names(root)) == 3
    assert json.loads(Path(f"{again}.json").read_text(encoding="utf-8"))["note"] == "고친 메모"

    kim, next_day = Patient("12345", "김하나"), datetime(2026, 11, 26, 9, 5)
    sheet = save_result(BOTH, results, ARRIVED, kim, tested_at=next_day, replacing=sheet, filed=again)
    moved = file_result(sheet, root, kim, next_day, again)

    assert place(root, moved) == "2026-11/2026-11-26/12345_김하나_0905"
    assert names(root) == [f"{place(root, moved)}{suffix}" for suffix in (".bin", ".jpg", ".json")]

    elsewhere = results.parent / "새 보관 폴더"                       # the archive folder itself is changed in the settings
    again = file_result(sheet, elsewhere, kim, next_day, moved)

    assert place(elsewhere, again) == "2026-11/2026-11-26/12345_김하나_0905" and len(names(elsewhere)) == 3
    assert names(root) == []                                         # the copy in the old place does not stay behind
    assert list(root.iterdir()) == []                                # nor the day's and the month's folder, left empty


def test_after_the_folders_were_moved_a_result_still_knows_its_own_copy(tmp_path):
    before, after = tmp_path / "C" / "results", tmp_path / "D" / "results"
    sheet = save_result(BOTH, before, ARRIVED, HONG, tested_at=TESTED)
    stale = file_result(sheet, archive_directory(before), HONG, TESTED)
    after.parent.mkdir()
    before.rename(after)                                             # the results folder, with the archive inside it
    moved_sheet = after / sheet.name

    again = file_result(moved_sheet, archive_directory(after), HONG, TESTED, stale)

    assert place(archive_directory(after), again) == "2026-11/2026-11-25/00512_홍길동_1430"      # not ..._2
    assert len(names(archive_directory(after))) == 3


def test_a_copy_is_brought_up_to_date_from_what_the_results_record_says(results):
    from tsm300.archive import refresh_copy
    from tsm300.store import note_in_record

    root = archive_directory(results)
    sheet = save_result(BOTH, results, ARRIVED, HONG, tested_at=TESTED)
    refresh_copy(sheet)                                              # no copy yet: nothing happens
    assert not root.exists()

    where = file_result(sheet, root, HONG, TESTED)
    note_in_record(sheet, filed=where, capture=[{"ok": True, "at": "2026-11-26T21:13:00"}])
    refresh_copy(sheet)

    assert json.loads(Path(f"{where}.json").read_text(encoding="utf-8"))["capture"] == [{"ok": True, "at": "2026-11-26T21:13:00"}]
    assert len(names(root)) == 3


def test_two_results_of_one_patient_in_the_same_minute_are_both_kept(results):
    root = archive_directory(results)
    one = save_result(BOTH, results, ARRIVED, HONG, tested_at=TESTED)
    two = save_result(BOTH, results, datetime(2026, 11, 26, 21, 12, 40), HONG, tested_at=TESTED)

    first, second = file_result(one, root, HONG, TESTED), file_result(two, root, HONG, TESTED)

    assert [place(root, where) for where in (first, second)] == ["2026-11/2026-11-25/00512_홍길동_1430",
                                                                 "2026-11/2026-11-25/00512_홍길동_1430_2"]
    assert file_result(two, root, HONG, TESTED, second) == second          # and filing the second again keeps its name


def test_a_name_with_brackets_in_it_does_not_let_one_copy_overwrite_another(results):
    root = archive_directory(results)
    bracketed = Patient("7[A]", "홍[길]동")
    one = save_result(BOTH, results, ARRIVED, bracketed, tested_at=TESTED)
    two = save_result(BOTH, results, ARRIVED, bracketed, tested_at=TESTED)

    assert one.name != two.name                                       # nor in the folder where results are saved on arrival
    first, second = file_result(one, root, bracketed, TESTED), file_result(two, root, bracketed, TESTED)

    assert first != second and len(names(root)) == 6


def test_a_copy_that_cannot_be_made_leaves_the_earlier_copy_as_it_was(results, monkeypatch):
    root = archive_directory(results)
    sheet = save_result(BOTH, results, ARRIVED, HONG, tested_at=TESTED)
    where = file_result(sheet, root, HONG, TESTED)
    before = {name: (root / name).read_bytes() for name in names(root)}

    def no_room(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(archive.shutil, "copyfile", no_room)
    with pytest.raises(OSError):
        file_result(sheet, root, HONG, TESTED, where)

    assert {name: (root / name).read_bytes() for name in names(root)} == before
