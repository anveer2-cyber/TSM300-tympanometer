"""The printable result sheet: one A4 portrait page at 300 dpi, saved as a JPEG.

Positions below are millimetres from the top-left corner of the page.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

import matplotlib
from matplotlib import font_manager
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.textpath import TextToPath

from . import __version__
from .clinic import Clinic
from .patient import Patient
from .protocol import (
    REFLEX_FREQUENCIES, REFLEX_POINTS, EarResult, Measurement, tympanogram_pressures,
)

PAGE_WIDTH, PAGE_HEIGHT = 210.0, 297.0
DPI = 300
PIXELS = (2480, 3508)
MARGIN = 15.0
COLUMN_WIDTH = 86.0
RIGHT_COLUMN, LEFT_COLUMN = MARGIN, PAGE_WIDTH - MARGIN - COLUMN_WIDTH
AXES_INSET = 7.0

SURFACE = "#ffffff"
INK = "#191919"
INK_SECONDARY = "#666666"
GRIDLINE = "#a6a6a6"
GRID_DOTS = (0, (1, 3))
AXIS = "#c2c2c2"
ROW_RULE = "#e7e7e7"
RIGHT_EAR = "#ec4647"
LEFT_EAR = "#257de0"

# The box the TSM-300 draws on its own screen. The screen's rows are too coarse to tell 0.2 from 0.25 ml,
# so the lower edge was measured on printouts of the maker's own program (see docs/device-notes.md).
NORMAL_PRESSURE = (-150, 100)
NORMAL_COMPLIANCE = (0.25, 1.5)

# 90% ranges of Margolis & Heller (1987), rounded as in the ASHA (1990) screening guidelines.
# The device gives no range for these two. The children were three to five years old.
ADULT_FROM_AGE = 18

PROBE_TONE_HZ = 226

# The memo box: where it sits, the size of its text, and how many lines of it fit.
NOTE_TOP, NOTE_HEIGHT, NOTE_PADDING = 244.0, 30.0, 3.5
NOTE_FONT_SIZE = 9.5
NOTE_LINES = 5
POINTS_PER_MM = 72 / 25.4
EAR_VOLUME_RANGE = {"adult": (0.6, 1.5), "child": (0.4, 1.0)}
WIDTH_RANGE = {"adult": (50, 110), "child": (60, 150)}

_KOREAN_FONTS = ("Malgun Gothic", "Apple SD Gothic Neo", "NanumGothic", "Noto Sans CJK KR", "Noto Sans KR")

_KOREAN = {
    "title": "임피던스 청력검사 (Tympanometry)",
    "tested_at": "검사일시",
    "chart_no": "차트번호",
    "name": "이름",
    "sex_age": "성별 / 나이",
    "birth_date": "생년월일",
    "age": "만 {age}세",
    "right": "오른쪽 (R)",
    "left": "왼쪽 (L)",
    "pressure_axis": "압력 (daPa)",
    "compliance_axis": "컴플라이언스 (ml)",
    "values": "측정값",
    "item": "항목",
    "reference": "참고 범위",
    "pressure": "중이 압력 (daPa)",
    "volume": "외이도 용적 (ml)",
    "compliance": "컴플라이언스 (ml)",
    "width": "폭 (daPa)",
    "reference_note": ("참고 범위: 중이 압력과 컴플라이언스는 장비 화면의 정상 범위 사각형, "
                       "외이도 용적과 폭은 Margolis & Heller (1987) 기준입니다. 소아는 3~5세 자료입니다."),
    "adult": "성인",
    "child": "소아",
    "reflex": "등골근 반사 · 컴플라이언스 변화 (ml)",
    "probe": "프로브 {hz} Hz",
    "note": "메모",
    "normal_range": "정상 범위",
    "not_measured": "측정 없음",
    "no_reflex": "반사 검사 없음",
    "device": "장비 {name} · 메모리 ID {memory_id}",
    "phone": "전화 {phone}",
}
_ENGLISH = {
    "title": "Tympanometry",
    "tested_at": "Tested",
    "chart_no": "Chart no.",
    "name": "Name",
    "sex_age": "Sex / age",
    "birth_date": "Date of birth",
    "age": "{age} y",
    "right": "Right (R)",
    "left": "Left (L)",
    "pressure_axis": "Pressure (daPa)",
    "compliance_axis": "Compliance (ml)",
    "values": "Values",
    "item": "Item",
    "reference": "Reference",
    "pressure": "Middle ear pressure (daPa)",
    "volume": "Ear volume (ml)",
    "compliance": "Compliance (ml)",
    "width": "Width (daPa)",
    "reference_note": ("Reference: pressure and compliance follow the normal box on the device's screen; "
                       "ear volume and width follow Margolis & Heller (1987), children aged 3 to 5."),
    "adult": "Adult",
    "child": "Child",
    "reflex": "Acoustic reflex · compliance change (ml)",
    "probe": "Probe {hz} Hz",
    "note": "Notes",
    "normal_range": "Normal range",
    "not_measured": "Not measured",
    "no_reflex": "No reflex test",
    "device": "Device {name} · memory ID {memory_id}",
    "phone": "Tel. {phone}",
}
_SEX_IN_ENGLISH = {"남": "M", "여": "F"}


@lru_cache(maxsize=1)
def _font_and_labels() -> tuple[list[str], dict[str, str]]:
    """Korean labels when a Korean font is installed, English otherwise."""
    installed = set(font_manager.get_font_names())
    for name in _KOREAN_FONTS:
        if name in installed:
            return [name, "DejaVu Sans"], _KOREAN
    return ["DejaVu Sans"], _ENGLISH


def prepare() -> None:
    """Does the slow one-off font lookup now, so the first result is not held up by it."""
    _font_and_labels()


def wrap_note(text: str) -> tuple[list[str], bool]:
    """Breaks a memo into the lines that fit the sheet's memo box.

    Returns the lines and whether some of the memo did not fit. Lines break at spaces where
    they can; Hangul has few, so a line also breaks inside a word when it must. Widths are
    measured in the sheet's own font, since a row of W is nearly twice as wide as a row of i.
    """
    room = (PAGE_WIDTH - 2 * MARGIN - 2 * NOTE_PADDING) * POINTS_PER_MM
    wrapped: list[str] = []
    for paragraph in text.strip().splitlines():
        line, width, last_space = "", 0.0, -1
        for character in paragraph.rstrip():
            size = _character_width(character)
            if width + size > room:
                if character != " " and last_space > 0:
                    wrapped.append(line[:last_space])
                    line = line[last_space + 1:]
                else:
                    wrapped.append(line)
                    line = ""
                width = sum(_character_width(kept) for kept in line)
                last_space = -1
                if character == " ":
                    continue
            if character == " ":
                last_space = len(line)
            line += character
            width += size
        wrapped.append(line)
    return wrapped[:NOTE_LINES], len(wrapped) > NOTE_LINES


@lru_cache(maxsize=None)
def _character_width(character: str) -> float:
    """The advance of one character in the memo's font, in points."""
    fonts, _ = _font_and_labels()
    properties = font_manager.FontProperties(family=fonts, size=NOTE_FONT_SIZE)
    return TextToPath().get_text_width_height_descent(character, properties, False)[0]


def fitted(text: str, size: float, room: float, weight: str = "normal", smallest: float | None = None) -> tuple[str, float]:
    """The text and the type size at which it fits in ``room`` millimetres.

    Type is made smaller first, down to ``smallest`` points; text that is still too long is
    cut and ends in an ellipsis. Whatever a clinic or a patient is called, nothing on the
    sheet may run into what is printed beside it.
    """
    text = " ".join(text.split())
    if not text:
        return "", size
    smallest = size if smallest is None else smallest
    while size > smallest and _text_width(text, size, weight) > room:
        size = max(smallest, size - 0.5)
    if _text_width(text, size, weight) <= room:
        return text, size
    while text and _text_width(text + "…", size, weight) > room:
        text = text[:-1]
    return text.rstrip() + "…", size


def fitted_lines(text: str, size: float, room: float, weight: str = "normal",
                 smallest: float | None = None) -> tuple[list[str], float]:
    """Like ``fitted``, but text too long even in the smallest type goes onto a second line
    before any of it is cut. A patient's name is printed whole wherever that is possible."""
    whole = " ".join(text.split())
    one, one_size = fitted(whole, size, room, weight, smallest)
    if one == whole:
        return [one], one_size
    first = whole
    while first and _text_width(first, one_size, weight) > room:
        first = first[:-1]
    if " " in first and whole[len(first)] != " ":
        between_words = first[:first.rindex(" ")]
        if _text_width(whole[len(between_words):].strip(), one_size, weight) <= room:
            first = between_words  # only when the rest then fits: a cut name is worse than a split word
    second, _ = fitted(whole[len(first):], one_size, room, weight)
    return [first.rstrip(), second], one_size


def _text_width(text: str, size: float, weight: str = "normal") -> float:
    """The width of ``text`` on the sheet, in millimetres."""
    fonts, _ = _font_and_labels()
    properties = font_manager.FontProperties(family=fonts, size=size, weight=weight)
    return TextToPath().get_text_width_height_descent(text, properties, False)[0] / POINTS_PER_MM


def render(measurement: Measurement, path: Path, tested_at: datetime,
           patient: Patient = Patient(), note: str = "", clinic: Clinic = Clinic()) -> None:
    """Writes the result sheet to ``path``; the file type follows its suffix (.jpg or .png)."""
    fonts, labels = _font_and_labels()
    with matplotlib.rc_context({"font.family": fonts, "axes.unicode_minus": False}):
        figure = Figure(figsize=(PIXELS[0] / DPI, PIXELS[1] / DPI), dpi=DPI, facecolor=SURFACE)
        _draw_header(figure, labels, clinic, tested_at)
        _draw_patient(figure, labels, patient, tested_at)
        _draw_tympanograms(figure, labels, measurement)
        _draw_reflexes(figure, labels, measurement)
        _draw_values(figure, labels, measurement, patient.age_on(tested_at.date()))
        _draw_note(figure, labels, note)
        _draw_footer(figure, labels, labels["device"].format(name=measurement.device_name, memory_id=measurement.memory_id),
                     clinic)
        options = {"quality": 95, "subsampling": 0} if Path(path).suffix.lower() in (".jpg", ".jpeg") else {}
        figure.savefig(path, dpi=DPI, pil_kwargs=options)


def _draw_header(figure: Figure, labels: dict, clinic: Clinic, tested_at: datetime) -> None:
    when = f"{tested_at:%Y-%m-%d %H:%M}"
    room = PAGE_WIDTH - 2 * MARGIN - _text_width(when, 12, "bold") - 8
    name, size = fitted(_printable(clinic.name, labels), 11, room, "bold", smallest=8)
    _text(figure, MARGIN, 17, name, size=size, weight="bold")
    _text(figure, MARGIN, 25.5, labels["title"], size=18, weight="bold")
    _text(figure, PAGE_WIDTH - MARGIN, 17, labels["tested_at"], size=8.5, color=INK_SECONDARY, ha="right")
    _text(figure, PAGE_WIDTH - MARGIN, 25.5, when, size=12, weight="bold", ha="right")
    _rule(figure, 31.5, color=INK, width=0.8)


def _draw_patient(figure: Figure, labels: dict, patient: Patient, tested_at: datetime) -> None:
    age = patient.age_on(tested_at.date())
    sex = patient.sex if labels is _KOREAN else _SEX_IN_ENGLISH.get(patient.sex, "")
    sex_age = " / ".join(part for part in (sex, labels["age"].format(age=age) if age is not None else "") if part)
    fields = [
        (MARGIN, labels["chart_no"], patient.chart_no),
        (62.0, labels["name"], patient.name),
        (109.0, labels["sex_age"], sex_age),
        (156.0, labels["birth_date"], patient.birth_date.isoformat() if patient.birth_date else ""),
    ]
    edges = [x for x, _, _ in fields[1:]] + [PAGE_WIDTH - MARGIN]
    for (x, label, value), next_x in zip(fields, edges):
        _text(figure, x, 38.5, label, size=8.5, color=INK_SECONDARY)
        if not value:
            _rule(figure, 48, x, x + 36)
            continue
        lines, size = fitted_lines(value, 13, next_x - x - 3, "bold", smallest=9)
        baselines = (46.0,) if len(lines) == 1 else (44.3, 48.6)
        for line, y in zip(lines, baselines):
            _text(figure, x, y, line, size=size, weight="bold")
    _rule(figure, 52.5)


def _draw_tympanograms(figure: Figure, labels: dict, measurement: Measurement) -> None:
    for x, ear, name, color in _columns(labels, measurement):
        _line(figure, x, x + 6, 60.7, color=color, width=2.4)
        _text(figure, x + 8.5, 62, name, size=12, weight="bold")
        if ear is None:
            _text(figure, x + COLUMN_WIDTH / 2, 100, labels["not_measured"], size=11, color=INK_SECONDARY, ha="center")
            continue
        _text(figure, x + AXES_INSET, 68.5, labels["compliance_axis"], size=8.5, color=INK_SECONDARY)
        _text(figure, x + COLUMN_WIDTH - 1, 68.5, labels["probe"].format(hz=PROBE_TONE_HZ), size=8.5,
              color=INK_SECONDARY, ha="right")
        axes = _axes(figure, x + AXES_INSET, 71, COLUMN_WIDTH - AXES_INSET - 1, 58)
        _plot_tympanogram(axes, ear, color, labels)


def _plot_tympanogram(axes, ear: EarResult, color: str, labels: dict) -> None:
    pressures = tympanogram_pressures()
    curve = ear.compliance_curve
    top = 3.0 if max(curve) <= 3.0 else 3.5
    axes.set_xlim(-300, 200)
    axes.set_ylim(-0.5, top)
    axes.set_xticks(range(-300, 201, 100))
    axes.set_yticks(range(0, int(top) + 1))
    _style_axes(axes, grid="both")
    axes.axhline(0, color=AXIS, linewidth=0.7, zorder=1)
    (left, right), (bottom, upper) = NORMAL_PRESSURE, NORMAL_COMPLIANCE
    axes.add_patch(Rectangle(
        (left, bottom), right - left, upper - bottom, zorder=1.5,
        facecolor=(0.1, 0.1, 0.1, 0.04), edgecolor=AXIS, linewidth=0.7,
    ))
    axes.text(left + 6, upper - 0.06, labels["normal_range"], fontsize=7.5, color=INK_SECONDARY, va="top")
    axes.fill_between(pressures, curve, 0, color=color, alpha=0.10, linewidth=0, zorder=2)
    axes.plot(pressures, curve, color=color, linewidth=1.6, solid_capstyle="round", solid_joinstyle="round", zorder=3)
    axes.plot(
        [ear.middle_ear_pressure], [ear.compliance], marker="o", markersize=6, zorder=4,
        markerfacecolor=color, markeredgecolor=SURFACE, markeredgewidth=1.2, clip_on=False,
    )
    axes.set_xlabel(labels["pressure_axis"], fontsize=8.5, color=INK_SECONDARY, labelpad=3)


def _draw_values(figure: Figure, labels: dict, measurement: Measurement, age: int | None) -> None:
    right_x, left_x, reference_x = 96.0, 127.0, 169.0
    _text(figure, MARGIN, 181, labels["values"], size=11, weight="bold")
    _text(figure, MARGIN, 189, labels["item"], size=9, color=INK_SECONDARY)
    for x, name, color in ((right_x, labels["right"], RIGHT_EAR), (left_x, labels["left"], LEFT_EAR)):
        _text(figure, x, 189, name, size=9, color=INK_SECONDARY, ha="center")
        _line(figure, x - 9, x + 9, 191.6, color=color, width=1.6)
    _text(figure, reference_x, 189, labels["reference"], size=9, color=INK_SECONDARY, ha="center")
    _rule(figure, 193)
    low_pressure, high_pressure = NORMAL_PRESSURE
    low_compliance, high_compliance = NORMAL_COMPLIANCE
    rows = [
        (labels["pressure"], lambda ear: f"{ear.middle_ear_pressure:d}", f"{low_pressure} ~ +{high_pressure}"),
        (labels["volume"], lambda ear: f"{ear.ear_volume:.2f}", _by_age(EAR_VOLUME_RANGE, age, labels)),
        (labels["compliance"], lambda ear: f"{ear.compliance:.2f}", f"{low_compliance} ~ {high_compliance}"),
        (labels["width"], lambda ear: f"{ear.width:d}", _by_age(WIDTH_RANGE, age, labels)),
    ]
    for index, (label, value_of, reference) in enumerate(rows):
        y = 199 + index * 8
        _text(figure, MARGIN, y, label, size=10)
        for x, ear in ((right_x, measurement.right), (left_x, measurement.left)):
            _text(figure, x, y, value_of(ear) if ear else "-", size=12, weight="bold", ha="center")
        _text(figure, reference_x, y, reference, size=9, color=INK_SECONDARY, ha="center")
        _rule(figure, y + 3.3, color=ROW_RULE)
    _text(figure, MARGIN, 231.5, labels["reference_note"], size=7, color=INK_SECONDARY)


def _by_age(ranges: dict, age: int | None, labels: dict) -> str:
    """The adult range for an adult; both ranges for a child or when the age is unknown."""
    groups = ("adult",) if age is not None and age >= ADULT_FROM_AGE else ("adult", "child")
    return " · ".join(f"{labels[group]} {ranges[group][0]} ~ {ranges[group][1]}" for group in groups)


def _draw_reflexes(figure: Figure, labels: dict, measurement: Measurement) -> None:
    """A strip of five traces under each tympanogram. A reflex lowers compliance, so the scale runs
    from 0 at the bottom to -0.2 ml at the top, as on the device's own screen."""
    span = COLUMN_WIDTH - AXES_INSET - 1
    gap = 2.5
    panel = (span - gap * (len(REFLEX_FREQUENCIES) - 1)) / len(REFLEX_FREQUENCIES)
    points = range(REFLEX_POINTS)
    for x, ear, _, color in _columns(labels, measurement):
        if ear is None:
            continue
        _text(figure, x + AXES_INSET, 148.5, labels["reflex"], size=8.5, color=INK_SECONDARY)
        if not ear.reflexes:
            _text(figure, x + COLUMN_WIDTH / 2, 161, labels["no_reflex"], size=10, color=INK_SECONDARY, ha="center")
            continue
        for index, frequency in enumerate(REFLEX_FREQUENCIES):
            axes = _axes(figure, x + AXES_INSET + index * (panel + gap), 151, panel, 17)
            axes.set_xlim(0, REFLEX_POINTS - 1)
            axes.set_ylim(0, 0.2)
            axes.set_xticks([])
            axes.set_yticks([0, 0.1, 0.2])
            axes.set_yticklabels(["0", "-0.1", "-0.2"])
            _style_axes(axes)
            if index > 0:
                axes.tick_params(labelleft=False)
            axes.set_xlabel(f"{frequency} Hz", fontsize=8, color=INK_SECONDARY, labelpad=3)
            if frequency in ear.reflexes:
                curve = ear.reflex_curve(frequency)
                axes.fill_between(points, curve, 0, color=color, alpha=0.10, linewidth=0, zorder=2)
                axes.plot(points, curve, color=color, linewidth=1.3, solid_capstyle="round",
                          solid_joinstyle="round", zorder=3, clip_on=False)


def _draw_note(figure: Figure, labels: dict, note: str) -> None:
    """The memo box. It is drawn even when empty, so there is room to write on a printed sheet."""
    _text(figure, MARGIN, NOTE_TOP - 3, labels["note"], size=11, weight="bold")
    figure.add_artist(Rectangle(
        (MARGIN / PAGE_WIDTH, 1 - (NOTE_TOP + NOTE_HEIGHT) / PAGE_HEIGHT), (PAGE_WIDTH - 2 * MARGIN) / PAGE_WIDTH,
        NOTE_HEIGHT / PAGE_HEIGHT, transform=figure.transFigure, facecolor="none", edgecolor=AXIS, linewidth=0.6,
    ))
    lines, _ = wrap_note(note)
    for index, line in enumerate(lines):
        _text(figure, MARGIN + NOTE_PADDING, NOTE_TOP + 6.3 + index * 5.2, line, size=NOTE_FONT_SIZE)


def _draw_footer(figure: Figure, labels: dict, device: str, clinic: Clinic) -> None:
    """The clinic's address and telephone on the left; the device and this program on the right."""
    _rule(figure, 279)
    source = f"{device} · TSM300 v{__version__}"
    _text(figure, PAGE_WIDTH - MARGIN, 284, source, size=8, color=INK_SECONDARY, ha="right")
    address, phone = _printable(clinic.address, labels), _printable(clinic.phone, labels)
    contact = " · ".join(part for part in (address, labels["phone"].format(phone=phone) if phone else "") if part)
    if contact:
        room = PAGE_WIDTH - 2 * MARGIN - _text_width(source, 8) - 8
        contact, size = fitted(contact, 8, room, smallest=6.5)
        _text(figure, MARGIN, 284, contact, size=size, color=INK_SECONDARY)


def _printable(text: str, labels: dict) -> str:
    """The clinic's own words, left out on a PC with no Korean font when they would print as empty boxes."""
    return text if labels is _KOREAN or text.isascii() else ""


def _columns(labels: dict, measurement: Measurement):
    """The right ear goes on the left of the page, as on an audiogram."""
    return (
        (RIGHT_COLUMN, measurement.right, labels["right"], RIGHT_EAR),
        (LEFT_COLUMN, measurement.left, labels["left"], LEFT_EAR),
    )


def _style_axes(axes, grid: str = "y") -> None:
    axes.set_facecolor(SURFACE)
    axes.grid(True, axis=grid, color=GRIDLINE, linewidth=0.7, linestyle=GRID_DOTS)
    axes.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(AXIS)
    axes.spines["bottom"].set_linewidth(0.7)
    axes.tick_params(colors=INK_SECONDARY, length=0, labelsize=8.5, pad=3)


def _axes(figure: Figure, left: float, top: float, width: float, height: float):
    return figure.add_axes([
        left / PAGE_WIDTH, 1 - (top + height) / PAGE_HEIGHT, width / PAGE_WIDTH, height / PAGE_HEIGHT,
    ])


def _text(figure: Figure, x: float, y: float, text: str, size: float, color: str = INK,
          weight: str = "normal", ha: str = "left") -> None:
    figure.text(x / PAGE_WIDTH, 1 - y / PAGE_HEIGHT, text, fontsize=size, color=color,
                fontweight=weight, ha=ha, va="baseline")


def _line(figure: Figure, x0: float, x1: float, y: float, color: str, width: float) -> None:
    figure.add_artist(Line2D(
        [x0 / PAGE_WIDTH, x1 / PAGE_WIDTH], [1 - y / PAGE_HEIGHT] * 2,
        color=color, linewidth=width, solid_capstyle="round",
    ))


def _rule(figure: Figure, y: float, x0: float = MARGIN, x1: float = PAGE_WIDTH - MARGIN,
          color: str = AXIS, width: float = 0.6) -> None:
    figure.add_artist(Line2D(
        [x0 / PAGE_WIDTH, x1 / PAGE_WIDTH], [1 - y / PAGE_HEIGHT] * 2, color=color, linewidth=width,
    ))
