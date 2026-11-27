import os
from pathlib import Path

import pytest

from tsm300.hearing import protocol as p
from tsm300.hearing.pta import average
from tsm300.hearing.samples import frame, speech, tonal

# The maker's own sample packets are never put in this repository. Point RESONANCE_SAMPLES at a
# folder holding them to run these four checks on a PC that has them; elsewhere they are skipped.
SAMPLES = Path(os.environ["RESONANCE_SAMPLES"]) if os.environ.get("RESONANCE_SAMPLES") else None


def sample(name: str) -> bytes:
    path = SAMPLES / name if SAMPLES is not None else None
    if path is None or not path.exists():
        pytest.skip("제조사 견본 패킷이 이 PC 에 없음 (저장소에는 넣지 않음)")
    data = path.read_bytes()
    return data[:p.packet_length(data)]


# --- the maker's sample packets (local only) ---------------------------------------------------

def test_vendor_tonal_sample():
    result = p.parse_packet(sample("tonal_R35C.bin"))
    assert isinstance(result, p.ToneAudiogram)
    assert result.packet.serial == "R35C13C000001"
    assert result.packet.model == "R35C"
    right, left = result.curve("Ac", "R"), result.curve("Ac", "L")
    assert [pt.level for pt in right.points] == [10, 15, 20, 20, 25, 35, 35, 40, 45, 45, 50]
    assert [pt.level for pt in left.points] == [15, 15, 20, 20, 25, 30, 35, 40, 40, 45, 50]
    assert right.at(12500) is None
    assert {c.name for c in result.curves} == {"Ac R", "Ac L"}
    assert average(right, "6분법").value == pytest.approx((20 + 2 * 25 + 2 * 35 + 45) / 6)


def test_vendor_speech_sample():
    result = p.parse_packet(sample("vocal_R35C.bin"))
    assert isinstance(result, p.SpeechAudiogram)
    curve = result.curve("Ac", "R")
    assert [(pt.level, pt.score) for pt in curve.points] == [(20, 10), (30, 30), (35, 50), (40, 70), (50, 90)]
    assert curve.stimulus == "SD CARD"
    assert curve.best.score == 90
    assert len(result.curves) == 1


def test_vendor_other_tests_are_kept_raw():
    for name in ("tymp_R35C.bin", "reflex_R35C.bin"):
        result = p.parse_packet(sample(name))
        assert isinstance(result, p.OtherTest)
        assert result.packet.mode == p.MODE_IMPEDANCE


# --- packets built here --------------------------------------------------------------------

def test_tonal_bone_soundfield_and_no_response():
    packet = tonal({
        "Ac R": ({500: 30, 1000: 40, 2000: 50, 4000: 70}, {4000: 244}),
        "Bc R": ({500: 10, 1000: 20}, {500: 50}),
        "Spkr R": ({500: 40, 1000: 45}, None),
        "SpkrA R": ({500: 25, 1000: 30}, None),
    })
    result = p.parse_packet(packet)
    ac = result.curve("Ac", "R")
    assert ac.at(4000).no_response is True and ac.at(4000).masked is False
    assert result.curve("Bc", "R").at(500).masking == 50
    assert result.curve("Spkr", "R").at(1000).level == 45
    assert result.curve("SpkrA", "R").at(500).level == 25
    six = average(ac, "6분법")
    assert six.value == pytest.approx((30 + 80 + 100 + 70) / 6) and six.at_least
    assert six.text() == "46↑"
    bone = average(result.curve("Bc", "R"), "6분법")
    assert bone.value == pytest.approx((10 + 2 * 20) / 3) and bone.missing == (2000, 4000)
    assert bone.text() == "16*"
    assert average(None).text() == "-"


def test_tonal_with_mcl_blocks():
    packet = tonal({"AcM L": ({1000: 60}, None)}, with_mcl=True)
    assert len(packet) == 418
    assert p.parse_packet(packet).curve("AcM", "L").at(1000).level == 60


def test_levels_are_signed_bytes():
    assert p.level(246) == -10
    assert p.level(251) == -5
    assert p.level(245) is None
    assert p.level(120) == 120
    assert p.level(235, p.HF_MARKS) is None and p.level(236, p.HF_MARKS) == -20


def test_masking_bytes():
    assert p.masking(245) == p.Masking(None, False, False)
    assert p.masking(244) == p.Masking(None, False, True)
    assert p.masking(243) == p.Masking(None, True, True)
    assert p.masking(60) == p.Masking(60, True, False)
    assert p.masking(0).masked is True


def test_no_response_with_masking():
    result = p.parse_packet(tonal({"Ac L": ({4000: 100, 8000: 246}, {4000: 243})}))
    point = result.curve("Ac", "L").at(4000)
    assert point.no_response and point.masked and point.masking is None
    assert result.curve("Ac", "L").at(8000).level == -10


def test_speech_long_form_with_srt_and_answers():
    packet = speech({"Ac L": [(40, 60, 245), (50, 88, 30)], "Spkr1 R": [(55, 72, 245)]},
                    srt={"Ac L": 35, "SpkrA R": 45}, long_form=True, answers=True)
    assert len(packet) == p.SPEECH_WITH_ANSWERS
    result = p.parse_packet(packet)
    assert result.curve("Ac", "L").best.level == 50
    assert result.curve("Ac", "L").points[1].masking == 30
    assert result.curve("Spkr1", "R").points[0].score == 72
    assert {(s.kind, s.ear, s.level) for s in result.srt} == {("Ac", "L", 35), ("SpkrA", "R", 45)}


def test_stream_splits_packets_and_survives_noise():
    one = tonal({"Ac R": ({1000: 20}, None)})
    two = speech({"Ac R": [(40, 80, 245)]})
    command = p.command(p.ACK)
    patient = b"KA" + (5).to_bytes(2, "big") + b"1;0;;" + bytes([p.xor(b"1;0;;"), 0x6B])
    decoder = p.StreamDecoder()
    events = []
    stream = b"\x00\xff" + one + command + patient + two
    for i in range(0, len(stream), 7):
        events += decoder.feed(stream[i:i + 7])
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["Corrupt", "ToneAudiogram", "Frame", "Frame", "SpeechAudiogram"]
    assert events[2].command == p.ACK and events[3].letter == "A" and events[3].data == b"1;0;;"
    assert decoder.pending == 0


def test_bad_check_byte_is_reported_not_parsed():
    packet = bytearray(tonal({"Ac R": ({1000: 20}, None)}))
    packet[30] ^= 0x01
    events = p.StreamDecoder().feed(bytes(packet))
    assert isinstance(events[0], p.Corrupt) and "check" in events[0].reason


def test_unknown_size_is_an_error():
    with pytest.raises(p.ProtocolError):
        p.parse_packet(frame("R37A20C000123", 0, 0, bytes(100)))


def test_speech_answers_give_words():
    packet = speech({"Ac R": [(75, 60, 245)]}, long_form=True, answers=True, correct={"Ac R": [15]})
    point = p.parse_packet(packet).curve("Ac", "R").points[0]
    assert (point.correct, point.words) == (15, 25)


def test_only_reading_commands_are_built():
    sync = p.identify(usb=True)
    assert sync[:4] == b"KC\x00\x10" and sync[4] == 0x16 and sync[-1] == 0x6B
    assert sync[14] == p.xor(sync[4:14])
    for dangerous in (0x03, 0x21, 0x27, 0x5B, 0x5D, 0x5F, 0x02):
        with pytest.raises(ValueError):
            p.command(dangerous)
