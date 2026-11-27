"""Builds Resonance result packets, following the layout in docs/hearing/통신_절차.md and 결과_구조.md.

Used by the tests, by the simulator and by the cross-check against the maker's program.
Every number here is made up; none belongs to a patient.
"""

from __future__ import annotations

from functools import reduce

from .protocol import (HF_FREQUENCIES, SPEECH_ORDER, SPEECH_ORDER_LONG, SRT_ORDER,
                                TONE_FREQUENCIES, TONE_ORDER, TONE_ORDER_WITH_MCL)

NO_VALUE = 245


def signed(db: int) -> int:
    """dB HL as the device stores it: a signed byte (-10 -> 246)."""
    return db & 0xFF


def frame(serial: str, mode: int, test: int, body: bytes) -> bytes:
    inner = serial.encode("latin-1").ljust(13, b"\x00")[:13] + bytes([0, mode, test]) + body
    check = reduce(lambda a, b: a ^ b, inner, 0)
    return b"KT" + len(inner).to_bytes(2, "big") + inner + bytes([check, 0x6B])


def tone_block(levels: dict[int, int] | None = None, masks: dict[int, int] | None = None,
               frequencies=TONE_FREQUENCIES, no_value=NO_VALUE, stimulus=0, masking=2, pulsed=0) -> bytes:
    if levels is None:
        return bytes(7 + 2 * len(frequencies))
    head = bytes([1, stimulus, masking, 0, 1, pulsed, 0])
    lv = bytes(signed(levels[f]) if f in levels else no_value for f in frequencies)
    mk = bytes((masks or {}).get(f, no_value) for f in frequencies)
    return head + lv + mk


def tonal(curves: dict[str, tuple[dict, dict | None]], serial="R37A20C000123", with_mcl=False) -> bytes:
    order = TONE_ORDER_WITH_MCL if with_mcl else TONE_ORDER
    body = b"".join(tone_block(*curves[name]) if name in curves else tone_block() for name in order)
    return frame(serial, 0, 0, body)


def speech_block(points: list[tuple[int, int, int]] | None = None, stimulus=7, words=10) -> bytes:
    if points is None:
        return bytes(40)
    head = bytes([1, stimulus, 7, 0, 1, words, 1, 1, 0, 0])
    pad = [(NO_VALUE, NO_VALUE, NO_VALUE)] * (10 - len(points))
    rows = points + pad
    return head + bytes(r[0] for r in rows) + bytes(r[1] for r in rows) + bytes(r[2] for r in rows)


def speech(curves: dict[str, list], srt: dict[str, int] | None = None, serial="R37A20C000123",
           long_form=False, answers=False, correct: dict[str, list[int]] | None = None) -> bytes:
    order = SPEECH_ORDER_LONG if long_form else SPEECH_ORDER
    body = b"".join(speech_block(curves.get(name)) for name in order)
    if long_form:
        for name in SRT_ORDER:
            body += bytes([1, srt[name]]) if srt and name in srt else bytes(2)
    if answers:
        for name in order:
            given = (correct or {}).get(name, [])
            body += bytes(given + [NO_VALUE] * (10 - len(given)))
    return frame(serial, 0, 1, body)



def hf(curves: dict[str, tuple[dict, dict | None]], serial="R37A20C000123") -> bytes:
    from .protocol import HF_MARKS, HF_ORDER
    body = b"".join(tone_block(*curves[name], frequencies=HF_FREQUENCIES, no_value=HF_MARKS.none)
                    if name in curves else tone_block(frequencies=HF_FREQUENCIES) for name in HF_ORDER)
    return frame(serial, 0, 11, body)


SILENT, SILENT_MASKED = 244, 243

DEMO_SERIAL = "R37A21C000777"


def demo_memory():
    """Three made-up patients in a simulated device's memory, covering what the sheet draws."""
    from .simulator import Memory
    s = DEMO_SERIAL
    memory = Memory(serial=s)
    memory.add(1, "견본", "가", "02/01/1960", "M", [
        tonal({
            "Ac R": ({250: 35, 500: 40, 750: 45, 1000: 50, 1500: 55, 2000: 60, 3000: 65, 4000: 70, 6000: 75, 8000: 80}, {}),
            "Bc R": ({500: 20, 1000: 30, 2000: 40, 4000: 55}, {1000: 65, 2000: 70}),
            "Ac L": ({250: 15, 500: 15, 750: 15, 1000: 20, 1500: 20, 2000: 25, 3000: 35, 4000: 45, 6000: 60, 8000: 90},
                     {8000: SILENT_MASKED}),
            "Bc L": ({500: 10, 1000: 15, 2000: 20, 4000: 40}, {}),
        }, serial=s),
        speech({"Ac R": [(55, 40, 245), (65, 64, 245), (75, 72, 50), (85, 68, 60)],
                "Ac L": [(30, 52, 245), (40, 88, 245), (50, 96, 245)]},
               srt={"Ac R": 50, "Ac L": 20}, serial=s, long_form=True, answers=True,
               correct={"Ac R": [10, 16, 18, 17], "Ac L": [13, 22, 24]}),
    ])
    memory.add(2, "견본", "나", "15/07/1948", "F", [
        tonal({
            "Ac R": ({500: 60, 1000: 65, 2000: 70, 4000: 80}, {4000: SILENT}),
            "Spkr R": ({500: 55, 1000: 60, 2000: 65, 4000: 75}, {}),
            "SpkrA R": ({500: 35, 1000: 35, 2000: 40, 4000: 50}, {}),
            "Ac L": ({500: -10, 1000: -5, 2000: 0, 4000: 5}, {}),
        }, serial=s, with_mcl=True),
        speech({"Spkr R": [(70, 48, 245)], "SpkrA R": [(60, 80, 245)]},
               srt={"Spkr R": 60, "SpkrA R": 40}, serial=s, long_form=True, answers=True,
               correct={"Spkr R": [12], "SpkrA R": [20]}),
    ])
    memory.add(3, "견본", "다", "03/03/2015", "M", [
        hf({"Ac R": ({8000: 20, 10000: 30, 12500: 45, 16000: 70}, {}), "Ac L": ({8000: 15, 10000: 25}, {})}, serial=s),
    ])
    return memory


def practice_memory():
    """The demo memory and one more made-up patient, who was tested the way the clinic tests a
    hearing aid: pure tone with soundfield, speech, then soundfield again with the aid in, as
    a test of its own. The simulated device in the program's practice mode holds this."""
    memory = demo_memory()
    s = DEMO_SERIAL
    memory.add(4, "견본", "라", "20/05/1955", "F", [
        tonal({
            "Ac R": ({250: 40, 500: 45, 750: 50, 1000: 55, 1500: 60, 2000: 65, 3000: 70, 4000: 75, 6000: 80, 8000: 85},
                     {}),
            "Bc R": ({500: 35, 1000: 45, 2000: 55, 4000: 65}, {2000: 75}),
            "Spkr R": ({500: 50, 1000: 55, 2000: 60, 4000: 70}, {}),
            "Ac L": ({250: 30, 500: 35, 750: 40, 1000: 45, 1500: 50, 2000: 55, 3000: 60, 4000: 65, 6000: 75, 8000: 90},
                     {8000: SILENT}),
            "Bc L": ({500: 25, 1000: 35, 2000: 45, 4000: 55}, {}),
            "Spkr L": ({500: 45, 1000: 50, 2000: 55, 4000: 65}, {}),
        }, serial=s),
        speech({"Ac R": [(60, 44, 245), (70, 64, 245), (80, 76, 55)],
                "Ac L": [(50, 52, 245), (60, 72, 245), (70, 84, 245)],
                "Spkr R": [(65, 56, 245)]},
               srt={"Ac R": 55, "Ac L": 45, "Spkr R": 55}, serial=s, long_form=True, answers=True,
               correct={"Ac R": [11, 16, 19], "Ac L": [13, 18, 21], "Spkr R": [14]}),
        tonal({"Spkr R": ({500: 30, 1000: 30, 2000: 35, 4000: 45}, {}),
               "Spkr L": ({500: 30, 1000: 35, 2000: 35, 4000: 50}, {})}, serial=s),
        speech({"Spkr R": [(50, 84, 245)]}, srt={"Spkr R": 35}, serial=s, long_form=True, answers=True,
               correct={"Spkr R": [21]}),
    ])
    return memory
