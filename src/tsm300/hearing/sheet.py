"""The pure-tone and speech audiometry sheet: one A4 portrait page at 300 dpi.

Laid out like the TSM300 tympanometry sheet (header, patient row, right ear on the left),
with the symbols of ASHA (1990):

            오른쪽(빨강)   왼쪽(파랑)
    기도      O             X
    기도 차폐  △             □
    골도      <             >
    골도 차폐  [             ]
    음장      S             S      (보청기 착용: A)
    무반응    기호 아래로 화살표

Positions below are millimetres from the top-left corner of the page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import matplotlib
from matplotlib import font_manager
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

from .protocol import SpeechAudiogram, SpeechPoint, ToneAudiogram, ToneCurve
from .pta import average

PAGE_WIDTH, PAGE_HEIGHT = 210.0, 297.0
DPI = 300
PIXELS = (2480, 3508)
MARGIN = 15.0
COLUMN_WIDTH = 86.0
RIGHT_COLUMN, LEFT_COLUMN = MARGIN, PAGE_WIDTH - MARGIN - COLUMN_WIDTH

SURFACE = "#ffffff"
INK = "#191919"
INK_SECONDARY = "#666666"
GRIDLINE = "#b5b5b5"
ROW_RULE = "#e3e3e3"
RIGHT_EAR = "#d93b3c"
LEFT_EAR = "#2470d1"
SOUNDFIELD = "#3a3a3a"
NORMAL_BAND = "#f3f6f1"

GRAPH_FREQUENCIES = (125, 250, 500, 750, 1000, 1500, 2000, 3000, 4000, 6000, 8000)
MAIN_TICKS = (125, 250, 500, 1000, 2000, 4000, 8000)
TABLE_FREQUENCIES = (250, 500, 750, 1000, 1500, 2000, 3000, 4000, 6000, 8000)
DB_RANGE = (-10, 120)
NORMAL_LIMIT = 25

_FONTS = ("Malgun Gothic", "Apple SD Gothic Neo", "NanumGothic", "Noto Sans CJK KR", "Noto Sans KR")


@dataclass(frozen=True)
class Patient:
    chart_no: str = ""
    name: str = ""
    sex: str = ""
    birth_date: date | None = None

    def age_on(self, day: date) -> int | None:
        if self.birth_date is None:
            return None
        b = self.birth_date
        return day.year - b.year - ((day.month, day.day) < (b.month, b.day))


@dataclass(frozen=True)
class Clinic:
    name: str = ""
    address: str = ""
    phone: str = ""


@dataclass(frozen=True)
class Session:
    """What goes on one sheet: the tests one patient had in one sitting."""

    tone: ToneAudiogram | None = None
    speech: SpeechAudiogram | None = None
    aided_tone: tuple[ToneCurve, ...] = field(default=())
    """Soundfield curves the operator marked as measured with hearing aids, from a second test."""


@lru_cache(maxsize=1)
def _fonts() -> list[str]:
    installed = set(font_manager.get_font_names())
    return [name for name in _FONTS if name in installed][:1] + ["DejaVu Sans"]


def render(session: Session, path: Path, tested_at: datetime, patient: Patient = Patient(),
           note: str = "", clinic: Clinic = Clinic()) -> None:
    with matplotlib.rc_context({"font.family": _fonts(), "axes.unicode_minus": False}):
        figure = Figure(figsize=(PIXELS[0] / DPI, PIXELS[1] / DPI), dpi=DPI, facecolor=SURFACE)
        _header(figure, clinic, tested_at, session)
        _patient(figure, patient, tested_at)
        y = 57.0
        if session.tone is not None or session.aided_tone:
            y = _audiograms(figure, session, y)
            y = _threshold_tables(figure, session, y + 3)
        if session.speech is not None:
            y = _speech_table(figure, session.speech, y + 6)
        _note(figure, note, max(y + 6, 236.0))
        _footer(figure, session, clinic)
        options = {"quality": 95, "subsampling": 0} if Path(path).suffix.lower() in (".jpg", ".jpeg") else {}
        figure.savefig(path, dpi=DPI, pil_kwargs=options)


# --- page furniture ------------------------------------------------------------------------

def _title(session: Session) -> str:
    parts = []
    if session.tone is not None:
        kinds = {c.kind for c in session.tone.curves}
        parts.append("순음청력검사")
        if kinds & {"Spkr", "SpkrA"} or session.aided_tone:
            parts.append("음장검사")
    if session.speech is not None:
        parts.append("어음청력검사")
    return " · ".join(dict.fromkeys(parts)) or "청력검사"


def _header(figure: Figure, clinic: Clinic, tested_at: datetime, session: Session) -> None:
    _text(figure, MARGIN, 17, clinic.name, size=11, weight="bold")
    _text(figure, MARGIN, 25.5, _title(session), size=18, weight="bold")
    _text(figure, PAGE_WIDTH - MARGIN, 17, "검사일시", size=8.5, color=INK_SECONDARY, ha="right")
    _text(figure, PAGE_WIDTH - MARGIN, 25.5, f"{tested_at:%Y-%m-%d %H:%M}", size=12, weight="bold", ha="right")
    _rule(figure, 31.5, color=INK, width=0.8)


def _patient(figure: Figure, patient: Patient, tested_at: datetime) -> None:
    age = patient.age_on(tested_at.date())
    sex_age = " / ".join(p for p in (patient.sex, f"만 {age}세" if age is not None else "") if p)
    fields = [(MARGIN, "차트번호", patient.chart_no), (62.0, "이름", patient.name),
              (109.0, "성별 / 나이", sex_age),
              (156.0, "생년월일", patient.birth_date.isoformat() if patient.birth_date else "")]
    for x, label, value in fields:
        _text(figure, x, 38.5, label, size=8.5, color=INK_SECONDARY)
        if value:
            _text(figure, x, 46.0, value, size=13, weight="bold")
        else:
            _rule(figure, 48, x, x + 36)
    _rule(figure, 52.5)


def _note(figure: Figure, note: str, top: float) -> None:
    height = 262.0 - top
    figure.add_artist(Rectangle((_x(MARGIN), _y(top + height)), (PAGE_WIDTH - 2 * MARGIN) / PAGE_WIDTH,
                                height / PAGE_HEIGHT, transform=figure.transFigure, fill=False,
                                edgecolor=GRIDLINE, linewidth=0.6))
    _text(figure, MARGIN + 3, top + 5.5, "메모", size=8.5, color=INK_SECONDARY)
    for i, line in enumerate(note.strip().splitlines()[:5]):
        _text(figure, MARGIN + 3, top + 11.5 + i * 5.2, line, size=9.5)


def _footer(figure: Figure, session: Session, clinic: Clinic) -> None:
    _rule(figure, 268)
    serials = {t.packet.serial for t in (session.tone, session.speech) if t is not None}
    device = "장비 MRS Resonance " + ", ".join(sorted(serials)) if serials else ""
    _text(figure, MARGIN, 274, device, size=8, color=INK_SECONDARY)
    contact = "  ·  ".join(p for p in (clinic.address, f"전화 {clinic.phone}" if clinic.phone else "") if p)
    _text(figure, PAGE_WIDTH - MARGIN, 274, contact, size=8, color=INK_SECONDARY, ha="right")
    _text(figure, MARGIN, 280, "평균청력 6분법 = (500 + 2×1k + 2×2k + 4k) / 6, 소수 버림  ·  ↑ 무반응 주파수 포함(그 이상)  ·  "
          "* 검사 안 한 주파수 빼고 평균", size=7.5, color=INK_SECONDARY)


# --- audiograms ----------------------------------------------------------------------------

def _audiograms(figure: Figure, session: Session, top: float) -> float:
    height = 72.0
    for x, ear, name, color in ((RIGHT_COLUMN, "R", "오른쪽 (R)", RIGHT_EAR), (LEFT_COLUMN, "L", "왼쪽 (L)", LEFT_EAR)):
        _line(figure, x, x + 6, top + 3.7, color=color, width=2.4)
        _text(figure, x + 8.5, top + 5, name, size=12, weight="bold")
        _text(figure, x + COLUMN_WIDTH - 1, top + 5, "dB HL", size=8, color=INK_SECONDARY, ha="right")
        axes = _axes(figure, x + 9, top + 9, COLUMN_WIDTH - 10, height - 14)
        _audiogram_axes(axes)
        curves = [c for c in (session.tone.curves if session.tone else ()) if c.ear == ear]
        curves += [c for c in session.aided_tone if c.ear == ear]
        for curve in curves:
            _plot_curve(axes, curve, color, aided=curve in session.aided_tone)
    return top + height


def _audiogram_axes(axes) -> None:
    import math
    axes.set_xscale("log", base=2)
    axes.set_xlim(125 / 1.25, 8000 * 1.25)
    axes.set_ylim(DB_RANGE[1], DB_RANGE[0])
    axes.set_xticks(MAIN_TICKS)
    axes.set_xticklabels(["125", "250", "500", "1k", "2k", "4k", "8k"], fontsize=7.5)
    axes.set_xticks([750, 1500, 3000, 6000], minor=True)
    axes.set_xticklabels([], minor=True)
    axes.set_yticks(range(-10, 121, 10))
    axes.set_yticklabels([str(v) if v % 20 == 0 else "" for v in range(-10, 121, 10)], fontsize=7.5)
    axes.tick_params(length=0, colors=INK_SECONDARY)
    axes.grid(True, which="major", color=GRIDLINE, linewidth=0.5)
    axes.grid(True, which="minor", axis="x", color=GRIDLINE, linewidth=0.4, linestyle=(0, (2, 2)))
    axes.axhspan(DB_RANGE[0], NORMAL_LIMIT, color=NORMAL_BAND, zorder=0)
    for spine in axes.spines.values():
        spine.set_color(GRIDLINE)
        spine.set_linewidth(0.6)
    del math


BONE_OFFSET = 1.13
"""Bone-conduction symbols sit beside the frequency line, outside the air symbol (ASHA)."""


def _glyph(curve: ToneCurve, masked: bool, aided: bool) -> tuple[str, str]:
    """('marker', matplotlib marker) or ('text', letter) for one point of ``curve``."""
    right = curve.ear == "R"
    if curve.kind == "Ac":
        if masked:
            return "marker", ("^" if right else "s")
        return "marker", ("o" if right else "x")
    if curve.kind == "Bc":
        if masked:
            return "text", ("[" if right else "]")
        return "text", ("<" if right else ">")
    if curve.kind in ("Spkr", "SpkrA"):
        return "text", ("A" if aided or curve.kind == "SpkrA" else "S")
    if curve.kind == "AcU":
        return "text", "U"
    if curve.kind == "AcM":
        return "text", "M"
    return "marker", "o"


def _plot_curve(axes, curve: ToneCurve, color: str, aided: bool = False) -> None:
    soundfield = curve.kind in ("Spkr", "SpkrA")
    tint = SOUNDFIELD if soundfield else color
    style = {"Ac": "-", "Spkr": (0, (4, 2)), "SpkrA": (0, (4, 2))}.get(curve.kind)
    shift = 1.0
    if curve.kind == "Bc":
        shift = 1 / BONE_OFFSET if curve.ear == "R" else BONE_OFFSET
    points = [p for p in curve.points if p.frequency <= 8000]
    if style is not None:
        run: list = []
        for p in points + [None]:
            if p is not None and not p.no_response:
                run.append(p)
                continue
            if len(run) > 1:
                axes.plot([q.frequency for q in run], [q.level for q in run], color=tint, linewidth=1.1,
                          linestyle=style, zorder=2)
            run = []
    for p in points:
        kind, glyph = _glyph(curve, p.masked, aided)
        x = p.frequency * shift
        if kind == "marker":
            axes.plot([x], [p.level], marker=glyph, markersize=7, markerfacecolor=SURFACE,
                      markeredgecolor=tint, markeredgewidth=1.2, linestyle="none", zorder=3)
        else:
            axes.text(x, p.level, glyph, color=tint, fontsize=13 if curve.kind == "Bc" else 9.5,
                      fontweight="bold", ha="center", va="center", zorder=3,
                      bbox={"boxstyle": "square,pad=0.05", "facecolor": SURFACE, "edgecolor": "none"}
                      if soundfield else None)
        if p.no_response:
            dx = 0.8 if curve.ear == "R" else 1.25
            axes.annotate("", xy=(x * dx, p.level + 9), xytext=(x, p.level + 2),
                          arrowprops={"arrowstyle": "->", "color": tint, "linewidth": 1.0}, zorder=3)


# --- tables --------------------------------------------------------------------------------

_ROWS = (("Ac", "기도"), ("Bc", "골도"), ("Spkr", "음장"), ("SpkrA", "음장 보청기"), ("AcU", "불쾌역치"),
         ("AcM", "쾌적역치"))


def _threshold_tables(figure: Figure, session: Session, top: float) -> float:
    bottom = top
    for x, ear, color in ((RIGHT_COLUMN, "R", RIGHT_EAR), (LEFT_COLUMN, "L", LEFT_EAR)):
        curves = {c.kind: c for c in (session.tone.curves if session.tone else ()) if c.ear == ear}
        for c in session.aided_tone:
            if c.ear == ear:
                curves["SpkrA"] = c
        rows = [(kind, label) for kind, label in _ROWS if kind in curves or kind in ("Ac", "Bc")]
        label_width, pta_width = 15.0, 10.0
        cell = (COLUMN_WIDTH - label_width - pta_width) / len(TABLE_FREQUENCIES)
        y = top + 4
        _text(figure, x, y, "Hz", size=7, color=INK_SECONDARY)
        for i, f in enumerate(TABLE_FREQUENCIES):
            label = f"{f // 1000}k" if f >= 1000 and f % 1000 == 0 else (f"{f / 1000:g}k" if f >= 1000 else str(f))
            _text(figure, x + label_width + (i + 0.5) * cell, y, label, size=7, color=INK_SECONDARY, ha="center")
        _text(figure, x + COLUMN_WIDTH - pta_width / 2, y, "평균", size=7, color=INK_SECONDARY, ha="center")
        _rule(figure, y + 1.5, x, x + COLUMN_WIDTH, color=color, width=0.9)
        for kind, label in rows:
            curve = curves.get(kind)
            masked = curve is not None and any(p.masking is not None for p in curve.points)
            y += 5.2
            _text(figure, x, y, label, size=7.5, weight="bold")
            for i, f in enumerate(TABLE_FREQUENCIES):
                point = curve.at(f) if curve else None
                text = "" if point is None else (f"{point.level}↓" if point.no_response else str(point.level))
                _text(figure, x + label_width + (i + 0.5) * cell, y, text, size=8, ha="center")
            if kind in ("Ac", "Bc", "Spkr", "SpkrA"):
                pta = average(curve, "6분법")
                _text(figure, x + COLUMN_WIDTH - pta_width / 2, y, pta.text() if curve else "", size=8.5,
                      weight="bold", ha="center")
            if masked:
                y += 4.2
                _text(figure, x + 2, y, "차폐", size=6.5, color=INK_SECONDARY)
                for i, f in enumerate(TABLE_FREQUENCIES):
                    point = curve.at(f)
                    if point is not None and point.masking is not None:
                        _text(figure, x + label_width + (i + 0.5) * cell, y, str(point.masking), size=6.5,
                              color=INK_SECONDARY, ha="center")
            _rule(figure, y + 1.6, x, x + COLUMN_WIDTH, color=ROW_RULE)
        bottom = max(bottom, y + 2)
    return bottom


_SPEECH_COLUMNS = (("Ac", "기도"), ("Bc", "골도"), ("Spkr", "음장"), ("SpkrA", "음장 보청기"))


def _speech_table(figure: Figure, speech: SpeechAudiogram, top: float) -> float:
    _text(figure, MARGIN, top + 4, "어음청력검사", size=12, weight="bold")
    y = top + 11
    label_width = 26.0
    cell = (PAGE_WIDTH - 2 * MARGIN - label_width) / 8
    for side, (ear, name, color) in enumerate((("R", "오른쪽 (R)", RIGHT_EAR), ("L", "왼쪽 (L)", LEFT_EAR))):
        x0 = MARGIN + label_width + side * 4 * cell
        _text(figure, x0 + 2 * cell, y, name, size=8.5, weight="bold", color=color, ha="center")
        _rule(figure, y + 1.5, x0 + 1, x0 + 4 * cell - 1, color=color, width=0.9)
        for j, (_, label) in enumerate(_SPEECH_COLUMNS):
            _text(figure, x0 + (j + 0.5) * cell, y + 6, label, size=7.5, color=INK_SECONDARY, ha="center")
    rows = (("SRT (dB HL)", lambda c, s: s), ("최고 명료도 (%)", lambda c, s: c and c.best and c.best.score),
            ("그때 강도 (dB HL)", lambda c, s: c and c.best and c.best.level),
            ("정답 / 단어", lambda c, s: c and c.best and _answers(c.best)),
            ("차폐 (dB HL)", lambda c, s: c and c.best and c.best.masking))
    y += 6
    for label, value in rows:
        y += 5.4
        _text(figure, MARGIN, y, label, size=7.5, weight="bold")
        for side, ear in enumerate(("R", "L")):
            for j, (kind, _) in enumerate(_SPEECH_COLUMNS):
                curve = speech.curve(kind, ear)
                srt = speech.srt_of(kind, ear)
                shown = value(curve, srt)
                x = MARGIN + label_width + (side * 4 + j + 0.5) * cell
                _text(figure, x, y, "" if shown in (None, False) else str(shown), size=8.5, ha="center")
        _rule(figure, y + 1.6, MARGIN, PAGE_WIDTH - MARGIN, color=ROW_RULE)
    return y + 2


def _answers(point: SpeechPoint) -> str | None:
    """Words repeated / words given, when the device sent the count (the longest packets)."""
    if point.correct is None or point.words is None:
        return None
    return f"{point.correct}/{point.words}"


# --- drawing helpers -----------------------------------------------------------------------

def _x(mm: float) -> float:
    return mm / PAGE_WIDTH


def _y(mm: float) -> float:
    return 1 - mm / PAGE_HEIGHT


def _text(figure: Figure, x: float, y: float, text: str, size: float = 9, color: str = INK,
          weight: str = "normal", ha: str = "left") -> None:
    if text:
        figure.text(_x(x), _y(y), text, fontsize=size, color=color, fontweight=weight, ha=ha, va="baseline")


def _rule(figure: Figure, y: float, x0: float = MARGIN, x1: float = PAGE_WIDTH - MARGIN,
          color: str = GRIDLINE, width: float = 0.5) -> None:
    _line(figure, x0, x1, y, color=color, width=width)


def _line(figure: Figure, x0: float, x1: float, y: float, color: str, width: float) -> None:
    figure.add_artist(Line2D([_x(x0), _x(x1)], [_y(y), _y(y)], transform=figure.transFigure,
                             color=color, linewidth=width))


def _axes(figure: Figure, x: float, top: float, width: float, height: float):
    return figure.add_axes((_x(x), _y(top + height), width / PAGE_WIDTH, height / PAGE_HEIGHT))
