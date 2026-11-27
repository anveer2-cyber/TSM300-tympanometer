"""The operator's window, driven without a display, a serial port or the clinic's systems."""

import gc
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

import shiboken6  # noqa: E402
from PySide6.QtCore import QDateTime, QObject, Signal  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import needs_pty, pty  # noqa: E402
from fakes import KEY, TOKEN, CaptureReceiver, RelayServer  # noqa: E402
from tsm300 import __version__, update  # noqa: E402
from tsm300 import settings as settings_file  # noqa: E402
from tsm300.app import (  # noqa: E402
    NO_PORT, REPLACE_LABEL, SAVE_LABEL, Device, MainWindow, SettingsDialog, UpdateDialog, only_instance, paint_sheet,
    pick_port, results_folder, run_in_background,
)
from tsm300.clinic import Clinic  # noqa: E402
from tsm300.patient import Order, Patient  # noqa: E402
from tsm300.protocol import Corrupt, parse_message  # noqa: E402
from tsm300.settings import Settings  # noqa: E402

DATA = Path(__file__).parent / "data"
BOTH = parse_message((DATA / "both_ears.bin").read_bytes())
LEFT_ONLY = parse_message((DATA / "left_only.bin").read_bytes())
EMPTY = parse_message((DATA / "empty.bin").read_bytes())
WHEN = datetime(2026, 11, 26, 21, 12, 5)
KIM = Patient("12345", "김하나", "여", date(1980, 1, 31))
LEE = Patient("67890", "이두리", "남", date(1975, 6, 15))
UNKNOWN = Patient("", "(환자 정보 없음)")
SENDING = Settings(cap_on=True, cap_url="192.0.2.1", cap_key="key")


class FakeRecords:
    """A stand-in for the clinic's records system."""

    def __init__(self, failing=False, delay=0.0):
        self.failing = failing
        self.delay = delay

    def find(self, chart_no):
        time.sleep(self.delay)
        if self.failing:
            raise RuntimeError("서버에 연결할 수 없습니다")
        if chart_no == "77777":
            raise LookupError("차트번호 77777 환자가 2명입니다 — 사람이 확인해 주세요")
        return {"12345": KIM, "67890": LEE}.get(chart_no)

    def todays_orders(self):
        time.sleep(self.delay)
        if self.failing:
            raise RuntimeError("서버에 연결할 수 없습니다")
        return [Order(KIM, "09:10", "고막운동성계측", "진료1실"), Order(LEE, "09:25", "고막운동성계측", "진료2실"),
                Order(UNKNOWN, "09:40", "고막운동성계측", "진료1실")]

    def internal_number(self, chart_no):
        return {"12345": "66001", "67890": "66002"}.get(chart_no, "")


class FakeSender:
    """Records what would be sent to 상쾌한캡처 and answers as told."""

    def __init__(self):
        self.sent = []
        self.internal_numbers = []
        self.outcome = {"ok": True, "duplicate": False, "id": "1", "imageDataName": "66001_20261126_중이검사.jpg",
                        "chartId": "66001"}

    def __call__(self, sheet, patient, tested_on, settings, internal_number=""):
        self.sent.append((sheet.name, patient, tested_on))
        self.internal_numbers.append(internal_number)
        return dict(self.outcome)


class FakeUpdates:
    """Stands in for the releases page and for replacing the program file."""

    PYTHON_COMMAND = "python -m pip install --upgrade (주소)"

    def __init__(self, version="99.0.0", program=Path("TSM300.exe"), unreachable=False, download_fails=False):
        self.version = version
        self.program = program
        self.unreachable = unreachable
        self.download_fails = download_fails
        self.installed = []
        self.restarted = []

    def latest_release(self):
        if self.unreachable:
            raise update.UpdateError("새 버전이 있는지 확인하지 못했습니다.")
        return update.Release(self.version, "http://unused")

    def is_newer(self, version):
        return update.is_newer(version)

    def program_file(self):
        return self.program

    def install(self, release, program, progress=None):
        progress(5, 10)
        if self.download_fails:
            raise update.UpdateError("내려받기가 중간에 끊겼습니다.")
        self.installed.append((release.version, program))

    def restart(self, program, arguments):
        self.restarted.append((program, arguments))


def run_now(owner, work, done):
    """Does background work on the spot, so tests need not wait."""
    try:
        result = work()
    except Exception as error:
        done(None, error)
    else:
        done(result, None)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def results(tmp_path):
    return tmp_path / "results"


_OPENED = []


@pytest.fixture(autouse=True)
def dispose_of_windows(qapp):
    """Destroys each test's windows while Qt is still running, not at interpreter exit."""
    yield
    while _OPENED:
        window = _OPENED.pop()
        window.close()
        shiboken6.delete(window)
    qapp.processEvents()
    gc.collect()


def unexpected_question(question):
    """Fails a test that would otherwise wait for ever on a question nobody answers."""
    raise AssertionError(f"unexpected question: {question}")


def open_window(qapp, results, **options):
    options.setdefault("background", run_now)
    options.setdefault("confirm", unexpected_question)
    options.setdefault("confirm_patient", lambda patient: True)
    options.setdefault("settings_directory", results.parent / "settings")
    window = MainWindow(results, now=lambda: WHEN, **options)
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    _OPENED.append(window)
    return window


def type_text(edit, text):
    """Types into a field as the operator would; Hangul cannot be sent as key presses."""
    if text.isascii():
        QTest.keyClicks(edit, text)
    else:
        edit.setText(edit.text() + text)
        edit.textEdited.emit(edit.text())


def saved(results):
    return sorted(path.name for path in results.iterdir()) if results.exists() else []


def order_row(window, row):
    table = window.orders_table
    return [table.item(row, column).text() for column in range(table.columnCount())]


def wait_until(condition, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if condition():
            return
        QTest.qWait(50)
    raise AssertionError("condition was not met in time")


# ----- typing the patient in ---------------------------------------------------------------

def test_patient_typed_before_the_test_goes_onto_the_result(qapp, results):
    window = open_window(qapp, results)
    type_text(window.chart_edit, "12345")
    type_text(window.name_edit, "김하나")
    window.sex_combo.setCurrentText("여")
    type_text(window.birth_edit, "19800131")
    assert "다음 검사는 김하나 (12345) 환자로 저장됩니다" in window.state_label.text()

    window.handle_event(BOTH)

    stem = "20261126_211205_12345"
    assert saved(results) == [f"{stem}.bin", f"{stem}.jpg", f"{stem}.json"]
    patient = json.loads((results / f"{stem}.json").read_text(encoding="utf-8"))["patient"]
    assert patient == {"chart_no": "12345", "name": "김하나", "sex": "여", "birth_date": "1980-01-31"}
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."
    assert not window.save_button.isEnabled()
    assert not window.preview.pixmap().isNull()


def test_result_without_a_patient_is_kept_and_can_be_given_one_afterwards(qapp, results):
    window = open_window(qapp, results)

    window.handle_event(BOTH)

    assert saved(results) == ["20261126_211205.bin", "20261126_211205.jpg", "20261126_211205.json"]
    assert "환자 정보 없이 저장했습니다" in window.state_label.text()
    assert not window.save_button.isEnabled()

    window.set_patient(KIM)
    assert window.save_button.isEnabled() and window.save_button.text() == SAVE_LABEL
    window.save_button.click()

    stem = "20261126_211205_12345"
    assert saved(results) == [f"{stem}.bin", f"{stem}.jpg", f"{stem}.json"]
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."


def test_patient_left_over_from_the_previous_test_is_not_reused_silently(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.handle_event(LEFT_ONLY)

    assert "20261126_211205.json" in saved(results)
    assert "앞 검사에 쓴 정보입니다" in window.state_label.text()
    window.save_button.click()
    assert "20261126_211205_12345_2.json" in saved(results)
    assert "20261126_211205.json" not in saved(results)


def test_next_patient_does_not_rename_the_previous_result_unless_confirmed(qapp, results):
    asked = []
    answers = iter([False, True])

    def confirm(question):
        asked.append(question)
        return next(answers)

    window = open_window(qapp, results, confirm=confirm)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.set_patient(LEE)
    assert window.save_button.text() == REPLACE_LABEL
    assert "직전 결과는 김하나 (12345) 환자로" in window.state_label.text()
    assert "다음 검사는 이두리 (67890) 환자로" in window.state_label.text()

    window.save_button.click()
    assert "김하나 (12345)" in asked[0] and "이두리 (67890)" in asked[0]
    assert saved(results)[0] == "20261126_211205_12345.bin"

    window.save_button.click()
    assert saved(results)[0] == "20261126_211205_67890.bin"
    assert len(saved(results)) == 3


def test_next_patient_gets_the_next_result(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.set_patient(LEE)

    window.handle_event(LEFT_ONLY)

    assert "20261126_211205_12345.json" in saved(results)
    assert "20261126_211205_67890.json" in saved(results)
    assert window.state_label.text() == "✔ 이두리 (67890) 환자 결과로 저장했습니다."


def test_the_same_transmission_twice_is_saved_once(qapp, results):
    window = open_window(qapp, results)
    window.handle_event(BOTH)
    window.handle_event(BOTH)

    assert len(saved(results)) == 3
    assert "같은 결과를 다시 받았습니다" in window.state_label.text()


def test_empty_memory_slot_and_garbled_transmission_save_nothing(qapp, results):
    window = open_window(qapp, results)

    window.handle_event(EMPTY)
    assert "ID 1 에는 저장된 검사가 없습니다" in window.state_label.text()
    window.handle_event(Corrupt("message stopped before its STOP tag", b"PAID"))
    assert "해독하지 못했습니다" in window.state_label.text()
    assert saved(results) == []


def test_unreadable_birth_date_is_pointed_out_and_keeps_the_patient_off_the_result(qapp, results):
    window = open_window(qapp, results)
    type_text(window.name_edit, "김하나")
    type_text(window.birth_edit, "800131")
    assert "생년월일을 읽을 수 없습니다" in window.form_note.text()

    window.handle_event(BOTH)

    assert saved(results) == ["20261126_211205.bin", "20261126_211205.jpg", "20261126_211205.json"]
    assert not window.save_button.isEnabled()

    window.birth_edit.clear()
    type_text(window.birth_edit, "19800131")
    assert window.form_note.text() == ""


def test_clearing_the_form_readies_it_for_the_next_patient(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.clear_patient()

    assert window.patient().is_empty
    assert "다음 환자 정보를 넣어 주세요" in window.state_label.text()


# ----- looking the patient up --------------------------------------------------------------

def test_without_a_records_system_only_manual_entry_is_offered(qapp, results):
    window = open_window(qapp, results)

    assert not window.orders_table.isVisible() and not window.find_button.isVisible()
    assert window.source_note.isVisible() and "직접 입력" in window.source_note.text()


def test_todays_orders_are_listed_and_fill_the_form_once_confirmed(qapp, results):
    offered = []
    window = open_window(qapp, results, source=FakeRecords(),
                         confirm_patient=lambda patient: offered.append(patient) or True)

    assert window.orders_table.isVisible() and not window.source_note.isVisible()
    assert window.orders_table.rowCount() == 3
    assert order_row(window, 1) == ["09:25", "67890", "이두리", "51세 남", "진료2실", ""]
    assert "3명 (검사 전 2명) · 21:12:05 읽음" in window.orders_note.text()

    window.orders_table.cellClicked.emit(1, 0)

    assert offered == [LEE]
    assert window.patient() == LEE
    assert "다음 검사는 이두리 (67890) 환자로 저장됩니다" in window.state_label.text()


def test_declining_the_confirmation_leaves_the_form_alone(qapp, results):
    window = open_window(qapp, results, source=FakeRecords(), confirm_patient=lambda patient: False)

    window.orders_table.cellClicked.emit(0, 0)

    assert window.patient().is_empty
    assert window.orders_table.selectedItems() == []


def test_an_order_without_an_identified_patient_cannot_be_chosen(qapp, results):
    offered = []
    window = open_window(qapp, results, source=FakeRecords(), confirm_patient=lambda patient: offered.append(patient))

    window.orders_table.cellClicked.emit(2, 0)

    assert offered == [] and window.patient().is_empty
    assert order_row(window, 2)[2] == "(환자 정보 없음)"


def test_patients_already_tested_today_are_ticked(qapp, results):
    window = open_window(qapp, results, source=FakeRecords())
    window.orders_table.cellClicked.emit(0, 0)

    window.handle_event(BOTH)

    assert order_row(window, 0)[5] == "✓" and order_row(window, 1)[5] == ""
    assert "3명 (검사 전 1명)" in window.orders_note.text()


def test_chart_number_lookup_fills_the_form_or_says_what_it_found(qapp, results):
    window = open_window(qapp, results, source=FakeRecords())

    type_text(window.chart_edit, "12345")
    window.find_button.click()
    assert window.patient() == KIM

    window.clear_patient()
    type_text(window.chart_edit, "999")
    window.find_button.click()
    assert window.form_note.text() == "차트번호 999 환자를 찾지 못했습니다."
    assert window.patient() == Patient(chart_no="999")

    window.clear_patient()
    type_text(window.chart_edit, "77777")
    window.find_button.click()
    assert "77777 환자가 2명입니다" in window.form_note.text()


def test_records_system_failures_are_shown_and_leave_manual_entry_usable(qapp, results):
    window = open_window(qapp, results, source=FakeRecords(failing=True))

    assert window.orders_note.text() == "오더 목록을 가져오지 못했습니다: 서버에 연결할 수 없습니다"
    type_text(window.chart_edit, "12345")
    window.find_button.click()
    assert window.form_note.text() == "조회하지 못했습니다: 서버에 연결할 수 없습니다"

    type_text(window.name_edit, "김하나")
    window.handle_event(BOTH)
    assert "20261126_211205_12345.jpg" in saved(results)


def test_lookups_run_in_the_background_without_freezing_the_window(qapp, results):
    window = open_window(qapp, results, source=FakeRecords(delay=0.3), background=run_in_background)

    assert window.orders_table.rowCount() == 0
    assert window.orders_note.text() == "오더 목록을 읽는 중..."
    wait_until(lambda: window.orders_table.rowCount() == 3)

    type_text(window.chart_edit, "67890")
    window.find_button.click()
    assert window.form_note.text() == "조회하는 중..."
    wait_until(lambda: window.patient() == LEE)


# ----- sending to 상쾌한캡처 ----------------------------------------------------------------

def test_saved_sheet_is_sent_straight_away_when_that_is_switched_on(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender)
    window.set_patient(KIM)

    window.handle_event(BOTH)

    assert sender.sent == [("20261126_211205_12345.jpg", KIM, date(2026, 11, 26))]
    assert window.capture_label.text() == "✔ 상쾌한캡처에 저장했습니다: 66001_20261126_중이검사.jpg"
    assert not window.send_button.isEnabled()


def test_nothing_is_sent_without_both_chart_number_and_name(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender, confirm=lambda question: True)

    window.handle_event(BOTH)
    assert sender.sent == [] and window.capture_label.text() == ""

    type_text(window.name_edit, "김하나")
    window.save_button.click()
    assert sender.sent == []
    assert window.capture_label.text() == "상쾌한캡처로 보내려면 차트번호와 이름이 모두 필요합니다."

    type_text(window.chart_edit, "12345")
    window.save_button.click()
    assert [name for name, _, _ in sender.sent] == ["20261126_211205_12345.jpg"]


def test_a_refused_sheet_says_why_and_can_be_sent_again(qapp, results):
    sender = FakeSender()
    sender.outcome = {"ok": False, "code": "connect", "error": "상쾌한캡처에 연결하지 못했습니다", "retry": True}
    window = open_window(qapp, results, settings=SENDING, send=sender)
    window.set_patient(KIM)

    window.handle_event(BOTH)

    assert window.capture_label.text() == (
        "⚠ 상쾌한캡처에 보내지 못했습니다: 상쾌한캡처에 연결하지 못했습니다 (connect). "
        "잠시 뒤 [상쾌한캡처로 보내기]를 눌러 다시 보낼 수 있습니다.")
    assert window.send_button.isEnabled()

    sender.outcome = {"ok": True, "duplicate": False, "id": "1", "imageDataName": "66001_20261126_중이검사.jpg"}
    window.send_button.click()
    assert len(sender.sent) == 2
    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")


def test_sending_waits_for_the_button_when_automatic_sending_is_off(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=Settings(cap_url="192.0.2.1", cap_key="key"), send=sender)
    window.set_patient(KIM)

    window.handle_event(BOTH)
    assert sender.sent == [] and window.send_button.isVisible() and window.send_button.isEnabled()

    window.send_button.click()
    assert len(sender.sent) == 1


def test_send_button_is_absent_until_the_receiver_is_set_up(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    assert not window.send_button.isVisible()


def test_moving_a_sent_result_to_another_patient_warns_that_the_sent_copy_stays(qapp, results):
    asked = []
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender, confirm=lambda q: asked.append(q) or True)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.set_patient(LEE)
    window.save_button.click()

    assert "이미 상쾌한캡처에 김하나 (12345) 환자로 들어가 있습니다" in asked[0]
    assert [patient for _, patient, _ in sender.sent] == [KIM, LEE]


def test_internal_number_from_the_lookup_goes_along_only_for_checking(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender, source=FakeRecords())
    window.orders_table.cellClicked.emit(0, 0)

    window.handle_event(BOTH)

    assert sender.internal_numbers == ["66001"]
    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")


def test_typed_in_patients_are_sent_without_an_internal_number(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender)
    window.set_patient(KIM)

    window.handle_event(BOTH)

    assert sender.internal_numbers == [""]


def test_a_sheet_filed_under_a_different_patient_record_is_pointed_out(qapp, results):
    sender = FakeSender()
    sender.outcome = dict(sender.outcome, chartId="66009", expected_chartId="66001")
    window = open_window(qapp, results, settings=SENDING, send=sender, source=FakeRecords())
    window.orders_table.cellClicked.emit(0, 0)

    window.handle_event(BOTH)

    text = window.capture_label.text()
    assert text.startswith("⚠ 상쾌한캡처에 저장했지만 확인이 필요합니다")
    assert "66001" in text and "66009" in text


def test_sheet_reaches_a_receiver_over_the_network(qapp, results):
    receiver = CaptureReceiver(patient=("12345", "김하나", "1980-01-31"))
    try:
        settings = Settings(cap_on=True, cap_url=receiver.url, cap_key=KEY)
        window = open_window(qapp, results, settings=settings, background=run_in_background)
        window.set_patient(KIM)

        window.handle_event(BOTH)

        assert window.capture_label.text() == "상쾌한캡처로 보내는 중..."
        wait_until(lambda: window.capture_label.text().startswith("✔"))
        assert receiver.received[0]["file"] == (results / "20261126_211205_12345.jpg").read_bytes()
        noted = json.loads((results / "20261126_211205_12345.json").read_text(encoding="utf-8"))["capture"]
        assert noted[0]["ok"] and noted[0]["imageDataName"] == "66000_20261126_중이검사.jpg"
    finally:
        receiver.close()


# ----- settings ----------------------------------------------------------------------------

def test_settings_dialog_returns_what_was_entered(qapp):
    dialog = SettingsDialog(None, Settings(port="COM5", emr_uid="kept"), run_now)
    _OPENED.append(dialog)
    assert not dialog.server_edit.isEnabled() and not dialog.lookup_test_button.isEnabled()

    dialog.via_combo.setCurrentIndex(dialog.via_combo.findData("server"))
    dialog.server_edit.setText(" 192.0.2.7 ")
    dialog.token_edit.setText("token")
    dialog.cap_url_edit.setText("192.0.2.8:5090")
    dialog.cap_key_edit.setText("key")
    dialog.cap_on_check.setChecked(True)

    assert dialog.server_edit.isEnabled() and not dialog.dsn_edit.isEnabled()
    assert dialog.settings() == Settings(port="COM5", emr_via="server", emr_uid="kept", emr_server="192.0.2.7",
                                         emr_token="token", cap_on=True, cap_url="192.0.2.8:5090", cap_key="key")


def test_settings_dialog_tests_the_receiver_connection(qapp):
    receiver = CaptureReceiver()
    try:
        dialog = SettingsDialog(None, Settings(cap_url=receiver.url, cap_key=KEY), run_now)
        _OPENED.append(dialog)

        dialog.capture_test_button.click()
        assert dialog.capture_result.text() == "✔ 연결됨: 중이검사실 → 진료실PC"

        dialog.cap_key_edit.setText("wrong")
        dialog.capture_test_button.click()
        assert dialog.capture_result.text() == "⚠ 키가 맞지 않습니다"
    finally:
        receiver.close()


def test_applying_settings_saves_them_and_switches_lookup_on(qapp, results):
    relay = RelayServer()
    try:
        window = open_window(qapp, results)
        directory = results.parent / "settings"

        window.apply_settings(Settings(emr_via="server", emr_server=relay.url, emr_token=TOKEN,
                                       cap_url="192.0.2.2", cap_key="key"))

        assert settings_file.load(directory).emr_server == relay.url
        assert window.orders_box.isVisible() and window.find_button.isVisible() and not window.source_note.isVisible()
        assert order_row(window, 0)[:5] == ["09:12", "00512", "홍길동", "51세 남", "진료1실"]
        assert window.send_button.isVisible()

        window.apply_settings(Settings())
        assert not window.orders_box.isVisible() and window.source_note.isVisible()
        assert not window.send_button.isVisible()
    finally:
        relay.close()


# ----- the serial port ---------------------------------------------------------------------

@needs_pty
class TestOnASerialPort:
    """The background reader and the program's start-up, against a pseudo-terminal."""

    pty = pty

    def test_worker_reads_the_port_and_hands_results_to_the_window(self, qapp, results):
        from tsm300.app import SerialWorker

        master, slave = self.pty.openpty()
        window = open_window(qapp, results)
        worker = SerialWorker(os.ttyname(slave), 9600)
        worker.status.connect(window.show_connection)
        worker.received.connect(window.handle_event)
        window.set_patient(KIM)
        worker.start()
        try:
            wait_until(lambda: "연결됨" in window.connection_label.text())
            os.write(master, (DATA / "power_on.bin").read_bytes() + BOTH.raw)
            wait_until(lambda: saved(results))
        finally:
            worker.requestInterruption()
            assert worker.wait(3000)
            worker.status.disconnect()
            worker.received.disconnect()

        assert "20261126_211205_12345.jpg" in saved(results)
        assert "연결됨" in window.connection_label.text()
        assert "장비 켜짐 (TSM-300 SCREENING)" in window.connection_label.text()

    def test_program_starts_listens_and_saves_until_the_window_closes(self, qapp, tmp_path, monkeypatch):
        from PySide6.QtCore import QTimer

        from tsm300 import cli

        monkeypatch.setenv("TSM300_DIR", str(tmp_path / "settings"))
        master, slave = self.pty.openpty()
        out = tmp_path / "results"
        QTimer.singleShot(300, lambda: os.write(master, LEFT_ONLY.raw))
        watcher = QTimer()
        watcher.timeout.connect(lambda: qapp.quit() if list(out.glob("*.jpg")) else None)
        watcher.start(50)
        QTimer.singleShot(8000, qapp.quit)

        code = cli.main(["--port", os.ttyname(slave), "--out", str(out)])

        watcher.stop()
        assert code == 0
        assert len(list(out.glob("*.jpg"))) == 1


# ----- confirming the patient --------------------------------------------------------------

def test_confirmation_shows_who_was_found_and_ignores_a_held_enter_key(qapp):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QLabel

    from tsm300.app import PatientConfirm

    dialog = PatientConfirm(None, LEE, date(2026, 11, 26))
    _OPENED.append(dialog)
    dialog.show()
    assert QTest.qWaitForWindowExposed(dialog)

    shown = [label.text() for label in dialog.findChildren(QLabel)]
    assert shown == ["이 환자가 맞습니까?", "이두리", "차트번호 67890", "1975-06-15 · 남 51세"]

    held = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r", True)
    QApplication.sendEvent(dialog, held)
    assert dialog.isVisible()

    QTest.keyClick(dialog, Qt.Key.Key_Return)
    assert not dialog.isVisible() and dialog.result() == PatientConfirm.DialogCode.Accepted


# ----- unexpected errors -------------------------------------------------------------------

def test_unexpected_errors_are_logged_and_shown_when_there_is_no_console(qapp, tmp_path, monkeypatch):
    import sys

    from PySide6.QtWidgets import QMessageBox

    from tsm300.app import report_crashes

    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: shown.append(text))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    report_crashes(tmp_path / "settings")

    try:
        raise ValueError("깨진 결과")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    log = (tmp_path / "settings" / "error.log").read_text(encoding="utf-8")
    assert "ValueError: 깨진 결과" in log and "Traceback" in log
    assert "깨진 결과" in shown[0] and "error.log" in shown[0]


# ----- memo and earlier results ------------------------------------------------------------

def note_of(results, stem):
    return json.loads((results / f"{stem}.json").read_text(encoding="utf-8"))["note"]


def test_memo_can_only_be_written_once_a_result_is_shown(qapp, results):
    window = open_window(qapp, results)
    assert not window.note_edit.isEnabled() and not window.note_button.isEnabled()

    window.set_patient(KIM)
    window.handle_event(BOTH)

    assert window.note_edit.isEnabled() and not window.note_button.isEnabled()
    window.note_edit.setPlainText("오른쪽 프로브 밀착이 어려웠음")
    assert window.note_button.isEnabled()


def test_saving_a_memo_prints_it_on_the_sheet_of_the_same_patient(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    before = (results / "20261126_211205_12345.jpg").read_bytes()

    window.note_edit.setPlainText("  오른쪽 프로브 밀착이 어려웠음  ")
    window.note_button.click()

    assert saved(results) == ["20261126_211205_12345.bin", "20261126_211205_12345.jpg", "20261126_211205_12345.json"]
    assert note_of(results, "20261126_211205_12345") == "오른쪽 프로브 밀착이 어려웠음"
    assert (results / "20261126_211205_12345.jpg").read_bytes() != before
    assert not window.note_button.isEnabled()
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."


def test_a_memo_saved_while_the_next_patient_is_chosen_stays_with_the_shown_result(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.set_patient(LEE)

    window.note_edit.setPlainText("김하나 환자 메모")
    window.note_button.click()

    assert note_of(results, "20261126_211205_12345") == "김하나 환자 메모"
    assert "다음 검사는 이두리 (67890) 환자로" in window.state_label.text()

    window.handle_event(LEFT_ONLY)
    assert note_of(results, "20261126_211205_67890") == ""
    assert window.note_edit.toPlainText() == ""


def test_a_memo_left_unsaved_is_kept_with_its_result_when_the_next_one_arrives(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("저장을 누르지 않은 메모")
    window.set_patient(LEE)

    window.handle_event(LEFT_ONLY)

    assert note_of(results, "20261126_211205_12345") == "저장을 누르지 않은 메모"
    assert note_of(results, "20261126_211205_67890") == ""
    assert window.state_label.text() == "✔ 이두리 (67890) 환자 결과로 저장했습니다."


def test_a_memo_too_long_for_the_sheet_is_pointed_out(qapp, results):
    window = open_window(qapp, results)
    window.handle_event(BOTH)

    window.note_edit.setPlainText("짧은 메모")
    assert window.note_hint.text() == ""
    window.note_edit.setPlainText("\n".join(f"{line}번째 줄" for line in range(1, 8)))
    assert "앞부분만 인쇄됩니다" in window.note_hint.text()


def test_a_memo_added_after_sending_is_not_sent_again_by_itself(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    assert len(sender.sent) == 1

    window.note_edit.setPlainText("보낸 뒤에 쓴 메모")
    window.note_button.click()

    assert len(sender.sent) == 1
    assert window.capture_label.text().startswith("⚠ 보낸 뒤에 결과지를 고쳤습니다")
    assert window.send_button.isEnabled()
    history = json.loads((results / "20261126_211205_12345.json").read_text(encoding="utf-8"))["capture"]
    assert history[-1]["code"] == "edited"

    window.send_button.click()
    assert len(sender.sent) == 2
    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")


def test_a_memo_written_before_sending_goes_out_with_the_sheet(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=Settings(cap_url="192.0.2.1", cap_key="key"), send=sender)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.note_edit.setPlainText("보내기 전에 쓴 메모")
    window.note_button.click()
    assert sender.sent == []

    window.send_button.click()
    assert len(sender.sent) == 1
    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")


def test_an_earlier_result_can_be_opened_and_its_memo_corrected(qapp, results):
    first = open_window(qapp, results)
    first.set_patient(KIM)
    first.handle_event(BOTH)
    first.note_edit.setPlainText("처음 메모")
    first.note_button.click()

    window = open_window(qapp, results)
    window.open_result(results / "20261126_211205_12345.json")

    assert window.patient() == KIM
    assert window.note_edit.toPlainText() == "처음 메모"
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."
    assert not window.preview.pixmap().isNull()

    window.note_edit.setPlainText("고친 메모")
    window.note_button.click()

    assert saved(results) == ["20261126_211205_12345.bin", "20261126_211205_12345.jpg", "20261126_211205_12345.json"]
    assert note_of(results, "20261126_211205_12345") == "고친 메모"
    assert json.loads((results / "20261126_211205_12345.json").read_text(encoding="utf-8"))["received_at"] \
        == "2026-11-26T21:12:05"


def test_an_earlier_result_opens_with_what_is_known_about_its_sending(qapp, results):
    from functools import partial

    from tsm300.records import send_sheet

    def receiver(*_, **__):
        return {"ok": True, "duplicate": False, "id": "1", "chartId": "66001",
                "imageDataName": "66001_20261126_중이검사.jpg"}

    first = open_window(qapp, results, settings=SENDING, send=partial(send_sheet, send=receiver))
    first.set_patient(KIM)
    first.handle_event(BOTH)

    window = open_window(qapp, results, settings=SENDING, send=FakeSender())
    window.open_result(results / "20261126_211205_12345.jpg")

    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")
    assert not window.send_button.isEnabled()


def test_opening_a_result_does_not_carry_its_patient_onto_the_next_test(qapp, results):
    first = open_window(qapp, results)
    first.set_patient(KIM)
    first.handle_event(BOTH)

    window = open_window(qapp, results)
    window.open_result(results / "20261126_211205_12345.json")
    window.handle_event(LEFT_ONLY)

    assert "앞 검사에 쓴 정보입니다" in window.state_label.text()
    assert "20261126_211205.json" in saved(results)


def test_a_result_that_cannot_be_read_is_reported(qapp, results, tmp_path):
    window = open_window(qapp, results)
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")

    window.open_result(broken)
    assert window.problem_label.text().startswith("⚠ 결과 파일을 읽지 못했습니다")

    window.open_result(tmp_path / "missing.json")
    assert window.problem_label.text() == "⚠ 결과 파일이 없습니다: missing.json"


# ----- updating ----------------------------------------------------------------------------

def open_update_dialog(updates):
    dialog = UpdateDialog(None, updates, run_now)
    _OPENED.append(dialog)
    return dialog


def test_update_dialog_says_when_nothing_newer_is_out(qapp):
    dialog = open_update_dialog(FakeUpdates(version=__version__))

    assert dialog.message.text() == f"지금 쓰는 v{__version__} 이 최신 버전입니다."
    assert dialog.install_button.isHidden() and dialog.restart_button.isHidden()


def test_update_dialog_installs_a_newer_version_and_offers_to_start_it(qapp):
    updates = FakeUpdates()
    dialog = open_update_dialog(updates)
    assert dialog.message.text().startswith(f"새 버전 v99.0.0 이 있습니다. (지금 v{__version__})")
    assert not dialog.install_button.isHidden() and dialog.restart_button.isHidden()

    dialog.install_button.click()

    assert updates.installed == [("99.0.0", Path("TSM300.exe"))]
    assert dialog.message.text().startswith("✔ v99.0.0 을 받았습니다.")
    assert dialog.install_button.isHidden() and not dialog.restart_button.isHidden()
    assert dialog.close_button.text() == "나중에"

    dialog.restart_button.click()

    assert dialog.result() == UpdateDialog.DialogCode.Accepted


def test_update_dialog_says_what_went_wrong_and_lets_the_download_be_tried_again(qapp):
    unreachable = open_update_dialog(FakeUpdates(unreachable=True))
    assert unreachable.message.text() == "⚠ 새 버전이 있는지 확인하지 못했습니다."
    assert unreachable.install_button.isHidden()

    updates = FakeUpdates(download_fails=True)
    dialog = open_update_dialog(updates)
    dialog.install_button.click()

    assert dialog.message.text() == "⚠ 내려받기가 중간에 끊겼습니다."
    assert dialog.install_button.isEnabled() and dialog.close_button.isEnabled() and dialog.restart_button.isHidden()

    updates.download_fails = False
    dialog.install_button.click()

    assert updates.installed == [("99.0.0", Path("TSM300.exe"))]


def test_update_dialog_gives_a_python_install_the_command_instead(qapp):
    dialog = open_update_dialog(FakeUpdates(program=None))

    assert "새 버전 v99.0.0 이 있습니다." in dialog.message.text()
    assert FakeUpdates.PYTHON_COMMAND in dialog.message.text()
    assert dialog.install_button.isHidden()


def test_starting_the_new_version_keeps_a_memo_being_typed_and_closes_this_one(qapp, results):
    updates = FakeUpdates()
    window = open_window(qapp, results, updates=updates)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("저장을 누르지 않은 메모")

    window.restart_updated()

    assert updates.restarted == [(Path("TSM300.exe"), sys.argv[1:])]
    assert note_of(results, "20261126_211205_12345") == "저장을 누르지 않은 메모"
    assert not window.isVisible()


# ----- the clinic on the sheet ---------------------------------------------------------------

BARUN = Settings(clinic_name="바른이비인후과의원", clinic_address="부산시 해운대구 가나로 12", clinic_phone="051-123-4567")


def record_of(results, stem):
    return json.loads((results / f"{stem}.json").read_text(encoding="utf-8"))


def test_each_result_is_saved_with_the_clinic_in_the_settings(qapp, results):
    window = open_window(qapp, results, settings=BARUN)
    assert window.clinic_hint.isHidden()

    window.handle_event(BOTH)

    assert record_of(results, "20261126_211205")["clinic"] == {
        "name": "바른이비인후과의원", "address": "부산시 해운대구 가나로 12", "phone": "051-123-4567"}


def test_a_window_without_a_clinic_name_asks_for_one_until_it_is_set(qapp, results):
    window = open_window(qapp, results)
    assert not window.clinic_hint.isHidden()

    window.apply_settings(BARUN)

    assert window.clinic_hint.isHidden()


def test_changing_the_clinic_redraws_the_shown_sheet_with_what_was_being_typed(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("쓰던 메모")
    before = (results / "20261126_211205_12345.jpg").read_bytes()

    window.apply_settings(BARUN)

    record = record_of(results, "20261126_211205_12345")
    assert (record["clinic"]["name"], record["note"]) == ("바른이비인후과의원", "쓰던 메모")
    assert (results / "20261126_211205_12345.jpg").read_bytes() != before
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."


def test_changing_the_clinic_leaves_a_sheet_that_was_already_sent(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    before = (results / "20261126_211205_12345.jpg").read_bytes()

    window.apply_settings(Settings(cap_on=True, cap_url="192.0.2.1", cap_key="key", clinic_name="바른이비인후과의원"))

    assert (results / "20261126_211205_12345.jpg").read_bytes() == before
    assert len(sender.sent) == 1
    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")


# ----- the test time -------------------------------------------------------------------------

def test_test_time_can_be_corrected_and_is_what_is_sent(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=Settings(cap_url="192.0.2.1", cap_key="key"), send=sender)
    assert not window.tested_edit.isEnabled()
    window.set_patient(KIM)
    window.handle_event(BOTH)
    assert window.tested_edit.isEnabled() and not window.note_button.isEnabled()
    assert window.tested_edit.dateTime().toPython() == datetime(2026, 11, 26, 21, 12)

    window.tested_edit.setDateTime(QDateTime(datetime(2026, 11, 25, 14, 30)))
    assert window.note_button.isEnabled()
    window.note_button.click()

    record = record_of(results, "20261126_211205_12345")
    assert (record["received_at"], record["tested_at"]) == ("2026-11-26T21:12:05", "2026-11-25T14:30:00")
    assert not window.note_button.isEnabled()

    window.send_button.click()

    assert sender.sent == [("20261126_211205_12345.jpg", KIM, date(2026, 11, 25))]


def test_a_test_time_changed_but_not_saved_is_kept_when_the_next_result_arrives(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.tested_edit.setDateTime(QDateTime(datetime(2026, 11, 20, 9, 5)))
    window.set_patient(LEE)

    window.handle_event(LEFT_ONLY)

    assert record_of(results, "20261126_211205_12345")["tested_at"] == "2026-11-20T09:05:00"
    assert record_of(results, "20261126_211205_67890")["tested_at"] == "2026-11-26T21:12:00"
    assert window.tested_edit.dateTime().toPython() == datetime(2026, 11, 26, 21, 12)


def test_an_earlier_result_opens_with_its_test_time_and_tomorrow_cannot_be_chosen(qapp, results):
    first = open_window(qapp, results)
    first.set_patient(KIM)
    first.handle_event(BOTH)
    first.tested_edit.setDateTime(QDateTime(datetime(2026, 11, 20, 9, 5)))
    first.note_button.click()

    window = open_window(qapp, results)
    window.open_result(results / "20261126_211205_12345.json")

    assert window.tested_edit.dateTime().toPython() == datetime(2026, 11, 20, 9, 5)
    assert not window.note_button.isEnabled()
    window.tested_edit.setDateTime(QDateTime(datetime(2026, 11, 27, 9, 0)))
    assert window.tested_edit.dateTime().toPython() == datetime(2026, 11, 26, 23, 59)


# ----- when the files cannot be written --------------------------------------------------------

def test_a_result_that_cannot_be_saved_is_kept_aside_and_saved_when_sent_again(qapp, results):
    results.write_text("이 이름은 폴더가 아니라 파일입니다", encoding="utf-8")
    window = open_window(qapp, results)
    window.set_patient(KIM)

    window.handle_event(BOTH)

    assert window.problem_label.text().startswith("⚠ 방금 받은 결과를 저장하지 못했습니다")
    assert "장비에서 → 키를 다시 눌러 주세요" in window.problem_label.text()
    assert window.state_label.text().startswith("다음 검사는 김하나 (12345) 환자로 저장됩니다.")   # still said, beside it
    kept = list((results.parent / "settings" / "unsaved").glob("*.bin"))
    assert [path.read_bytes() for path in kept] == [BOTH.raw]
    assert not window.note_edit.isEnabled() and not window.print_button.isEnabled()
    window.name_edit.setText("김하나")      # the warning stays while the operator does other things
    assert window.problem_label.text().startswith("⚠ 방금 받은 결과를 저장하지 못했습니다")

    results.unlink()
    window.handle_event(BOTH)

    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."
    assert window.problem_label.isHidden()
    assert saved(results) == ["20261126_211205_12345.bin", "20261126_211205_12345.jpg", "20261126_211205_12345.json"]


def test_a_correction_that_cannot_be_saved_leaves_the_sheet_and_says_so(qapp, results, monkeypatch):
    import tsm300.app as app

    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    before = (results / "20261126_211205_12345.jpg").read_bytes()
    really_save = app.save_result

    def held_open(*arguments, **options):
        raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")

    monkeypatch.setattr(app, "save_result", held_open)
    window.note_edit.setPlainText("메모")
    window.note_button.click()

    assert window.problem_label.text().startswith("⚠ 고친 내용을 저장하지 못했습니다")
    assert (results / "20261126_211205_12345.jpg").read_bytes() == before
    assert note_of(results, "20261126_211205_12345") == ""
    assert window.note_button.isEnabled() and window.note_edit.toPlainText() == "메모"

    monkeypatch.setattr(app, "save_result", really_save)
    window.note_button.click()

    assert note_of(results, "20261126_211205_12345") == "메모"
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다." and window.problem_label.isHidden()


def test_a_sheet_that_cannot_be_drawn_is_reported_not_left_to_end_the_program(qapp, results, monkeypatch):
    import tsm300.app as app

    def broken(*arguments, **options):
        raise RuntimeError("그릴 수 없는 값")

    monkeypatch.setattr(app, "save_result", broken)
    window = open_window(qapp, results)

    window.handle_event(BOTH)

    assert "그릴 수 없는 값" in window.problem_label.text()
    assert "RuntimeError" in (results.parent / "settings" / "error.log").read_text(encoding="utf-8")


# ----- printing --------------------------------------------------------------------------------

def test_the_sheet_is_drawn_on_a_page_for_the_printer(qapp, results, tmp_path):
    from PySide6.QtPrintSupport import QPrinter

    window = open_window(qapp, results)
    assert not window.print_button.isEnabled()
    window.handle_event(BOTH)
    assert window.print_button.isEnabled()
    page = tmp_path / "page.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(page))

    paint_sheet(results / "20261126_211205.jpg", printer)

    assert page.read_bytes()[:4] == b"%PDF" and page.stat().st_size > 50_000
    with pytest.raises(OSError, match="읽지 못했습니다"):
        paint_sheet(results / "없는 결과지.jpg", printer)


# ----- the device's port -------------------------------------------------------------------------

class FakePort:
    """A port as the PC lists it. A USB adapter has a maker, a model and, unless it is a cheap one, a serial number."""

    def __init__(self, device, kind="usb", serial="", model=0x6001):
        self.device = device
        self.description = {"usb": "USB Serial Port", "own": "통신 포트", "bluetooth": "Bluetooth 링크"}[kind]
        self.hwid = {"usb": f"USB VID:PID=0403:{model:04X} SER={serial}", "own": "ACPI\\PNP0501\\1",
                     "bluetooth": "BTHENUM\\{00001101-0000-1000-8000-00805F9B34FB}"}[kind]
        self.vid, self.pid = (0x0403, model) if kind == "usb" else (None, None)
        self.serial_number = serial or None


TSM_CABLE = "0403:6001:TSM"


class FakeReader(QObject):
    """Stands in for the thread that reads a port. Like the real one it ends a moment after it is told to."""

    status = Signal(str)
    received = Signal(object)
    finished = Signal()
    made: list = []

    def __init__(self, port, baud):
        super().__init__()
        self.port, self.reading, self.told_to_end, self.waited_for = port, False, False, []
        FakeReader.made.append(self)

    def start(self):
        self.reading = True
        self.status.emit(f"{self.port} 연결됨")

    def requestInterruption(self):
        self.told_to_end = True

    def isFinished(self):
        return not self.reading

    def end(self):
        """The port has closed and the thread has returned."""
        self.reading = False
        self.finished.emit()

    def wait(self, milliseconds):
        self.waited_for.append(milliseconds)
        self.end()
        return True

    def terminate(self):
        self.end()


@pytest.fixture
def plugged():
    """The ports the PC has, which a test changes as cables come and go, and a device looking at them."""
    FakeReader.made = []
    ports: list = []
    device = Device(9600, ports=lambda: list(ports), reader=FakeReader, background=run_now)
    said: list = []
    device.status.connect(said.append)
    yield ports, device, said
    device.stop()


def test_a_port_the_operator_chose_is_used_or_nothing_is():
    chosen, other, own = FakePort("COM5"), FakePort("COM6"), FakePort("COM1", "own")

    assert pick_port("COM5", [own, chosen, other]) == "COM5"
    assert pick_port("COM1", [own, chosen]) == "COM1"
    assert pick_port("COM5", [own, other]) is None                      # another adapter may be another instrument's
    assert pick_port("COM5", [own, other], current="COM6", heard=True, remembered=("COM6", "")) is None


def test_left_to_the_program_with_nothing_remembered_a_single_usb_adapter_is_taken_to_be_the_device():
    adapter, other, own, radio = FakePort("COM5"), FakePort("COM7"), FakePort("COM1", "own"), FakePort("COM4", "bluetooth")

    assert pick_port("", [own, adapter]) == "COM5"
    assert pick_port("", [own, adapter, other]) is None                 # which of two? neither is opened
    assert pick_port("", [own]) == "COM1"                               # a desktop PC's own port, with no adapter
    assert pick_port("", [radio]) is None                               # never a Bluetooth port
    assert pick_port("", [radio, own]) == "COM1"
    assert pick_port("", []) is None


def test_the_adapter_the_device_was_heard_on_is_found_wherever_it_is_plugged_in_and_no_other_is_opened_for_it():
    cable, moved = FakePort("COM5", serial="TSM"), FakePort("COM9", serial="TSM")
    audiometer, own = FakePort("COM7", serial="AUDIO"), FakePort("COM1", "own")
    heard_on = ("COM5", TSM_CABLE)

    assert pick_port("", [cable, audiometer], remembered=heard_on) == "COM5"
    assert pick_port("", [audiometer, moved], remembered=heard_on) == "COM9"       # another socket, another name: followed
    assert pick_port("", [audiometer], remembered=heard_on) is None                # unplugged: the audiometer's is left alone
    assert pick_port("", [own], remembered=heard_on) is None                       # and so is a port of the PC's own
    assert pick_port("", [own, audiometer], remembered=("COM1", "")) == "COM1"     # heard on the PC's own port before


def test_adapters_without_serial_numbers_are_told_apart_by_the_port_name_or_not_at_all():
    one, twin, renamed = FakePort("COM5"), FakePort("COM7"), FakePort("COM8")
    heard_on = ("COM5", "0403:6001:")

    assert pick_port("", [one, twin], remembered=heard_on) == "COM5"
    assert pick_port("", [twin], remembered=heard_on) == "COM7"                    # the only one of its kind: the same cable moved
    assert pick_port("", [twin, renamed], remembered=heard_on) is None             # two of a kind, neither with that name


def test_a_port_remembered_by_an_earlier_version_is_a_hint_and_not_a_wall():
    cable, moved, other = FakePort("COM5"), FakePort("COM6"), FakePort("COM7")
    earlier = ("COM5", "")                                                         # a name only; no adapter was noted then

    assert pick_port("", [cable, other], remembered=earlier) == "COM5"
    assert pick_port("", [moved], remembered=earlier) == "COM6"
    assert pick_port("", [moved, other], remembered=earlier) is None


def test_a_connection_is_kept_once_the_device_has_been_heard_and_not_merely_because_it_was_opened():
    first, later, own = FakePort("COM3"), FakePort("COM2"), FakePort("COM1", "own")

    assert pick_port("", [later, first], current="COM3", heard=True) == "COM3"      # proven: nothing takes it away
    assert pick_port("", [later, first], current="COM3") is None                    # unproven, and now there are two
    assert pick_port("", [later], current="COM3") == "COM2"                         # the one in use has gone
    assert pick_port("", [own, first], current="COM1") == "COM3"                    # an adapter replaces the PC's own port
    assert pick_port("", [own, first], current="COM1", heard=True) == "COM1"        # but not once the device was heard there


def test_ports_are_listed_in_the_order_a_person_counts_them(monkeypatch):
    from tsm300 import console

    monkeypatch.setattr(console.list_ports, "comports", lambda: [FakePort("COM10"), FakePort("COM3"), FakePort("COM1")])

    assert [port.device for port in console.available_ports()] == ["COM1", "COM3", "COM10"]


def test_a_cable_plugged_in_after_the_program_started_is_found(qapp, plugged):
    ports, device, said = plugged

    device.use("")
    assert said == [NO_PORT] and FakeReader.made == []

    device.look()
    assert said == [NO_PORT]                                   # nothing new is said while nothing changes

    ports.append(FakePort("COM5"))
    device.look()

    assert said == [NO_PORT, "COM5 연결됨"]
    assert [(reader.port, reader.reading) for reader in FakeReader.made] == [("COM5", True)]


def test_another_adapter_plugged_in_later_does_not_take_the_connection_once_the_device_was_heard(qapp, plugged):
    from tsm300.protocol import PowerOn

    ports, device, said = plugged
    ports.append(FakePort("COM3", serial="TSM"))
    device.use("")
    FakeReader.made[0].received.emit(PowerOn("TSM-300 SCREENING", "6349"))

    ports.insert(0, FakePort("COM2", serial="AUDIO"))
    device.look()

    assert [(reader.port, reader.told_to_end) for reader in FakeReader.made] == [("COM3", False)]
    assert said == ["COM3 연결됨"]


def test_an_adapter_the_device_was_never_heard_on_is_let_go_when_a_second_one_appears(qapp, plugged):
    ports, device, said = plugged
    ports.append(FakePort("COM3"))
    device.use("")                                                   # the only adapter: taken to be the device's

    ports.append(FakePort("COM4"))
    device.look()

    assert FakeReader.made[0].told_to_end                            # it may be the other instrument's after all
    assert said[-1].startswith("USB-시리얼 포트(COM3, COM4) 중 어느 것이 장비인지 알 수 없어 열지 않았습니다.")


def test_while_the_devices_own_cable_is_away_nothing_is_opened_and_the_operator_is_told_so(qapp):
    FakeReader.made = []
    ports = [FakePort("COM7", serial="AUDIO"), FakePort("COM1", "own")]
    device = Device(9600, ports=lambda: list(ports), reader=FakeReader, remembered=("COM5", TSM_CABLE),
                    background=run_now)
    said = []
    device.status.connect(said.append)

    device.use("")

    assert FakeReader.made == []
    assert said == ["장비의 USB-시리얼 케이블이 연결되어 있지 않습니다. 꽂으면 자동으로 연결합니다. "
                    "케이블을 다른 것으로 바꿨다면 [설정] → 장비 · 저장에서 포트를 골라 주세요."]

    ports.append(FakePort("COM9", serial="TSM"))                     # plugged back, into another socket
    device.look()

    assert [reader.port for reader in FakeReader.made] == ["COM9"]
    device.stop()


def test_with_several_adapters_and_no_memory_of_the_device_none_is_opened_and_the_operator_is_asked(qapp, plugged):
    ports, device, said = plugged
    ports.extend([FakePort("COM4"), FakePort("COM7")])

    device.use("")

    assert FakeReader.made == []
    assert said == ["USB-시리얼 포트(COM4, COM7) 중 어느 것이 장비인지 알 수 없어 열지 않았습니다. "
                    "[설정] → 장비 · 저장에서 장비가 연결된 포트를 골라 주세요."]

    device.use("COM7")

    assert [(reader.port, reader.reading) for reader in FakeReader.made] == [("COM7", True)]


def test_the_port_the_device_is_heard_on_is_announced_once_and_found_again_among_several(qapp):
    from tsm300.protocol import PowerOn

    FakeReader.made = []
    ports = [FakePort("COM7", serial="TSM")]
    device = Device(9600, ports=lambda: list(ports), reader=FakeReader, background=run_now)
    heard, events = [], []
    device.heard_on.connect(lambda port, adapter: heard.append((port, adapter)))
    device.received.connect(events.append)
    device.use("")

    FakeReader.made[0].received.emit(PowerOn("TSM-300 SCREENING", "6349"))
    FakeReader.made[0].received.emit(BOTH)

    assert heard == [("COM7", TSM_CABLE)] and len(events) == 2
    device.stop()

    FakeReader.made = []
    ports[:] = [FakePort("COM4", serial="AUDIO"), FakePort("COM9", serial="TSM")]   # next day: another socket, and an audiometer
    again = Device(9600, ports=lambda: list(ports), reader=FakeReader, remembered=heard[0], background=run_now)
    again.use("")

    assert [reader.port for reader in FakeReader.made] == ["COM9"]
    again.stop()


def test_the_look_repeated_every_few_seconds_lists_the_ports_away_from_the_window(qapp):
    FakeReader.made = []
    ports, listings = [], []

    def later(owner, work, done):
        listings.append((work, done))

    device = Device(9600, ports=lambda: list(ports), reader=FakeReader, background=later)
    device.use("")
    ports.append(FakePort("COM5"))

    device.look_later()
    device.look_later()                                              # one listing at a time
    assert len(listings) == 1 and FakeReader.made == []

    work, done = listings.pop()
    done(work(), None)

    assert [reader.port for reader in FakeReader.made] == ["COM5"]
    device.stop()


def test_the_pcs_own_port_gives_way_to_an_adapter_only_until_the_device_is_heard_on_it(qapp, plugged):
    from tsm300.protocol import PowerOn

    ports, device, said = plugged
    ports.append(FakePort("COM1", "own"))
    device.use("")

    FakeReader.made[0].received.emit(PowerOn("TSM-300 SCREENING", "6349"))
    ports.append(FakePort("COM7"))
    device.look()

    assert [(reader.port, reader.told_to_end) for reader in FakeReader.made] == [("COM1", False)]


def test_choosing_another_port_moves_on_without_waiting_for_the_first_to_close(qapp, plugged):
    ports, device, said = plugged
    ports.extend([FakePort("COM5"), FakePort("COM7")])
    device.use("COM5")
    first = FakeReader.made[0]

    device.use("COM7")

    assert (first.told_to_end, first.waited_for) == (True, [])       # the window is not held up
    assert [(reader.port, reader.reading) for reader in FakeReader.made] == [("COM5", True), ("COM7", True)]
    assert said == ["COM5 연결됨", "COM7 연결됨"]
    first.status.emit("COM5 연결이 끊겼습니다")                        # a reader on its way out is no longer heard
    assert said == ["COM5 연결됨", "COM7 연결됨"]

    device.stop()                                                    # closing the program does wait for them

    assert first.waited_for == [3000] and not first.reading
    assert all(reader.told_to_end and not reader.reading for reader in FakeReader.made)


def test_a_chosen_port_that_is_unplugged_is_said_to_be_missing_and_read_again_when_back(qapp, plugged):
    ports, device, said = plugged
    ports.extend([FakePort("COM1", "own"), FakePort("COM6")])
    device.use("COM5")
    assert said == ["COM5 포트가 보이지 않습니다. 케이블을 꽂으면 자동으로 연결합니다."] and FakeReader.made == []

    ports.append(FakePort("COM5"))
    device.look()

    assert said[-1] == "COM5 연결됨"


def test_what_is_missing_is_said_again_when_the_choice_changes(qapp, plugged):
    ports, device, said = plugged

    device.use("")
    device.use("COM7")
    device.use("")

    assert said == [NO_PORT, "COM7 포트가 보이지 않습니다. 케이블을 꽂으면 자동으로 연결합니다.", NO_PORT]


def test_a_port_named_on_starting_is_opened_even_when_the_pc_does_not_list_it(qapp, plugged):
    ports, device, said = plugged
    ports.append(FakePort("COM5"))

    device.use("/dev/pts/7", exactly=True)

    assert [(reader.port, reader.reading) for reader in FakeReader.made] == [("/dev/pts/7", True)]


def test_window_announces_a_port_chosen_in_the_settings(qapp, results):
    window = open_window(qapp, results, settings=Settings(port="COM5"))
    wanted = []
    window.port_wanted.connect(wanted.append)

    window.apply_settings(Settings(port="COM5", clinic_name="바른이비인후과의원"))
    window.apply_settings(Settings(port="COM7"))
    window.apply_settings(Settings(port=""))

    assert wanted == ["COM7", ""]


# ----- the rest of the settings --------------------------------------------------------------------

def test_settings_dialog_takes_the_clinic_the_port_and_the_folder(qapp, tmp_path):
    ports = [FakePort("COM3"), FakePort("COM5")]
    dialog = SettingsDialog(None, Settings(port="COM9"), run_now, ports=lambda: list(ports))
    _OPENED.append(dialog)
    listed = [dialog.port_combo.itemText(index) for index in range(dialog.port_combo.count())]
    assert listed == ["자동 (USB-시리얼 포트를 찾아 연결)", "COM3 - USB Serial Port", "COM5 - USB Serial Port",
                      "COM9 - 지금은 보이지 않음"]
    assert dialog.port_combo.currentData() == "COM9"

    ports.append(FakePort("COM9"))
    dialog.rescan_button.click()
    assert dialog.port_combo.currentText() == "COM9 - USB Serial Port"

    dialog.port_combo.setCurrentIndex(dialog.port_combo.findData("COM5"))
    dialog.clinic_name_edit.setText(" 바른이비인후과의원 ")
    dialog.clinic_address_edit.setText("부산시 해운대구 가나로 12")
    dialog.clinic_phone_edit.setText("051-123-4567")
    dialog.folder_edit.setText(str(tmp_path / "검사 결과"))

    assert dialog.settings() == Settings(port="COM5", clinic_name="바른이비인후과의원",
                                         clinic_address="부산시 해운대구 가나로 12", clinic_phone="051-123-4567",
                                         results_dir=str(tmp_path / "검사 결과"))
    dialog.accept()
    assert dialog.result() == SettingsDialog.DialogCode.Accepted and (tmp_path / "검사 결과").is_dir()


def test_settings_dialog_does_not_close_on_a_folder_that_cannot_be_written(qapp, tmp_path):
    blocked = tmp_path / "파일.txt"
    blocked.write_text("폴더가 아님", encoding="utf-8")
    dialog = SettingsDialog(None, Settings(), run_now, ports=lambda: [])
    _OPENED.append(dialog)
    dialog.folder_edit.setText(str(blocked / "결과"))

    dialog.accept()

    assert dialog.result() != SettingsDialog.DialogCode.Accepted
    assert dialog.problem_label.text().startswith("⚠ 저장 폴더에 쓸 수 없습니다")
    assert dialog.tabs.currentIndex() == 1


def test_results_go_to_the_folder_in_the_settings_unless_one_was_named_on_starting(qapp, tmp_path):
    chosen = tmp_path / "설정에서 고른 폴더"
    assert results_folder(None, Settings(results_dir=str(chosen))) == (chosen, True)
    assert results_folder("named", Settings(results_dir=str(chosen))) == (Path("named"), False)
    assert results_folder(None, Settings()) == (Path("results"), True)

    following = open_window(qapp, tmp_path / "처음 폴더", directory_follows_settings=True)
    following.apply_settings(Settings(results_dir=str(chosen)))
    following.handle_event(BOTH)
    assert saved(chosen) == ["20261126_211205.bin", "20261126_211205.jpg", "20261126_211205.json"]

    fixed = open_window(qapp, tmp_path / "명령으로 정한 폴더")
    fixed.apply_settings(Settings(results_dir=str(chosen)))
    fixed.handle_event(LEFT_ONLY)
    assert len(saved(tmp_path / "명령으로 정한 폴더")) == 3


# ----- one window at a time ------------------------------------------------------------------------

def test_a_second_window_is_refused_while_the_first_is_open(qapp, tmp_path, monkeypatch):
    import argparse

    import tsm300.app as app

    first = only_instance(tmp_path, 50)
    assert first is not None and only_instance(tmp_path, 50) is None
    told = []
    monkeypatch.setenv("TSM300_DIR", str(tmp_path))
    monkeypatch.setattr(app, "INSTANCE_WAIT_MS", 50)
    monkeypatch.setattr(app.QMessageBox, "information", lambda *arguments: told.append(arguments[2]))

    assert app.run(argparse.Namespace(out=None, port=None, baud=9600)) == 1
    assert told == [app.ALREADY_RUNNING]

    first.unlock()
    again = only_instance(tmp_path, 50)
    assert again is not None
    again.unlock()


def test_closing_the_window_keeps_a_memo_that_was_not_saved_yet(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("닫기 전에 쓰던 메모")

    window.close()

    assert note_of(results, "20261126_211205_12345") == "닫기 전에 쓰던 메모"


def test_the_mouse_wheel_does_not_change_the_test_time_or_the_sex(qapp, results):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    for box in (window.tested_edit, window.sex_combo):
        box.setFocus()
        centre = QPointF(box.rect().center())
        wheel = QWheelEvent(centre, box.mapToGlobal(centre), QPoint(0, 0), QPoint(0, 240), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(box, wheel)

    assert window.tested_edit.dateTime().toPython() == datetime(2026, 11, 26, 21, 12)
    assert window.sex_combo.currentText() == "여"
    assert not window.note_button.isEnabled()


# ----- corrections are never dropped quietly --------------------------------------------------------

def test_a_corrected_test_time_goes_with_a_patient_put_on_the_result(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender)
    window.handle_event(BOTH)
    window.tested_edit.setDateTime(QDateTime(datetime(2026, 11, 25, 10, 0)))
    window.note_edit.setPlainText("전날 검사")
    window.set_patient(KIM)

    window.save_button.click()

    record = record_of(results, "20261126_211205_12345")
    assert (record["tested_at"], record["note"]) == ("2026-11-25T10:00:00", "전날 검사")
    assert sender.sent == [("20261126_211205_12345.jpg", KIM, date(2026, 11, 25))]
    assert not window.note_button.isEnabled()


def test_sending_takes_the_corrections_being_typed_with_it(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=Settings(cap_url="192.0.2.1", cap_key="key"), send=sender)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("보내기 직전에 쓴 메모")

    window.send_button.click()

    assert note_of(results, "20261126_211205_12345") == "보내기 직전에 쓴 메모"
    assert len(sender.sent) == 1
    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")


def test_corrections_that_could_not_be_saved_are_kept_and_put_back_when_their_sheet_is_reopened(qapp, results, monkeypatch):
    import tsm300.app as app

    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("놓치면 안 되는 메모")
    window.tested_edit.setDateTime(QDateTime(datetime(2026, 11, 25, 9, 0)))
    window.clear_patient()
    really_save = app.save_result

    def held_open(*arguments, replacing=None, **options):
        if replacing is not None:
            raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")
        return really_save(*arguments, **options)

    monkeypatch.setattr(app, "save_result", held_open)
    window.handle_event(LEFT_ONLY)

    said = window.problem_label.text()
    assert said.startswith("⚠ 앞 결과지(20261126_211205_12345.jpg)에 고치던 내용을 저장하지 못했습니다")
    assert "놓치면 안 되는 메모" in said and "2026-11-25 09:00" in said
    assert window.state_label.text().startswith("⚠ 환자 정보 없이 저장했습니다")     # the new result's own warning is not hidden
    assert saved(results)[0] == "20261126_211205.bin"                              # and it is saved all the same

    window.set_patient(LEE)
    window.save_button.click()                                                     # other work does not wipe what was lost
    assert "놓치면 안 되는 메모" in window.problem_label.text()

    monkeypatch.setattr(app, "save_result", really_save)
    window.open_result(results / "20261126_211205_12345.json")

    assert window.note_edit.toPlainText() == "놓치면 안 되는 메모"
    assert window.tested_edit.dateTime().toPython() == datetime(2026, 11, 25, 9, 0)
    assert window.note_button.isEnabled() and "다시 채웠습니다" in window.problem_label.text()

    window.note_button.click()

    record = record_of(results, "20261126_211205_12345")
    assert (record["note"], record["tested_at"]) == ("놓치면 안 되는 메모", "2026-11-25T09:00:00")
    assert window.problem_label.isHidden()


def test_the_window_stays_open_when_corrections_cannot_be_saved_unless_the_operator_says_so(qapp, results, monkeypatch):
    import tsm300.app as app

    answers, asked = [False, True], []

    def answer(question):
        asked.append(question)
        return answers.pop(0)

    window = open_window(qapp, results, confirm=answer)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("메모")

    def held_open(*arguments, **options):
        raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")

    monkeypatch.setattr(app, "save_result", held_open)

    window.close()
    assert window.isVisible() and "저장하지 않고 프로그램을 닫을까요?" in asked[0]

    window.close()
    assert not window.isVisible() and len(asked) == 2


def test_opening_the_result_already_on_screen_keeps_what_was_being_typed(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("쓰던 메모")

    window.open_result(results / "20261126_211205_12345.json")

    assert window.note_edit.toPlainText() == "쓰던 메모" and not window.note_button.isEnabled()
    assert note_of(results, "20261126_211205_12345") == "쓰던 메모"


def test_the_sheet_on_screen_when_printing_was_asked_for_is_the_one_printed(qapp, results, monkeypatch):
    import tsm300.app as app

    printed = []
    monkeypatch.setattr(app, "paint_sheet", lambda sheet, printer: printed.append((sheet.name, printer.docName())))
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("인쇄 전에 쓴 메모")

    def another_result_arrives_while_choosing(printer):
        window.set_patient(LEE)
        window.handle_event(LEFT_ONLY)
        return True

    monkeypatch.setattr(window, "_choose_printer", another_result_arrives_while_choosing)
    window.print_button.click()

    assert printed == [("20261126_211205_12345.jpg", "20261126_211205_12345")]
    assert note_of(results, "20261126_211205_12345") == "인쇄 전에 쓴 메모"


def test_a_sheet_corrected_twice_after_sending_is_still_not_sent_again_by_itself(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    assert len(sender.sent) == 1

    for memo in ("첫 번째 고침", "두 번째 고침"):
        window.note_edit.setPlainText(memo)
        window.note_button.click()

    assert note_of(results, "20261126_211205_12345") == "두 번째 고침"
    assert len(sender.sent) == 1
    assert window.capture_label.text().startswith("⚠ 보낸 뒤에 결과지를 고쳤습니다")

    window.send_button.click()                                       # the operator can still send it on purpose

    assert len(sender.sent) == 2


def test_a_warning_about_corrections_goes_when_they_are_taken_back(qapp, results, monkeypatch):
    import tsm300.app as app

    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    def held_open(*arguments, **options):
        raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")

    monkeypatch.setattr(app, "save_result", held_open)
    window.note_edit.setPlainText("메모")
    window.note_button.click()
    assert window.problem_label.text().startswith("⚠ 고친 내용을 저장하지 못했습니다")

    window.note_edit.setPlainText("")

    assert window.problem_label.isHidden()
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."


def test_a_start_waits_for_another_window_to_close_only_when_it_follows_an_update(qapp, tmp_path, monkeypatch):
    import argparse

    import tsm300.app as app

    waited = []

    def no_lock(folder, wait_ms):
        waited.append(wait_ms)

    monkeypatch.setattr(app, "only_instance", no_lock)
    monkeypatch.setattr(app.QMessageBox, "information", lambda *arguments: None)
    arguments = argparse.Namespace(out=None, port=None, baud=9600)

    assert app.run(arguments) == 1
    monkeypatch.setenv(update.RESTARTED, "1")
    assert app.run(arguments) == 1

    assert waited == [app.INSTANCE_CHECK_MS, app.INSTANCE_WAIT_MS]
    assert update.RESTARTED not in os.environ                         # and it is not passed on to anything started later


def test_a_result_that_arrives_while_the_one_before_is_being_sent_is_sent_after_it(qapp, results):
    sender, on_their_way = FakeSender(), []
    window = open_window(qapp, results, settings=SENDING, send=sender,
                         background=lambda owner, work, done: on_their_way.append((work, done)))
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.set_patient(LEE)
    window.handle_event(LEFT_ONLY)
    assert len(on_their_way) == 1 and sender.sent == []          # the first has not finished going yet

    while on_their_way:
        work, done = on_their_way.pop(0)
        done(work(), None)

    assert [sheet for sheet, _, _ in sender.sent] == ["20261126_211205_12345.jpg", "20261126_211205_67890.jpg"]
    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")


# ----- third round ----------------------------------------------------------------------------------

def test_window_remembers_the_port_the_device_was_heard_on(qapp, results):
    window = open_window(qapp, results, settings=Settings(clinic_name="바른이비인후과의원"))

    window.remember_port("COM7", TSM_CABLE)
    window.remember_port("COM7", TSM_CABLE)
    window.remember_port("")

    kept = settings_file.load(results.parent / "settings")
    assert (kept.port, kept.port_heard, kept.port_heard_id, kept.clinic_name) == ("", "COM7", TSM_CABLE, "바른이비인후과의원")


def test_saving_the_settings_does_not_forget_what_was_learned_while_they_were_open(qapp, results):
    window = open_window(qapp, results)
    as_the_dialog_opened = window._settings

    window.remember_port("COM7", TSM_CABLE)                          # the device is switched on meanwhile
    window.apply_settings(Settings(clinic_name="바른이비인후과의원", port_heard=as_the_dialog_opened.port_heard))

    kept = settings_file.load(results.parent / "settings")
    assert (kept.port_heard, kept.port_heard_id, kept.clinic_name) == ("COM7", TSM_CABLE, "바른이비인후과의원")


def test_a_reopened_sheet_keeps_the_clinic_it_was_printed_with(qapp, results):
    earlier = Settings(clinic_name="옛 이름 의원", clinic_address="옛 주소", clinic_phone="051-111-1111")
    first = open_window(qapp, results, settings=earlier)
    first.set_patient(KIM)
    first.handle_event(BOTH)

    window = open_window(qapp, results, settings=Settings(clinic_name="새 이름 의원"))
    window.open_result(results / "20261126_211205_12345.json")
    window.note_edit.setPlainText("나중에 적은 메모")
    window.note_button.click()

    record = record_of(results, "20261126_211205_12345")
    assert record["note"] == "나중에 적은 메모"
    assert record["clinic"] == {"name": "옛 이름 의원", "address": "옛 주소", "phone": "051-111-1111"}


def test_a_reopened_sheet_printed_without_a_clinic_gets_the_one_now_in_the_settings(qapp, results):
    first = open_window(qapp, results)
    first.set_patient(KIM)
    first.handle_event(BOTH)
    first.set_patient(LEE)
    first.handle_event(LEFT_ONLY)
    record_file = results / "20261126_211205_67890.json"
    earlier_record = json.loads(record_file.read_text(encoding="utf-8"))
    del earlier_record["clinic"]                                     # as saved by a version that did not write it down
    record_file.write_text(json.dumps(earlier_record), encoding="utf-8")

    window = open_window(qapp, results, settings=BARUN)
    for stem in ("20261126_211205_12345", "20261126_211205_67890"):
        window.open_result(results / f"{stem}.json")
        window.note_edit.setPlainText("메모")
        window.note_button.click()

        assert record_of(results, stem)["clinic"]["name"] == "바른이비인후과의원"


def test_a_patient_that_cannot_be_put_on_the_sheet_is_reported_and_stays_in_line(qapp, results, monkeypatch):
    import tsm300.app as app

    window = open_window(qapp, results)
    window.handle_event(BOTH)
    window.set_patient(KIM)
    really_save = app.save_result

    def held_open(*arguments, **options):
        raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")

    monkeypatch.setattr(app, "save_result", held_open)
    window.save_button.click()

    assert window.problem_label.text().startswith("⚠ 고친 내용을 저장하지 못했습니다")
    window.note_edit.setPlainText("")                                # nothing about it changes by itself
    assert window.problem_label.text().startswith("⚠ 고친 내용을 저장하지 못했습니다")
    assert window.save_button.isEnabled()

    monkeypatch.setattr(app, "save_result", really_save)
    window.save_button.click()

    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."
    assert saved(results) == ["20261126_211205_12345.bin", "20261126_211205_12345.jpg", "20261126_211205_12345.json"]


def test_results_that_arrive_faster_than_they_can_be_sent_are_all_sent_in_turn(qapp, results):
    sender, on_their_way = FakeSender(), []
    times = iter([datetime(2026, 11, 26, 21, 12, second) for second in (1, 2, 3)])
    moment = [WHEN]

    def clock():
        return moment[0]

    window = open_window(qapp, results, settings=SENDING, send=sender,
                         background=lambda owner, work, done: on_their_way.append((work, done)))
    window._now = clock
    for patient, result in ((KIM, BOTH), (LEE, LEFT_ONLY), (Patient("00512", "홍길동"), BOTH)):
        moment[0] = next(times)
        window.set_patient(patient)
        window.handle_event(result)
    assert len(on_their_way) == 1

    while on_their_way:
        work, done = on_their_way.pop(0)
        done(work(), None)

    assert [patient.chart_no for _, patient, _ in sender.sent] == ["12345", "67890", "00512"]
    assert len({sheet for sheet, _, _ in sender.sent}) == 3
    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")


def test_a_result_dated_before_the_date_box_usually_allows_opens_unchanged(qapp, results):
    from tsm300.store import save_result

    save_result(BOTH, results, WHEN, KIM, tested_at=datetime(1999, 5, 1, 10, 0))
    window = open_window(qapp, results)

    window.open_result(results / "20261126_211205_12345.json")

    assert window.tested_edit.dateTime().toPython() == datetime(1999, 5, 1, 10, 0)
    assert not window.note_button.isEnabled()
    window.close()
    assert record_of(results, "20261126_211205_12345")["tested_at"] == "1999-05-01T10:00:00"


def test_a_folder_typed_without_a_drive_is_kept_as_a_full_path(qapp, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dialog = SettingsDialog(None, Settings(), run_now, ports=lambda: [])
    _OPENED.append(dialog)
    dialog.folder_edit.setText(" 검사결과 ")

    dialog.accept()

    assert dialog.settings().results_dir == str(tmp_path / "검사결과")
    assert (tmp_path / "검사결과").is_dir()


# ----- fourth round ---------------------------------------------------------------------------------

def test_closing_while_sheets_are_still_going_to_capture_is_asked_about(qapp, results):
    sender, on_their_way, asked = FakeSender(), [], []

    def answer(question):
        asked.append(question)
        return len(asked) > 1

    window = open_window(qapp, results, settings=SENDING, send=sender, confirm=answer,
                         background=lambda owner, work, done: on_their_way.append((work, done)))
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.close()
    assert window.isVisible() and "아직 다 보내지 못한 결과지가 1건" in asked[0]

    window.close()
    assert not window.isVisible()


def test_a_clinic_changed_while_a_sheet_is_on_its_way_reaches_it_only_if_it_did_not_arrive(qapp, results):
    for arrives in (True, False):
        folder = results / ("arrived" if arrives else "refused")
        sender, on_their_way = FakeSender(), []
        if not arrives:
            sender.outcome = {"ok": False, "code": "connect", "error": "연결할 수 없습니다", "retry": True}
        window = open_window(qapp, folder, settings=SENDING, send=sender,
                             background=lambda owner, work, done: on_their_way.append((work, done)))
        window.set_patient(KIM)
        window.handle_event(BOTH)
        window.apply_settings(Settings(cap_on=True, cap_url="192.0.2.1", cap_key="key", clinic_name="바른이비인후과의원"))
        assert record_of(folder, "20261126_211205_12345")["clinic"]["name"] == ""          # not under a sheet being sent

        work, done = on_their_way.pop(0)
        done(work(), None)

        assert record_of(folder, "20261126_211205_12345")["clinic"]["name"] == ("" if arrives else "바른이비인후과의원")


def test_a_patient_question_answered_after_another_result_took_the_screen_changes_nothing(qapp, results):
    def another_result_arrives_then_yes(question):
        window.handle_event(LEFT_ONLY)
        return True

    window = open_window(qapp, results, confirm=another_result_arrives_then_yes)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.set_patient(LEE)

    window.save_button.click()

    assert window.problem_label.text().startswith("⚠ 답하는 동안 새 결과가 도착해서 환자를 바꾸지 않았습니다")
    assert record_of(results, "20261126_211205_12345")["patient"]["name"] == "김하나"


# ----- output: print, file on this PC and send, together or one at a time ---------------------------

READY = Settings(cap_url="192.0.2.1", cap_key="key")            # 상쾌한캡처 set up, automatic sending off
FILED_KIM = "보관/2026-11/2026-11-26/12345_김하나_2112"


def printing(window, monkeypatch, chosen=True):
    """Stands in for the printer: answers the choice of printer and notes what was put on paper."""
    import tsm300.app as app

    printed = []
    monkeypatch.setattr(window, "_choose_printer", lambda printer: chosen)
    monkeypatch.setattr(app, "paint_sheet", lambda sheet, printer: printed.append(sheet.name))
    return printed


def filed(results):
    folder = results / "보관"
    return sorted(path.relative_to(results).as_posix() for path in folder.rglob("*") if path.is_file()) if folder.exists() else []


def test_output_prints_the_sheet_files_it_with_its_raw_data_and_sends_it(qapp, results, monkeypatch):
    from tsm300.records import send_sheet

    sender = FakeSender()

    def send_and_note_it_in_the_record(sheet, patient, tested_on, settings, internal_number=""):
        return send_sheet(sheet, patient, tested_on, settings, internal_number,
                          send=lambda *arguments, **options: sender(sheet, patient, tested_on, settings))

    window = open_window(qapp, results, settings=READY, send=send_and_note_it_in_the_record)
    printed = printing(window, monkeypatch)
    assert window.output_button.text() == "출력 (인쇄 + 보내기 + 보관)" and not window.output_button.isEnabled()
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("출력 전에 쓴 메모")

    window.output_button.click()

    assert printed == ["20261126_211205_12345.jpg"]
    assert filed(results) == [f"{FILED_KIM}.bin", f"{FILED_KIM}.jpg", f"{FILED_KIM}.json"]
    assert sender.sent == [("20261126_211205_12345.jpg", KIM, date(2026, 11, 26))]
    assert window.filing_label.text() == f"✔ 컴퓨터에 보관했습니다: {results / (FILED_KIM + '.jpg')}"
    assert window.capture_label.text().startswith("✔ 상쾌한캡처에 저장했습니다")
    copy = json.loads((results / f"{FILED_KIM}.json").read_text(encoding="utf-8"))
    assert copy["note"] == "출력 전에 쓴 메모" and [entry["ok"] for entry in copy["capture"]] == [True]
    assert (results / f"{FILED_KIM}.jpg").read_bytes() == (results / "20261126_211205_12345.jpg").read_bytes()
    assert (results / f"{FILED_KIM}.bin").read_bytes() == BOTH.raw


def test_output_does_nothing_more_when_the_printing_is_called_off(qapp, results, monkeypatch):
    sender = FakeSender()
    window = open_window(qapp, results, settings=READY, send=sender)
    printed = printing(window, monkeypatch, chosen=False)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.output_button.click()

    assert (printed, filed(results), sender.sent) == ([], [], [])
    assert window.filing_label.isHidden()


def test_output_without_capture_set_up_prints_and_files(qapp, results, monkeypatch):
    window = open_window(qapp, results)
    printed = printing(window, monkeypatch)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    assert window.output_button.text() == "출력 (인쇄 + 보관)" and window.send_button.isHidden()

    window.output_button.click()

    assert printed == ["20261126_211205_12345.jpg"] and len(filed(results)) == 3


def test_output_of_a_sheet_without_a_patient_is_asked_about_and_never_sent(qapp, results, monkeypatch):
    sender, answers, asked = FakeSender(), [False, True], []

    def answer(question):
        asked.append(question)
        return answers.pop(0)

    window = open_window(qapp, results, settings=READY, send=sender, confirm=answer)
    printed = printing(window, monkeypatch)
    window.handle_event(BOTH)

    window.output_button.click()
    assert "환자 정보 없이 출력할까요?" in asked[0] and (printed, filed(results)) == ([], [])

    window.output_button.click()
    assert printed == ["20261126_211205.jpg"] and sender.sent == []
    assert filed(results)[1] == "보관/2026-11/2026-11-26/환자정보없음_2112.jpg"


def test_output_does_not_send_again_a_sheet_that_is_already_in_capture(qapp, results, monkeypatch):
    sender = FakeSender()
    window = open_window(qapp, results, settings=SENDING, send=sender)
    printed = printing(window, monkeypatch)
    window.set_patient(KIM)
    window.handle_event(BOTH)                                        # sent by itself on arrival
    window.output_button.click()
    window.note_edit.setPlainText("보낸 뒤에 고침")
    window.output_button.click()

    assert len(printed) == 2 and len(sender.sent) == 1
    assert window.capture_label.text().startswith("⚠ 보낸 뒤에 결과지를 고쳤습니다")
    assert json.loads((results / f"{FILED_KIM}.json").read_text(encoding="utf-8"))["note"] == "보낸 뒤에 고침"


def test_each_part_of_output_has_a_button_of_its_own(qapp, results, monkeypatch):
    sender = FakeSender()
    window = open_window(qapp, results, settings=READY, send=sender)
    printed = printing(window, monkeypatch)
    for button in (window.print_button, window.file_button, window.send_button):
        assert not button.isEnabled()
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.print_button.click()
    assert (printed, filed(results), sender.sent) == (["20261126_211205_12345.jpg"], [], [])

    window.file_button.click()
    assert len(filed(results)) == 3 and sender.sent == [] and len(printed) == 1

    window.send_button.click()
    assert len(sender.sent) == 1 and len(printed) == 1


def test_a_filed_copy_follows_what_is_corrected_afterwards(qapp, results, monkeypatch):
    window = open_window(qapp, results, confirm=lambda question: True)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.file_button.click()

    window.note_edit.setPlainText("보관한 뒤에 쓴 메모")
    window.note_button.click()
    assert json.loads((results / f"{FILED_KIM}.json").read_text(encoding="utf-8"))["note"] == "보관한 뒤에 쓴 메모"
    assert (results / f"{FILED_KIM}.jpg").read_bytes() == (results / "20261126_211205_12345.jpg").read_bytes()

    window.set_patient(LEE)
    window.save_button.click()                                       # the result was another patient's after all

    moved = "보관/2026-11/2026-11-26/67890_이두리_2112"
    assert filed(results) == [f"{moved}.bin", f"{moved}.jpg", f"{moved}.json"]
    assert window.filing_label.text().endswith("67890_이두리_2112.jpg")


def test_opening_a_filed_copy_opens_the_result_it_was_made_from(qapp, results):
    first = open_window(qapp, results)
    first.set_patient(KIM)
    first.handle_event(BOTH)
    first.file_button.click()

    window = open_window(qapp, results)
    window.open_result(results / f"{FILED_KIM}.json")
    window.note_edit.setPlainText("보관본을 열어 쓴 메모")
    window.note_button.click()

    assert note_of(results, "20261126_211205_12345") == "보관본을 열어 쓴 메모"
    assert json.loads((results / f"{FILED_KIM}.json").read_text(encoding="utf-8"))["note"] == "보관본을 열어 쓴 메모"
    assert saved(results) == ["20261126_211205_12345.bin", "20261126_211205_12345.jpg", "20261126_211205_12345.json", "보관"]
    assert len(filed(results)) == 3


def test_a_filed_copy_whose_result_has_gone_brings_the_result_back(qapp, results):
    first = open_window(qapp, results)
    first.set_patient(KIM)
    first.handle_event(BOTH)
    first.note_edit.setPlainText("메모")
    first.file_button.click()
    for suffix in (".jpg", ".json", ".bin"):
        (results / f"20261126_211205_12345{suffix}").unlink()

    window = open_window(qapp, results)
    for _ in range(2):                                               # opening it twice does not make a second copy
        window.open_result(results / f"{FILED_KIM}.json")

    assert saved(results) == ["20261126_211205_12345.bin", "20261126_211205_12345.jpg", "20261126_211205_12345.json", "보관"]
    assert len(filed(results)) == 3
    assert window.note_edit.toPlainText() == "메모" and window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."


def test_a_copy_that_cannot_be_filed_is_said_and_the_rest_of_output_goes_on(qapp, results, monkeypatch):
    sender = FakeSender()
    elsewhere = results.parent / "보관할 수 없는 곳"
    elsewhere.write_text("폴더가 아님", encoding="utf-8")
    window = open_window(qapp, results, settings=Settings(cap_url="192.0.2.1", cap_key="key", archive_dir=str(elsewhere)),
                         send=sender)
    printing(window, monkeypatch)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.output_button.click()

    assert window.filing_label.text().startswith("⚠ 컴퓨터에 보관하지 못했습니다")
    assert len(sender.sent) == 1


def test_settings_dialog_takes_the_archive_folder_and_refuses_one_that_cannot_be_written(qapp, tmp_path):
    dialog = SettingsDialog(None, Settings(), run_now, ports=lambda: [])
    _OPENED.append(dialog)
    dialog.archive_edit.setText(str(tmp_path / "검사 보관"))
    assert dialog.settings().archive_dir == str(tmp_path / "검사 보관")

    blocked = tmp_path / "파일.txt"
    blocked.write_text("폴더가 아님", encoding="utf-8")
    dialog.archive_edit.setText(str(blocked / "보관"))
    dialog.accept()

    assert dialog.result() != SettingsDialog.DialogCode.Accepted
    assert dialog.problem_label.text().startswith("⚠ 보관 폴더에 쓸 수 없습니다")


def test_filing_waits_while_the_sheet_is_on_its_way_to_capture(qapp, results, monkeypatch):
    sender, on_their_way = FakeSender(), []
    window = open_window(qapp, results, settings=READY, send=sender,
                         background=lambda owner, work, done: on_their_way.append((work, done)))
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.send_button.click()
    assert not window.file_button.isEnabled() and not window.output_button.isEnabled()   # its record is being written to

    work, done = on_their_way.pop(0)
    done(work(), None)

    assert window.file_button.isEnabled() and window.output_button.isEnabled()


# ----- fifth round ----------------------------------------------------------------------------------

def test_a_results_folder_given_in_short_is_the_same_folder_as_its_full_path(qapp, tmp_path, monkeypatch):
    import tsm300.app as app

    monkeypatch.chdir(tmp_path)
    window = open_window(qapp, Path("results"))
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("놓치면 안 되는 메모")
    window.clear_patient()
    really_save = app.save_result

    def held_open(*arguments, replacing=None, **options):
        if replacing is not None:
            raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")
        return really_save(*arguments, **options)

    monkeypatch.setattr(app, "save_result", held_open)
    window.handle_event(LEFT_ONLY)
    monkeypatch.setattr(app, "save_result", really_save)

    window.open_result(tmp_path / "results" / "20261126_211205_12345.json")   # as the file picker gives it: in full

    assert window.note_edit.toPlainText() == "놓치면 안 되는 메모"


def test_output_is_called_off_when_another_result_arrives_while_the_question_is_open(qapp, results, monkeypatch):
    sender = FakeSender()

    def another_result_arrives_then_yes(question):
        window.set_patient(KIM)
        window.handle_event(LEFT_ONLY)
        return True

    window = open_window(qapp, results, settings=READY, send=sender, confirm=another_result_arrives_then_yes)
    printed = printing(window, monkeypatch)
    window.handle_event(BOTH)                                        # no patient: output asks first

    window.output_button.click()

    assert (printed, filed(results), sender.sent) == ([], [], [])
    assert window.problem_label.text().startswith("⚠ 출력하는 동안 새 결과가 도착했습니다. 앞 결과지는 출력하지 않았습니다.")


def test_a_filed_copy_finds_its_result_after_the_results_folder_was_moved(qapp, tmp_path):
    before, after = tmp_path / "C 드라이브" / "results", tmp_path / "D 드라이브" / "results"
    first = open_window(qapp, before)
    first.set_patient(KIM)
    first.handle_event(BOTH)
    first.file_button.click()
    first.close()
    after.parent.mkdir()
    before.rename(after)

    window = open_window(qapp, after)
    window.open_result(after / f"{FILED_KIM}.json")

    assert saved(after) == ["20261126_211205_12345.bin", "20261126_211205_12345.jpg", "20261126_211205_12345.json", "보관"]
    assert window.state_label.text() == "✔ 김하나 (12345) 환자 결과로 저장했습니다."


def test_a_clinic_changed_while_another_sheet_is_on_its_way_reaches_the_sheet_on_screen_at_once(qapp, results):
    sender, on_their_way = FakeSender(), []
    window = open_window(qapp, results, settings=SENDING, send=sender,
                         background=lambda owner, work, done: on_their_way.append((work, done)))
    window.set_patient(KIM)
    window.handle_event(BOTH)                                        # this one is on its way
    window.clear_patient()
    window._now = lambda: datetime(2026, 11, 26, 21, 13, 0)
    window.handle_event(LEFT_ONLY)                                   # this one, without a patient, is on screen

    window.apply_settings(Settings(cap_on=True, cap_url="192.0.2.1", cap_key="key", clinic_name="바른이비인후과의원"))

    assert record_of(results, "20261126_211300")["clinic"]["name"] == "바른이비인후과의원"
    assert record_of(results, "20261126_211205_12345")["clinic"]["name"] == ""
    while on_their_way:
        work, done = on_their_way.pop(0)
        done(work(), None)


# ----- sixth round ----------------------------------------------------------------------------------

def test_a_port_named_on_starting_still_tells_which_adapter_the_device_is_on(qapp, results):
    from tsm300.protocol import PowerOn

    FakeReader.made = []
    device = Device(9600, ports=lambda: [FakePort("COM5", serial="TSM")], reader=FakeReader, background=run_now)
    heard = []
    device.heard_on.connect(lambda port, adapter: heard.append((port, adapter)))
    device.use("COM5", exactly=True)

    FakeReader.made[0].received.emit(PowerOn("TSM-300 SCREENING", "6349"))

    assert heard == [("COM5", TSM_CABLE)]
    device.stop()

    window = open_window(qapp, results, settings=Settings(port_heard="COM5", port_heard_id=TSM_CABLE))
    window.remember_port("COM5", "")                                 # heard again, the adapter not known this time
    assert window._settings.port_heard_id == TSM_CABLE


def test_the_copy_of_a_sheet_that_left_the_screen_while_on_its_way_learns_that_it_arrived(qapp, results, monkeypatch):
    from tsm300.records import send_sheet

    sender, on_their_way = FakeSender(), []

    def send_and_note_it_in_the_record(sheet, patient, tested_on, settings, internal_number=""):
        return send_sheet(sheet, patient, tested_on, settings, internal_number,
                          send=lambda *arguments, **options: sender(sheet, patient, tested_on, settings))

    window = open_window(qapp, results, settings=READY, send=send_and_note_it_in_the_record,
                         background=lambda owner, work, done: on_their_way.append((work, done)))
    printing(window, monkeypatch)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.output_button.click()                                     # printed, filed, and on its way
    window.clear_patient()
    window.handle_event(LEFT_ONLY)                                   # another result takes the screen

    work, done = on_their_way.pop(0)
    done(work(), None)

    copy = json.loads((results / f"{FILED_KIM}.json").read_text(encoding="utf-8"))
    assert [entry["ok"] for entry in copy["capture"]] == [True]


def test_a_clinic_that_could_not_be_written_onto_the_sheet_goes_on_it_at_the_next_save(qapp, results, monkeypatch):
    import tsm300.app as app

    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    really_save = app.save_result

    def held_open(*arguments, **options):
        raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")

    monkeypatch.setattr(app, "save_result", held_open)
    window.apply_settings(BARUN)
    assert window.problem_label.text().startswith("⚠ 고친 내용을 저장하지 못했습니다")
    assert record_of(results, "20261126_211205_12345")["clinic"]["name"] == ""

    monkeypatch.setattr(app, "save_result", really_save)
    window.note_edit.setPlainText("메모")
    window.note_button.click()

    assert record_of(results, "20261126_211205_12345")["clinic"]["name"] == "바른이비인후과의원"
    assert window.problem_label.isHidden()


def test_a_copy_that_was_made_is_not_made_twice_when_noting_it_in_the_record_failed(qapp, results, monkeypatch):
    import tsm300.app as app

    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    def record_held(*arguments, **options):
        raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")

    monkeypatch.setattr(app, "note_in_record", record_held)
    window.file_button.click()
    window.file_button.click()

    assert filed(results) == [f"{FILED_KIM}.bin", f"{FILED_KIM}.jpg", f"{FILED_KIM}.json"]
    assert window.filing_label.text().startswith("✔ 컴퓨터에 보관했습니다")


def test_a_sheet_in_capture_is_still_known_to_be_there_after_its_patient_is_changed_twice(qapp, results):
    sender = FakeSender()
    window = open_window(qapp, results, settings=READY, send=sender, confirm=lambda question: True)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.send_button.click()

    for patient in (LEE, Patient("00512", "홍길동")):
        window.set_patient(patient)
        window.save_button.click()

    assert window.capture_label.text().startswith("⚠ 보낸 뒤에 결과지를 고쳤습니다")
    assert len(sender.sent) == 1


def test_a_warning_that_a_patient_could_not_be_saved_does_not_go_with_a_memo_taken_back(qapp, results, monkeypatch):
    import tsm300.app as app

    window = open_window(qapp, results)
    window.handle_event(BOTH)
    window.note_edit.setPlainText("메모")
    window.set_patient(KIM)

    def held_open(*arguments, **options):
        raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")

    monkeypatch.setattr(app, "save_result", held_open)
    window.save_button.click()
    window.note_edit.setPlainText("")

    assert window.problem_label.text().startswith("⚠ 고친 내용을 저장하지 못했습니다")


# ----- remarks: kept with the result, never printed ---------------------------------------------------------

def test_remarks_are_saved_with_a_tympanogram_filed_and_reopened_but_never_printed(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    stem = "20261126_211205_12345"
    sheet_before = (results / f"{stem}.jpg").read_bytes()
    assert window.remarks_edit.isEnabled() and not window.note_button.isEnabled()

    window.remarks_edit.setPlainText("  아이가 울어서 두 번 검사함  ")
    assert window.note_button.isEnabled()
    window.note_button.click()

    record = record_of(results, stem)
    assert (record["remarks"], record["note"], record["exam"]) == ("아이가 울어서 두 번 검사함", "", "tymp")
    assert (results / f"{stem}.jpg").read_bytes() == sheet_before
    assert not window.note_button.isEnabled()

    window.file_button.click()
    copy = results / "보관" / "2026-11" / "2026-11-26" / "12345_김하나_2112.json"
    assert json.loads(copy.read_text(encoding="utf-8"))["remarks"] == "아이가 울어서 두 번 검사함"

    other = open_window(qapp, results)
    other.open_result(copy)
    assert other.remarks_edit.toPlainText() == "아이가 울어서 두 번 검사함"
    other.remarks_edit.setPlainText("고친 특이사항")
    other.note_button.click()
    assert record_of(results, stem)["remarks"] == "고친 특이사항"
    assert json.loads(copy.read_text(encoding="utf-8"))["remarks"] == "고친 특이사항"


def test_remarks_left_unsaved_stay_with_their_result_when_the_next_one_arrives_or_the_window_closes(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.remarks_edit.setPlainText("저장을 누르지 않은 특이사항")
    window.set_patient(LEE)

    window.handle_event(LEFT_ONLY)

    assert record_of(results, "20261126_211205_12345")["remarks"] == "저장을 누르지 않은 특이사항"
    assert record_of(results, "20261126_211205_67890")["remarks"] == ""
    assert window.remarks_edit.toPlainText() == ""

    window.remarks_edit.setPlainText("닫기 전에 쓰던 특이사항")
    window.close()
    assert record_of(results, "20261126_211205_67890")["remarks"] == "닫기 전에 쓰던 특이사항"


def test_remarks_that_could_not_be_saved_are_kept_and_put_back_when_their_sheet_is_reopened(qapp, results, monkeypatch):
    import tsm300.app as app

    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)
    window.remarks_edit.setPlainText("놓치면 안 되는 특이사항")
    window.clear_patient()
    really_save = app.save_result

    def held_open(*arguments, replacing=None, **options):
        if replacing is not None:
            raise PermissionError(13, "다른 프로세스가 파일을 사용 중입니다")
        return really_save(*arguments, **options)

    monkeypatch.setattr(app, "save_result", held_open)
    window.handle_event(LEFT_ONLY)
    assert "특이사항: 놓치면 안 되는 특이사항" in window.problem_label.text()

    monkeypatch.setattr(app, "save_result", really_save)
    window.open_result(results / "20261126_211205_12345.json")
    assert window.remarks_edit.toPlainText() == "놓치면 안 되는 특이사항" and window.note_button.isEnabled()
    window.note_button.click()
    assert record_of(results, "20261126_211205_12345")["remarks"] == "놓치면 안 되는 특이사항"
