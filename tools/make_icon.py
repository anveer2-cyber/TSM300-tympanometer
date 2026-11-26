"""Draws the program's icon: a tympanogram peak on a blue tile.

Run from the repository root to regenerate ``src/tsm300/assets/tsm300.ico`` and ``tsm300.png``.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "src" / "tsm300" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
TOP, BOTTOM = (45, 136, 236), (23, 92, 184)
INK = (255, 255, 255)
OVERSAMPLE = 8


def draw(size: int) -> Image.Image:
    """One icon image. Small sizes get a proportionally thicker line so the peak stays readable."""
    scale = size * OVERSAMPLE
    tile = Image.new("RGB", (scale, scale))
    shade = ImageDraw.Draw(tile)
    for y in range(scale):
        mix = y / (scale - 1)
        shade.line([(0, y), (scale, y)], fill=tuple(round(a + (b - a) * mix) for a, b in zip(TOP, BOTTOM)))
    mask = Image.new("L", (scale, scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, scale - 1, scale - 1], radius=scale * 0.22, fill=255)
    icon = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
    icon.paste(tile, (0, 0), mask)

    stroke = max(size * 0.085, 1.9) * OVERSAMPLE
    left, right = scale * 0.16, scale * 0.84
    base, peak = scale * 0.70, scale * 0.24
    points = []
    for step in range(1201):
        t = step / 1200
        x = left + (right - left) * t
        height = math.exp(-(((t - 0.5) / 0.15) ** 2))
        points.append((x, base - (base - peak) * height))

    under = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
    ImageDraw.Draw(under).polygon(points + [(right, base), (left, base)], fill=INK + (56,))
    icon = Image.alpha_composite(icon, under)

    pen = ImageDraw.Draw(icon)
    for x, y in points:  # stamping discs gives a smooth round-ended stroke; a wide polyline frays at the joints
        pen.ellipse([x - stroke / 2, y - stroke / 2, x + stroke / 2, y + stroke / 2], fill=INK)
    return icon.resize((size, size), Image.LANCZOS)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    images = [draw(size) for size in SIZES]
    images[-1].save(ASSETS / "tsm300.png")
    images[-1].save(ASSETS / "tsm300.ico", sizes=[(size, size) for size in SIZES], append_images=images[:-1])
    print("wrote", ASSETS / "tsm300.ico", "and tsm300.png")


if __name__ == "__main__":
    main()
