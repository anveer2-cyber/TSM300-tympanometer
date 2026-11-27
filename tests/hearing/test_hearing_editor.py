"""Correcting a hearing result's values in the window, and typing a whole result in without the audiometer."""

from __future__ import annotations

import gc
import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

import shiboken6  # noqa: E402
from PySide6.QtCore import QDateTime  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import KEY, CaptureReceiver  # noqa: E402
from hearing_fakes import Audiometer  # noqa: E402
from tsm300 import app as app_module  # noqa: E402
from tsm300.app import MANUAL_LABEL, VALUES_LABEL, HearingReceiveDialog, MainWindow  # noqa: E402
from tsm300.hearing import device  # noqa: E402
from tsm300.hearing.editor import NOT_SAVED, HearingEditor  # noqa: E402
from tsm300.hearing.record import HearingResult, can_be_aided  # noqa: E402
from tsm300.patient import Patient  # noqa: E402
from tsm300.protocol import parse_message  # noqa: E402
from tsm300.settings import Settings  # noqa: E402
from tsm300.store import load_result  # noqa: E402

WHEN = datetime(2026, 11, 27, 10, 30, 5)
KIM = Patient("12345", "김하나", "여", date(1980, 1, 31))
HONG = Patient("00512", "홍길동", "남", date(1975, 3, 4))
HEARING = Settings(hearing_on=True, hearing_link=device.SIMULATOR)
TYMPANOGRAM = parse_message((Path(__file__).parents[1] / "data" / "both_ears.bin").read_bytes())
STEM = "20261127_103005"


def run_now(owner, work, done):
    try:
        result = work()
    except Exception as error:
        done(None, error)
    else:
        done(result, None)


def unexpected_question(question):
    raise AssertionError(f"unexpected question: {question}")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


_OPENED = []


@pytest.fixture(autouse=True)
def dispose_of_windows(qapp):
    yield
    while _OPENED:
        window = _OPENED.pop()
        window.close()
        shiboken6.delete(window)
    qapp.processEvents()
    gc.collect()


@pytest.fixture
def results(tmp_path):
    return tmp_path / "results"


def choose_practice(window, fetched, seen, patient, patient_is_new, received_at):
    """Ticks the made-up hearing-aid patient's four tests, the soundfield-only ones as done with the aid."""
    dialog = HearingReceiveDialog(window, fetched, seen, patient, patient_is_new, received_at)
    for key in dialog.chosen_keys():
        if can_be_aided(dialog._tests[key][1]):
            dialog.set_chosen(key, True, aided=True)
    dialog.accept()
    return dialog.choice()


class Staff:
    """Does in the editor what each test tells it to, then saves or cancels as told."""

    def __init__(self) -> None:
        self.steps = []
        self.dialogs: list[HearingEditor] = []

    def __call__(self, dialog: HearingEditor) -> None:
        self.dialogs.append(dialog)
        step = self.steps.pop(0) if self.steps else (lambda dialog: dialog.accept())
        step(dialog)


def open_window(qapp, results, settings=HEARING, **options):
    audiometer = Audiometer()
    staff = Staff()
    options.setdefault("background", run_now)
    options.setdefault("confirm", unexpected_question)
    options.setdefault("confirm_patient", lambda patient: True)
    options.setdefault("settings_directory", results.parent / "settings")
    options.setdefault("hearing_opener", lambda link: audiometer.open)
    options.setdefault("fetch_hearing", lambda opener, folder, progress: device.fetch(
        opener, folder, progress, now=lambda: WHEN, clock=audiometer.clock))
    window = MainWindow(results, settings, now=lambda: WHEN, run_editor=staff,
                        choose_hearing=lambda *args: choose_practice(window, *args), **options)
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    _OPENED.append(window)
    window.staff = staff
    return window


def received(qapp, results, patient=KIM, **options):
    window = open_window(qapp, results, **options)
    window.set_patient(patient)
    window.hearing_button.click()
    return window


def record_of(results, stem):
    return json.loads((results / f"{stem}.json").read_text(encoding="utf-8"))


def saved(results):
    return sorted(path.name for path in results.iterdir() if path.is_file()) if results.exists() else []


def type_values(dialog: HearingEditor) -> None:
    right, left = dialog.tone_grid("R"), dialog.tone_grid("L")
    right.put(right.row_of("Ac"), right.column_of(1000), "60")              # a threshold changed
    right.put(right.row_of("AcU"), right.column_of(1000), "100")            # a row the device did not send
    right.put(right.row_of("Bc"), right.column_of(4000), "70n")             # masked, no response
    right.put(right.row_of("Bc", masking=True), right.column_of(4000), "80")
    left.put(left.row_of("Ac"), left.column_of(6000), "85n")                # no response
    words = dialog.speech_grid("R", "Ac")
    words.put(0, 1, "48")
    words.srt_edit.setText("50")


# ----- correcting the values of a received result -------------------------------------------------------

def test_the_button_is_there_for_hearing_results_and_typing_in_only_when_the_audiometer_is_used(qapp, results):
    window = open_window(qapp, results, settings=Settings())
    assert window.manual_button.isHidden() and window.values_button.isHidden()

    window.apply_settings(HEARING)
    assert window.manual_button.text() == MANUAL_LABEL and not window.manual_button.isHidden()
    assert window.values_button.text() == VALUES_LABEL and not window.values_button.isEnabled()

    window.handle_event(TYMPANOGRAM)
    assert not window.values_button.isEnabled()               # a tympanogram has no such values
    window.hearing_button.click()
    assert window.values_button.isEnabled()


def test_values_corrected_in_the_editor_are_printed_and_the_devices_are_kept_beside_them(qapp, results):
    window = received(qapp, results)
    stem = f"{STEM}_12345"
    raw = (results / f"{stem}.bin").read_bytes()
    sheet_before = (results / f"{stem}.jpg").read_bytes()
    window.staff.steps.append(lambda dialog: (type_values(dialog), dialog.accept()))

    window.values_button.click()

    dialog = window.staff.dialogs[0]
    assert dialog.outcome is not None and dialog.patient_note.text() == "김하나 (12345) 환자 결과지의 검사 값을 고칩니다."
    assert (results / f"{stem}.bin").read_bytes() == raw
    assert (results / f"{stem}.jpg").read_bytes() != sheet_before
    assert saved(results) == [f"{stem}.bin", f"{stem}.jpg", f"{stem}.json"]
    record = record_of(results, stem)
    curves = {(curve["kind"], curve["ear"]): curve for curve in record["tone"]["curves"]}
    point = next(point for point in curves["Ac", "R"]["points"] if point["frequency_hz"] == 1000)
    assert (point["level_db_hl"], point["corrected"], point["device_value"]["level_db_hl"]) == (60, True, 55)
    assert curves["AcU", "R"]["points"][0]["level_db_hl"] == 100
    bone = next(point for point in curves["Bc", "R"]["points"] if point["frequency_hz"] == 4000)
    assert (bone["level_db_hl"], bone["masking_db"], bone["masked"], bone["no_response"]) == (70, 80, True, True)
    assert next(point for point in curves["Ac", "L"]["points"] if point["frequency_hz"] == 6000)["no_response"]
    average = next(entry for entry in record["tone"]["averages"]
                   if (entry["ear"], entry["kind"], entry["method"]) == ("R", "Ac", "6분법"))
    assert (average["text"], average["device_value"]["text"]) == ("61", "60")
    assert len(record["corrections"]) == 6 and record["source"] == "device"
    assert record["speech"]["curves"][0]["points"][0]["score_percent"] == 48
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."
    assert window.problem_label.isHidden()


def test_corrected_values_are_there_again_when_the_result_is_reopened_and_can_be_corrected_further(qapp, results):
    first = received(qapp, results)
    first.staff.steps.append(lambda dialog: (type_values(dialog), dialog.accept()))
    first.values_button.click()
    stem = f"{STEM}_12345"
    raw = (results / f"{stem}.bin").read_bytes()

    window = open_window(qapp, results)
    window.open_result(results / f"{stem}.json")
    assert window.values_button.isEnabled()

    def look_and_change(dialog):
        grid = dialog.tone_grid("R")
        assert grid.text_at(grid.row_of("Ac"), grid.column_of(1000)) == "60"
        assert grid.text_at(grid.row_of("Bc"), grid.column_of(4000)) == "70↓"
        assert grid.text_at(grid.row_of("Bc", True), grid.column_of(4000)) == "80"
        assert grid.average_text("Ac") == "61"
        assert dialog.speech_grid("R", "Ac").srt_edit.text() == "50"
        grid.put(grid.row_of("Ac"), grid.column_of(1000), "55")           # back to what the device sent
        assert grid.average_text("Ac") == "60"
        dialog.accept()

    window.staff.steps.append(look_and_change)
    window.values_button.click()

    loaded = load_result(results / f"{stem}.jpg")
    assert loaded.measurement.tone_curve("Ac", "R").at(1000).level == 55
    assert len(loaded.measurement.corrections) == 5
    assert (results / f"{stem}.bin").read_bytes() == raw


def test_cells_that_cannot_be_read_are_pointed_out_and_nothing_is_saved_until_they_are_put_right(qapp, results):
    window = received(qapp, results)
    stem = f"{STEM}_12345"
    before = (results / f"{stem}.json").read_text(encoding="utf-8")

    def type_wrongly(dialog):
        grid = dialog.tone_grid("R")
        grid.put(grid.row_of("Ac"), grid.column_of(1000), "125")
        grid.put(grid.row_of("Spkr", masking=True), grid.column_of(250), "60")
        words = dialog.speech_grid("L", "Ac")
        words.put(0, 0, "60")
        words.put(0, 3, "26")
        words.put(0, 4, "25")
        dialog.accept()
        said = dialog.problem_label.text()
        assert "오른쪽 기도 1k: 125 는 -10 ~ 120 사이가 아닙니다" in said
        assert "오른쪽 음장 250: 차폐만 있고 역치가 없습니다" in said
        assert "어음 왼쪽 기도 1번째 점: 맞힌 단어 수(26)가 단어 수(25)보다 많습니다" in said
        assert dialog.outcome is None and dialog.tabs.currentIndex() == 0
        assert grid.item(grid.row_of("Ac"), grid.column_of(1000)).background().color().name() == "#f9d6d2"
        grid.put(grid.row_of("Ac"), grid.column_of(1000), "65")
        grid.put(grid.row_of("Spkr", masking=True), grid.column_of(250), "")
        words.put(0, 3, "20")
        dialog.accept()

    window.staff.steps.append(type_wrongly)
    window.values_button.click()

    assert window.staff.dialogs[0].outcome is not None
    assert (results / f"{stem}.json").read_text(encoding="utf-8") != before
    words = {(curve["kind"], curve["ear"]): curve for curve in record_of(results, stem)["speech"]["curves"]}
    first = words["Ac", "L"]["points"][0]
    assert (first["level_db_hl"], first["correct"], first["words"], first["score_percent"]) == (60, 20, 25, 52)


def test_cancelling_after_typing_asks_first_and_changes_nothing(qapp, results):
    asked = []
    answers = [False, True]
    window = received(qapp, results, confirm=lambda question: asked.append(question) or answers.pop(0))
    stem = f"{STEM}_12345"
    before = (results / f"{stem}.json").read_bytes()

    def type_then_cancel(dialog):
        type_values(dialog)
        dialog.cancel_button.click()                          # "no": the editor stays
        dialog.reject()

    window.staff.steps.append(type_then_cancel)
    window.values_button.click()

    assert asked == [NOT_SAVED, NOT_SAVED]
    assert (results / f"{stem}.json").read_bytes() == before
    window.staff.steps.append(lambda dialog: dialog.reject())  # nothing typed: closes without a question
    window.values_button.click()
    assert len(asked) == 2


def test_a_sheet_already_sent_is_not_sent_again_after_its_values_are_corrected(qapp, results):
    receiver = CaptureReceiver()
    try:
        settings = Settings(hearing_on=True, hearing_link=device.SIMULATOR, cap_on=True, cap_url=receiver.url,
                            cap_key=KEY)
        window = received(qapp, results, patient=HONG, settings=settings)
        assert len(receiver.received) == 1
        window.staff.steps.append(lambda dialog: (type_values(dialog), dialog.accept()))

        window.values_button.click()

        assert len(receiver.received) == 1
        assert window.capture_label.text().startswith("⚠ 보낸 뒤에 결과지를 고쳤습니다")
        assert record_of(results, f"{STEM}_00512")["capture"][-1]["code"] == "edited"
    finally:
        receiver.close()


def test_values_saved_after_another_result_took_the_screen_go_to_the_result_they_were_typed_for(qapp, results):
    window = received(qapp, results)
    stem = f"{STEM}_12345"

    def another_result_arrives_while_typing(dialog):
        type_values(dialog)
        window.handle_event(TYMPANOGRAM)
        dialog.accept()

    window.staff.steps.append(another_result_arrives_while_typing)
    window.values_button.click()

    assert len(record_of(results, stem)["corrections"]) == 6
    assert record_of(results, STEM)["exam"] == "tymp"
    assert not isinstance(window._measurement, HearingResult)
    assert "고친 값은 앞 결과지(20261127_103005_12345.jpg)에 저장했습니다" in window.problem_label.text()


def test_the_filed_copy_follows_corrected_values(qapp, results):
    window = received(qapp, results)
    window.file_button.click()
    window.staff.steps.append(lambda dialog: (type_values(dialog), dialog.accept()))

    window.values_button.click()

    copy = results / "보관" / "2026-11" / "2026-11-27" / "12345_김하나_1030.json"
    assert len(json.loads(copy.read_text(encoding="utf-8"))["corrections"]) == 6


def test_the_test_time_of_a_corrected_result_can_be_changed_and_the_corrections_stay(qapp, results):
    window = received(qapp, results)
    window.staff.steps.append(lambda dialog: (type_values(dialog), dialog.accept()))
    window.values_button.click()
    window.handle_event(TYMPANOGRAM)
    window.open_result(results / f"{STEM}_12345.json")

    window.tested_edit.setDateTime(QDateTime(datetime(2026, 11, 26, 16, 0)))
    window.note_button.click()

    record = record_of(results, f"{STEM}_12345")
    assert record["tested_at"] == "2026-11-26T16:00:00" and len(record["corrections"]) == 6


def test_memo_and_remarks_can_be_written_in_the_editor_too(qapp, results):
    window = received(qapp, results)
    window.note_edit.setPlainText("창에서 쓰던 메모")

    def write(dialog):
        assert dialog.note_edit.toPlainText() == "창에서 쓰던 메모"
        dialog.note_edit.setPlainText("고친 메모")
        dialog.remarks_edit.setPlainText("인쇄 안 되는 특이사항")
        dialog.accept()

    window.staff.steps.append(write)
    window.values_button.click()

    record = record_of(results, f"{STEM}_12345")
    assert (record["note"], record["remarks"]) == ("고친 메모", "인쇄 안 되는 특이사항")
    assert window.note_edit.toPlainText() == "고친 메모" and window.remarks_edit.toPlainText() == "인쇄 안 되는 특이사항"
    assert not window.note_button.isEnabled()


def test_values_that_cannot_be_saved_keep_the_editor_open_with_what_was_typed(qapp, results, monkeypatch):
    window = received(qapp, results)
    stem = f"{STEM}_12345"
    before = (results / f"{stem}.json").read_bytes()
    really_save = app_module.save_result

    def held_open(*arguments, **options):
        raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")

    def try_twice(dialog):
        type_values(dialog)
        monkeypatch.setattr(app_module, "save_result", held_open)
        dialog.accept()
        assert dialog.outcome is None and "고친 내용을 저장하지 못했습니다" in dialog.problem_label.text()
        assert (results / f"{stem}.json").read_bytes() == before
        monkeypatch.setattr(app_module, "save_result", really_save)
        dialog.accept()

    window.staff.steps.append(try_twice)
    window.values_button.click()

    assert len(record_of(results, stem)["corrections"]) == 6


# ----- typing a result in by hand ----------------------------------------------------------------------

def type_a_result(dialog: HearingEditor, note="", remarks="", tested=None) -> None:
    right, left = dialog.tone_grid("R"), dialog.tone_grid("L")
    for frequency, level in ((250, "30"), (500, "35"), (1000, "40"), (2000, "50"), (4000, "60"), (8000, "90n")):
        right.put(right.row_of("Ac"), right.column_of(frequency), level)
    for frequency, level in ((500, "20"), (1000, "25"), (2000, "30"), (4000, "40")):
        left.put(left.row_of("Ac"), left.column_of(frequency), level)
    right.put(right.row_of("Bc"), right.column_of(1000), "30")
    right.put(right.row_of("Bc", True), right.column_of(1000), "60")
    words = dialog.speech_grid("R", "Ac")
    words.srt_edit.setText("40")
    for row, cells in enumerate((("50", "", "", "15", "25"), ("60", "", "", "22", "25"))):
        for column, text in enumerate(cells):
            words.put(row, column, text)
    dialog.note_edit.setPlainText(note)
    dialog.remarks_edit.setPlainText(remarks)
    if tested is not None:
        dialog.tested_edit.setDateTime(QDateTime(tested))
    assert right.average_text("Ac") == "45"                  # (35 + 2×40 + 2×50 + 60) / 6 = 45.8, decimals dropped


def test_a_result_typed_in_by_hand_is_saved_printed_reopened_corrected_and_sent_like_a_received_one(
        qapp, results, monkeypatch):
    receiver = CaptureReceiver(patient=("00512", "홍길동", "1975-03-04"))
    printed = []
    monkeypatch.setattr(app_module, "paint_sheet", lambda sheet, printer: printed.append(sheet.name))
    try:
        settings = Settings(hearing_on=True, hearing_link=device.SIMULATOR, cap_url=receiver.url, cap_key=KEY)
        window = open_window(qapp, results, settings=settings)
        monkeypatch.setattr(window, "_choose_printer", lambda printer: True)
        window.set_patient(HONG)

        def enter(dialog):
            assert dialog.windowTitle() == "청력검사 직접 입력"
            assert dialog.patient_check.isChecked() and dialog.patient_check.text() == "홍길동 (00512) 환자의 검사입니다"
            type_a_result(dialog, "종이 결과지에서 옮김", "MDS 고장으로 손으로 넣음", datetime(2026, 11, 25, 15, 20))
            dialog.accept()

        window.staff.steps.append(enter)
        window.manual_button.click()

        stem = f"{STEM}_00512"
        assert saved(results) == [f"{stem}.bin", f"{stem}.jpg", f"{stem}.json"]
        assert (results / f"{stem}.bin").read_bytes() == b""
        record = record_of(results, stem)
        assert (record["exam"], record["format"], record["source"], record["device"]) == ("hearing", 1, "manual", None)
        assert (record["note"], record["remarks"], record["tested_at"]) == (
            "종이 결과지에서 옮김", "MDS 고장으로 손으로 넣음", "2026-11-25T15:20:00")
        assert record["patient"]["chart_no"] == "00512" and record["tests"] == []
        assert record["speech"]["curves"][0]["points"][1]["score_percent"] == 88   # worked out from 22 of 25
        assert window.state_label.text() == "✔ 홍길동 (00512) 환자 결과로 저장했습니다."
        assert "직접 입력한 값으로" in window.hearing_label.text()
        assert window.remarks_edit.toPlainText() == "MDS 고장으로 손으로 넣음"

        window.print_button.click()
        assert printed == [f"{stem}.jpg"]

        window.send_button.click()
        (sent,) = receiver.received
        assert (sent["exam"], sent["chart"], sent["date"]) == ("hearing", "00512", "2026-11-25")
        assert sent["file"] == (results / f"{stem}.jpg").read_bytes()
        assert all("MDS 고장" not in str(value) for key, value in sent.items() if key != "file")

        window.handle_event(TYMPANOGRAM)
        window.open_result(results / f"{stem}.json")
        assert isinstance(window._measurement, HearingResult) and window._measurement.manual
        assert window.remarks_edit.toPlainText() == "MDS 고장으로 손으로 넣음"

        def correct(dialog):
            grid = dialog.tone_grid("L")
            assert grid.text_at(grid.row_of("Ac"), grid.column_of(1000)) == "25"
            assert dialog.windowTitle() == "검사 값 고치기"
            grid.put(grid.row_of("Ac"), grid.column_of(1000), "30")
            dialog.accept()

        window.staff.steps.append(correct)
        window.values_button.click()
        window.tested_edit.setDateTime(QDateTime(datetime(2026, 11, 24, 9, 0)))
        window.note_button.click()

        record = record_of(results, stem)
        assert record["source"] == "manual" and record["tested_at"] == "2026-11-24T09:00:00"
        left = next(curve for curve in record["tone"]["curves"] if (curve["kind"], curve["ear"]) == ("Ac", "L"))
        assert left["points"][1]["level_db_hl"] == 30
        assert (results / f"{stem}.bin").read_bytes() == b""
        assert len(receiver.received) == 1                    # corrected after sending: not sent again by itself
    finally:
        receiver.close()


def test_typing_in_needs_at_least_one_value(qapp, results):
    window = open_window(qapp, results)

    def save_empty(dialog):
        dialog.accept()
        assert "값을 하나 이상 넣으세요" in dialog.problem_label.text() and dialog.outcome is None
        dialog.reject()

    window.staff.steps.append(save_empty)
    window.manual_button.click()
    assert saved(results) == []


def test_a_patient_left_from_the_previous_result_is_not_put_on_a_typed_in_result_unless_confirmed(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(TYMPANOGRAM)

    def enter(dialog):
        assert not dialog.patient_check.isChecked()
        assert "앞 검사에 쓴 것입니다" in dialog.patient_note.text()
        type_a_result(dialog)
        dialog.accept()

    window.staff.steps.append(enter)
    window.manual_button.click()

    assert f"{STEM}.json" in saved(results)
    assert record_of(results, STEM)["patient"]["chart_no"] == ""
    assert "환자 정보 없이 저장했습니다" in window.state_label.text()


def test_a_typed_in_result_that_cannot_be_saved_keeps_the_editor_open(qapp, results, monkeypatch):
    window = open_window(qapp, results)
    window.set_patient(KIM)

    def full_disk(*arguments, **options):
        raise OSError("디스크가 가득 찼습니다")

    def enter(dialog):
        type_a_result(dialog)
        monkeypatch.setattr(app_module, "save_result", full_disk)
        dialog.accept()
        assert dialog.outcome is None and "디스크가 가득 찼습니다" in dialog.problem_label.text()
        monkeypatch.undo()
        dialog.accept()

    window.staff.steps.append(enter)
    window.manual_button.click()

    assert f"{STEM}_12345.json" in saved(results)
    assert window.problem_label.isHidden()


# ----- remarks, which are never printed ------------------------------------------------------------------

def test_remarks_on_a_hearing_result_are_saved_reopened_and_never_printed_or_sent(qapp, results):
    receiver = CaptureReceiver()
    try:
        settings = Settings(hearing_on=True, hearing_link=device.SIMULATOR, cap_url=receiver.url, cap_key=KEY)
        window = received(qapp, results, patient=HONG, settings=settings)
        stem = f"{STEM}_00512"
        sheet_before = (results / f"{stem}.jpg").read_bytes()

        window.remarks_edit.setPlainText("검사 중 기침이 잦았음")
        assert window.note_button.isEnabled()
        window.note_button.click()

        assert record_of(results, stem)["remarks"] == "검사 중 기침이 잦았음"
        assert (results / f"{stem}.jpg").read_bytes() == sheet_before
        window.send_button.click()
        (sent,) = receiver.received
        assert sent["file"] == sheet_before
        assert all("기침" not in str(value) for key, value in sent.items() if key != "file")

        window.handle_event(TYMPANOGRAM)
        assert window.remarks_edit.toPlainText() == ""
        window.open_result(results / f"{stem}.json")
        assert window.remarks_edit.toPlainText() == "검사 중 기침이 잦았음"
    finally:
        receiver.close()


def test_no_response_is_typed_as_n_or_marked_from_the_menu_and_delete_empties_the_chosen_cells(qapp, results):
    from PySide6.QtCore import Qt

    def work(dialog):
        grid = dialog.tone_grid("R")
        row, column = grid.row_of("Ac"), grid.column_of(2000)
        grid.put(row, column, "70N")
        assert grid.text_at(row, column) == "70↓"                    # written as the sheet prints it
        grid.put(grid.row_of("Ac", True), column, "M")
        assert grid.text_at(grid.row_of("Ac", True), column) == "?"
        grid.toggle_no_response([grid.item(row, column)])
        assert grid.text_at(row, column) == "70"
        grid.toggle_no_response([grid.item(row, column)])
        assert grid.text_at(row, column) == "70↓" and grid.average_text("Ac") == "61↑"   # (45 + 110 + 140 + 75) / 6

        grid.clearSelection()
        grid.item(row, column).setSelected(True)
        grid.item(grid.row_of("Ac", True), column).setSelected(True)
        QTest.keyClick(grid, Qt.Key.Key_Delete)
        assert grid.text_at(row, column) == "" and grid.text_at(grid.row_of("Ac", True), column) == ""
        assert grid.average_text("Ac") == "57*"                      # 2k left out: (45 + 110 + 75) / 4
        dialog.accept()

    window = received(qapp, results)
    window.staff.steps.append(work)
    window.values_button.click()

    assert window.staff.dialogs[0].outcome is not None
    assert load_result(results / f"{STEM}_12345.jpg").measurement.tone_curve("Ac", "R").at(2000) is None
