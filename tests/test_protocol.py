"""Decoding checked against transmissions captured from a real TSM-300.

The values asserted for ``both_ears.bin`` are the ones the device showed on its own
screen for that measurement: left 0 daPa / 9.30 ml / 1.66 ml / 103 daPa and
right 0 daPa / 8.39 ml / 1.59 ml / 103 daPa.
"""

from pathlib import Path

import pytest

from tsm300.protocol import (
    Corrupt, Measurement, PowerOn, ProtocolError, StreamDecoder,
    parse_message, split_records, tympanogram_pressures,
)

DATA = Path(__file__).parent / "data"
BOTH = (DATA / "both_ears.bin").read_bytes()
LEFT_ONLY = (DATA / "left_only.bin").read_bytes()
EMPTY = (DATA / "empty.bin").read_bytes()
POWER_ON = (DATA / "power_on.bin").read_bytes()


def test_records_of_a_full_measurement():
    tags = [tag for tag, _ in split_records(BOTH)]
    assert tags == [
        "PAID", "LT02", "RT02",
        "LMEP", "LPVO", "LCOM", "LTWI", "RMEP", "RPVO", "RCOM", "RTWI",
        "LR05", "LR10", "LR20", "LR30", "LR40", "RR05", "RR10", "RR20", "RR30", "RR40",
        "DNAM", "CODE",
    ]


def test_values_match_what_the_device_displayed():
    result = parse_message(BOTH)

    assert result.memory_id == 1
    assert result.device_name == "TSM-300"
    assert result.device_code == "6349"
    assert result.unknown == {}

    assert result.left.middle_ear_pressure == 0
    assert round(result.left.ear_volume, 2) == 9.30
    assert round(result.left.compliance, 2) == 1.66
    assert result.left.width == 103

    assert result.right.middle_ear_pressure == 0
    assert round(result.right.ear_volume, 2) == 8.39
    assert round(result.right.compliance, 2) == 1.59
    assert result.right.width == 103


def test_curve_peak_agrees_with_the_reported_compliance_and_pressure():
    pressures = tympanogram_pressures()
    assert pressures[0] == -296.875 and pressures[-1] == 200.0
    for ear in (parse_message(BOTH).left, parse_message(BOTH).right):
        curve = ear.compliance_curve
        peak = max(curve)
        assert peak == pytest.approx(ear.compliance, abs=0.005)
        last_peak_index = max(i for i, value in enumerate(curve) if value == peak)
        assert pressures[last_peak_index] == ear.middle_ear_pressure
        assert curve[-1] == 0.0


def test_reflex_curves_cover_five_frequencies_within_the_displayed_scale():
    left = parse_message(BOTH).left
    assert sorted(left.reflexes) == [500, 1000, 2000, 3000, 4000]
    for frequency in left.reflexes:
        curve = left.reflex_curve(frequency)
        assert len(curve) == 40
        assert 0.0 <= min(curve) and max(curve) < 0.2
    assert max(left.reflex_curve(1000)) == pytest.approx(0.198, abs=0.001)


def test_an_unmeasured_ear_is_none():
    left_only = parse_message(LEFT_ONLY)
    assert left_only.left is not None and left_only.right is None
    assert left_only.left == parse_message(BOTH).left

    empty = parse_message(EMPTY)
    assert empty.left is None and empty.right is None
    assert empty.device_name == "TSM-300"


def test_negative_pressure_is_signed():
    message = BOTH.replace(b"LMEP\x02\x00\x00\x00", b"LMEP\x02\x00\x6a\xff")
    assert parse_message(message).left.middle_ear_pressure == -150


def test_unknown_records_are_kept_rather_than_dropped():
    message = BOTH.replace(b"DNAM", b"XNEW\x03\x00abcDNAM")
    assert parse_message(message).unknown == {"XNEW": b"abc"}


@pytest.mark.parametrize("damaged, reason", [
    (BOTH[:-4], "incomplete"),
    (BOTH + b"\x00", "follow the STOP tag"),
    (BOTH.replace(b"LMEP", b"lmep"), "invalid record tag"),
    (BOTH.replace(b"LT02\xa0\x00", b"LT02\xa0\xff"), "claims"),
    (BOTH.replace(b"LMEP\x02\x00\x00\x00", b"LMEP\x01\x00\x00"), "LMEP has 1 bytes"),
    (BOTH.replace(b"LCOM", b"LXXX"), "no LCOM record"),
])
def test_damaged_messages_are_rejected(damaged, reason):
    with pytest.raises(ProtocolError, match=reason):
        parse_message(damaged)


def test_stream_yields_a_measurement_as_soon_as_stop_arrives():
    decoder = StreamDecoder()
    events = []
    for offset in range(0, len(BOTH), 7):
        chunk = BOTH[offset:offset + 7]
        events += decoder.feed(chunk)
        if offset + 7 < len(BOTH):
            assert events == []
    assert events == [parse_message(BOTH)]
    assert decoder.pending == 0


def test_stream_reports_power_on_then_measurements():
    decoder = StreamDecoder()
    events = decoder.feed(POWER_ON + LEFT_ONLY + BOTH)
    assert events == [PowerOn("TSM-300 SCREENING", "6349"), parse_message(LEFT_ONLY), parse_message(BOTH)]


def test_stream_skips_noise_between_messages():
    decoder = StreamDecoder()
    events = decoder.feed(b"\x00\xff\x00" + LEFT_ONLY + b"\x00" + BOTH)
    assert [type(event) for event in events] == [Measurement, Measurement]


def test_stream_recovers_after_a_damaged_message():
    decoder = StreamDecoder()
    damaged = BOTH.replace(b"LMEP", b"lmep")
    events = decoder.feed(damaged + BOTH)
    assert isinstance(events[0], Corrupt) and "invalid record tag" in events[0].reason
    assert events[1:] == [parse_message(BOTH)]


def test_flush_reports_a_message_that_stopped_part_way():
    decoder = StreamDecoder()
    assert decoder.feed(BOTH[:300]) == []
    assert decoder.pending == 300
    (corrupt,) = decoder.flush()
    assert corrupt.data == BOTH[:300]
    assert decoder.pending == 0
    assert decoder.feed(BOTH) == [parse_message(BOTH)]


def test_flush_is_silent_when_only_noise_is_pending():
    decoder = StreamDecoder()
    decoder.feed(b"\x00")
    assert decoder.flush() == []
