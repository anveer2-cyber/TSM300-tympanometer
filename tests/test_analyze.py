import random

import pytest

from tsm300.analyze import (
    guess_baud, hexdump, is_line_noise, is_oversampled, looks_like_text, printable_ratio,
)
from tsm300.cli import SCAN_BAUDS
from uart_sim import receive


def test_printable_ratio_counts_text_and_line_endings():
    assert printable_ratio(b"TSM300\r\n") == 1.0
    assert printable_ratio(b"\x00\x01AB") == 0.5
    assert printable_ratio(b"") == 0.0


def test_looks_like_text_needs_enough_printable_bytes():
    assert looks_like_text(b"TSM300\r\n")
    assert not looks_like_text(b"OK")
    assert not looks_like_text(b"\x02\x10\x80\xfe\x00")


def test_line_noise_is_only_a_stray_zero_or_ff():
    assert is_line_noise(b"\x00")
    assert is_line_noise(b"\xff\x00")
    assert not is_line_noise(b"")
    assert not is_line_noise(b"\x05")
    assert not is_line_noise(b"\x00\x00\x00")


def test_hexdump_shows_offsets_hex_and_ascii():
    dump = hexdump(b"TSM300\r\n" + bytes(range(16)), limit=16)
    first, second = dump.splitlines()
    assert first.startswith("000000  54 53 4D 33 30 30 0D 0A 00 01")
    assert first.endswith("TSM300..........")
    assert second == "... (+8 bytes)"


def test_uart_model_reads_back_the_payload_at_the_matching_baud():
    payload = bytes(range(256))
    assert receive(payload, 9600, 9600) == payload


@pytest.mark.parametrize("tx_baud, rx_baud", [(9600, 19200), (9600, 115200), (1200, 2400), (19200, 57600)])
def test_reading_at_twice_the_baud_or_more_gives_only_even_bytes(tx_baud, rx_baud):
    payload = random.Random(1).randbytes(64)
    assert is_oversampled(receive(payload, tx_baud, rx_baud))


@pytest.mark.parametrize("device_baud", [1200, 2400, 4800, 9600, 19200, 57600, 115200])
@pytest.mark.parametrize("payload", [b"TSM300 v1.02\r\n", random.Random(7).randbytes(24)])
def test_guess_baud_finds_the_device_rate(device_baud, payload):
    results = [(baud, receive(payload, device_baud, baud)) for baud in SCAN_BAUDS]
    assert guess_baud(results) == device_baud


def test_guess_baud_is_none_when_nothing_meaningful_arrived():
    assert guess_baud([(9600, b""), (19200, b"\x00")]) is None
