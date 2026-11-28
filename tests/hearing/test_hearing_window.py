"""Receiving from the audiometer in the operator's window, against the simulated device, without a display."""

from __future__ import annotations

import gc
import json
import os
import time
from datetime import date, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

import shiboken6  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import KEY, CaptureReceiver  # noqa: E402
from hearing_fakes import Audiometer, Bench, Clock, Silent, Unplugged  # noqa: E402
from tsm300 import app as app_module  # noqa: E402
from tsm300.app import HEARING_LABEL, HearingReceiveDialog, MainWindow, SettingsDialog, run_in_background  # noqa: E402
from tsm300.hearing import device  # noqa: E402
from tsm300.hearing.record import HearingResult, can_be_aided  # noqa: E402
from tsm300.hearing.samples import DEMO_SERIAL, tonal  # noqa: E402
from tsm300.hearing.simulator import Behaviour, SimulatedAudiometer  # noqa: E402
from tsm300.patient import Order, Patient  # noqa: E402
from tsm300.protocol import parse_message  # noqa: E402
from tsm300.settings import Settings  # noqa: E402
from tsm300.store import load_result  # noqa: E402

WHEN = datetime(2026, 11, 27, 10, 30, 5)
KIM = Patient("12345", "김하나", "여", date(1980, 1, 31))
LEE = Patient("67890", "이두리", "남", date(1975, 6, 15))
HONG = Patient("00512", "홍길동", "남", date(1975, 3, 4))
HEARING = Settings(hearing_on=True, hearing_link=device.SIMULATOR)
TYMPANOGRAM = parse_message((Path(__file__).parents[1] / "data" / "both_ears.bin").read_bytes())
STEM = "20261127_103005"
PRACTICE = [(3, 0), (3, 1), (3, 2), (3, 3)]   # the made-up hearing-aid patient's four tests, the newest


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


SOUNDFIELD_ONLY = "soundfield-only"


class Operator:
    """Answers the receiving dialog as told: which tests to tick, which were done with hearing aids
    (or ``SOUNDFIELD_ONLY``: every ticked test with soundfield curves only), whether to show those
    saved before, and whether to confirm the patient."""

    def __init__(self, choose=None, aided=(), show_all=False, patient=None, cancel=False) -> None:
        self.choose, self.show_all, self.patient, self.cancel = choose, show_all, patient, cancel
        self.aided = aided if aided == SOUNDFIELD_ONLY else set(aided)
        self.dialogs: list[HearingReceiveDialog] = []
        self.offered: list[set] = []

    def __call__(self, window, fetched, seen, patient, patient_is_new, received_at):
        dialog = HearingReceiveDialog(window, fetched, seen, patient, patient_is_new, received_at)
        self.dialogs.append(dialog)
        self.offered.append(dialog.chosen_keys())
        if self.show_all:
            dialog.all_check.setChecked(True)
        if self.choose is not None:
            for key in list(dialog.chosen_keys()):
                dialog.set_chosen(key, False)
            for key in self.choose:
                dialog.set_chosen(key, True)
        aided = self.aided
        if aided == SOUNDFIELD_ONLY:
            aided = {key for key in dialog.chosen_keys() if can_be_aided(dialog._tests[key][1])}
        for key in aided:
            dialog.set_chosen(key, True, aided=True)
        if self.patient is not None:
            dialog.patient_check.setChecked(self.patient)
        if self.cancel:
            return None
        dialog.accept()
        return dialog.choice()


def open_window(qapp, results, audiometer=None, operator=None, settings=HEARING, **options):
    audiometer = audiometer or Audiometer()
    operator = operator or Operator(aided=SOUNDFIELD_ONLY)
    options.setdefault("background", run_now)
    options.setdefault("confirm", unexpected_question)
    options.setdefault("confirm_patient", lambda patient: True)
    options.setdefault("settings_directory", results.parent / "settings")
    options.setdefault("hearing_opener", lambda link: audiometer.open)
    options.setdefault("fetch_hearing", lambda opener, folder, progress, **kw: device.fetch(
        opener, folder, progress, now=lambda: WHEN, clock=audiometer.clock, **kw))
    window = MainWindow(results, settings, now=lambda: WHEN,
                        choose_hearing=lambda *args: operator(window, *args), **options)
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    _OPENED.append(window)
    window.audiometer, window.operator = audiometer, operator
    return window


def saved(results):
    return sorted(path.name for path in results.iterdir() if path.is_file()) if results.exists() else []


def record_of(results, stem):
    return json.loads((results / f"{stem}.json").read_text(encoding="utf-8"))


def seen(results):
    return device.SeenTests(results.parent / "settings" / device.SEEN_FILE).load()


# ----- the button --------------------------------------------------------------------------------

def test_the_button_is_there_only_when_the_audiometer_is_used(qapp, results):
    window = open_window(qapp, results, settings=Settings())
    assert window.hearing_button.isHidden()
    assert "청력검사" not in window.state_label.text()

    window.apply_settings(HEARING)

    assert not window.hearing_button.isHidden() and window.hearing_button.text() == HEARING_LABEL
    assert "[청력검사기에서 받기]" in window.state_label.text()


# ----- receiving -----------------------------------------------------------------------------------

def test_the_newest_device_patients_new_tests_become_one_sheet_for_the_patient_in_the_window(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)

    window.hearing_button.click()

    dialog = window.operator.dialogs[0]
    assert window.operator.offered[0] == set(PRACTICE)
    assert dialog.patient_check.isChecked() and dialog.patient_check.text() == "김하나 (12345) 환자의 검사입니다"
    assert "새 검사 9건 · 기기에 있는 검사 9건" in dialog.summary.text()
    assert saved(results) == [f"{STEM}_12345.bin", f"{STEM}_12345.jpg", f"{STEM}_12345.json"]
    record = record_of(results, f"{STEM}_12345")
    assert (record["exam"], record["patient"]["name"], record["tested_at"]) == ("hearing", "김하나", "2026-11-27T10:30:00")
    assert [test["aided"] for test in record["tests"]] == [False, False, True, True]
    assert {(curve["kind"], curve["ear"]) for curve in record["tone"]["curves"]} >= {("Spkr", "R"), ("SpkrA", "R")}
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."
    assert window.tested_edit.dateTime().toPython() == datetime(2026, 11, 27, 10, 30)
    assert "검사 4건으로 결과지를 만들었습니다" in window.hearing_label.text()
    assert "아직 결과지로 만들지 않은 새 검사가 5건" in window.hearing_label.text()
    assert not window.preview.pixmap().isNull()
    assert len(seen(results)) == 4
    assert window.hearing_button.isEnabled()


def test_while_the_audiometer_waits_for_connect_to_pc_the_window_says_what_to_press(qapp, results):
    bench = Bench(Behaviour(needs_button=True), press_at=6.0)
    shown = []

    def fetch(opener, folder, progress, waiting=None):
        def told(left):
            waiting(left)
            window._show_hearing()
            shown.append(window.hearing_label.text())
        return device.fetch(bench.open, folder, progress, now=lambda: WHEN, clock=bench.clock,
                            sleep=bench.sleep, waiting=told)

    window = open_window(qapp, results, fetch_hearing=fetch)
    window.set_patient(KIM)
    window.hearing_button.click()

    assert shown and shown[0] == device.WAITING.format(left=29)
    assert all("장비에서 [Connect to PC]를 눌러 주세요" in text for text in shown)
    assert f"{STEM}_12345.json" in saved(results)
    assert "검사 4건으로 결과지를 만들었습니다" in window.hearing_label.text()


def test_a_second_reading_offers_only_the_tests_not_saved_yet(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.hearing_button.click()

    window.operator.choose, window.operator.aided = [(2, 0)], set()
    window.set_patient(LEE)
    window.hearing_button.click()

    dialog = window.operator.dialogs[1]
    assert "새 검사 5건" in dialog.summary.text()
    assert window.operator.offered[1] == {(2, 0)}              # the high-frequency patient is now the newest with new tests
    assert dialog.tree.topLevelItemCount() == 3
    assert record_of(results, f"{STEM}_67890")["high_frequency"]["curves"][0]["ear"] == "R"

    window.audiometer.memory.add(5, "견본", "마", "01/01/1990", "F",
                                 [tonal({"Ac R": ({1000: 25}, None)}, serial=DEMO_SERIAL)])
    window.operator.choose = None
    window.set_patient(Patient("24680", "박세나"))
    window.hearing_button.click()

    assert window.operator.offered[2] == {(4, 0)}              # a test done since, under its own device patient
    assert "새 검사 5건" in window.operator.dialogs[2].summary.text()
    assert len(seen(results)) == 6


def test_tests_saved_before_can_be_shown_and_made_into_a_sheet_again(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.hearing_button.click()

    window.operator.choose, window.operator.show_all = PRACTICE, True
    window.set_patient(KIM)
    window.hearing_button.click()

    dialog = window.operator.dialogs[1]
    items = [dialog.tree.topLevelItem(3).child(index).text(0) for index in range(4)]
    assert all(text.endswith("(이미 받음)") for text in items)
    assert f"{STEM}_12345_2.json" in saved(results)

    dialog.all_check.setChecked(False)  # hidden again, they are no longer chosen
    assert dialog.chosen_keys() == set()


def test_the_same_soundfield_twice_is_refused_until_the_second_is_marked_as_aided(qapp, results):
    window = open_window(qapp, results, operator=Operator())
    window.set_patient(KIM)

    window.hearing_button.click()

    dialog = window.operator.dialogs[0]
    assert dialog.choice() is None and saved(results) == []
    assert "음장 오른쪽 결과가 두 검사에" in dialog.problem_label.text()
    assert "[보청기 착용]" in dialog.problem_label.text()
    assert "만들지 않았습니다" in window.hearing_label.text()
    assert seen(results) == set()

    dialog.set_chosen((3, 2), aided=True)
    dialog.set_chosen((3, 3), aided=True)
    dialog.accept()
    assert isinstance(dialog.choice()[0], HearingResult)


def test_cancelling_saves_nothing(qapp, results):
    window = open_window(qapp, results, operator=Operator(cancel=True))
    window.hearing_button.click()
    assert saved(results) == [] and seen(results) == set()
    assert "만들지 않았습니다" in window.hearing_label.text()


# ----- whose tests --------------------------------------------------------------------------------

def test_without_a_patient_the_sheet_is_saved_without_one_and_given_one_afterwards(qapp, results):
    window = open_window(qapp, results)

    window.hearing_button.click()

    assert window.operator.dialogs[0].patient_check.isHidden()
    assert saved(results) == [f"{STEM}.bin", f"{STEM}.jpg", f"{STEM}.json"]
    assert "환자 정보 없이 저장했습니다" in window.state_label.text()

    window.set_patient(KIM)
    window.save_button.click()

    assert saved(results) == [f"{STEM}_12345.bin", f"{STEM}_12345.jpg", f"{STEM}_12345.json"]
    again = load_result(results / f"{STEM}_12345.jpg").measurement
    assert isinstance(again, HearingResult) and [part.aided for part in again.parts] == [False, False, True, True]


def test_a_patient_left_from_the_previous_result_has_to_be_confirmed(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(TYMPANOGRAM)
    assert f"{STEM}_12345.json" in saved(results)

    window.hearing_button.click()

    dialog = window.operator.dialogs[0]
    assert not dialog.patient_check.isChecked()
    assert "앞 검사에 쓴 것입니다" in dialog.patient_note.text()
    assert f"{STEM}.json" in saved(results)                  # saved without the patient,
    assert "앞 검사에 쓴 정보입니다" in window.state_label.text()

    window.operator.patient = True                           # unless the operator says it is theirs
    window.operator.show_all = True
    window.operator.choose = PRACTICE
    window.hearing_button.click()
    assert record_of(results, f"{STEM}_12345_2")["exam"] == "hearing"


def test_a_device_patient_with_another_birth_date_is_pointed_out(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.hearing_button.click()
    heading = window.operator.dialogs[0].tree.topLevelItem(3).text(0)
    assert heading.startswith("기기 환자 4 · 라 견본 · 20/05/1955 · F")
    assert "생년월일이 화면의 환자(1980-01-31)와 다릅니다" in heading


# ----- what can go wrong ----------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "USB 에 연결된 청력검사기가 없습니다. 선과 전원, 그리고 MRS 드라이버 설치를 확인해 주세요.",
    "청력검사기를 다른 프로그램이 쓰고 있습니다. MDS 가 켜져 있으면 닫아 주세요.",
])
def test_no_device_or_one_held_by_another_program_is_explained(qapp, results, message):
    def opener():
        raise device.DeviceError(message)

    window = open_window(qapp, results, hearing_opener=lambda link: opener)

    window.hearing_button.click()

    assert window.hearing_label.text().startswith(f"⚠ 청력검사기에서 받지 못했습니다. {message}")
    assert "주고받은 기록:" in window.hearing_label.text()
    assert saved(results) == [] and window.operator.dialogs == [] and window.hearing_button.isEnabled()


def test_a_device_that_does_not_answer_is_explained(qapp, results):
    clock = Clock()
    window = open_window(qapp, results, hearing_opener=lambda link: lambda: Silent(clock),
                         fetch_hearing=lambda opener, folder, progress, **kw: device.fetch(opener, folder, progress, clock=clock, **kw))
    window.hearing_button.click()
    assert device.NO_ANSWER in window.hearing_label.text()
    assert saved(results) == []


def test_a_cable_pulled_half_way_is_explained(qapp, results):
    audiometer = Audiometer()
    window = open_window(qapp, results, audiometer=audiometer, hearing_opener=lambda link: lambda: Unplugged(
        SimulatedAudiometer(audiometer.memory), audiometer.clock, reads=10))
    window.hearing_button.click()
    assert "주고받다가 멈췄습니다" in window.hearing_label.text() and "USB 선을 확인" in window.hearing_label.text()
    assert saved(results) == []


@pytest.mark.parametrize("behaviour", [
    Behaviour(answer_identification="ACK"),
    Behaviour(ack_at_end=False),
    Behaviour(chunk=7),
    Behaviour(noise_before=b"\x00\xff"),
    Behaviour(unasked_test=True),
])
def test_other_ways_a_device_might_answer_still_give_the_sheet(qapp, results, behaviour):
    window = open_window(qapp, results, audiometer=Audiometer(behaviour))
    window.set_patient(KIM)
    window.hearing_button.click()
    assert f"{STEM}_12345.json" in saved(results)
    assert len(record_of(results, f"{STEM}_12345")["tests"]) == 4
    dialog = window.operator.dialogs[0]
    assert "새 검사 9건 · 기기에 있는 검사 9건" in dialog.summary.text()   # a test pushed twice is listed once
    if not behaviour.ack_at_end:
        assert device.NOT_FINISHED in dialog.warning_label.text()


def test_a_sheet_that_cannot_be_saved_leaves_the_tests_new_for_next_time(qapp, results, monkeypatch):
    def full_disk(*args, **kwargs):
        raise OSError("디스크가 가득 찼습니다")

    window = open_window(qapp, results)
    window.set_patient(KIM)
    monkeypatch.setattr(app_module, "save_result", full_disk)

    window.hearing_button.click()

    assert "[청력검사기에서 받기]를 다시 눌러 주세요" in window.problem_label.text()
    assert seen(results) == set()
    kept = list((results.parent / "settings" / "unsaved").glob("*.bin"))
    assert len(kept) == 1 and kept[0].read_bytes().startswith(b"KA")


def test_every_byte_is_logged_in_the_results_folder_unless_switched_off(qapp, results):
    window = open_window(qapp, results)
    window.hearing_button.click()
    (log,) = (results / device.LOG_FOLDER).glob("*.txt")
    assert "<DEV" in log.read_text(encoding="utf-8")
    assert f"주고받은 기록: {log}" in window.hearing_label.text()

    quiet = open_window(qapp, results.parent / "other", settings=Settings(hearing_on=True, hearing_log=False))
    quiet.hearing_button.click()
    assert not (results.parent / "other" / device.LOG_FOLDER).exists()


def test_receiving_does_not_hold_the_window_up(qapp, results):
    audiometer = Audiometer()

    class Slow:
        def __init__(self, link):
            self.link = link

        def read(self):
            time.sleep(0.002)
            return self.link.read()

        def write(self, data):
            self.link.write(data)

        def close(self):
            self.link.close()

    window = open_window(qapp, results, audiometer=audiometer, background=run_in_background,
                         hearing_opener=lambda link: lambda: Slow(audiometer.open()))
    window.set_patient(KIM)

    window.hearing_button.click()

    assert not window.hearing_button.isEnabled() and "받는 중" in window.hearing_label.text()
    for _ in range(200):
        if f"{STEM}_12345.json" in saved(results):
            break
        QTest.qWait(50)
    assert f"{STEM}_12345.json" in saved(results) and window.hearing_button.isEnabled()


def test_closing_while_receiving_asks_first(qapp, results):
    asked = []
    window = open_window(qapp, results, confirm=lambda question: asked.append(question) or False)
    window._receiving = True
    assert not window._may_close()
    assert "받는 중입니다" in asked[0]
    window._receiving = False


# ----- after saving: correcting, reopening, sending, filing -------------------------------------

def test_the_test_time_is_the_time_received_and_can_be_corrected(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.hearing_button.click()

    window.tested_edit.setDateTime(datetime(2026, 11, 27, 9, 50))
    window.note_edit.setPlainText("보청기 착용 전 · 후")
    window.note_button.click()

    record = record_of(results, f"{STEM}_12345")
    assert (record["tested_at"], record["note"], record["exam"]) == ("2026-11-27T09:50:00", "보청기 착용 전 · 후", "hearing")


def test_an_earlier_hearing_result_opens_and_is_corrected(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.hearing_button.click()
    window.handle_event(TYMPANOGRAM)

    window.open_result(results / f"{STEM}_12345.json")
    assert isinstance(window._measurement, HearingResult)
    window.note_edit.setPlainText("재검")
    window.note_button.click()

    loaded = load_result(results / f"{STEM}_12345.jpg")
    assert loaded.note == "재검" and [part.aided for part in loaded.measurement.parts] == [False, False, True, True]


def test_the_sheet_goes_to_capture_as_a_hearing_test(qapp, results):
    receiver = CaptureReceiver()
    try:
        settings = Settings(hearing_on=True, cap_on=True, cap_url=receiver.url, cap_key=KEY)
        window = open_window(qapp, results, settings=settings)
        window.set_patient(HONG)
        window.hearing_button.click()

        (sent,) = receiver.received
        assert (sent["exam"], sent["chart"], sent["date"]) == ("hearing", "00512", "2026-11-27")
        assert sent["file"] == (results / f"{STEM}_00512.jpg").read_bytes()
        assert "상쾌한캡처에 저장했습니다" in window.capture_label.text()
    finally:
        receiver.close()


def test_a_hearing_result_is_filed_like_a_tympanogram(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.hearing_button.click()

    window.file_button.click()

    folder = results / "보관" / "2026-11" / "2026-11-27"
    assert sorted(path.name for path in folder.iterdir()) == [
        "12345_김하나_1030.bin", "12345_김하나_1030.jpg", "12345_김하나_1030.json"]
    assert json.loads((folder / "12345_김하나_1030.json").read_text(encoding="utf-8"))["exam"] == "hearing"


# ----- settings and today's orders -------------------------------------------------------------------

def test_settings_dialog_has_the_audiometer_tab_and_tries_the_connection(qapp, results):
    audiometer = Audiometer()
    opened = []

    def opener_for(link):
        opened.append(link)
        return audiometer.open

    dialog = SettingsDialog(None, Settings(), run_now, ports=lambda: [], fixed_folder=results,
                            hearing_opener=opener_for)
    _OPENED.append(dialog)
    assert dialog.tabs.tabText(4) == "청력검사기"
    assert not dialog.hearing_on_check.isChecked() and dialog.hearing_log_check.isChecked()

    dialog.hearing_on_check.setChecked(True)
    dialog.hearing_link_combo.setCurrentIndex(dialog.hearing_link_combo.findData(device.SIMULATOR))
    edited = dialog.settings()
    assert (edited.hearing_on, edited.hearing_link, edited.hearing_log) == (True, device.SIMULATOR, True)
    assert "실제 청력검사기가 아님" in dialog.hearing_link_combo.currentText()

    dialog.hearing_test_button.click()

    assert dialog.hearing_result.text() == f"✔ 연결됨: MRS Resonance R37A (일련번호 {DEMO_SERIAL})"
    assert opened == [device.SIMULATOR]
    assert [command[4] for command in audiometer.sent()] == [0x16]  # who it is, and nothing more
    assert list((results / device.LOG_FOLDER).glob("*.txt"))


class OrderedRecords:
    def __init__(self, orders):
        self.orders = orders

    def find(self, chart_no):
        return None

    def todays_orders(self):
        return self.orders

    def internal_number(self, chart_no):
        return ""


def test_orders_say_which_tests_were_ordered_and_tick_each_when_done(qapp, results):
    orders = [Order(KIM, "09:10", "순음청력(표준) · 언어청각(어음)", "진료1실", ("F6341", "F6300")),
              Order(LEE, "09:20", "고막운동성계측 · 순음청력(표준)", "진료2실", ("F6361", "F6341")),
              Order(HONG, "09:30", "고막운동성계측", "진료1실", ("F6361",))]
    window = open_window(qapp, results, source=OrderedRecords(orders))
    marks = lambda: [window.orders_table.item(row, 5).text() for row in range(3)]  # noqa: E731
    assert marks() == ["청력", "중이 · 청력", "중이"]
    assert window.orders_table.item(0, 2).toolTip() == "순음청력(표준) · 언어청각(어음)"

    window.set_patient(KIM)
    window.hearing_button.click()
    window.set_patient(LEE)
    window.handle_event(TYMPANOGRAM)

    assert marks() == ["청력 ✓", "중이 ✓ · 청력", "중이"]
    assert "(검사 전 2명)" in window.orders_note.text()


def test_without_the_audiometer_the_order_list_is_as_it_was(qapp, results):
    window = open_window(qapp, results, settings=Settings(),
                         source=OrderedRecords([Order(KIM, "09:10", "고막운동성계측", "진료1실", ("F6361",))]))
    window.set_patient(KIM)
    window.handle_event(TYMPANOGRAM)
    assert window.orders_table.item(0, 5).text() == "✓"
