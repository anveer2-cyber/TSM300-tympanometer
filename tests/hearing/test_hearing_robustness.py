"""Whatever the USB link brings, the decoder neither stops for good nor loses good packets.

The maker's program stalls on several of these (docs/hearing/통신_절차.md §3.4): one byte of
noise before a packet, a packet with no length (KJ), a short live-tone packet at the end.
"""

import random

import pytest

from tsm300.hearing import protocol as p
from tsm300.hearing.samples import demo_memory
from tsm300.hearing.simulator import SimulatedAudiometer


def stream() -> bytes:
    device = SimulatedAudiometer(demo_memory())
    device.write(p.command(p.GET_ALL))
    out = bytearray()
    while chunk := device.read():
        out += chunk
    return bytes(out)


def decode(data: bytes, chunk: int = 100) -> list:
    decoder = p.StreamDecoder()
    events = []
    for i in range(0, len(data), chunk):
        events += decoder.feed(data[i:i + chunk])
    events += decoder.flush()
    assert decoder.pending == 0
    return events


def results_in(events) -> list:
    return [e for e in events if isinstance(e, (p.ToneAudiogram, p.SpeechAudiogram, p.OtherTest))]


CLEAN = stream()
FIRST = CLEAN.index(b"KT")
SECOND = CLEAN.index(b"KT", FIRST + 2)


@pytest.mark.parametrize("name, data, expected", [
    ("noise before", b"\x00\xff\x13" + CLEAN, 5),
    ("a packet cut short, then the rest", CLEAN[:FIRST + 40] + CLEAN[FIRST:], 5),
    ("a packet cut short and lost", CLEAN[:FIRST + 40] + CLEAN[SECOND:], 4),
    ("a wrong check byte", CLEAN[:FIRST + 30] + bytes([CLEAN[FIRST + 30] ^ 0xFF]) + CLEAN[FIRST + 31:], 4),
    ("a short live-tone packet at the end", CLEAN + b"K^\x00\x01\x02", 5),
    ("a device-info packet with no data", p.frame(ord("J"), b"") + CLEAN, 5),
    ("a length that claims 60 kB", b"KT\xea\x60" + CLEAN, 5),
    ("an unknown kind", b"KZ\x00\x02ab\x03k" + CLEAN, 5),
])
def test_bad_stretches_cost_only_themselves(name, data, expected):
    for chunk in (1, 7, 64, 100, 4096):
        assert len(results_in(decode(data, chunk))) == expected, (name, chunk)


def test_random_damage_never_raises_or_hangs():
    rng = random.Random(20261127)
    for _ in range(300):
        data = bytearray(CLEAN)
        for _ in range(rng.randint(1, 6)):
            what = rng.random()
            at = rng.randrange(len(data))
            if what < 0.4:
                data[at] = rng.randrange(256)
            elif what < 0.7:
                del data[at:at + rng.randint(1, 80)]
            else:
                data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 40)))
        events = decode(bytes(data), rng.choice((1, 13, 64, 100, 512)))
        assert len(results_in(events)) <= 5 + 1


def test_pure_noise():
    rng = random.Random(7)
    noise = bytes(rng.randrange(256) for _ in range(20000)) + b"K"
    events = decode(noise, 333)
    assert not results_in(events)


def test_every_test_that_survives_damage_is_a_real_one():
    """A test pulled out of damaged bytes is byte for byte one the device sent, never a mix."""
    rng = random.Random(3)
    real = {t.packet.raw for t in results_in(decode(CLEAN))}
    for _ in range(200):
        data = bytearray(CLEAN)
        at = rng.randrange(len(data))
        data[at] ^= 1 + rng.randrange(255)
        for test in results_in(decode(bytes(data), 64)):
            assert test.packet.raw in real


def test_a_device_that_never_stops_sending_is_left_after_the_time_limit():
    from tsm300.hearing.session import DOWNLOAD_LIMIT, Session

    class Endless:
        """Answers who it is, then sends a ping every read, for ever."""

        def __init__(self) -> None:
            self.now = 0.0
            self.identified = False

        def read(self) -> bytes:
            self.now += 0.05
            if not self.identified:
                self.identified = True
                return p.frame(p.KIND_IDENTIFICATION, b"R37A21C000777")
            return p.frame(p.KIND_COMMAND, bytes([p.PING]).ljust(10, b"\x00"))

        def write(self, data: bytes) -> None:
            pass

        def close(self) -> None:
            pass

    link = Endless()
    session = Session(link, clock=lambda: link.now)
    session.identify()
    result = session.download()
    assert not result.finished and result.tests == []
    assert DOWNLOAD_LIMIT <= link.now < DOWNLOAD_LIMIT + 5


class PingsFirst(SimulatedAudiometer):
    """Sends a ping, then (a read later) the usual answer, whenever it is asked who it is."""

    def _handle(self, command: bytes) -> None:
        if command[4] == p.SYNC:
            self._outgoing += p.frame(p.KIND_COMMAND, bytes([p.PING]).ljust(10, b"\x00"))
        super()._handle(command)


@pytest.mark.parametrize("make", [
    lambda: PingsFirst(demo_memory()),
    lambda: PingsFirst(demo_memory(), __import__("tsm300.hearing.simulator", fromlist=["Behaviour"]).Behaviour(chunk=7)),
    lambda: SimulatedAudiometer(demo_memory(), __import__("tsm300.hearing.simulator", fromlist=["Behaviour"]).Behaviour(
        unasked_test=True, chunk=7)),
])
def test_something_else_before_the_answer_neither_hangs_nor_ends_the_download_early(make):
    from tsm300.hearing.session import Session

    device = make()
    clock = {"now": 0.0}

    class Timed:
        def read(self):
            data = device.read()
            clock["now"] += 0.01 if data else 0.5
            return data

        def write(self, data):
            device.write(data)

        def close(self):
            device.close()

    session = Session(Timed(), clock=lambda: clock["now"])
    session.identify()
    result = session.download()
    assert result.finished
    assert len({t.packet.raw for t in result.tests}) == 5
    assert clock["now"] < 30
