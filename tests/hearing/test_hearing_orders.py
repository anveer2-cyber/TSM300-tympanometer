"""Hearing test orders from the clinic's records."""

from __future__ import annotations

from datetime import date

from fakes import TOKEN, RelayServer, records_database
from tsm300 import links
from tsm300.records import EXAM_NAMES, Records, exams_of, records_from_settings
from tsm300.settings import Settings

DAY = date(2026, 11, 26)


def with_hearing_orders():
    database = records_database()
    database.executemany("INSERT INTO exam_date VALUES (?, ?, ?, ?)", [
        (6, 66001, "2026-11-26 11:00:00", "청력검사실"),
        (7, 66003, "2026-11-26 11:20:00", "청력검사실"),
    ])
    database.executemany("INSERT INTO exam_yak VALUES (?, ?)", [
        (6, "F6341"), (6, "F6300"), (6, "F63411"),
        (7, "F0343"), (7, "F6361"),
    ])
    return database


def test_hearing_orders_are_listed_only_when_the_audiometer_is_used():
    database = with_hearing_orders()
    tympanometry = Records(links.MagicDb(connect_fn=lambda: database), today=lambda: DAY)
    both = Records(links.MagicDb(connect_fn=lambda: database), today=lambda: DAY,
                   codes=links.TYMP_CODES + links.HEARING_CODES)

    assert [order.time for order in tympanometry.todays_orders()] == ["09:12", "10:05", "10:30", "11:20"]
    orders = both.todays_orders()
    assert [order.time for order in orders] == ["09:12", "10:05", "10:30", "11:00", "11:20"]
    hearing = orders[3]
    assert (hearing.patient.chart_no, hearing.label, hearing.codes) == ("12345", "언어청각(어음) · 순음청력(표준)",
                                                                       ("F6300", "F6341"))
    assert orders[4].label == "음장어음 · 고막운동성계측"
    assert [exams_of(order.codes) for order in orders] == [
        ("tymp",), ("tymp",), ("tymp",), ("hearing",), ("tymp", "hearing")]


def test_the_settings_decide_which_codes_are_asked_for():
    assert records_from_settings(Settings()) is None
    plain = records_from_settings(Settings(emr_via="server"))
    hearing = records_from_settings(Settings(emr_via="server", hearing_on=True))
    assert plain._codes == links.TYMP_CODES
    assert hearing._codes == links.TYMP_CODES + links.HEARING_CODES


def test_orders_whose_codes_are_unknown_count_as_tympanometry():
    assert exams_of(()) == ("tymp",)
    assert exams_of(["f6341 "]) == ("hearing",)
    assert EXAM_NAMES == {"tymp": "중이", "hearing": "청력"}


def test_the_relay_servers_orders_are_kept_to_the_codes_asked_for():
    relay = RelayServer()
    relay.ORDERS = RelayServer.ORDERS + [
        {"chart": "67890", "chart_code": "", "name": "이두리", "birth": "1975-06-15", "age": 51, "sex": "남",
         "time": "11:00", "room": "청력검사실", "codes": ["F6341"], "visit": 9}]
    try:
        plain = Records(links.YejinServer(relay.url, TOKEN), today=lambda: DAY)
        hearing = Records(links.YejinServer(relay.url, TOKEN), today=lambda: DAY,
                          codes=links.TYMP_CODES + links.HEARING_CODES)
        assert [order.patient.chart_no for order in plain.todays_orders()] == ["00512", "12345", ""]
        orders = hearing.todays_orders()
        assert [order.patient.chart_no for order in orders] == ["00512", "12345", "", "67890"]
        assert orders[-1].label == "순음청력(표준)"
        # the request itself is as it always was: the server's contract is unchanged
        assert {tuple(sorted(query)) for path, query, _ in relay.requests if path == "/tsm/api/orders"} == {("day",)}
    finally:
        relay.close()
