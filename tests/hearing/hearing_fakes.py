"""Stand-ins for the audiometer's link: the simulated device with time that passes only when it
is silent, a device that never answers, and one whose cable is pulled half-way."""

from __future__ import annotations

from tsm300.hearing.samples import practice_memory
from tsm300.hearing.simulator import Behaviour, SimulatedAudiometer


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Timed:
    """The simulated device; whenever it has nothing to say, half a second goes by, as a USB read times out."""

    name = "모의 장비 (시험)"

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


class Silent:
    name = "말 없는 장치"

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.written = []
        self.closed = False

    def read(self) -> bytes:
        self.clock.now += 0.5
        return b""

    def write(self, data: bytes) -> None:
        self.written.append(bytes(data))

    def close(self) -> None:
        self.closed = True


class Unplugged(Timed):
    """Answers for a while, then fails as Windows does when the cable is pulled."""

    def __init__(self, device, clock: Clock, reads: int) -> None:
        super().__init__(device, clock)
        self.reads = reads

    def read(self) -> bytes:
        self.reads -= 1
        if self.reads < 0:
            raise RuntimeError("청력검사기에서 읽지 못했습니다 (Windows 오류 22) — USB 선을 확인해 주세요")
        return super().read()


class Audiometer:
    """A simulated audiometer whose memory lasts between connections, as the real one's does."""

    def __init__(self, behaviour: Behaviour | None = None, memory=None) -> None:
        self.memory = memory or practice_memory()
        self.behaviour = behaviour
        self.clock = Clock()
        self.devices = []

    def open(self):
        device = SimulatedAudiometer(self.memory, self.behaviour)
        self.devices.append(device)
        return Timed(device, self.clock)

    def sent(self) -> list[bytes]:
        return [command for device in self.devices for command in device.received]
