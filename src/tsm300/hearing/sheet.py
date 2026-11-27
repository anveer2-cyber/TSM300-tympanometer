"""The pure tone, soundfield and speech audiometry sheet: one A4 portrait page at 300 dpi.

It shares the tympanometry sheet's header, patient row, memo box and footer (``report``), so
that the two look like one set. The audiograms use the symbols of ASHA (1990):

            오른쪽(빨강)   왼쪽(파랑)
    기도      O             X
    기도 차폐  △             □
    골도      <             >
    골도 차폐  [             ]
    음장      S             S      (보청기 착용: A)
    불쾌역치   U             U      쾌적역치 M
    무반응    기호 아래로 화살표

Below them, a table per ear gives every threshold with its masking, and the 6분법 average as
the clinic's former program printed it: decimals dropped (see ``pta``). Positions are
millimetres from the top-left corner of the page.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib
from matplotlib.figure import Figure

from ..clinic import Clinic
from ..patient import Patient
from ..report import (
    _KOREAN, AXIS, COLUMN_WIDTH, DPI, GRIDLINE, INK, INK_SECONDARY, LEFT_COLUMN, LEFT_EAR, MARGIN, NOTE_TOP,
    PAGE_WIDTH, PIXELS, RIGHT_COLUMN, RIGHT_EAR, ROW_RULE, SURFACE, _axes, _draw_footer, _draw_note, _draw_patient,
    _font_and_labels, _line, _printable, _rule, _text, _text_width, fitted,
)
from .protocol import HF_FREQUENCIES
from .record import AVERAGED, HearingResult, Placed

SOUNDFIELD = "#3a3a3a"
NORMAL_BAND = "#f3f6f1"
GRAPH_TICKS = (125, 250, 500, 1000, 2000, 4000, 8000)
GRAPH_IN_BETWEEN = (750, 1500, 3000, 6000)
TABLE_FREQUENCIES = (250, 500, 750, 1000, 1500, 2000, 3000, 4000, 6000, 8000)
DB_RANGE = (-10, 120)
NORMAL_LIMIT = 25
BONE_OFFSET = 1.13
"""Bone-conduction symbols sit beside the frequency line, outside the air symbol (ASHA)."""

CONTENT_BOTTOM = NOTE_TOP - 8
"""Where the results must end, to leave the memo box (shared with the tympanometry sheet) its place."""

ROWS = ("Ac", "Bc", "Spkr", "SpkrA", "AcU", "AcM")
SPEECH_COLUMNS = ("Ac", "Bc", "Spkr", "SpkrA")
LABEL_WIDTH = 15.0
AVERAGE_WIDTH = 13.0

# Sizes to fall back on, one after the other, when everything a sheet holds does not fit at once.
LAYOUTS = (
    {"audiogram": 72.0, "row": 5.2, "mask": 4.2, "speech_row": 5.4, "gap": 6.0},
    {"audiogram": 64.0, "row": 4.8, "mask": 3.8, "speech_row": 5.0, "gap": 5.0},
    {"audiogram": 56.0, "row": 4.4, "mask": 3.4, "speech_row": 4.6, "gap": 4.0},
    {"audiogram": 50.0, "row": 4.0, "mask": 3.0, "speech_row": 4.2, "gap": 3.0},
)

_KO = {
    "tone": "순음청력검사", "soundfield": "음장검사", "speech": "어음청력검사", "high_frequency": "고주파 청력검사",
    "hearing": "청력검사",
    "rows": {"Ac": "기도", "Bc": "골도", "Spkr": "음장", "SpkrA": "음장 보청기", "AcU": "불쾌역치", "AcM": "쾌적역치"},
    "masking": "차폐",
    "average": "평균(6분법)",
    "average_note": ("평균 = 6분법 (500 + 2×1k + 2×2k + 4k) ÷ 6, 소수점 아래 버림  ·  ↓ 무반응  ·  "
                     "↑ 무반응 주파수가 든 평균(그 이상)  ·  * 검사하지 않은 주파수는 빼고 평균"),
    "speech_rows": ("SRT (dB HL)", "최고 명료도 (%)", "그때 강도 (dB HL)", "정답 / 단어", "차폐 (dB HL)"),
    "high_frequency_title": "고주파 청력 (dB HL)",
    "device": "장비 MRS Resonance {serials}",
}
_EN = {
    "tone": "Pure tone audiometry", "soundfield": "Soundfield", "speech": "Speech audiometry",
    "high_frequency": "High frequencies", "hearing": "Audiometry",
    "rows": {"Ac": "Air", "Bc": "Bone", "Spkr": "Soundfield", "SpkrA": "SF aided", "AcU": "UCL", "AcM": "MCL"},
    "masking": "Mask",
    "average": "PTA (6)",
    "average_note": ("PTA = (500 + 2×1k + 2×2k + 4k) / 6, decimals dropped  ·  ↓ no response  ·  "
                     "↑ includes a no-response frequency (this or worse)  ·  * untested frequencies left out"),
    "speech_rows": ("SRT (dB HL)", "Best score (%)", "At (dB HL)", "Correct / words", "Masking (dB HL)"),
    "high_frequency_title": "High frequencies (dB HL)",
    "device": "Device MRS Resonance {serials}",
}


def render(result: HearingResult, path: Path, tested_at: datetime, patient: Patient = Patient(),
           note: str = "", clinic: Clinic = Clinic()) -> None:
    """Writes the sheet to ``path``; the file type follows its suffix (.jpg or .png)."""
    fonts, page = _font_and_labels()
    labels = _KO if page is _KOREAN else _EN
    with matplotlib.rc_context({"font.family": fonts, "axes.unicode_minus": False}):
        for layout in LAYOUTS:
            figure = Figure(figsize=(PIXELS[0] / DPI, PIXELS[1] / DPI), dpi=DPI, facecolor=SURFACE)
            if _draw_results(figure, labels, page, result, layout) <= CONTENT_BOTTOM:
                break
        _draw_header(figure, page, clinic, tested_at, title(result, labels))
        _draw_patient(figure, page, patient, tested_at)
        _draw_note(figure, page, note)
        _draw_footer(figure, page, labels["device"].format(serials=", ".join(result.serials)), clinic)
        options = {"quality": 95, "subsampling": 0} if Path(path).suffix.lower() in (".jpg", ".jpeg") else {}
        figure.savefig(path, dpi=DPI, pil_kwargs=options)


def title(result: HearingResult, labels: dict = _KO) -> str:
    """What the sheet is headed: the tests it holds."""
    parts = []
    if any(not kind.startswith("Spkr") for kind, _ in result.tone):
        parts.append(labels["tone"])
    if result.has_soundfield:
        parts.append(labels["soundfield"])
    if result.has_speech:
        parts.append(labels["speech"])
    if result.has_high_frequency:
        parts.append(labels["high_frequency"])
    return " · ".join(parts) or labels["hearing"]


def _draw_header(figure: Figure, page: dict, clinic: Clinic, tested_at: datetime, heading: str) -> None:
    """As on the tympanometry sheet, with the heading made smaller when many tests share the sheet."""
    when = f"{tested_at:%Y-%m-%d %H:%M}"
    room = PAGE_WIDTH - 2 * MARGIN - _text_width(when, 12, "bold") - 8
    name, size = fitted(_printable(clinic.name, page), 11, room, "bold", smallest=8)
    _text(figure, MARGIN, 17, name, size=size, weight="bold")
    heading, size = fitted(heading, 18, room, "bold", smallest=12)
    _text(figure, MARGIN, 25.5, heading, size=size, weight="bold")
    _text(figure, PAGE_WIDTH - MARGIN, 17, page["tested_at"], size=8.5, color=INK_SECONDARY, ha="right")
    _text(figure, PAGE_WIDTH - MARGIN, 25.5, when, size=12, weight="bold", ha="right")
    _rule(figure, 31.5, color=INK, width=0.8)


def _draw_results(figure: Figure, labels: dict, page: dict, result: HearingResult, layout: dict) -> float:
    """Draws the audiograms and tables, and returns where they end."""
    y = 57.0
    if result.tone:
        y = _audiograms(figure, page, result, y, layout["audiogram"])
        y = _threshold_tables(figure, labels, result, y + 3, layout)
    if result.speech or result.srt:
        y = _speech_table(figure, labels, page, result, y + layout["gap"], layout)
    if result.high_frequency:
        y = _high_frequency_tables(figure, labels, page, result, y + layout["gap"], layout)
    return y


def _columns(page: dict):
    """The right ear goes on the left of the page, as on every audiogram."""
    return ((RIGHT_COLUMN, "R", page["right"], RIGHT_EAR), (LEFT_COLUMN, "L", page["left"], LEFT_EAR))


def _frequency_label(frequency: int) -> str:
    if frequency < 1000:
        return str(frequency)
    return f"{frequency / 1000:g}k"


# --- audiograms ---------------------------------------------------------------------------------

def _audiograms(figure: Figure, page: dict, result: HearingResult, top: float, height: float) -> float:
    for x, ear, name, color in _columns(page):
        _line(figure, x, x + 6, top + 3.7, color=color, width=2.4)
        _text(figure, x + 8.5, top + 5, name, size=12, weight="bold")
        _text(figure, x + COLUMN_WIDTH - 1, top + 5, "dB HL", size=8, color=INK_SECONDARY, ha="right")
        axes = _axes(figure, x + 9, top + 9, COLUMN_WIDTH - 10, height - 14)
        _audiogram_axes(axes)
        for placed in sorted(result.tone.values(), key=lambda placed: _row_rank(placed.kind)):
            if placed.ear == ear:
                _plot_curve(axes, placed, color)
    return top + height


def _audiogram_axes(axes) -> None:
    axes.set_xscale("log", base=2)
    axes.set_xlim(125 / 1.25, 8000 * 1.25)
    axes.set_ylim(DB_RANGE[1], DB_RANGE[0])
    axes.set_xticks(GRAPH_TICKS)
    axes.set_xticklabels([_frequency_label(frequency) for frequency in GRAPH_TICKS], fontsize=7.5)
    axes.set_xticks(GRAPH_IN_BETWEEN, minor=True)
    axes.set_xticklabels([], minor=True)
    axes.set_yticks(range(DB_RANGE[0], DB_RANGE[1] + 1, 10))
    axes.set_yticklabels([str(value) if value % 20 == 0 else "" for value in range(DB_RANGE[0], DB_RANGE[1] + 1, 10)],
                         fontsize=7.5)
    axes.tick_params(which="both", length=0, colors=INK_SECONDARY)
    axes.grid(True, which="major", color=GRIDLINE, linewidth=0.5)
    axes.grid(True, which="minor", axis="x", color=GRIDLINE, linewidth=0.4, linestyle=(0, (2, 2)))
    axes.axhspan(DB_RANGE[0], NORMAL_LIMIT, color=NORMAL_BAND, zorder=0)
    axes.set_facecolor(SURFACE)
    for spine in axes.spines.values():
        spine.set_color(AXIS)
        spine.set_linewidth(0.6)


def _glyph(kind: str, ear: str, masked: bool) -> tuple[str, str]:
    """('marker', a matplotlib marker) or ('text', a letter) for one point of a curve shown as ``kind``."""
    right = ear == "R"
    if kind == "Ac":
        return "marker", ("^" if right else "s") if masked else ("o" if right else "x")
    if kind == "Bc":
        return "text", ("[" if right else "]") if masked else ("<" if right else ">")
    if kind == "SpkrA":
        return "text", "A"
    if kind == "Spkr":
        return "text", "S"
    if kind == "AcU":
        return "text", "U"
    if kind == "AcM":
        return "text", "M"
    return "marker", "o"


def _plot_curve(axes, placed: Placed, color: str) -> None:
    kind, ear, curve = placed.kind, placed.ear, placed.curve
    soundfield = kind in ("Spkr", "SpkrA")
    tint = SOUNDFIELD if soundfield else color
    style = {"Ac": "-", "Spkr": (0, (4, 2)), "SpkrA": (0, (1.5, 1.5))}.get(kind)
    shift = 1.0
    if kind == "Bc":
        shift = 1 / BONE_OFFSET if ear == "R" else BONE_OFFSET
    points = [point for point in curve.points if point.frequency <= 8000]
    if style is not None:  # lines join the points that were heard, as on the maker's sheet
        run: list = []
        for point in points + [None]:
            if point is not None and not point.no_response:
                run.append(point)
                continue
            if len(run) > 1:
                axes.plot([q.frequency for q in run], [q.level for q in run], color=tint, linewidth=1.1,
                          linestyle=style, zorder=2)
            run = []
    for point in points:
        shape, glyph = _glyph(kind, ear, point.masked)
        x = point.frequency * shift
        if shape == "marker":
            axes.plot([x], [point.level], marker=glyph, markersize=7, markerfacecolor=SURFACE, markeredgecolor=tint,
                      markeredgewidth=1.2, linestyle="none", zorder=3, clip_on=False)
        else:
            axes.text(x, point.level, glyph, color=tint, fontsize=13 if kind == "Bc" else 9.5, fontweight="bold",
                      ha="center", va="center", zorder=3, clip_on=False,
                      bbox={"boxstyle": "square,pad=0.05", "facecolor": SURFACE, "edgecolor": "none"}
                      if soundfield else None)
        if point.no_response:
            toward = 0.8 if ear == "R" else 1.25
            axes.annotate("", xy=(x * toward, point.level + 9), xytext=(x, point.level + 2),
                          arrowprops={"arrowstyle": "->", "color": tint, "linewidth": 1.0}, zorder=3)


def _row_rank(kind: str) -> int:
    return ROWS.index(kind) if kind in ROWS else len(ROWS)


# --- tables -------------------------------------------------------------------------------------

def _level_text(point) -> str:
    if point is None:
        return ""
    return f"{point.level}↓" if point.no_response else str(point.level)


def _cell(figure: Figure, x: float, y: float, text: str, width: float, size: float, **style) -> None:
    if text:
        text, size = fitted(text, size, width - 0.4, style.get("weight", "normal"), smallest=size - 1.5)
        _text(figure, x, y, text, size=size, ha="center", **style)


def _ear_tables(figure: Figure, labels: dict, places: dict, kinds: list[str], frequencies: tuple[int, ...],
                top: float, layout: dict, with_average: bool, average_of=None) -> float:
    """One table per ear, side by side, with the same rows on both sides so they read across."""
    masked = {kind: any(point.masking is not None for (shown, _), placed in places.items() if shown == kind
                        for point in placed.curve.points if point.frequency in frequencies)
              for kind in kinds}
    end = top
    for x, ear, _, color in _columns({"right": "", "left": ""}):  # the ears' names are above the tables
        average_width = AVERAGE_WIDTH if with_average else 0.0
        cell = (COLUMN_WIDTH - LABEL_WIDTH - average_width) / len(frequencies)
        y = top + 4
        _text(figure, x, y, "Hz", size=7, color=INK_SECONDARY)
        for index, frequency in enumerate(frequencies):
            _text(figure, x + LABEL_WIDTH + (index + 0.5) * cell, y, _frequency_label(frequency), size=7,
                  color=INK_SECONDARY, ha="center")
        if with_average:
            _cell(figure, x + COLUMN_WIDTH - average_width / 2, y, labels["average"], average_width, 7,
                  color=INK_SECONDARY)
        _rule(figure, y + 1.5, x, x + COLUMN_WIDTH, color=color, width=0.9)
        for kind in kinds:
            placed = places.get((kind, ear))
            curve = placed.curve if placed else None
            y += layout["row"]
            label, size = fitted(labels["rows"].get(kind, kind), 7.5, LABEL_WIDTH - 0.5, "bold", smallest=6)
            _text(figure, x, y, label, size=size, weight="bold")
            for index, frequency in enumerate(frequencies):
                _cell(figure, x + LABEL_WIDTH + (index + 0.5) * cell, y,
                      _level_text(curve.at(frequency) if curve else None), cell, 8)
            if with_average and kind in AVERAGED and curve is not None:
                _cell(figure, x + COLUMN_WIDTH - average_width / 2, y, average_of(kind, ear).text(), average_width,
                      8.5, weight="bold")
            if masked[kind]:
                y += layout["mask"]
                _text(figure, x + 2, y, labels["masking"], size=6.5, color=INK_SECONDARY)
                for index, frequency in enumerate(frequencies):
                    point = curve.at(frequency) if curve else None
                    if point is not None and point.masking is not None:
                        _cell(figure, x + LABEL_WIDTH + (index + 0.5) * cell, y, str(point.masking), cell, 6.5,
                              color=INK_SECONDARY)
            _rule(figure, y + 1.6, x, x + COLUMN_WIDTH, color=ROW_RULE)
        end = max(end, y + 2)
    return end


def _row_kinds(places: dict, always: tuple[str, ...] = ()) -> list[str]:
    present = {kind for kind, _ in places}
    if present & set(always):
        present |= set(always)
    return sorted(present, key=_row_rank)


def _threshold_tables(figure: Figure, labels: dict, result: HearingResult, top: float, layout: dict) -> float:
    kinds = _row_kinds(result.tone, always=("Ac", "Bc"))
    bottom = _ear_tables(figure, labels, result.tone, kinds, TABLE_FREQUENCIES, top, layout, with_average=True,
                         average_of=lambda kind, ear: result.average(kind, ear))
    text, size = fitted(labels["average_note"], 6.5, PAGE_WIDTH - 2 * MARGIN, smallest=5.5)
    _text(figure, MARGIN, bottom + 3.8, text, size=size, color=INK_SECONDARY)
    return bottom + 4.5


def _speech_table(figure: Figure, labels: dict, page: dict, result: HearingResult, top: float, layout: dict) -> float:
    _text(figure, MARGIN, top + 4, labels["speech"], size=11, weight="bold")
    label_width = 26.0
    cell = (PAGE_WIDTH - 2 * MARGIN - label_width) / (2 * len(SPEECH_COLUMNS))
    y = top + 11
    for side, (_, _, name, color) in enumerate(_columns(page)):
        x0 = MARGIN + label_width + side * len(SPEECH_COLUMNS) * cell
        _text(figure, x0 + len(SPEECH_COLUMNS) * cell / 2, y, name, size=8.5, weight="bold", color=color, ha="center")
        _rule(figure, y + 1.5, x0 + 1, x0 + len(SPEECH_COLUMNS) * cell - 1, color=color, width=0.9)
        for index, kind in enumerate(SPEECH_COLUMNS):
            _cell(figure, x0 + (index + 0.5) * cell, y + 6, labels["rows"][kind], cell, 7.5, color=INK_SECONDARY)
    y += 6

    def best(kind, ear):
        curve = result.speech_curve(kind, ear)
        return curve.best if curve is not None else None

    def answers(point):
        if point is None or point.correct is None or point.words is None:
            return None
        return f"{point.correct}/{point.words}"

    rows = (
        (lambda kind, ear: result.srt_of(kind, ear), True),
        (lambda kind, ear: best(kind, ear) and best(kind, ear).score, True),
        (lambda kind, ear: best(kind, ear) and best(kind, ear).level, True),
        (lambda kind, ear: answers(best(kind, ear)), False),
        (lambda kind, ear: best(kind, ear) and best(kind, ear).masking, False),
    )
    for label, (value, always) in zip(labels["speech_rows"], rows):
        shown = [[value(kind, ear) for kind in SPEECH_COLUMNS] for ear in ("R", "L")]
        if not always and all(item is None for side in shown for item in side):
            continue
        y += layout["speech_row"]
        _text(figure, MARGIN, y, label, size=7.5, weight="bold")
        for side, values in enumerate(shown):
            for index, item in enumerate(values):
                x = MARGIN + label_width + (side * len(SPEECH_COLUMNS) + index + 0.5) * cell
                _cell(figure, x, y, "" if item in (None, False) else str(item), cell, 8.5)
        _rule(figure, y + 1.6, MARGIN, PAGE_WIDTH - MARGIN, color=ROW_RULE)
    return y + 2


def _high_frequency_tables(figure: Figure, labels: dict, page: dict, result: HearingResult, top: float,
                           layout: dict) -> float:
    _text(figure, MARGIN, top + 4, labels["high_frequency_title"], size=11, weight="bold")
    for x, _, name, color in _columns(page):
        _text(figure, x + COLUMN_WIDTH, top + 4, name, size=8.5, weight="bold", color=color, ha="right")
    kinds = _row_kinds(result.high_frequency)
    return _ear_tables(figure, labels, result.high_frequency, kinds, HF_FREQUENCIES, top + 5, layout,
                       with_average=False)
