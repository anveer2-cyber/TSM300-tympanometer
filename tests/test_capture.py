import json

from tsm300.capture import Burst, BurstSplitter, SessionWriter, listen_once


class ScriptedPort:
    """A port whose bytes arrive at scripted times; each read costs one 50 ms timeout."""

    def __init__(self, arrivals):
        self.now = 0.0
        self._arrivals = sorted(arrivals)

    def clock(self):
        return self.now

    @property
    def in_waiting(self):
        return sum(len(data) for at, data in self._arrivals if at <= self.now)

    def read(self, size):
        due = [item for item in self._arrivals if item[0] <= self.now]
        if not due:
            self.now += 0.05
            return b""
        self._arrivals = [item for item in self._arrivals if item[0] > self.now]
        return b"".join(data for _, data in due)


def test_splitter_ends_a_burst_after_the_idle_gap():
    splitter = BurstSplitter(idle_gap=1.0)
    splitter.feed(b"AB", 0.0)
    splitter.feed(b"C", 0.4)
    assert splitter.poll(1.0) is None
    assert splitter.poll(1.5) == Burst(1, 0.0, 0.4, b"ABC")
    assert splitter.poll(5.0) is None
    splitter.feed(b"D", 6.0)
    assert splitter.flush() == Burst(2, 6.0, 6.0, b"D")
    assert splitter.flush() is None


def test_listen_once_returns_the_message_after_it_goes_quiet():
    port = ScriptedPort([(2.0, b"TSM"), (2.1, b"300")])
    assert listen_once(port, wait=25, settle=4, clock=port.clock) == b"TSM300"
    assert 6.0 <= port.now < 6.5


def test_listen_once_keeps_listening_through_power_off_noise():
    port = ScriptedPort([(1.0, b"\x00"), (8.0, b"TSM300")])
    assert listen_once(port, wait=25, settle=4, clock=port.clock) == b"\x00TSM300"


def test_listen_once_gives_up_after_the_wait():
    port = ScriptedPort([])
    assert listen_once(port, wait=3, settle=1, clock=port.clock) == b""
    assert 3.0 <= port.now < 3.2


def test_session_writer_persists_stream_bursts_and_notes(tmp_path):
    writer = SessionWriter(tmp_path / "session", {"port": "COM3"})
    writer.scan_result(9600, b"TSM300")
    writer.set_meta(baud=9600)
    writer.rx(0.5, b"AB")
    writer.rx(0.6, b"C")
    writer.burst(Burst(1, 0.5, 0.6, b"ABC"))
    writer.note(1, "오른쪽, 압력 -15")
    writer.close()

    directory = tmp_path / "session"
    assert (directory / "raw.bin").read_bytes() == b"ABC"
    assert (directory / "burst_001.bin").read_bytes() == b"ABC"
    assert (directory / "scan_9600.bin").read_bytes() == b"TSM300"
    summary = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    assert summary["port"] == "COM3" and summary["baud"] == 9600
    assert summary["bursts"][0]["note"] == "오른쪽, 압력 -15"
    events = [json.loads(line) for line in (directory / "timeline.jsonl").read_text().splitlines()]
    assert events == [
        {"t": 0.5, "event": "rx", "offset": 0, "length": 2},
        {"t": 0.6, "event": "rx", "offset": 2, "length": 1},
    ]
