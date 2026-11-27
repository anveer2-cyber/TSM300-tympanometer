"""Patient lookup, today's orders and sending to 상쾌한캡처, against stand-ins for the clinic's systems."""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from fakes import KEY, TOKEN, CaptureReceiver, RelayServer, records_database
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


@pytest.fixture
def relay():
    relay = RelayServer()
    yield relay
    relay.close()


def test_relay_server_is_asked_at_its_tsm300_addresses_with_the_tsm300_token(relay):
    records = Records(links.YejinServer(relay.url, TOKEN), today=lambda: DAY)

    assert records.find("00512") == HONG
    assert records.find("99999") is None
    orders = records.todays_orders()

    assert relay.requests == [
        ("/tsm/api/patients", {"q": "00512"}, TOKEN),
        ("/tsm/api/patients", {"q": "99999"}, TOKEN),
        ("/tsm/api/orders", {"day": "2026-11-26"}, TOKEN),
    ]
    assert [(order.time, order.patient.chart_no, order.patient.name, order.room) for order in orders] == [
        ("09:12", "00512", "홍길동", "진료1실"),
        ("09:40", "12345", "김하나", "진료2실"),
        ("10:30", "", "(환자 정보 없음)", ""),
    ]
    assert orders[0].label == "고막운동성계측 · 등골근반사"
    assert orders[0].patient == HONG
    assert [order.selectable for order in orders] == [True, True, False]


def test_internal_number_is_remembered_from_the_relay_lookup_when_it_gives_one(relay):
    records = Records(links.YejinServer(relay.url, TOKEN), today=lambda: DAY)
    assert records.internal_number("00512") == ""

    records.find("00512")
    records.find("12345")

    assert records.internal_number("00512") == "512"
    assert records.internal_number("12345") == ""


def test_internal_number_is_remembered_from_the_records_database(records):
    records.todays_orders()

    assert records.internal_number("00512") == "66000"
    assert records.internal_number("24680") == "66002"
    assert records.internal_number("99999") == ""


def test_relay_server_refusals_are_passed_on_in_its_own_words(relay):
    wrong_token = Records(links.YejinServer(relay.url, "wrong"))
    with pytest.raises(RuntimeError, match="예진이 서버 401: 토큰이 맞지 않습니다"):
        wrong_token.find("00512")
    with pytest.raises(RuntimeError, match="예진이 서버 401"):
        wrong_token.todays_orders()
    with pytest.raises(RuntimeError, match="주소 · 토큰이 비어 있습니다"):
        Records(links.YejinServer(relay.url, "")).find("00512")


def test_a_relay_server_too_old_to_know_tsm300_says_which_version_is_needed():
    old = RelayServer(old_version=True)
    try:
        with pytest.raises(RuntimeError, match="v4.52.0 이상인지 확인"):
            Records(links.YejinServer(old.url, TOKEN)).todays_orders()
    finally:
        old.close()


def test_connection_check_describes_the_relay_server(relay):
    assert Records(links.YejinServer(relay.url, TOKEN)).check() == "예진이 서버에 연결됨"


def test_settings_choose_where_patients_are_looked_up():
    assert records_from_settings(Settings()) is None
    assert isinstance(records_from_settings(Settings(emr_via="odbc"))._backend, links.MagicDb)
    assert isinstance(records_from_settings(Settings(emr_via="server", emr_server="192.0.2.1"))._backend, links.YejinServer)


def test_sheet_is_sent_as_a_tympanometry_result_and_the_outcome_is_noted(tmp_path, receiver):
    sheet = save_result(BOTH, tmp_path, datetime(2026, 11, 26, 21, 12, 5), HONG)
    settings = Settings(cap_url=receiver.url, cap_key=KEY)

    outcome = send_sheet(sheet, HONG, DAY, settings, now=lambda: datetime(2026, 11, 26, 21, 13))

    assert outcome == {"ok": True, "duplicate": False, "id": "0" * 31 + "1",
                       "imageDataName": "66000_20261126_중이검사.jpg", "chartId": "66000"}
    (sent,) = receiver.received
    assert (sent["chart"], sent["date"], sent["exam"], sent["name"], sent["birth"]) == \
        ("00512", "2026-11-26", "tymp", "홍길동", "1975-03-04")
    assert sent["note"] == ""
    assert sent["file"] == sheet.read_bytes() and sent["file"][:2] == b"\xff\xd8"
    noted = json.loads(sheet.with_suffix(".json").read_text(encoding="utf-8"))["capture"]
    assert noted == [dict(outcome, at="2026-11-26T21:13:00")]
    assert "홍길동" not in json.dumps(noted, ensure_ascii=False)


def test_only_the_chart_number_on_screen_is_sent_never_the_internal_number(tmp_path, receiver):
    sheet = save_result(BOTH, tmp_path, datetime(2026, 11, 26, 21, 12, 5), HONG)

    send_sheet(sheet, HONG, DAY, Settings(cap_url=receiver.url, cap_key=KEY), internal_number="66000")

    (sent,) = receiver.received
    assert sent["chart"] == "00512"
    assert "66000" not in [value for name, value in sent.items() if name != "file"]


@pytest.mark.parametrize("internal_number, flagged", [("66000", False), ("066000", False), ("", False), ("512", True)])
def test_a_sheet_filed_under_another_internal_number_than_the_lookup_gave_is_flagged(
        tmp_path, receiver, internal_number, flagged):
    sheet = save_result(BOTH, tmp_path, datetime(2026, 11, 26, 21, 12, 5), HONG)

    outcome = send_sheet(sheet, HONG, DAY, Settings(cap_url=receiver.url, cap_key=KEY), internal_number)

    assert outcome["ok"] and outcome["chartId"] == "66000"
    assert outcome.get("expected_chartId") == ("512" if flagged else None)


def test_server_side_failures_may_be_sent_again_but_refusals_may_not():
    assert links.SendError("save_failed", "저장 실패", 500).retry
    assert links.SendError("http503", "Service Unavailable", 503).retry
    assert links.SendError("connect", "연결 안 됨").retry
    assert not links.SendError("name_mismatch", "이름이 다릅니다", 409).retry
    assert not links.SendError("patient_not_found", "환자 없음", 404).retry
    assert not links.SendError("bad_date", "날짜 오류", 400).retry


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


def test_a_result_received_today_but_tested_another_day_does_not_count_as_tested_today(tmp_path):
    save_result(BOTH, tmp_path, datetime(2026, 11, 26, 9, 30), HONG, tested_at=datetime(2026, 11, 25, 16, 0))
    save_result(BOTH, tmp_path, datetime(2026, 11, 26, 9, 45), Patient("12345", "김하나"),
                tested_at=datetime(2026, 11, 26, 9, 40))

    assert charts_tested_on(tmp_path, DAY) == {"12345"}


def test_a_result_of_another_kind_of_test_does_not_count_as_tympanometry_done(tmp_path):
    sheet = save_result(BOTH, tmp_path, datetime(2026, 11, 26, 9, 30), HONG)
    record_file = sheet.with_suffix(".json")
    record = json.loads(record_file.read_text(encoding="utf-8"))
    record_file.write_text(json.dumps(dict(record, exam="pta")), encoding="utf-8")

    assert charts_tested_on(tmp_path, DAY) == set()
