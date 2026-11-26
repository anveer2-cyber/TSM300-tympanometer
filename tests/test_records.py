"""Patient lookup, today's orders and sending to 상쾌한캡처, against stand-ins for the clinic's systems."""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from fakes import KEY, CaptureReceiver, records_database
from tsm300 import links
from tsm300.patient import Patient
from tsm300.protocol import parse_message
from tsm300.records import Records, error_text, records_from_settings, send_sheet, charts_tested_on
from tsm300.settings import Settings
from tsm300.store import save_result

DAY = date(2026, 11, 26)
HONG = Patient("00512", "홍길동", "남", date(1975, 3, 4))
BOTH = parse_message((Path(__file__).parent / "data" / "both_ears.bin").read_bytes())


@pytest.fixture
def records():
    database = records_database()
    return Records(links.MagicDb(connect_fn=lambda: database), today=lambda: DAY)


@pytest.fixture
def receiver():
    receiver = CaptureReceiver()
    yield receiver
    receiver.close()


def test_chart_number_is_matched_as_text_keeping_leading_zeros(records):
    assert records.find("00512") == HONG
    assert records.find("512") is None
    assert records.find("") is None


def test_birth_date_and_sex_come_from_the_resident_number_or_the_birthday_field(records):
    assert records.find("12345") == Patient("12345", "김하나", "여", date(1980, 1, 31))
    assert records.find("24680") == Patient("24680", "박세나", "", date(2015, 3, 2))


def test_two_patients_sharing_a_chart_number_need_a_person_to_decide(records):
    with pytest.raises(LookupError, match="77777 환자가 2명"):
        records.find("77777")


def test_todays_orders_list_each_visit_with_a_tympanometry_code_in_time_order(records):
    orders = records.todays_orders()

    assert [(order.time, order.patient.chart_no, order.patient.name, order.room) for order in orders] == [
        ("09:12", "00512", "홍길동", "진료1실"),
        ("10:05", "24680", "박세나", "진료1실"),
        ("10:30", "", "(환자 정보 없음)", "진료1실"),
    ]
    assert orders[0].label == "고막운동성계측 · 등골근반사"
    assert [order.selectable for order in orders] == [True, True, False]


def test_database_failures_are_described_without_connection_details():
    def refuse():
        raise RuntimeError("[IM002] [Microsoft][ODBC Driver Manager] Data source name not found (DSN=clinic;PWD=x)")

    broken = Records(links.MagicDb(connect_fn=refuse))
    with pytest.raises(RuntimeError) as failure:
        broken.find("00512")
    assert str(failure.value) == "ODBC 데이터 원본을 찾지 못했습니다. 설정의 DSN 이름을 확인해 주세요."
    assert "pyodbc 가 설치되어 있지 않습니다" in error_text(ModuleNotFoundError("No module named 'pyodbc'"))


def test_relay_server_has_no_tympanometry_order_list_yet():
    relay = Records(links.YejinServer("192.0.2.1", "token"))
    with pytest.raises(RuntimeError, match="아직 중이검사 오더 목록 주소가 없습니다"):
        relay.todays_orders()


def test_settings_choose_where_patients_are_looked_up():
    assert records_from_settings(Settings()) is None
    assert isinstance(records_from_settings(Settings(emr_via="odbc"))._backend, links.MagicDb)
    assert isinstance(records_from_settings(Settings(emr_via="server", emr_server="192.0.2.1"))._backend, links.YejinServer)


def test_sheet_is_sent_as_a_tympanometry_result_and_the_outcome_is_noted(tmp_path, receiver):
    sheet = save_result(BOTH, tmp_path, datetime(2026, 11, 26, 21, 12, 5), HONG)
    settings = Settings(cap_url=receiver.url, cap_key=KEY)

    outcome = send_sheet(sheet, HONG, DAY, settings, now=lambda: datetime(2026, 11, 26, 21, 13))

    assert outcome == {"ok": True, "duplicate": False, "id": "0" * 31 + "1",
                       "imageDataName": "66000_20261126_중이검사.jpg"}
    (sent,) = receiver.received
    assert (sent["chart"], sent["date"], sent["exam"], sent["name"], sent["birth"]) == \
        ("00512", "2026-11-26", "tymp", "홍길동", "1975-03-04")
    assert sent["note"] == ""
    assert sent["file"] == sheet.read_bytes() and sent["file"][:2] == b"\xff\xd8"
    noted = json.loads(sheet.with_suffix(".json").read_text(encoding="utf-8"))["capture"]
    assert noted == [dict(outcome, at="2026-11-26T21:13:00")]
    assert "홍길동" not in json.dumps(noted, ensure_ascii=False)


def test_sending_the_same_sheet_again_is_reported_as_a_duplicate(tmp_path, receiver):
    sheet = save_result(BOTH, tmp_path, datetime(2026, 11, 26, 21, 12, 5), HONG)
    settings = Settings(cap_url=receiver.url, cap_key=KEY)

    send_sheet(sheet, HONG, DAY, settings)
    again = send_sheet(sheet, HONG, DAY, settings)

    assert again["ok"] and again["duplicate"]
    assert len(receiver.received) == 1


@pytest.mark.parametrize("patient, key, code, retry", [
    (Patient("00512", "홍길똥", "남", date(1975, 3, 4)), KEY, "name_mismatch", False),
    (Patient("00512", "홍길동", "남", date(1975, 3, 5)), KEY, "birth_mismatch", False),
    (Patient("99999", "홍길동"), KEY, "patient_not_found", False),
    (HONG, "wrong-key", "bad_key", False),
    (Patient("00512", ""), KEY, "name_missing", False),
])
def test_refusals_are_reported_with_their_reason(tmp_path, receiver, patient, key, code, retry):
    sheet = save_result(BOTH, tmp_path, datetime(2026, 11, 26, 21, 12, 5), patient)

    outcome = send_sheet(sheet, patient, DAY, Settings(cap_url=receiver.url, cap_key=key))

    assert (outcome["ok"], outcome["code"], outcome["retry"]) == (False, code, retry)
    assert outcome["error"]
    assert receiver.received == []


def test_an_unreachable_receiver_can_be_retried_later(tmp_path):
    sheet = save_result(BOTH, tmp_path, datetime(2026, 11, 26, 21, 12, 5), HONG)

    outcome = send_sheet(sheet, HONG, DAY, Settings(cap_url="127.0.0.1:9", cap_key=KEY))

    assert (outcome["ok"], outcome["code"], outcome["retry"]) == (False, "connect", True)


def test_charts_with_a_result_today_are_found_from_the_saved_records(tmp_path):
    save_result(BOTH, tmp_path, datetime(2026, 11, 26, 9, 30), HONG)
    save_result(BOTH, tmp_path, datetime(2026, 11, 26, 9, 45))
    save_result(BOTH, tmp_path, datetime(2026, 11, 25, 9, 30), Patient("12345", "김하나"))

    assert charts_tested_on(tmp_path, DAY) == {"00512"}
    assert charts_tested_on(tmp_path / "missing", DAY) == set()
