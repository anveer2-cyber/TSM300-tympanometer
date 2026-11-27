"""The hearing result sheet as a file."""

from datetime import date, datetime

from matplotlib.figure import Figure
from PIL import Image

from tsm300.clinic import Clinic
from tsm300.hearing import protocol as p
from tsm300.hearing import sheet
from tsm300.hearing.record import HearingResult, Part
from tsm300.hearing.samples import DEMO_SERIAL, hf, practice_memory, speech, tonal
from tsm300.patient import Patient
from tsm300.report import _font_and_labels

WHEN = datetime(2026, 11, 27, 10, 30)
HONG = Patient("00512", "홍길동", "남", date(1960, 1, 2))


def practice_result():
    patient_packet, tests = practice_memory().groups[-1]
    patient = p.device_patient(p.parse_frame(patient_packet))
    decoded = [p.parse_packet(test) for test in tests]
    return HearingResult(tuple(Part(test, patient, aided=index >= 2) for index, test in enumerate(decoded)))


def everything_at_once():
    """More than any sheet should ever hold: every row, every masking row, speech and high frequencies."""
    every = {name: ({250: 40, 500: 45, 1000: 55, 2000: 65, 4000: 75, 8000: 85}, {500: 60, 1000: 65})
             for name in ("Ac R", "Bc R", "Spkr R", "SpkrA R", "AcU R", "Ac L", "Bc L", "Spkr L", "SpkrA L", "AcU L")}
    every["AcM R"] = every["AcM L"] = ({1000: 70}, {1000: 50})
    tone = p.parse_packet(tonal(every, serial=DEMO_SERIAL, with_mcl=True))
    words = p.parse_packet(speech({"Ac R": [(60, 44, 50)], "Ac L": [(60, 44, 50)]}, srt={"Ac R": 55},
                                  serial=DEMO_SERIAL, long_form=True, answers=True, correct={"Ac R": [11]}))
    high = p.parse_packet(hf({"Ac R": ({8000: 20, 16000: 70}, {8000: 50}), "Spkr R": ({8000: 20}, {}),
                              "Ac L": ({8000: 15}, {})}, serial=DEMO_SERIAL))
    return HearingResult((Part(tone), Part(words), Part(high)))


def test_sheet_is_an_upright_a4_jpeg_at_print_resolution(tmp_path):
    path = tmp_path / "sheet.jpg"

    sheet.render(practice_result(), path, WHEN, HONG, "보청기 착용 전 · 후", Clinic("바른이비인후과", "부산", "051-000-0000"))

    assert path.read_bytes()[:2] == b"\xff\xd8"
    with Image.open(path) as image:
        assert image.format == "JPEG" and image.size == (2480, 3508)
        assert tuple(round(value) for value in image.info["dpi"]) == (300, 300)


def test_sheet_can_be_drawn_without_patient_or_clinic(tmp_path):
    path = tmp_path / "sheet.png"
    sheet.render(HearingResult((Part(p.parse_packet(hf({"Ac R": ({8000: 20}, {})}))),)), path, WHEN)
    with Image.open(path) as image:
        assert image.size == (2480, 3508)


def test_everything_a_sheet_can_hold_stays_clear_of_the_memo_box():
    fonts, page = _font_and_labels()
    labels = sheet._KO if page is sheet._KOREAN else sheet._EN
    result = everything_at_once()

    ends = [sheet._draw_results(Figure(), labels, page, result, layout) for layout in sheet.LAYOUTS]

    assert ends[0] > sheet.CONTENT_BOTTOM           # too much for the usual sizes,
    assert ends[-1] <= sheet.CONTENT_BOTTOM         # so the smallest ones are used
    assert sheet._draw_results(Figure(), labels, page, practice_result(), sheet.LAYOUTS[0]) <= sheet.CONTENT_BOTTOM


def test_heading_names_the_tests_on_the_sheet():
    soundfield_only = HearingResult((Part(p.parse_packet(tonal({"Spkr R": ({500: 40}, {})}))),))

    assert sheet.title(practice_result()) == "순음청력검사 · 음장검사 · 어음청력검사"
    assert sheet.title(soundfield_only) == "음장검사"
    assert sheet.title(everything_at_once()) == "순음청력검사 · 음장검사 · 어음청력검사 · 고주파 청력검사"


def test_symbols_follow_asha():
    assert sheet._glyph("Ac", "R", False) == ("marker", "o")
    assert sheet._glyph("Ac", "L", False) == ("marker", "x")
    assert sheet._glyph("Ac", "R", True) == ("marker", "^")
    assert sheet._glyph("Ac", "L", True) == ("marker", "s")
    assert sheet._glyph("Bc", "R", False) == ("text", "<")
    assert sheet._glyph("Bc", "L", True) == ("text", "]")
    assert sheet._glyph("Spkr", "R", False) == ("text", "S")
    assert sheet._glyph("SpkrA", "L", False) == ("text", "A")
