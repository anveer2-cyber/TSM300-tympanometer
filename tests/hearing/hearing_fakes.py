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


class Bench:
    """One simulated audiometer on the desk, with its clock and an operator who presses Connect to
    PC on it ``press_at`` seconds after [받기] (0: before, None: never). ``on_usb`` False: Windows
    sees the device only once it has been pressed."""

    def __init__(self, behaviour: Behaviour, memory=None, press_at: float | None = 0.0, on_usb: bool = True) -> None:
        self.clock = Clock()
        self.device = SimulatedAudiometer(memory or practice_memory(), behaviour)
        self.press_at, self.on_usb = press_at, on_usb
        self.waited: list[int] = []
        self.opened = 0
        self._tick()

    def _tick(self) -> None:
        if self.press_at is not None and self.clock.now >= self.press_at:
            self.press_at = None  # pressed once
            self.device.press()

    def open(self):
        from tsm300.hearing import device
        self._tick()
        if not self.on_usb and not self.device.ready:
            raise device.DeviceMissing(device.NO_DEVICE)
        self.opened += 1
        return BenchLink(self)

    def sleep(self, seconds: float) -> None:
        self.clock.now += seconds
        self._tick()

    def fetch(self, **options):
        from tsm300.hearing import device
        return device.fetch(self.open, clock=self.clock, sleep=self.sleep, waiting=self.waited.append, **options)

    def check(self, **options):
        from tsm300.hearing import device
        return device.check(self.open, clock=self.clock, sleep=self.sleep, waiting=self.waited.append, **options)


class BenchLink(Timed):
    name = "모의 장비 (시험대)"

    def __init__(self, bench: Bench) -> None:
        super().__init__(bench.device, bench.clock)
        self.bench = bench

    def read(self) -> bytes:
        self.bench._tick()
        return super().read()
