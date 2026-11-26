"""Byte-level statistics and dumps for inspecting captured serial data."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_TEXT_CONTROL = frozenset({0x09, 0x0A, 0x0D})
_LINE_NOISE = frozenset({0x00, 0xFF})


def printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    printable = sum(1 for b in data if 0x20 <= b <= 0x7E or b in _TEXT_CONTROL)
    return printable / len(data)


def looks_like_text(data: bytes) -> bool:
    return len(data) >= 4 and printable_ratio(data) >= 0.9


def is_line_noise(data: bytes) -> bool:
    """True for the stray byte or two a UART reports when the far end powers off or on."""
    return 0 < len(data) <= 2 and all(b in _LINE_NOISE for b in data)


def is_oversampled(data: bytes) -> bool:
    """True when the bytes look like a slower sender read at twice its baud rate or more.

    Every low period on the line then spans at least two receiver bit times, covering
    the receiver's start bit and its first data bit, so each received byte is even.
    """
    return len(data) >= 3 and all(b % 2 == 0 for b in data)


def guess_baud(results: list[tuple[int, bytes]]) -> int | None:
    """Picks the sender's baud rate from the same kind of message captured at several rates."""
    meaningful = [(baud, data) for baud, data in results if data and not is_line_noise(data)]
    text = [(baud, data) for baud, data in meaningful if looks_like_text(data)]
    if text:
        return max(text, key=lambda item: printable_ratio(item[1]))[0]
    plausible = [baud for baud, data in meaningful if not is_oversampled(data)]
    return max(plausible) if plausible else None


def ascii_preview(data: bytes, limit: int = 64) -> str:
    text = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in data[:limit])
    return text + ("..." if len(data) > limit else "")


def hexdump(data: bytes, limit: int | None = None, width: int = 16) -> str:
    shown = data if limit is None else data[:limit]
    lines = []
    for offset in range(0, len(shown), width):
        row = shown[offset:offset + width]
        hexpart = " ".join(f"{b:02X}" for b in row)
        text = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in row)
        lines.append(f"{offset:06X}  {hexpart:<{width * 3 - 1}}  {text}")
    if limit is not None and len(data) > limit:
        lines.append(f"... (+{len(data) - limit} bytes)")
    return "\n".join(lines)


def describe_session(directory: Path) -> str:
    summary = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    parts = [f"{directory.name}: port={summary.get('port')} baud={summary.get('baud')}"]
    for scan in summary.get("scan", []):
        data = (directory / scan["file"]).read_bytes()
        parts.append(f"\n[scan {scan['baud']} bps] {len(data)} bytes, printable {printable_ratio(data):.0%}")
        parts.append(hexdump(data, limit=256))
    for burst in summary.get("bursts", []):
        data = (directory / burst["file"]).read_bytes()
        parts.append(
            f"\n[burst {burst['index']}] t={burst['started']:.3f}s {len(data)} bytes, "
            f"printable {printable_ratio(data):.0%}, note: {burst.get('note', '')!r}"
        )
        parts.append(hexdump(data))
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        print("usage: python -m tsm300.analyze <session directory> [...]")
        return 2
    for path in paths:
        print(describe_session(Path(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
