"""Talking to the audiometer: who it is, and fetching the tests in its memory.

The order and the bytes are the maker's program's own over USB (docs/hearing/통신_절차.md §5):

    connect       PC  KC 16 (length 0x10, version 4, 1)      device  KI serial number
    download      PC  KC 16 again                             device  KC ack (protocol version)
                  PC  KC 01 (get all)                         device  KA patient, KT test, KT test,
                                                                      KA patient, KT ..., KC ack
    close         PC  KC 07

The device keeps its tests after they are fetched, so every download brings back old tests
too; telling new from old is up to the caller (``Download.new_since``).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as p

ANSWER_WAIT = 1.5
"""Seconds to wait for the answer to the identification."""
QUIET_END = 6.0
"""Seconds without a byte after which a download is taken as finished even without its ack."""
DOWNLOAD_LIMIT = 120.0


class SessionError(RuntimeError):
    pass


@dataclass
class DeviceGroup:
    """One patient on the device and the tests that came after them."""

    patient: p.DevicePatient | None
    tests: list = field(default_factory=list)


@dataclass
class Download:
    serial: str
    groups: list[DeviceGroup]
    finished: bool
    """True when the device said it had sent everything; False when it went quiet."""
    raw: bytes = b""
    corrupt: list[p.Corrupt] = field(default_factory=list)

    @property
    def tests(self) -> list:
        return [t for g in self.groups for t in g.tests]

    def new_since(self, seen: set[str]) -> list[tuple[DeviceGroup, object]]:
        """(group, test) for every test whose fingerprint is not in ``seen``, in arrival order."""
        return [(g, t) for g in self.groups for t in g.tests if fingerprint(t) not in seen]


def fingerprint(test) -> str:
    """The same test fetched twice has the same fingerprint."""
    return hashlib.sha1(test.packet.raw).hexdigest()


class Session:
    """One open link to the audiometer. ``usb`` decides the identification's length field."""

    def __init__(self, link, usb: bool = True, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str, bytes], None] | None = None) -> None:
        self._link = link
        self._usb = usb
        self._clock = clock
        self._decoder = p.StreamDecoder()
        self._log = log or (lambda direction, data: None)
        self._held: list = []
        self.serial = ""
        self.protocol_version: int | None = None

    # --- low level ---------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        self._log("PC>", data)
        self._link.write(data)

    def _events(self, until: float) -> list:
        """Whatever arrives before ``until`` (at least one read)."""
        if self._held:
            held, self._held = self._held, []
            return held
        data = self._link.read()
        if data:
            self._log("<DEV", data)
            return self._decoder.feed(data)
        if self._decoder.pending and self._clock() >= until:
            return self._decoder.flush()
        return []

    # --- steps -------------------------------------------------------------------------

    def identify(self, wait: float = ANSWER_WAIT) -> str:
        """Asks who the device is, twice at most as the maker does. Returns the serial number."""
        for _ in range(2):
            self.send(p.identify(usb=self._usb))
            deadline = self._clock() + wait
            while self._clock() < deadline:
                for event in self._events(deadline):
                    self._note(event)
                if self.serial:
                    return self.serial
        raise SessionError("청력검사기가 답하지 않습니다")

    def download(self, quiet: float = QUIET_END, limit: float = DOWNLOAD_LIMIT,
                 progress: Callable[[int], None] | None = None) -> Download:
        """Fetches every test in the device's memory."""
        self.send(p.identify(usb=self._usb))
        deadline = self._clock() + ANSWER_WAIT
        answered = False
        while not answered and self._clock() < deadline:
            for event in self._events(deadline):
                if isinstance(event, p.Frame) and (event.command == p.ACK or event.kind == p.KIND_IDENTIFICATION):
                    self._note(event)
                    answered = True
                else:
                    self._held.append(event)  # a test sent unasked: keep it for the download
        self.send(p.command(p.GET_ALL))
        groups: list[DeviceGroup] = []
        corrupt: list[p.Corrupt] = []
        received = bytearray()
        start = last = self._clock()
        finished = False
        while not finished:
            now = self._clock()
            if now - last > quiet or now - start > limit:
                break
            for event in self._events(last + quiet):
                last = self._clock()
                if isinstance(event, p.Corrupt):
                    corrupt.append(event)
                    continue
                raw = event.packet.raw if hasattr(event, "packet") else event.raw
                received += raw
                if isinstance(event, p.Frame) and event.kind == p.KIND_PATIENT:
                    groups.append(DeviceGroup(p.device_patient(event)))
                elif isinstance(event, (p.ToneAudiogram, p.SpeechAudiogram, p.OtherTest)):
                    if not groups:
                        groups.append(DeviceGroup(None))
                    groups[-1].tests.append(event)
                    if not self.serial:
                        self.serial = event.packet.serial
                    if progress:
                        progress(len([t for g in groups for t in g.tests]))
                elif isinstance(event, p.Frame) and event.command in (p.ACK, p.NACK):
                    finished = True
                    break
                else:
                    self._note(event)
        return Download(self.serial, [g for g in groups if g.tests], finished, bytes(received), corrupt)

    def close(self) -> None:
        """Tells the device the PC is done, as the maker's program does when its window closes."""
        try:
            self.send(p.command(p.NACK))
        finally:
            self._link.close()

    def _note(self, event) -> None:
        if isinstance(event, p.Frame) and event.kind == p.KIND_IDENTIFICATION:
            self.serial = p.serial_number(event)
        elif isinstance(event, p.Frame) and event.command == p.ACK:
            self._version_from(event)

    def _version_from(self, ack: p.Frame) -> None:
        if len(ack.data) > 1 and 0 < ack.data[1] < 4:
            self.protocol_version = ack.data[1]
