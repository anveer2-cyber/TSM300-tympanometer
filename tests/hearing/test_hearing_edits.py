"""Values corrected by hand over what the audiometer sent, and results typed in without it."""

import json
from datetime import date, datetime

import pytest

from tsm300.clinic import Clinic
from tsm300.hearing import edits
from tsm300.hearing import protocol as p
from tsm300.hearing import sheet
from tsm300.hearing.record import (
    HIGH_FREQUENCY, SPEECH, SRT, TONE, Correction, HearingResult, Part, TypedSpeechPoint, from_saved, record,
)
from tsm300.hearing.samples import DEMO_SERIAL, SILENT, SILENT_MASKED, hf, practice_memory, tonal
from tsm300.patient import Patient
from tsm300.store import load_result, save_result

WHEN = datetime(2026, 11, 27, 10, 30, 12)
LATER = datetime(2026, 11, 27, 10, 41)
MUCH_LATER = datetime(2026, 11, 27, 11, 2)
KIM = Patient("12345", "김하나", "여", date(1980, 1, 31))


def practice_result():
    """The made-up hearing-aid patient: tone, speech, then soundfield tone and speech with the aid in."""
    patient_packet, tests = practice_memory().groups[-1]
    patient = p.device_patient(p.parse_frame(patient_packet))
    decoded = [p.parse_packet(test) for test in tests]
    return HearingResult(tuple(Part(test, patient, aided=index >= 2) for index, test in enumerate(decoded)))


def tone(level, frequency, masking=None, masked=False, no_response=False):
    return p.TonePoint(frequency, level, masking, masked or masking is not None, no_response)


# ----- typing a cell --------------------------------------------------------------------------------

@pytest.mark.parametrize("typed, read", [
    ("45", (45, False)), (" -10 ", (-10, False)), ("90n", (90, True)), ("90 NR", (90, True)), ("90↓", (90, True)),
    ("90ㅜ", (90, True)), ("120v", (120, True)), ("", None),
])
def test_a_threshold_is_a_number_and_n_after_it_says_no_response(typed, read):
    assert edits.parse_threshold(typed) == read


@pytest.mark.parametrize("typed, message", [("125", "-10 ~ 120 사이가 아닙니다"), ("-15", "사이가 아닙니다"),
                                            ("4o", "읽을 수 없습니다"), ("90x", "읽을 수 없습니다")])
def test_a_threshold_that_cannot_be_read_says_why(typed, message):
    with pytest.raises(edits.CellError, match=message):
        edits.parse_threshold(typed)


def test_masking_is_a_level_or_a_question_mark_when_the_level_is_not_known():
    assert edits.parse_masking("") == (False, None)
    assert edits.parse_masking("60") == (True, 60)
    assert edits.parse_masking("?") == edits.parse_masking("m") == (True, None)
    assert edits.tone_point(1000, "90n", "?") == p.TonePoint(1000, 90, None, True, True)       # masked, no response
    assert edits.tone_point(1000, "90n", "70") == p.TonePoint(1000, 90, 70, True, True)
    assert edits.tone_texts(p.TonePoint(1000, 90, None, True, True)) == ("90↓", "?")
    assert edits.toggled_no_response("90") == "90↓" and edits.toggled_no_response("90↓") == "90"
    with pytest.raises(edits.CellError, match="차폐만 있고 역치가 없습니다") as refused:
        edits.tone_point(1000, "", "60")
    assert refused.value.field == "masking"


def test_a_speech_point_is_checked_and_its_score_worked_out_from_the_words():
    assert edits.speech_point("", "", "", "", "") is None
    assert edits.speech_point("60", "", "", "11", "25") == TypedSpeechPoint(60, 44, None, False, 11, given_words=25)
    assert edits.speech_point("60", "80", "55", "", "").words is None
    assert edits.speech_texts(TypedSpeechPoint(60, 44, 55, True, 11, given_words=25)) == ("60", "44", "55", "11", "25")
    for cells, message, field in [
        (("", "40", "", "", ""), "강도가 없습니다", "level"),
        (("60", "101", "", "", ""), "0 ~ 100 사이가 아닙니다", "score"),
        (("60", "", "", "26", "25"), "보다 많습니다", "correct"),
        (("60", "", "", "11", ""), "함께 넣습니다", "words"),
    ]:
        with pytest.raises(edits.CellError, match=message) as refused:
            edits.speech_point(*cells)
        assert refused.value.field == field


# ----- corrections over the device's values ---------------------------------------------------------

def test_a_corrected_threshold_is_what_the_result_shows_and_the_devices_value_is_kept_beside_it():
    result = practice_result()

    corrected = result.with_cells({(TONE, "R", "Ac", 1000): tone(60, 1000)}, LATER)

    assert result.tone_curve("Ac", "R").at(1000).level == 55
    assert corrected.tone_curve("Ac", "R").at(1000).level == 60
    (correction,) = corrected.corrections
    assert (correction.cell, correction.device.level, correction.value.level, correction.at) == (
        (TONE, "R", "Ac", 1000), 55, 60, "2026-11-27T10:41:00")
    assert corrected.raw == result.raw and corrected.parts == result.parts
    assert corrected.average("Ac", "R").text() == "61"          # (45 + 2×60 + 2×65 + 75) / 6 = 61.7, was 60
    assert result.average("Ac", "R").text() == "60"


def test_a_value_put_back_as_the_device_sent_it_is_no_longer_a_correction_and_an_untouched_one_keeps_its_time():
    result = practice_result()
    once = result.with_cells({(TONE, "R", "Ac", 1000): tone(60, 1000), (TONE, "R", "Ac", 2000): tone(70, 2000)}, LATER)

    again = once.with_cells({(TONE, "R", "Ac", 1000): tone(55, 1000)}, MUCH_LATER)

    (correction,) = again.corrections
    assert correction.cell == (TONE, "R", "Ac", 2000) and correction.at == "2026-11-27T10:41:00"
    assert again.with_cells({(TONE, "R", "Ac", 2000): tone(65, 2000)}, MUCH_LATER) == result


def test_no_response_and_masked_no_response_can_be_marked():
    result = practice_result()

    corrected = result.with_cells({
        (TONE, "L", "Ac", 6000): p.TonePoint(6000, 85, None, False, True),
        (TONE, "R", "Bc", 4000): p.TonePoint(4000, 70, 80, True, True),
    }, LATER)

    assert corrected.tone_curve("Ac", "L").at(6000).no_response
    bone = corrected.tone_curve("Bc", "R").at(4000)
    assert (bone.level, bone.masking, bone.masked, bone.no_response) == (70, 80, True, True)
    assert corrected.average("Bc", "R").at_least                      # 4k with no response goes into the average


def test_a_row_the_device_did_not_send_can_be_added_and_one_it_sent_emptied():
    result = practice_result()
    assert result.tone_curve("AcU", "R") is None

    corrected = result.with_cells({
        (TONE, "R", "AcU", 1000): tone(100, 1000), (TONE, "R", "AcU", 2000): tone(105, 2000),
        **{(TONE, "L", "Spkr", frequency): None for frequency in (500, 1000, 2000, 4000)},
    }, LATER)

    added = corrected.tone["AcU", "R"]
    assert added.part is None and [point.level for point in added.curve.points] == [100, 105]
    assert ("Spkr", "L") not in corrected.tone
    assert corrected.device_curve(TONE, "Spkr", "L") is not None


def test_speech_points_and_the_srt_can_be_corrected_added_and_removed():
    result = practice_result()

    corrected = result.with_cells({
        (SPEECH, "R", "Ac", 2): TypedSpeechPoint(70, 68, None, False, 17, given_words=25),
        (SPEECH, "R", "Ac", 1): None,
        (SPEECH, "L", "Bc", 1): TypedSpeechPoint(50, 80, 40, True, None),
        (SRT, "R", "Ac", 0): 50,
        (SRT, "L", "Ac", 0): None,
    }, LATER)

    right = corrected.speech_curve("Ac", "R")
    assert [(point.level, point.score) for point in right.points] == [(70, 68), (80, 76)]
    assert corrected.speech_numbers("Ac", "R") == (2, 3)
    assert right.points[0].words == 25 and right.best.score == 76
    assert corrected.speech["Bc", "L"].part is None and corrected.speech_curve("Bc", "L").best.masking == 40
    assert corrected.srt_of("Ac", "R") == 50 and corrected.srt_of("Ac", "L") is None
    assert corrected.cells()[(SPEECH, "R", "Ac", 3)] == result.speech_curve("Ac", "R").points[2]


def test_a_correction_for_a_cell_that_does_not_exist_is_refused():
    with pytest.raises(ValueError, match="자리를 알 수 없습니다"):
        HearingResult(practice_result().parts, (Correction(TONE, "X", "Ac", 1000, tone(40, 1000)),))
    with pytest.raises(ValueError, match="형식이 맞지 않습니다"):
        HearingResult(practice_result().parts, (Correction(TONE, "R", "Ac", 1000, tone(40, 2000)),))


# ----- the saved record ------------------------------------------------------------------------------

def corrected_practice():
    return practice_result().with_cells({
        (TONE, "R", "Ac", 1000): tone(60, 1000),
        (TONE, "L", "Ac", 8000): None,
        (TONE, "R", "AcU", 1000): tone(100, 1000),
        (SPEECH, "R", "Ac", 1): TypedSpeechPoint(60, 48, None, False, 12, given_words=25),
        (SRT, "R", "Ac", 0): 50,
    }, LATER)


def test_the_record_holds_the_printed_values_each_corrected_one_with_the_devices_and_the_list_of_corrections():
    saved = record(corrected_practice())

    curves = {(curve["kind"], curve["ear"]): curve for curve in saved["tone"]["curves"]}
    right = curves["Ac", "R"]
    assert right["corrected"] is True
    assert right["points"][3] == {"frequency_hz": 1000, "level_db_hl": 60, "masking_db": None, "masked": False,
                                  "no_response": False, "corrected": True,
                                  "device_value": {"level_db_hl": 55, "masking_db": None, "masked": False,
                                                   "no_response": False}}
    assert "corrected" not in right["points"][2]                   # what was not touched stays as it was
    left = curves["Ac", "L"]
    assert [point["frequency_hz"] for point in left["points"]][-1] == 6000
    assert left["removed_points"] == [{"frequency_hz": 8000, "level_db_hl": 90, "masking_db": None, "masked": False,
                                       "no_response": True}]
    added = curves["AcU", "R"]
    assert (added["device_kind"], added["test"], added["stimulus"], added["pulsed"]) == (None, None, None, None)
    assert added["points"][0]["device_value"] is None
    assert "corrected" not in curves["Bc", "R"]

    averages = {(entry["ear"], entry["kind"], entry["method"]): entry for entry in saved["tone"]["averages"]}
    six = averages["R", "Ac", "6분법"]
    assert (six["text"], six["corrected"], six["device_value"]["text"]) == ("61", True, "60")
    assert "corrected" not in averages["R", "Bc", "6분법"]

    words = {(curve["kind"], curve["ear"]): curve for curve in saved["speech"]["curves"]}
    first = words["Ac", "R"]["points"][0]
    assert (first["score_percent"], first["correct"], first["words"], first["device_value"]["score_percent"]) == (
        48, 12, 25, 44)
    srt = {(entry["kind"], entry["ear"]): entry for entry in saved["speech"]["srt"]}
    assert (srt["Ac", "R"]["level_db_hl"], srt["Ac", "R"]["device_value"]) == (50, 55)
    assert words["Ac", "R"]["srt_db_hl"] == 50

    assert [(entry["section"], entry["ear"], entry["kind"], entry.get("frequency_hz", entry.get("point")))
            for entry in saved["corrections"]] == [
        ("tone", "R", "Ac", 1000), ("tone", "R", "AcU", 1000), ("tone", "L", "Ac", 8000),
        ("speech", "R", "Ac", 1), ("srt", "R", "Ac", None)]
    assert saved["corrections"][0] == {
        "at": "2026-11-27T10:41:00", "section": "tone", "ear": "R", "kind": "Ac", "frequency_hz": 1000,
        "device_value": {"level_db_hl": 55, "masking_db": None, "masked": False, "no_response": False},
        "value": {"level_db_hl": 60, "masking_db": None, "masked": False, "no_response": False}}
    assert saved["corrections"][2]["value"] is None
    assert saved["corrections"][4] == {"at": "2026-11-27T10:41:00", "section": "srt", "ear": "R", "kind": "Ac",
                                       "device_value": 55, "value": 50}
    assert saved["source"] == "device"
    json.dumps(saved, ensure_ascii=False)


def test_corrections_are_made_again_over_the_packets_when_the_result_is_reopened(tmp_path):
    result = corrected_practice()
    sheet_path = save_result(result, tmp_path, WHEN, KIM)

    assert sheet_path.with_suffix(".bin").read_bytes() == practice_result().raw   # the packets as the device sent them
    loaded = load_result(sheet_path).measurement

    assert loaded == result
    assert loaded.tone_curve("Ac", "R").at(1000).level == 60 and loaded.average("Ac", "R").text() == "61"
    assert loaded.device_values[(TONE, "R", "Ac", 1000)].level == 55
    assert [part.aided for part in loaded.parts] == [False, False, True, True]


def test_the_raw_data_stays_byte_for_byte_what_was_received_through_every_correction(tmp_path):
    first = save_result(practice_result(), tmp_path, WHEN, KIM)
    received = first.with_suffix(".bin").read_bytes()

    again = save_result(corrected_practice(), tmp_path, WHEN, KIM, replacing=first)
    loaded = load_result(again).measurement
    third = save_result(loaded.with_cells({(TONE, "R", "Ac", 500): tone(50, 500)}, MUCH_LATER), tmp_path, WHEN, KIM,
                        replacing=again)

    assert third.with_suffix(".bin").read_bytes() == received
    assert len(load_result(third).measurement.corrections) == 6


def test_corrections_that_cannot_be_read_are_reported():
    with pytest.raises(ValueError, match="손으로 고친 값을 읽지 못했습니다"):
        from_saved(practice_result().raw, {"corrections": [{"section": "tone", "ear": "R"}]})


# ----- typed in by hand ------------------------------------------------------------------------------

def typed_in():
    return HearingResult(manual=True).with_cells({
        (TONE, "R", "Ac", frequency): tone(level, frequency) for frequency, level in
        ((500, 40), (1000, 45), (2000, 50), (4000, 60))
    } | {
        (TONE, "L", "Ac", 1000): tone(30, 1000), (TONE, "L", "Bc", 1000): tone(25, 1000, masking=55),
        (SPEECH, "R", "Ac", 1): TypedSpeechPoint(70, 88, None, False, 22, given_words=25), (SRT, "R", "Ac", 0): 45,
    }, LATER)


def test_a_result_typed_in_by_hand_has_no_device_and_saves_an_empty_raw_file(tmp_path):
    result = typed_in()
    assert result.manual and result.parts == () and result.serials == () and result.raw == b""

    saved = record(result)
    assert (saved["source"], saved["device"], saved["device_patients"], saved["tests"]) == ("manual", None, [], [])
    assert all(point["corrected"] and point["device_value"] is None
               for curve in saved["tone"]["curves"] for point in curve["points"])
    assert saved["tone"]["curves"][0]["device_kind"] is None
    assert len(saved["corrections"]) == 8

    sheet_path = save_result(result, tmp_path, WHEN, KIM, "메모", tested_at=datetime(2026, 11, 20, 9, 0))
    assert sheet_path.with_suffix(".bin").read_bytes() == b""
    loaded = load_result(sheet_path)
    assert loaded.measurement == result and loaded.measurement.average("Ac", "R").text() == "48"
    assert loaded.tested_at == datetime(2026, 11, 20, 9, 0)

    edited = loaded.measurement.with_cells({(TONE, "R", "Ac", 4000): tone(65, 4000)}, MUCH_LATER)
    assert edited.manual and edited.average("Ac", "R").text() == "49"


def test_an_empty_result_is_known_to_be_empty():
    assert HearingResult(manual=True).is_empty
    emptied = typed_in().with_cells({cell: None for cell in typed_in().cells()}, MUCH_LATER)
    assert emptied.is_empty and emptied.corrections == ()


# ----- the sheet --------------------------------------------------------------------------------------

def test_a_corrected_sheet_is_drawn_exactly_as_if_the_device_had_sent_those_values(tmp_path):
    """No mark says a value was put in by hand: the sheet is the same as one received that way."""
    received = p.parse_packet(tonal({
        "Ac R": ({500: 40, 1000: 45, 2000: 50, 4000: 60}, {}),
        "Ac L": ({500: 20, 1000: 25, 2000: 30, 4000: 90}, {4000: SILENT}),
    }, serial=DEMO_SERIAL))
    as_if = p.parse_packet(tonal({
        "Ac R": ({500: 40, 1000: 55, 2000: 50, 4000: 60}, {1000: 70}),
        "Ac L": ({500: 20, 1000: 25, 2000: 30, 4000: 90}, {4000: SILENT_MASKED}),
        "AcU R": ({1000: 100}, {}),
    }, serial=DEMO_SERIAL))
    corrected = HearingResult((Part(received),)).with_cells({
        (TONE, "R", "Ac", 1000): tone(55, 1000, masking=70),
        (TONE, "L", "Ac", 4000): p.TonePoint(4000, 90, None, True, True),
        (TONE, "R", "AcU", 1000): tone(100, 1000),
    }, LATER)
    clinic = Clinic("바른이비인후과")

    sheet.render(corrected, tmp_path / "corrected.png", WHEN, KIM, "메모", clinic)
    sheet.render(HearingResult((Part(as_if),)), tmp_path / "as_if.png", WHEN, KIM, "메모", clinic)
    sheet.render(HearingResult((Part(received),)), tmp_path / "received.png", WHEN, KIM, "메모", clinic)

    assert (tmp_path / "corrected.png").read_bytes() == (tmp_path / "as_if.png").read_bytes()
    assert (tmp_path / "corrected.png").read_bytes() != (tmp_path / "received.png").read_bytes()


def test_a_sheet_typed_in_by_hand_names_no_device_and_its_heading_follows_what_it_holds(tmp_path):
    result = typed_in()

    sheet.render(result, tmp_path / "typed.jpg", WHEN, KIM)

    assert sheet.title(result) == "순음청력검사 · 어음청력검사"
    assert (tmp_path / "typed.jpg").stat().st_size > 0
    high = HearingResult((Part(p.parse_packet(hf({"Ac R": ({8000: 20}, {})}, serial=DEMO_SERIAL))),))
    assert sheet.title(high.with_cells({(HIGH_FREQUENCY, "R", "Ac", 8000): None,
                                        (TONE, "R", "Ac", 1000): tone(20, 1000)}, LATER)) == "순음청력검사"


def test_remarks_are_kept_in_the_record_and_never_drawn(tmp_path):
    with_remarks = save_result(practice_result(), tmp_path / "a", WHEN, KIM, "메모", remarks="보호자 동반, 재검 권유")
    without = save_result(practice_result(), tmp_path / "b", WHEN, KIM, "메모")

    assert with_remarks.read_bytes() == without.read_bytes()
    assert json.loads(with_remarks.with_suffix(".json").read_text(encoding="utf-8"))["remarks"] == "보호자 동반, 재검 권유"
    assert load_result(with_remarks).remarks == "보호자 동반, 재검 권유"
