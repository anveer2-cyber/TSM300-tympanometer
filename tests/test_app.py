"""The operator's window, driven without a display or a serial port."""

import gc
import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

import shiboken6  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tsm300.app import REPLACE_LABEL, SAVE_LABEL, MainWindow  # noqa: E402
from tsm300.patient import Order, Patient  # noqa: E402
from tsm300.protocol import Corrupt, parse_message  # noqa: E402

DATA = Path(__file__).parent / "data"
BOTH = parse_message((DATA / "both_ears.bin").read_bytes())
LEFT_ONLY = parse_message((DATA / "left_only.bin").read_bytes())
EMPTY = parse_message((DATA / "empty.bin").read_bytes())
WHEN = datetime(2026, 11, 26, 21, 12, 5)
KIM = Patient("12345", "김하나", "여", date(1980, 1, 31))
LEE = Patient("67890", "이두리", "남", date(1975, 6, 15))


class Records:
    """A stand-in for the clinic's records system."""

    def __init__(self, failing=False):
        self.failing = failing

    def find(self, chart_no):
        if self.failing:
            raise RuntimeError("서버에 연결할 수 없습니다")
        return {"12345": KIM, "67890": LEE}.get(chart_no)

    def todays_orders(self):
        if self.failing:
            raise RuntimeError("서버에 연결할 수 없습니다")
        return [Order(KIM, "09:10", "임피던스"), Order(LEE, "09:25", "임피던스")]


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


def open_window(qapp, results, **options):
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


def test_clearing_the_form_readies_it_for_the_next_patient(qapp, results):
    window = open_window(qapp, results)
    window.set_patient(KIM)
    window.handle_event(BOTH)

    window.clear_patient()

    assert window.patient().is_empty
    assert "다음 환자 정보를 넣어 주세요" in window.state_label.text()


def test_without_a_records_system_only_manual_entry_is_offered(qapp, results):
    window = open_window(qapp, results)

    assert not window.orders_list.isVisible() and not window.find_button.isVisible()
    assert window.source_note.isVisible() and "직접 입력" in window.source_note.text()


def test_with_a_records_system_the_manual_entry_note_is_gone(qapp, results):
    window = open_window(qapp, results, source=Records())

    assert window.orders_list.isVisible() and window.find_button.isVisible()
    assert not window.source_note.isVisible()


def test_todays_orders_are_listed_and_fill_the_form_when_chosen(qapp, results):
    window = open_window(qapp, results, source=Records())

    assert window.orders_list.count() == 2
    assert window.orders_list.item(1).text() == "09:25  67890  이두리  (남/51세)  임피던스"
    window.orders_list.itemClicked.emit(window.orders_list.item(1))

    assert window.patient() == LEE
    assert "다음 검사는 이두리 (67890) 환자로 저장됩니다" in window.state_label.text()


def test_chart_number_lookup_fills_the_form_or_says_it_found_nothing(qapp, results):
    window = open_window(qapp, results, source=Records())

    type_text(window.chart_edit, "12345")
    window.find_button.click()
    assert window.patient() == KIM

    window.clear_patient()
    type_text(window.chart_edit, "999")
    window.find_button.click()
    assert window.form_note.text() == "차트번호 999 환자를 찾지 못했습니다."
    assert window.patient() == Patient(chart_no="999")


def test_records_system_failures_are_shown_and_leave_manual_entry_usable(qapp, results):
    window = open_window(qapp, results, source=Records(failing=True))

    assert "오더 목록을 가져오지 못했습니다: 서버에 연결할 수 없습니다" in window.orders_note.text()
    type_text(window.chart_edit, "12345")
    window.find_button.click()
    assert "조회하지 못했습니다: 서버에 연결할 수 없습니다" in window.form_note.text()

    type_text(window.name_edit, "김하나")
    window.handle_event(BOTH)
    assert "20261126_211205_12345.jpg" in saved(results)


class TestOnASerialPort:
    """The background reader and the program's start-up, against a pseudo-terminal."""

    pty = pytest.importorskip("pty")

    @staticmethod
    def wait_until(condition, timeout=5.0):
        """Lets the event loop run until ``condition()`` holds; opening a port discards earlier bytes."""
        for _ in range(int(timeout / 0.05)):
            if condition():
                return
            QTest.qWait(50)
        raise AssertionError("condition was not met in time")

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
            self.wait_until(lambda: "연결됨" in window.connection_label.text())
            os.write(master, (DATA / "power_on.bin").read_bytes() + BOTH.raw)
            self.wait_until(lambda: saved(results))
        finally:
            worker.requestInterruption()
            assert worker.wait(3000)
            worker.status.disconnect()
            worker.received.disconnect()

        assert "20261126_211205_12345.jpg" in saved(results)
        assert "연결됨" in window.connection_label.text()
        assert "장비 켜짐 (TSM-300 SCREENING)" in window.connection_label.text()

    def test_program_starts_listens_and_saves_until_the_window_closes(self, qapp, tmp_path):
        from PySide6.QtCore import QTimer

        from tsm300 import cli

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
