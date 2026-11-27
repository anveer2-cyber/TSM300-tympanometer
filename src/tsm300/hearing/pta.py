"""Pure-tone averages, worked out as the maker's Korean report does.

    3분법  (500 + 1k + 2k) / 3
    4분법  (500 + 2×1k + 2k) / 4
    6분법  (500 + 2×1k + 2×2k + 4k) / 6     장애 판정 · 병원 결과지

Like the maker's program: a frequency with no response counts at the loudest level given,
a frequency not tested is left out and the rest averaged, and the printed value drops its
decimals (56.7 -> 56). Unlike it, the sheet says so: ↑ when a no-response frequency went
in (the hearing is this bad or worse) and * when a frequency was missing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .protocol import MARKS, TONE_FREQUENCIES, ToneCurve, signed

METHODS = {
    "3분법": ((500, 1), (1000, 1), (2000, 1)),
    "4분법": ((500, 1), (1000, 2), (2000, 1)),
    "6분법": ((500, 1), (1000, 2), (2000, 2), (4000, 1)),
}


@dataclass(frozen=True)
class Average:
    method: str
    value: float | None
    at_least: bool = False
    """A no-response frequency went in: the hearing is this bad or worse."""
    missing: tuple[int, ...] = ()
    """Frequencies of the method that were not tested and so left out."""

    @property
    def shown(self) -> int | None:
        """The printed number: decimals dropped, as the maker's report does."""
        return None if self.value is None else math.trunc(self.value)

    def text(self) -> str:
        if self.value is None:
            return "-"
        return f"{self.shown}{'↑' if self.at_least else ''}{'*' if self.missing else ''}"


def average(curve: ToneCurve | None, method: str = "6분법") -> Average:
    if curve is None:
        return Average(method, None)
    total = 0.0
    count = 0
    silent = False
    missing = []
    for frequency, weight in METHODS[method]:
        point = curve.at(frequency)
        if point is None:
            missing.append(frequency)
            continue
        total += point.level * weight
        count += weight
        silent = silent or point.no_response
    if count == 0:
        return Average(method, None, False, tuple(missing))
    return Average(method, total / count, silent, tuple(missing))


def vendor_average(levels: bytes, method: str = "6분법") -> float | None:
    """The same average from a block's 13 raw threshold bytes (for the cross-check)."""
    weights = dict(METHODS[method])
    total = count = 0
    for frequency, byte in zip(TONE_FREQUENCIES, levels):
        if byte == MARKS.none or frequency not in weights:
            continue
        total += signed(byte) * weights[frequency]
        count += weights[frequency]
    return total / count if count else None
