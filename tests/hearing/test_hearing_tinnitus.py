"""The tinnitus tests (이명도검사), typed in by hand on a hearing result: values, sheet, audiogram T, window, orders."""

from __future__ import annotations

import gc
import json
import os
from datetime import date, datetime

import pytest
from matplotlib.figure import Figure

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

import shiboken6  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import KEY, TOKEN, CaptureReceiver, RelayServer, records_database  # noqa: E402
from tsm300 import app as app_module  # noqa: E402
from tsm300 import links  # noqa: E402
from tsm300.app import HearingReceiveDialog, MainWindow  # noqa: E402
from tsm300.clinic import Clinic  # noqa: E402
from tsm300.hearing import device, sheet  # noqa: E402
from tsm300.hearing import protocol as p  # noqa: E402
from tsm300.hearing.editor import HearingEditor  # noqa: E402
from tsm300.hearing.record import (  # noqa: E402
    TINNITUS_ITEMS, TINNITUS_TEXT, TONE, HearingResult, Part, Tinnitus, TinnitusPoint, record, tinnitus_value,
)
from tsm300.hearing.samples import DEMO_SERIAL, practice_memory, tonal  # noqa: E402
from tsm300.patient import Order, Patient  # noqa: E402
from tsm300.records import EXAM_NAMES, Records, exams_of  # noqa: E402
from tsm300.report import _font_and_labels  # noqa: E402
from tsm300.settings import Settings  # noqa: E402
from tsm300.store import load_result, save_result  # noqa: E402

WHEN = datetime(2026, 11, 27, 10, 30, 5)
LATER = datetime(2026, 11, 27, 10, 41)
KIM = Patient("12345", "김하나", "여", date(1980, 1, 31))
HONG = Patient("00512", "홍길동", "남", date(1975, 3, 4))
HEARING = Settings(hearing_on=True, hearing_link=device.SIMULATOR)
STEM = "20261127_103005"


def practice_result():
    patient_packet, tests = practice_memory().groups[-1]
    patient = p.device_patient(p.parse_frame(patient_packet))
    decoded = [p.parse_packet(test) for test in tests]
    return HearingResult(tuple(Part(test, patient, aided=index >= 2) for index, test in enumerate(decoded)))


def tinnitus(**by_ear) -> Tinnitus:
    """tinnitus(R={"pitch_hz": "4000", ...}, L={...})"""
    return Tinnitus.of({(ear, item): text for ear, items in by_ear.items() for item, text in items.items()})


CLINIC_WAY = tinnitus(R={"pitch_hz": "1500", "loudness_db": "28 dB", "threshold_db": "85 dB (o)",
                         "masking_noise_db": "Mo51dB", "minimum_masking_db_hl": "40", "residual_inhibition_s": "30",
                         "inhibition": "완전 억제"})


# ----- the values ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("typed, number", [
    ("1500", 1500), ("28 dB", 28), ("85 dB (o)", 85), ("Mo51dB", 51), ("완전 억제", None), ("", None),
    ("1.5 kHz", 1500), ("4k", 4000), ("12.5", 12.5), ("-5 dB", -5), ("30 sec", 30),
])
def test_a_number_is_read_from_the_text_as_the_clinic_writes_it(typed, number):
    assert tinnitus_value(typed) == number


def test_values_are_trimmed_kept_short_put_in_order_and_empty_ones_dropped():
    values = Tinnitus.of({("L", "pitch_hz"): "  4000 ", ("R", "inhibition"): "부분   억제", ("R", "pitch_hz"): "",
                          ("R", "loudness_db"): "x" * 60})
    assert values.entries == (("R", "loudness_db", "x" * TINNITUS_TEXT), ("R", "inhibition", "부분 억제"),
                              ("L", "pitch_hz", "4000"))
    assert values.text("R", "pitch_hz") == "" and not Tinnitus() and values
    with pytest.raises(ValueError, match="자리를 알 수 없습니다"):
        Tinnitus.of({("X", "pitch_hz"): "1"})


def test_the_record_holds_every_item_as_typed_with_the_number_read_from_it():
    saved = record(practice_result().with_tinnitus(CLINIC_WAY))["tinnitus"]

    right = saved["R"]
    assert [item for item in right if item != "plotted"] == list(TINNITUS_ITEMS)
    assert right["pitch_hz"] == {"text": "1500", "value": 1500}
    assert right["loudness_db"] == {"text": "28 dB", "value": 28}
    assert right["threshold_db"] == {"text": "85 dB (o)", "value": 85}
    assert right["masking_noise_db"] == {"text": "Mo51dB", "value": 51}
    assert right["inhibition"] == {"text": "완전 억제", "value": None}
    assert right["plotted"] == {"frequency_hz": 1500, "level_db_hl": 28, "from": "HL"}
    assert saved["L"]["pitch_hz"] == {"text": "", "value": None} and saved["L"]["plotted"] is None
    assert record(practice_result())["tinnitus"] is None
    json.dumps(saved, ensure_ascii=False)


def test_tinnitus_is_saved_and_read_back_with_the_result_and_no_correction_is_made_of_it(tmp_path):
    result = practice_result().with_tinnitus(CLINIC_WAY)
    sheet_path = save_result(result, tmp_path, WHEN, KIM)

    loaded = load_result(sheet_path).measurement

    assert loaded == result and loaded.tinnitus.text("R", "masking_noise_db") == "Mo51dB"
    assert loaded.corrections == () and sheet_path.with_suffix(".bin").read_bytes() == practice_result().raw
    kept = loaded.with_cells({(TONE, "R", "Ac", 1000): p.TonePoint(1000, 60)}, LATER)
    assert kept.tinnitus == CLINIC_WAY


# ----- the T on the audiogram ------------------------------------------------------------------------------

def test_a_loudness_in_hl_is_put_on_the_audiogram_as_it_is():
    result = practice_result().with_tinnitus(tinnitus(R={"pitch_hz": "5000", "loudness_db": "60 dB HL"},
                                                      L={"pitch_hz": "4000", "loudness_db": "55"}))
    assert result.tinnitus_point("R") == TinnitusPoint(5000, 60, "HL")
    assert result.tinnitus_point("L") == TinnitusPoint(4000, 55, "HL")


def test_a_loudness_in_sl_is_put_above_the_threshold_measured_there_or_read_between_the_nearest_two():
    # the right ear's air conduction: 4k 75, 6k 80 dB HL
    exact = practice_result().with_tinnitus(tinnitus(R={"pitch_hz": "4000", "loudness_db": "10 dB SL"}))
    between = practice_result().with_tinnitus(tinnitus(R={"pitch_hz": "5000", "loudness_db": "10SL"}))

    assert exact.tinnitus_point("R") == TinnitusPoint(4000, 85, "SL")
    # 5k on the log axis: 75 + (ln 5000/4000 ÷ ln 6000/4000) × (80 − 75) = 77.75, + 10
    assert between.tinnitus_point("R") == TinnitusPoint(5000, 87.8, "SL")
    assert record(between)["tinnitus"]["R"]["plotted"] == {"frequency_hz": 5000, "level_db_hl": 87.8, "from": "SL"}


@pytest.mark.parametrize("pitch, loudness", [
    ("4000", "20 dB SPL"),        # no conversion to trust
    ("4000", "크게"),              # no number
    ("높은 음", "30 dB"),          # no pitch
    ("12000", "30 dB"),           # off the graph
    ("4000", "130"),              # off the graph
    ("8000", "10 dB SL"),         # the left ear's 8k gave no response: no threshold there
    ("10000", "10 dB SL"),        # nothing measured above it to read between
])
def test_what_cannot_be_put_on_the_audiogram_with_confidence_is_left_to_the_table(pitch, loudness):
    result = practice_result().with_tinnitus(tinnitus(L={"pitch_hz": pitch, "loudness_db": loudness}))
    assert result.tinnitus_point("L") is None
    assert result.tinnitus.text("L", "loudness_db") == loudness


def test_a_tinnitus_only_result_puts_an_hl_match_on_empty_audiograms_but_cannot_place_an_sl_one():
    only = HearingResult(manual=True).with_tinnitus(tinnitus(R={"pitch_hz": "6000", "loudness_db": "50"},
                                                             L={"pitch_hz": "4000", "loudness_db": "10 dB SL"}))
    assert not only.is_empty and only.tone == {}
    assert only.tinnitus_point("R") == TinnitusPoint(6000, 50, "HL")
    assert only.tinnitus_point("L") is None
    assert sheet.title(only) == "이명도검사"


# ----- the sheet --------------------------------------------------------------------------------------------

def draw_ends(result):
    fonts, page = _font_and_labels()
    labels = sheet._KO if page is sheet._KOREAN else sheet._EN
    return [sheet._draw_results(Figure(), labels, page, result, layout) for layout in sheet.LAYOUTS]


def test_the_tinnitus_table_is_printed_only_when_something_is_filled_in(tmp_path):
    plain = practice_result()
    emptied = plain.with_tinnitus(tinnitus(R={"pitch_hz": "   "}))
    filled = plain.with_tinnitus(CLINIC_WAY)

    for name, result in (("plain", plain), ("emptied", emptied), ("filled", filled)):
        sheet.render(result, tmp_path / f"{name}.png", WHEN, KIM, "", Clinic("바른이비인후과"))

    assert (tmp_path / "plain.png").read_bytes() == (tmp_path / "emptied.png").read_bytes()
    assert (tmp_path / "filled.png").read_bytes() != (tmp_path / "plain.png").read_bytes()
    assert draw_ends(filled)[0] > draw_ends(plain)[0]
    assert sheet.title(filled) == "순음청력검사 · 음장검사 · 어음청력검사 · 이명도검사"


def test_the_t_is_drawn_where_the_match_is_and_explained(monkeypatch):
    drawn, said = [], []
    monkeypatch.setattr(sheet, "_plot_tinnitus", lambda axes, point, color: drawn.append((point, color)))
    real_text = sheet._text
    monkeypatch.setattr(sheet, "_text", lambda figure, x, y, text, **style: said.append(text) or
                        real_text(figure, x, y, text, **style))
    result = practice_result().with_tinnitus(tinnitus(R={"pitch_hz": "4000", "loudness_db": "10 dB SL"},
                                                      L={"pitch_hz": "4000", "loudness_db": "20 dB SPL"}))

    draw_ends(result)

    assert drawn[0] == (TinnitusPoint(4000, 85, "SL"), sheet.RIGHT_EAR)
    assert all(point.frequency == 4000 and color == sheet.RIGHT_EAR for point, color in drawn)
    legend = " ".join(text for text in said if text.startswith(("기호:", "Symbols:")))
    assert "T 이명 (주파수 · 크기)" in legend or "T tinnitus (pitch · loudness)" in legend

    drawn.clear()
    said.clear()
    draw_ends(practice_result().with_tinnitus(tinnitus(L={"pitch_hz": "4000", "loudness_db": "20 dB SPL"})))
    legend = " ".join(text for text in said if text.startswith(("기호:", "Symbols:")))
    assert drawn == [] and legend and "T " not in legend


def test_the_legend_names_the_symbols_drawn_on_the_sheet_and_no_others():
    result = practice_result()
    with_t = result.with_tinnitus(tinnitus(R={"pitch_hz": "4000", "loudness_db": "10 dB SL"}))
    every = {name: ({500: 45, 1000: 55}, {1000: 65}) for name in
             ("Ac R", "Bc R", "Spkr R", "SpkrA R", "AcU R", "Ac L", "Bc L")}
    every["AcM L"] = ({1000: 70}, {})
    full = HearingResult((Part(p.parse_packet(tonal(every, serial=DEMO_SERIAL, with_mcl=True))),))
    left_only = HearingResult((Part(p.parse_packet(tonal({"Ac L": ({1000: 30}, {})}, serial=DEMO_SERIAL))),))

    assert sheet.legend(result, {}) == ("기호: O·X 기도  ·  <·> 골도  ·  [ 골도 차폐  ·  S 음장(보청기 없이)  ·  "
                                        "A 음장(보청기 착용)  ·  ↓ 무반응")
    assert "T 이명 (주파수 · 크기)" in sheet.legend(with_t, {"R": with_t.tinnitus_point("R")})
    assert sheet.legend(full, {}) == ("기호: O·X 기도  ·  △·□ 기도 차폐  ·  <·> 골도  ·  [·] 골도 차폐  ·  "
                                      "S 음장(보청기 없이)  ·  A 음장(보청기 착용)  ·  U 불쾌역치  ·  M 쾌적역치")
    assert sheet.legend(left_only, {}) == "기호: X 기도"
    assert sheet.legend(HearingResult(manual=True), {}) == ""
    only_t = HearingResult(manual=True).with_tinnitus(tinnitus(R={"pitch_hz": "6000", "loudness_db": "50"}))
    assert sheet.legend(only_t, {"R": only_t.tinnitus_point("R")}) == "기호: T 이명 (주파수 · 크기)"
    assert sheet.legend(result, {}, sheet._EN).startswith("Symbols: O·X air  ·  <·> bone  ·  [ bone masked")


def test_the_legend_is_printed_under_the_tables(monkeypatch):
    said = []
    real_text = sheet._text
    monkeypatch.setattr(sheet, "_text", lambda figure, x, y, text, **style: said.append((y, text)) or
                        real_text(figure, x, y, text, **style))

    draw_ends(practice_result())

    legend = [(y, text) for y, text in said if text.startswith(("기호:", "Symbols:"))]
    averaging = [y for y, text in said if text.startswith(("평균 =", "PTA ="))]
    assert legend and averaging and all(y > averaging[0] for y, _ in legend[:1])
    assert all(y < sheet.CONTENT_BOTTOM for y, _ in legend)


def test_a_tinnitus_only_sheet_is_drawn(tmp_path):
    only = HearingResult(manual=True).with_tinnitus(tinnitus(R={"pitch_hz": "6000", "loudness_db": "50"}))
    words_only = HearingResult(manual=True).with_tinnitus(tinnitus(R={"inhibition": "부분 억제"}))

    sheet.render(only, tmp_path / "only.jpg", WHEN, KIM)
    sheet.render(words_only, tmp_path / "words.jpg", WHEN, KIM)

    assert (tmp_path / "only.jpg").stat().st_size > 0 and (tmp_path / "words.jpg").stat().st_size > 0
    assert draw_ends(words_only)[0] < draw_ends(only)[0]           # no audiograms without a T to put on them


def test_a_crowded_sheet_with_tinnitus_still_leaves_the_memo_box_clear():
    every = {name: ({250: 40, 500: 45, 1000: 55, 2000: 65, 4000: 75, 8000: 85}, {500: 60, 1000: 65})
             for name in ("Ac R", "Bc R", "Spkr R", "SpkrA R", "AcU R", "Ac L", "Bc L", "Spkr L", "SpkrA L", "AcU L")}
    every["AcM R"] = every["AcM L"] = ({1000: 70}, {1000: 50})
    from tsm300.hearing.samples import hf, speech
    crowded = HearingResult((
        Part(p.parse_packet(tonal(every, serial=DEMO_SERIAL, with_mcl=True))),
        Part(p.parse_packet(speech({"Ac R": [(60, 44, 50)], "Ac L": [(60, 44, 50)]}, srt={"Ac R": 55},
                                   serial=DEMO_SERIAL, long_form=True, answers=True, correct={"Ac R": [11]}))),
        Part(p.parse_packet(hf({"Ac R": ({8000: 20, 16000: 70}, {8000: 50}), "Ac L": ({8000: 15}, {})},
                               serial=DEMO_SERIAL))),
    )).with_tinnitus(tinnitus(R={item: text for _, item, text in CLINIC_WAY.entries},
                              L={"pitch_hz": "4000", "loudness_db": "60"}))

    assert draw_ends(crowded)[-1] <= sheet.CONTENT_BOTTOM


# ----- orders ---------------------------------------------------------------------------------------------

def test_tinnitus_orders_are_listed_and_marked_apart():
    database = records_database()
    database.executemany("INSERT INTO exam_date VALUES (?, ?, ?, ?)", [(6, 66001, "2026-11-26 11:00:00", "청력검사실"),
                                                                       (7, 66002, "2026-11-26 11:30:00", "청력검사실")])
    database.executemany("INSERT INTO exam_yak VALUES (?, ?)", [(6, "F6341"), (6, "F6346"), (7, "F6346")])
    records = Records(links.MagicDb(connect_fn=lambda: database), today=lambda: date(2026, 11, 26),
                      codes=links.TYMP_CODES + links.HEARING_CODES)

    orders = records.todays_orders()

    assert "F6346" in links.HEARING_CODES and links.ORDER_NAMES["F6346"] == "이명도검사"
    assert [(order.time, order.label) for order in orders[-2:]] == [
        ("11:00", "순음청력(표준) · 이명도검사"), ("11:30", "이명도검사")]
    assert [exams_of(order.codes) for order in orders[-2:]] == [("hearing", "tinnitus"), ("tinnitus",)]
    assert EXAM_NAMES["tinnitus"] == "이명"


def test_the_relay_servers_tinnitus_orders_are_kept_when_hearing_orders_are_asked_for():
    relay = RelayServer()
    relay.ORDERS = RelayServer.ORDERS + [
        {"chart": "67890", "chart_code": "", "name": "이두리", "birth": "1975-06-15", "age": 51, "sex": "남",
         "time": "11:00", "room": "청력검사실", "codes": ["F6346"], "visit": 9}]
    try:
        plain = Records(links.YejinServer(relay.url, TOKEN), today=lambda: date(2026, 11, 26))
        hearing = Records(links.YejinServer(relay.url, TOKEN), today=lambda: date(2026, 11, 26),
                          codes=links.TYMP_CODES + links.HEARING_CODES)
        assert "67890" not in [order.patient.chart_no for order in plain.todays_orders()]
        assert hearing.todays_orders()[-1].label == "이명도검사"
    finally:
        relay.close()


# ----- the window -------------------------------------------------------------------------------------------

def run_now(owner, work, done):
    try:
        result = work()
    except Exception as error:
        done(None, error)
    else:
        done(result, None)


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


class OrderedRecords:
    def __init__(self, orders):
        self.orders = orders

    def find(self, chart_no):
        return None

    def todays_orders(self):
        return self.orders

    def internal_number(self, chart_no):
        return ""


def open_window(qapp, results, settings=HEARING, steps=(), **options):
    from hearing_fakes import Audiometer

    audiometer = Audiometer()
    pending = list(steps)
    dialogs = []

    def staff(dialog):
        dialogs.append(dialog)
        (pending.pop(0) if pending else (lambda dialog: dialog.accept()))(dialog)

    def choose(fetched, seen, patient, patient_is_new, received_at):
        dialog = HearingReceiveDialog(window, fetched, seen, patient, patient_is_new, received_at)
        dialog.set_chosen((3, 2), True, aided=True)
        dialog.set_chosen((3, 3), True, aided=True)
        dialog.accept()
        return dialog.choice()

    options.setdefault("background", run_now)
    options.setdefault("confirm", lambda question: True)
    options.setdefault("confirm_patient", lambda patient: True)
    options.setdefault("settings_directory", results.parent / "settings")
    window = MainWindow(results, settings, now=lambda: WHEN, run_editor=staff, choose_hearing=choose,
                        hearing_opener=lambda link: audiometer.open,
                        fetch_hearing=lambda opener, folder, progress, **kw: device.fetch(
                            opener, folder, progress, now=lambda: WHEN, clock=audiometer.clock, **kw), **options)
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    _OPENED.append(window)
    window.steps, window.dialogs = pending, dialogs
    return window


def record_of(results, stem):
    return json.loads((results / f"{stem}.json").read_text(encoding="utf-8"))


def type_tinnitus(dialog: HearingEditor) -> None:
    for item, text in (("pitch_hz", "4000"), ("loudness_db", "10 dB SL"), ("threshold_db", "45 dB"),
                       ("masking_noise_db", "Mo51dB"), ("residual_inhibition_s", "30")):
        dialog.set_tinnitus("R", item, text)
    dialog.set_tinnitus("R", "inhibition", "완전 억제")
    dialog.set_tinnitus("L", "pitch_hz", "1500")


def test_a_tinnitus_only_result_is_typed_in_saved_reopened_corrected_printed_filed_and_sent(qapp, results, monkeypatch):
    receiver = CaptureReceiver()
    printed = []
    monkeypatch.setattr(app_module, "paint_sheet", lambda sheet_path, printer: printed.append(sheet_path.name))
    try:
        settings = Settings(hearing_on=True, hearing_link=device.SIMULATOR, cap_url=receiver.url, cap_key=KEY)

        def enter(dialog):
            assert [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())] == [
                "순음 · 음장", "어음", "이명", "검사일시 · 메모"]
            type_tinnitus(dialog)
            dialog.accept()

        window = open_window(qapp, results, settings=settings, steps=[enter])
        monkeypatch.setattr(window, "_choose_printer", lambda printer: True)
        window.set_patient(HONG)
        window.manual_button.click()

        stem = f"{STEM}_00512"
        saved = record_of(results, stem)
        assert (saved["source"], saved["tone"], saved["speech"], saved["corrections"]) == ("manual", None, None, [])
        assert saved["tinnitus"]["R"]["masking_noise_db"] == {"text": "Mo51dB", "value": 51}
        assert saved["tinnitus"]["R"]["plotted"] is None          # SL with no threshold to put it on
        assert saved["tinnitus"]["L"]["pitch_hz"] == {"text": "1500", "value": 1500}

        window.output_sheet()
        assert printed == [f"{stem}.jpg"]
        (sent,) = receiver.received
        assert (sent["exam"], sent["chart"]) == ("hearing", "00512")
        copy = results / "보관" / "2026-11" / "2026-11-27" / "00512_홍길동_1030.json"
        assert json.loads(copy.read_text(encoding="utf-8"))["tinnitus"]["R"]["inhibition"]["text"] == "완전 억제"

        def correct(dialog):
            assert dialog.tabs.tabText(2) == "이명 •"
            assert dialog.tinnitus_fields[("R", "masking_noise_db")].text() == "Mo51dB"
            assert dialog.tinnitus_fields[("R", "inhibition")].currentText() == "완전 억제"
            dialog.set_tinnitus("R", "loudness_db", "50")
            dialog.set_tinnitus("L", "pitch_hz", "")
            dialog.accept()

        window.steps.append(correct)
        window.open_result(results / f"{stem}.json")
        window.values_button.click()

        loaded = load_result(results / f"{stem}.jpg").measurement
        assert loaded.manual and loaded.tinnitus.text("R", "loudness_db") == "50"
        assert loaded.tinnitus.text("L", "pitch_hz") == ""
        assert record_of(results, stem)["tinnitus"]["R"]["plotted"] == {"frequency_hz": 4000, "level_db_hl": 50,
                                                                        "from": "HL"}
        assert json.loads(copy.read_text(encoding="utf-8"))["tinnitus"]["R"]["loudness_db"]["text"] == "50"
        assert len(receiver.received) == 1                     # corrected after sending: not sent again by itself
    finally:
        receiver.close()


def test_tinnitus_typed_onto_a_received_result_goes_beside_its_values_and_is_marked_done_in_the_orders(qapp, results):
    orders = [Order(KIM, "09:10", "순음청력(표준) · 이명도검사", "청력검사실", ("F6341", "F6346"))]
    window = open_window(qapp, results, source=OrderedRecords(orders))
    mark = lambda: window.orders_table.item(0, 5).text()  # noqa: E731
    assert mark() == "청력 · 이명"

    window.set_patient(KIM)
    window.hearing_button.click()
    assert mark() == "청력 ✓ · 이명"

    window.steps.append(lambda dialog: (type_tinnitus(dialog), dialog.accept()))
    window.values_button.click()

    stem = f"{STEM}_12345"
    saved = record_of(results, stem)
    assert saved["source"] == "device" and saved["corrections"] == []
    assert saved["tinnitus"]["R"]["plotted"] == {"frequency_hz": 4000, "level_db_hl": 85, "from": "SL"}  # 75 + 10
    assert mark() == "청력 ✓ · 이명 ✓"


def test_typing_in_nothing_but_an_empty_tinnitus_is_still_refused(qapp, results):
    def enter(dialog):
        dialog.set_tinnitus("R", "pitch_hz", "   ")
        dialog.accept()
        assert "값을 하나 이상 넣으세요" in dialog.problem_label.text()
        dialog.reject()

    window = open_window(qapp, results, steps=[enter])
    window.manual_button.click()
    assert not results.exists() or not any(results.glob("*.json"))
