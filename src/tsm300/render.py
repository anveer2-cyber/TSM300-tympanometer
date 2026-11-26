"""Drawing a measurement as a PNG: tympanogram, device-reported values and reflex traces per ear."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

import matplotlib
from matplotlib import font_manager
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpecFromSubplotSpec

from .protocol import (
    REFLEX_FREQUENCIES, REFLEX_POINTS, EarResult, Measurement, tympanogram_pressures,
)

SURFACE = "#ffffff"
INK = "#191919"
INK_SECONDARY = "#666666"
GRIDLINE = "#e7e7e7"
AXIS = "#c2c2c2"
RIGHT_EAR = "#ec4647"
LEFT_EAR = "#257de0"

_KOREAN_FONTS = ("Malgun Gothic", "Apple SD Gothic Neo", "NanumGothic", "Noto Sans CJK KR", "Noto Sans KR")

_KOREAN = {
    "title": "임피던스 청력검사 (Tympanometry)",
    "right": "오른쪽 (R)",
    "left": "왼쪽 (L)",
    "pressure_axis": "압력 (daPa)",
    "compliance_axis": "컴플라이언스 (ml)",
    "pressure": "중이 압력",
    "volume": "외이도 용적",
    "compliance": "컴플라이언스",
    "width": "폭",
    "reflex": "등골근 반사 (ml)",
    "not_measured": "측정 없음",
    "no_reflex": "반사 검사 없음",
}
_ENGLISH = {
    "title": "Tympanometry",
    "right": "Right (R)",
    "left": "Left (L)",
    "pressure_axis": "Pressure (daPa)",
    "compliance_axis": "Compliance (ml)",
    "pressure": "Middle ear pressure",
    "volume": "Ear volume",
    "compliance": "Compliance",
    "width": "Width",
    "reflex": "Acoustic reflex (ml)",
    "not_measured": "Not measured",
    "no_reflex": "No reflex test",
}


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


def render(measurement: Measurement, path: Path, received_at: datetime) -> None:
    fonts, labels = _font_and_labels()
    with matplotlib.rc_context({"font.family": fonts, "axes.unicode_minus": False}):
        figure = Figure(figsize=(12, 8), dpi=150, facecolor=SURFACE)
        figure.text(0.05, 0.95, labels["title"], fontsize=17, fontweight="bold", color=INK, va="center")
        caption = f"{received_at:%Y-%m-%d %H:%M} · {measurement.device_name} · ID {measurement.memory_id}"
        figure.text(0.96, 0.95, caption, fontsize=11, color=INK_SECONDARY, va="center", ha="right")
        columns = figure.add_gridspec(1, 2, left=0.05, right=0.96, top=0.9, bottom=0.065, wspace=0.16)
        _draw_ear(figure, columns[0], measurement.right, labels["right"], RIGHT_EAR, labels)
        _draw_ear(figure, columns[1], measurement.left, labels["left"], LEFT_EAR, labels)
        figure.savefig(path, format="png")


def _draw_ear(figure: Figure, cell, ear: EarResult | None, name: str, color: str, labels: dict) -> None:
    heights = {"heading": 0.4, "gap1": 0.55, "tympanogram": 3.5, "gap2": 0.85, "values": 0.8,
               "gap3": 0.45, "reflex_heading": 0.3, "gap4": 0.15, "reflexes": 1.2}
    grid = GridSpecFromSubplotSpec(len(heights), 1, subplot_spec=cell, height_ratios=list(heights.values()), hspace=0.0)
    rows = dict(zip(heights, grid))
    heading = _blank_axes(figure, rows["heading"])
    heading.plot([0.0, 0.045], [0.5, 0.5], color=color, linewidth=4, solid_capstyle="round")
    heading.text(0.065, 0.5, name, fontsize=14, fontweight="bold", color=INK, va="center")
    if ear is None:
        body = _blank_axes(figure, rows["tympanogram"])
        body.text(0.5, 0.5, labels["not_measured"], fontsize=14, color=INK_SECONDARY, ha="center", va="center")
        return
    _draw_tympanogram(figure.add_subplot(rows["tympanogram"]), ear, color, labels)
    _draw_values(_blank_axes(figure, rows["values"]), ear, labels)
    reflex_heading = _blank_axes(figure, rows["reflex_heading"])
    reflex_heading.text(0.0, 0.5, labels["reflex"], fontsize=10.5, color=INK_SECONDARY, va="center")
    if ear.reflexes:
        _draw_reflexes(figure, rows["reflexes"], ear, color)
    else:
        empty = _blank_axes(figure, rows["reflexes"])
        empty.text(0.5, 0.5, labels["no_reflex"], fontsize=11, color=INK_SECONDARY, ha="center", va="center")


def _draw_tympanogram(axes, ear: EarResult, color: str, labels: dict) -> None:
    pressures = tympanogram_pressures()
    curve = ear.compliance_curve
    top = 3.0 if max(curve) <= 3.0 else 3.5
    axes.set_xlim(-300, 200)
    axes.set_ylim(-0.5, top)
    axes.set_xticks(range(-300, 201, 100))
    axes.set_yticks(range(0, int(top) + 1))
    _style_axes(axes)
    axes.axhline(0, color=AXIS, linewidth=1, zorder=1)
    axes.fill_between(pressures, curve, 0, color=color, alpha=0.10, linewidth=0, zorder=2)
    axes.plot(pressures, curve, color=color, linewidth=2, solid_capstyle="round", solid_joinstyle="round", zorder=3)
    axes.plot(
        [ear.middle_ear_pressure], [ear.compliance], marker="o", markersize=9, zorder=4,
        markerfacecolor=color, markeredgecolor=SURFACE, markeredgewidth=2, clip_on=False,
    )
    axes.set_xlabel(labels["pressure_axis"], fontsize=10, color=INK_SECONDARY, labelpad=4)
    axes.text(0.0, 1.03, labels["compliance_axis"], transform=axes.transAxes, fontsize=10, color=INK_SECONDARY)


def _draw_values(axes, ear: EarResult, labels: dict) -> None:
    tiles = [
        (labels["pressure"], f"{ear.middle_ear_pressure:d}", "daPa"),
        (labels["volume"], f"{ear.ear_volume:.2f}", "ml"),
        (labels["compliance"], f"{ear.compliance:.2f}", "ml"),
        (labels["width"], f"{ear.width:d}", "daPa"),
    ]
    for index, (label, value, unit) in enumerate(tiles):
        x = index / len(tiles)
        axes.text(x, 0.85, label, fontsize=10, color=INK_SECONDARY, va="center")
        number = axes.text(x, 0.25, value, fontsize=17, fontweight="bold", color=INK, va="center")
        axes.annotate(
            unit, xy=(1, 0), xycoords=number, xytext=(4, 0), textcoords="offset points",
            fontsize=10, color=INK_SECONDARY, va="bottom",
        )


def _draw_reflexes(figure: Figure, cell, ear: EarResult, color: str) -> None:
    panels = GridSpecFromSubplotSpec(1, len(REFLEX_FREQUENCIES), subplot_spec=cell, wspace=0.12)
    points = range(REFLEX_POINTS)
    for index, frequency in enumerate(REFLEX_FREQUENCIES):
        axes = figure.add_subplot(panels[index])
        axes.set_xlim(0, REFLEX_POINTS - 1)
        axes.set_ylim(0, 0.2)
        axes.set_xticks([])
        axes.set_yticks([0, 0.1, 0.2])
        _style_axes(axes)
        if index > 0:
            axes.tick_params(labelleft=False)
        axes.set_xlabel(f"{frequency} Hz", fontsize=10, color=INK_SECONDARY, labelpad=4)
        if frequency in ear.reflexes:
            curve = ear.reflex_curve(frequency)
            axes.fill_between(points, curve, 0, color=color, alpha=0.10, linewidth=0, zorder=2)
            axes.plot(points, curve, color=color, linewidth=2, solid_capstyle="round", solid_joinstyle="round",
                      zorder=3, clip_on=False)


def _style_axes(axes) -> None:
    axes.set_facecolor(SURFACE)
    axes.grid(True, axis="y", color=GRIDLINE, linewidth=1, linestyle="-")
    axes.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(AXIS)
    axes.tick_params(colors=INK_SECONDARY, length=0, labelsize=10)


def _blank_axes(figure: Figure, cell):
    axes = figure.add_subplot(cell)
    axes.set_xlim(0, 1)
    axes.set_ylim(0, 1)
    axes.axis("off")
    return axes
