import pytest

from tsm300.hearing import protocol as p
from tsm300.hearing.samples import DEMO_SERIAL, demo_memory, tonal
from tsm300.hearing.session import Session, SessionError, fingerprint
from tsm300.hearing.simulator import Behaviour, Memory, SimulatedAudiometer


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Timed:
    """The simulated device with time passing whenever it has nothing to say (a read timeout)."""

    def __init__(self, device, clock: Clock, step: float = 0.5) -> None:
        self.device, self.clock, self.step = device, clock, step

    def read(self) -> bytes:
        data = self.device.read()
        if not data:
            self.clock.now += self.step
        return data

    def write(self, data: bytes) -> None:
        self.device.write(data)

    def close(self) -> None:
        self.device.close()


def session_with(behaviour: Behaviour = None, memory: Memory = None):
    clock = Clock()
    device = SimulatedAudiometer(memory or demo_memory(), behaviour)
    log = []
    return Session(Timed(device, clock), usb=True, clock=clock, log=lambda d, b: log.append((d, b))), device, log


def test_identify_sends_the_makers_bytes_and_reads_the_serial():
    session, device, _ = session_with()
    assert session.identify() == DEMO_SERIAL
    assert device.received[0] == bytes.fromhex("4b43001016040100000000000000136b")
    assert device.received[0] == p.identify(usb=True)


def test_download_groups_tests_under_their_patients():
    session, device, _ = session_with()
    session.identify()
    result = session.download()
    assert result.finished and result.serial == DEMO_SERIAL
    assert [g.patient.number for g in result.groups] == ["1", "2", "3"]
    assert [type(t).__name__ for t in result.groups[0].tests] == ["ToneAudiogram", "SpeechAudiogram"]
    assert result.groups[2].tests[0].high_frequency
    assert [c[4] for c in device.received] == [p.SYNC, p.SYNC, p.GET_ALL]
    assert not device.refused
    second = result.groups[1]
    assert second.tests[0].curve("Ac", "L").at(500).level == -10
    assert second.tests[0].curve("AcM", "L") is None
    assert second.tests[1].srt_of("SpkrA", "R") == 40


def test_old_tests_are_told_apart_from_new_ones():
    session, device, _ = session_with()
    session.identify()
    first = session.download()
    seen = {fingerprint(t) for t in first.tests}
    device.memory.add(4, "견본", "라", "01/01/1990", "F", [tonal({"Ac R": ({1000: 25}, None)}, serial=DEMO_SERIAL)])
    second = session.download()
    new = second.new_since(seen)
    assert len(new) == 1 and new[0][0].patient.number == "4"


@pytest.mark.parametrize("behaviour", [
    Behaviour(answer_identification="ACK"),
    Behaviour(answer_identification="KI+ACK"),
    Behaviour(ack_at_end=False),
    Behaviour(chunk=7),
    Behaviour(chunk=512),
    Behaviour(noise_before=b"\x00\x00\xff"),
    Behaviour(ignore_length_field=False),
])
def test_download_copes_with_other_device_behaviour(behaviour):
    session, device, _ = session_with(behaviour)
    if behaviour.answer_identification == "ACK":
        with pytest.raises(SessionError):
            session.identify()
    else:
        session.identify()
    result = session.download()
    assert len(result.tests) == 5
    assert result.finished == behaviour.ack_at_end
    assert not device.refused


def test_a_test_sent_unasked_is_not_lost():
    session, device, _ = session_with(Behaviour(unasked_test=True))
    session.identify()
    result = session.download()
    assert len(result.tests) == 6  # the pushed copy and the memory's own
    assert len({fingerprint(t) for t in result.tests}) == 5


def test_empty_memory():
    session, _, _ = session_with(memory=Memory())
    session.identify()
    result = session.download()
    assert result.finished and result.tests == []


def test_no_answer():
    clock = Clock()

    class Silent:
        def read(self):
            clock.now += 0.5
            return b""

        def write(self, data):
            pass

        def close(self):
            pass

    with pytest.raises(SessionError):
        Session(Silent(), clock=clock).identify()


def test_close_says_goodbye_and_closes():
    session, device, _ = session_with()
    session.close()
    assert device.received[-1][4] == p.NACK and device.closed


def test_everything_sent_and_received_is_logged():
    session, _, log = session_with()
    session.identify()
    session.download()
    sent = b"".join(b for d, b in log if d == "PC>")
    got = b"".join(b for d, b in log if d == "<DEV")
    assert sent.count(b"KC") == 3
    assert got.count(DEMO_SERIAL.encode()) >= 6
