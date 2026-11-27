"""One hearing sheet made of several received tests, and how it is saved and read back."""

import json
from datetime import date, datetime

import pytest
from PIL import Image

from tsm300.clinic import Clinic
from tsm300.hearing import protocol as p
from tsm300.hearing.record import (
    EXAM, FORMAT, HearingResult, MergeError, Part, can_be_aided, describe, device_birth_date, from_saved, record,
)
from tsm300.hearing.samples import DEMO_SERIAL, SILENT, frame, hf, practice_memory, tonal
from tsm300.patient import Patient
from tsm300.store import load_result, save_result

WHEN = datetime(2026, 11, 27, 10, 30, 12)
KIM = Patient("12345", "김하나", "여", date(1980, 1, 31))


def device_patient(number="4", first="견본", surname="라", birth="20/05/1955", sex="F"):
    data = f"{number};0;{first};{surname};{birth};{sex};;;".encode("utf-8")
    return p.device_patient(p.parse_frame(p.frame(p.KIND_PATIENT, data)))


def practice_parts():
    """The made-up hearing-aid patient: tone, speech, then soundfield tone and speech with the aid in."""
    patient_packet, tests = practice_memory().groups[-1]
    patient = p.device_patient(p.parse_frame(patient_packet))
    decoded = [p.parse_packet(test) for test in tests]
    return patient, decoded


def aided_result():
    patient, (tone, words, aided_tone, aided_words) = practice_parts()
    return HearingResult((Part(tone, patient), Part(words, patient), Part(aided_tone, patient, aided=True),
                          Part(aided_words, patient, aided=True)))


def test_soundfield_with_hearing_aids_goes_on_the_same_sheet_as_the_aided_curves():
    result = aided_result()

    assert result.tone_curve("Spkr", "R").at(500).level == 50          # without the aid, from the first test
    assert result.tone_curve("SpkrA", "R").at(500).level == 30         # with it, from the second
    assert result.tone["SpkrA", "R"].curve.kind == "Spkr"               # as the device called it
    assert result.tone["SpkrA", "R"].part == 2
    assert result.speech_curve("SpkrA", "R").best.score == 84
    assert result.srt_of("SpkrA", "R") == 35 and result.srt_of("Spkr", "R") == 55
    assert result.average("SpkrA", "R").text() == "34"                  # (30 + 2×30 + 2×35 + 45) / 6 = 34.2
    assert result.has_aided and result.has_soundfield and result.has_speech and not result.has_high_frequency


def test_only_soundfield_tests_can_be_marked_as_done_with_hearing_aids():
    _, (tone, words, aided_tone, aided_words) = practice_parts()

    assert can_be_aided(aided_tone) and can_be_aided(aided_words)
    assert not can_be_aided(tone) and not can_be_aided(words)
    with pytest.raises(MergeError, match="음장만 있는 검사"):
        HearingResult((Part(tone, aided=True),))


def test_the_same_curve_from_two_tests_is_refused_with_a_hint_about_the_aid():
    _, (tone, _, aided_tone, _) = practice_parts()

    with pytest.raises(MergeError, match="순음 음장 오른쪽 결과가 두 검사에") as refused:
        HearingResult((Part(tone), Part(aided_tone)))
    assert "보청기" in str(refused.value)


def test_nothing_chosen_or_a_test_that_is_not_drawn_is_refused():
    with pytest.raises(MergeError, match="고른 검사가 없습니다"):
        HearingResult(())
    other = p.parse_packet(frame(DEMO_SERIAL, 1, 12, bytes(40)))
    assert isinstance(other, p.OtherTest)
    with pytest.raises(MergeError, match="고막운동도"):
        HearingResult((Part(other),))


def test_tests_are_described_for_the_operator():
    _, (tone, words, aided_tone, _) = practice_parts()

    assert describe(tone) == "순음 — 기도 R·L, 골도 R·L, 음장 R·L"
    assert describe(words) == "어음 — 기도 R·L, 음장 R"
    assert describe(aided_tone) == "순음 — 음장 R·L"
    assert describe(p.parse_packet(hf({"Ac R": ({8000: 20}, {})}))) == "고주파 순음 — 기도 R"


def test_the_birth_date_typed_on_the_device_is_read_day_first():
    assert device_birth_date(device_patient(birth="20/05/1955")) == date(1955, 5, 20)
    assert device_birth_date(device_patient(birth="01/01/1800")) is None   # the maker's "unknown"
    assert device_birth_date(device_patient(birth="")) is None
    assert device_birth_date(None) is None


def test_raw_data_holds_each_device_patient_once_ahead_of_its_tests():
    patient, (tone, words, _, _) = practice_parts()
    other = device_patient("5", "다른", "환자", "01/02/1990", "M")
    high = p.parse_packet(hf({"Ac R": ({8000: 20}, {})}, serial=DEMO_SERIAL))

    result = HearingResult((Part(tone, patient), Part(words, patient), Part(high, other)))

    assert result.raw == patient.raw + tone.packet.raw + words.packet.raw + other.raw + high.packet.raw
    assert result.device_patients == (patient, other)


# ----- the record: the contract in docs/hearing/원자료_형식.md --------------------------------------

def test_record_holds_every_value_and_the_packets():
    result = aided_result()
    saved = record(result)

    assert saved["device"] == {"maker": "MRS", "name": "Resonance", "model": "R37A", "serial": DEMO_SERIAL,
                               "serials": [DEMO_SERIAL]}
    (who,) = saved["device_patients"]
    assert (who["number"], who["first_name"], who["surname"], who["birth_date"], who["birth_date_iso"], who["sex"]) == \
        ("4", "견본", "라", "20/05/1955", "1955-05-20", "F")
    assert bytes.fromhex(who["packet"]) == result.parts[0].patient.raw

    tests = saved["tests"]
    assert [(test["type"], test["aided"], test["device_patient"]) for test in tests] == [
        ("tone", False, 0), ("speech", False, 0), ("tone", True, 0), ("speech", True, 0)]
    assert all(bytes.fromhex(test["packet"]) == part.test.packet.raw for test, part in zip(tests, result.parts))
    assert tests[0]["fingerprint"] == result.parts[0].fingerprint and tests[0]["name"] == "순음청력"

    curves = {(curve["kind"], curve["ear"]): curve for curve in saved["tone"]["curves"]}
    assert [(curve["ear"], curve["kind"]) for curve in saved["tone"]["curves"]][:4] == [
        ("R", "Ac"), ("R", "Bc"), ("R", "Spkr"), ("R", "SpkrA")]
    bone = curves["Bc", "R"]
    assert bone["points"][2] == {"frequency_hz": 2000, "level_db_hl": 55, "masking_db": 75, "masked": True,
                                 "no_response": False}
    assert curves["Ac", "L"]["points"][-1]["no_response"] is True
    assert (curves["SpkrA", "L"]["device_kind"], curves["SpkrA", "L"]["test"]) == ("Spkr", 2)
    assert curves["Ac", "R"]["stimulus"] == "TONE" and curves["Ac", "R"]["pulsed"] is False

    averages = {(entry["ear"], entry["kind"], entry["method"]): entry for entry in saved["tone"]["averages"]}
    assert {method for _, _, method in averages} == {"3분법", "4분법", "6분법"}
    six = averages["R", "Ac", "6분법"]
    assert six["value"] == pytest.approx((45 + 2 * 55 + 2 * 65 + 75) / 6, abs=1e-4)
    assert (six["shown"], six["text"], six["at_least"], six["missing_hz"]) == (60, "60", False, [])
    assert six["weights"] == {"500": 1, "1000": 2, "2000": 2, "4000": 1}
    assert averages["L", "Ac", "6분법"]["at_least"] is False
    assert ("R", "AcU", "6분법") not in averages

    words = {(curve["kind"], curve["ear"]): curve for curve in saved["speech"]["curves"]}
    right = words["Ac", "R"]
    assert right["points"][2] == {"level_db_hl": 80, "score_percent": 76, "masking_db": 55, "masked": True,
                                  "correct": 19, "words": 25}
    assert right["best"]["score_percent"] == 76 and right["srt_db_hl"] == 55
    assert words["SpkrA", "R"]["device_kind"] == "Spkr" and words["SpkrA", "R"]["best"]["score_percent"] == 84
    assert {(entry["kind"], entry["ear"], entry["level_db_hl"]) for entry in saved["speech"]["srt"]} == {
        ("Ac", "R", 55), ("Ac", "L", 45), ("Spkr", "R", 55), ("SpkrA", "R", 35)}
    assert saved["high_frequency"] is None


def test_record_of_a_high_frequency_test_and_of_a_missing_frequency():
    high = p.parse_packet(hf({"Ac R": ({8000: 20, 11200: 35, 20000: 90}, {20000: 234})}))
    tone = p.parse_packet(tonal({"Ac L": ({500: 30, 1000: 40, 2000: 50}, {})}))

    saved = record(HearingResult((Part(tone), Part(high))))

    (curve,) = saved["high_frequency"]["curves"]
    assert [(point["frequency_hz"], point["level_db_hl"], point["no_response"]) for point in curve["points"]] == [
        (8000, 20, False), (11200, 35, False), (20000, 90, True)]
    six = next(entry for entry in saved["tone"]["averages"] if entry["method"] == "6분법")
    assert (six["text"], six["missing_hz"]) == ("42*", [4000])   # (30 + 2×40 + 2×50) / 5
    assert saved["speech"] is None


def test_record_is_plain_json():
    json.dumps(record(aided_result()), ensure_ascii=False)


# ----- saved with the tympanometry results, and read back ---------------------------------------

def test_a_hearing_result_is_saved_as_three_files_and_reads_back_the_same(tmp_path):
    result = aided_result()

    sheet = save_result(result, tmp_path, WHEN, KIM, "메모", tested_at=datetime(2026, 11, 27, 10, 30),
                        clinic=Clinic("바른이비인후과"))

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "20261127_103012_12345.bin", "20261127_103012_12345.jpg", "20261127_103012_12345.json"]
    assert sheet.with_suffix(".bin").read_bytes() == result.raw
    saved = json.loads(sheet.with_suffix(".json").read_text(encoding="utf-8"))
    assert (saved["exam"], saved["format"]) == (EXAM, FORMAT) == ("hearing", 1)
    assert saved["patient"]["chart_no"] == "12345" and saved["note"] == "메모"
    assert saved["tested_at"] == "2026-11-27T10:30:00" and saved["received_at"] == "2026-11-27T10:30:12"
    assert list(saved) == ["format", "exam", "received_at", "tested_at", "clinic", "patient", "note", "remarks",
                           "source", "device", "device_patients", "tests", "tone", "speech", "high_frequency",
                           "tinnitus", "corrections", "capture", "filed"]
    assert (saved["source"], saved["corrections"], saved["remarks"], saved["tinnitus"]) == ("device", [], "", None)
    with Image.open(sheet) as image:
        assert image.size == (2480, 3508)

    loaded = load_result(sheet)

    assert loaded.measurement == result
    assert [part.aided for part in loaded.measurement.parts] == [False, False, True, True]
    assert loaded.patient == KIM and loaded.note == "메모" and loaded.clinic == Clinic("바른이비인후과")


def test_a_hearing_result_saved_again_replaces_its_files(tmp_path):
    result = aided_result()
    first = save_result(result, tmp_path, WHEN)

    again = save_result(result, tmp_path, WHEN, KIM, replacing=first)

    assert again.name == "20261127_103012_12345.jpg"
    assert sorted(path.suffix for path in tmp_path.iterdir()) == [".bin", ".jpg", ".json"]


def test_damaged_raw_data_is_reported(tmp_path):
    sheet = save_result(aided_result(), tmp_path, WHEN)
    raw = bytearray(sheet.with_suffix(".bin").read_bytes())
    raw[40] ^= 0xFF
    sheet.with_suffix(".bin").write_bytes(bytes(raw))

    with pytest.raises(ValueError, match="손상"):
        load_result(sheet)
    with pytest.raises(ValueError, match="검사가 없습니다"):
        from_saved(b"", {})


def test_no_response_is_kept_whatever_masking_was_used():
    result = HearingResult((Part(p.parse_packet(tonal({"Ac R": ({4000: 100}, {4000: SILENT})}))),))
    (point,) = record(result)["tone"]["curves"][0]["points"]
    assert point == {"frequency_hz": 4000, "level_db_hl": 100, "masking_db": None, "masked": False, "no_response": True}
