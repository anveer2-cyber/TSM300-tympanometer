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
