"""The result sheet as a file: what 상쾌한캡처 and a printer will receive."""

from datetime import date, datetime
from pathlib import Path

from PIL import Image

from tsm300.clinic import Clinic
from tsm300.patient import Patient
from tsm300.protocol import parse_message
from tsm300.report import render

BOTH = parse_message((Path(__file__).parent / "data" / "both_ears.bin").read_bytes())
ORIENTATION = 0x0112


def test_sheet_is_an_upright_a4_jpeg_at_print_resolution(tmp_path):
    sheet = tmp_path / "sheet.jpg"

    render(BOTH, sheet, datetime(2026, 11, 26, 21, 54), Patient("00512", "홍길동", "남", date(1975, 3, 4)))

    assert sheet.read_bytes()[:2] == b"\xff\xd8"
    with Image.open(sheet) as image:
        assert image.format == "JPEG" and image.mode == "RGB"
        assert image.size == (2480, 3508)
        assert tuple(round(value) for value in image.info["dpi"]) == (300, 300)
        # 상쾌한캡처 keeps the file byte for byte only when it carries no rotation tag.
        assert image.getexif().get(ORIENTATION) in (None, 1)
    assert sheet.stat().st_size < 3_000_000


def test_sheet_can_be_drawn_without_any_patient_details(tmp_path):
    sheet = tmp_path / "sheet.jpg"

    render(BOTH, sheet, datetime(2026, 11, 26, 21, 54))

    with Image.open(sheet) as image:
        assert image.size == (2480, 3508)


def test_memo_lines_break_at_spaces_and_never_run_past_the_box():
    from tsm300.report import MARGIN, NOTE_PADDING, PAGE_WIDTH, POINTS_PER_MM, _character_width, wrap_note

    room = (PAGE_WIDTH - 2 * MARGIN - 2 * NOTE_PADDING) * POINTS_PER_MM
    hangul = "오른쪽 프로브 밀착이 처음에 잘 안 되어 두 번째 측정값입니다. " * 3
    lines, cut = wrap_note(hangul + "\n" + "W" * 200 + "\n" + "가" * 150)

    assert cut and len(lines) == 5
    assert all(sum(_character_width(character) for character in line) <= room for line in lines)
    assert lines[0].endswith(("니다.", "측정값입니다.")) or " " in lines[0]
    assert not lines[0].endswith(" ") and not lines[1].startswith(" ")


def test_short_and_empty_memos_fit():
    from tsm300.report import wrap_note

    assert wrap_note("") == ([], False)
    assert wrap_note("  협조 양호  ") == (["협조 양호"], False)
    assert wrap_note("첫 줄\n\n셋째 줄") == (["첫 줄", "", "셋째 줄"], False)


def test_memo_is_printed_on_the_sheet(tmp_path):
    plain, noted = tmp_path / "plain.jpg", tmp_path / "noted.jpg"

    render(BOTH, plain, datetime(2026, 11, 26, 21, 54))
    render(BOTH, noted, datetime(2026, 11, 26, 21, 54), note="오른쪽 프로브 밀착이 어려웠음")

    with Image.open(plain) as before, Image.open(noted) as after:
        box = (0, int(3508 * 244 / 297), 2480, int(3508 * 275 / 297))
        assert before.crop(box).tobytes() != after.crop(box).tobytes()
        above = (0, 0, 2480, int(3508 * 236 / 297))
        assert before.crop(above).tobytes() == after.crop(above).tobytes()


WHEN = datetime(2026, 11, 26, 21, 54)
HONG = Patient("00512", "홍길동", "남", date(1975, 3, 4))


def band(top_mm, bottom_mm, left_mm=0.0, right_mm=210.0):
    """The pixels of a strip of the page given in millimetres."""
    scale = 3508 / 297
    return (int(left_mm * scale), int(top_mm * scale), int(right_mm * scale), int(bottom_mm * scale))


def same(one, other, box):
    with Image.open(one) as first, Image.open(other) as second:
        return first.crop(box).tobytes() == second.crop(box).tobytes()


# The clinic and the long names below are written in Latin letters on purpose: these tests then
# measure and print the same way whether or not the machine has a Korean font.
BARUN = Clinic("Barun ENT Clinic", "12 Gana-ro, Haeundae-gu, Busan", "051-123-4567")


def test_clinic_is_printed_at_the_top_and_its_address_and_telephone_at_the_bottom(tmp_path):
    bare, headed = tmp_path / "bare.jpg", tmp_path / "headed.jpg"

    render(BOTH, bare, WHEN, HONG)
    render(BOTH, headed, WHEN, HONG, clinic=BARUN)

    assert not same(bare, headed, band(11, 19))       # the clinic's name
    assert not same(bare, headed, band(280, 287))     # its address and telephone
    assert same(bare, headed, band(20, 278))          # nothing else moves


def test_the_corrected_test_time_is_what_the_sheet_shows(tmp_path):
    arrived, corrected = tmp_path / "arrived.jpg", tmp_path / "corrected.jpg"

    render(BOTH, arrived, WHEN, HONG)
    render(BOTH, corrected, datetime(2026, 11, 25, 14, 30), HONG)

    assert not same(arrived, corrected, band(20, 28, left_mm=150))
    assert same(arrived, corrected, band(33, 297))


def test_text_too_long_for_its_place_is_made_smaller_and_then_cut():
    from tsm300.report import _text_width, fitted

    name = "ALEXANDER MAXIMILIAN"
    wide = _text_width(name, 13, "bold")
    assert fitted(name, 13, wide + 1, "bold", smallest=9) == (name, 13)
    assert fitted("  two   spaced  words ", 13, 200) == ("two spaced words", 13)
    assert fitted("", 13, 44) == ("", 13)

    assert fitted(name, 13, _text_width(name, 11, "bold"), "bold", smallest=9) == (name, 11)   # smaller, still whole

    text, size = fitted(name * 5, 13, wide, "bold", smallest=9)                                # cut only at the smallest
    assert size == 9 and text.endswith("…") and name.startswith(text[:5])
    assert _text_width(text, size, "bold") <= wide


def test_a_name_too_long_for_one_line_is_printed_whole_on_two():
    from tsm300.report import _text_width, fitted_lines

    def width(text):
        return _text_width(text, 9, "bold")

    assert fitted_lines("HONG", 13, 44, "bold", smallest=9) == (["HONG"], 13)

    first, second = "ALEXANDER MAXIMILIAN", "CHRISTOPHER JOHNSON"
    room = max(width(first), width(second)) + 0.1
    assert fitted_lines(f"{first} {second}", 13, room, "bold", smallest=9) == ([first, second], 9)

    name = "Muhammad Abdurrahmanovich Al-Khwarizmi"      # a short first word must not push the rest into being cut
    room = width(name) * 0.55
    lines, size = fitted_lines(name, 13, room, "bold", smallest=9)
    assert len(lines) == 2 and "".join(lines).replace(" ", "") == name.replace(" ", "")
    assert all(width(line) <= room for line in lines)

    lines, size = fitted_lines("A" * 300, 13, 44, "bold", smallest=9)                # only what two lines cannot hold is cut
    assert len(lines) == 2 and lines[1].endswith("…") and set("".join(lines)) == {"A", "…"}
    assert all(width(line) <= 44 for line in lines)


def test_a_very_long_name_stays_out_of_the_columns_beside_it(tmp_path):
    usual, long = tmp_path / "usual.jpg", tmp_path / "long.jpg"
    endless = Clinic("Barun ENT Clinic " * 30, "12 Gana-ro, Haeundae-gu, Busan " * 30, "051-123-4567 " * 10)

    render(BOTH, usual, WHEN, HONG, clinic=Clinic("Barun ENT Clinic"))
    render(BOTH, long, WHEN, Patient("00512", "ALEXANDER MAXIMILIAN CHRISTOPHER " * 4, "남", date(1975, 3, 4)), clinic=endless)

    assert same(usual, long, band(33, 51, left_mm=108))          # sex, age and birth date beside the name
    assert same(usual, long, band(11, 28, left_mm=155))          # the test time beside the clinic's name
    assert same(usual, long, band(280, 287, left_mm=140))        # the device and program beside the address
    assert same(usual, long, band(53, 278))


def test_clinic_words_that_would_print_as_empty_boxes_are_left_out():
    from tsm300.report import _ENGLISH, _KOREAN, _printable

    assert _printable("바른이비인후과의원", _KOREAN) == "바른이비인후과의원"
    assert _printable("바른이비인후과의원", _ENGLISH) == ""
    assert _printable("Barun ENT Clinic, 051-123-4567", _ENGLISH) == "Barun ENT Clinic, 051-123-4567"
