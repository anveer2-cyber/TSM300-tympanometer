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
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import needs_pty, pty  # noqa: E402
from fakes import KEY, TOKEN, CaptureReceiver, RelayServer  # noqa: E402
from tsm300 import __version__, update  # noqa: E402
from tsm300 import settings as settings_file  # noqa: E402
from tsm300.app import (  # noqa: E402
    REPLACE_LABEL, SAVE_LABEL, MainWindow, SettingsDialog, UpdateDialog, run_in_background,
)
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
    assert window.state_label.text().startswith("⚠ 결과 파일을 읽지 못했습니다")

    window.open_result(tmp_path / "missing.json")
    assert window.state_label.text() == "⚠ 결과 파일이 없습니다: missing.json"


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
