"""The result sheet as a file: what 상쾌한캡처 and a printer will receive."""

from datetime import date, datetime
from pathlib import Path

from PIL import Image

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
