"""Talking to the audiometer: who it is, and fetching the tests in its memory.

The order and the bytes are the maker's program's own over USB (docs/hearing/통신_절차.md §5):

    connect       PC  KC 16 (length 0x10, version 4, 1)      device  KI serial number
    download      PC  KC 16 again                             device  KC ack (protocol version)
                  PC  KC 01 (get all)                         device  KA patient, KT test, KT test,
                                                                      KA patient, KT ..., KC ack
    close         PC  KC 07

The operator may have to press Connect to PC on the device first, and may press it only after
asking here, so ``connect`` keeps asking for a while. A device may also send its tests by
itself instead of answering (the maker's old firmware path); those are taken as they come and
nothing more is asked for, as the maker's program does.

Like the maker's program over USB, a test that comes with no device patient before it, or that
the device sent unasked, is answered with KC 07 (``protocol.USB_TEST_ACK``): a device that waits
for that answer before sending the next test would otherwise stop after the first.

The device keeps its tests after they are fetched, so every download brings back old tests
too; telling new from old is up to the caller (``Download.new_since``).
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as p

ANSWER_WAIT = 1.5
"""Seconds to wait for the answer to one identification."""
DEVICE_WAIT = 30.0
"""Seconds ``connect`` keeps asking: time for the operator to press Connect to PC on the device."""
QUIET_END = 6.0
"""Seconds without a byte after which a download is taken as finished even without its ack."""
DOWNLOAD_LIMIT = 120.0

TESTS = (p.ToneAudiogram, p.SpeechAudiogram, p.OtherTest)
ANSWERED, PUSHED = "answered", "pushed"


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
    pushed: bool = False
    """True when the device sent its tests by itself, without being asked: then it has no word
    for 'that was everything', and going quiet is how such a download ends."""
    repeats: int = 0
    """Tests that came twice in this download (once pushed, once asked for), kept once."""

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
    """One open link to the audiometer. ``usb`` decides the identification's length field.
    ``acknowledge`` False sends no KC 07 for tests: for a connection check, which keeps no test,
    so that a device that sends its tests by itself does not take them as delivered."""

    def __init__(self, link, usb: bool = True, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str, bytes], None] | None = None, acknowledge: bool = True) -> None:
        self._link = link
        self._usb = usb
        self._clock = clock
        self._decoder = p.StreamDecoder()
        self._log = log or (lambda direction, data: None)
        self._acknowledge = acknowledge
        self._held: list = []
        self._asked_all = False
        self._patients = False
        self.serial = ""
        self.protocol_version: int | None = None
        self.answered = False
        """The device answered the identification (a serial number or an ack)."""

    # --- low level ---------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        self._log("PC>", data)
        self._link.write(data)

    def _read(self, until: float) -> list:
        """What one read of the link brings (it waits up to the link's timeout)."""
        data = self._link.read()
        if data:
            self._log("<DEV", data)
            events = self._decoder.feed(data)
        elif self._decoder.pending and self._clock() >= until:
            events = self._decoder.flush()
        else:
            return []
        for event in events:
            self._arrived(event)
        return events

    def _arrived(self, event) -> None:
        """Answers a test the way the maker's program does over USB, the moment it arrives."""
        if isinstance(event, p.Frame) and event.kind == p.KIND_PATIENT:
            self._patients = True
        elif isinstance(event, TESTS) and self._acknowledge and (not self._asked_all or not self._patients):
            self.send(p.command(p.USB_TEST_ACK))

    def _events(self, until: float) -> list:
        """Packets put aside while waiting for an answer first, then one read."""
        if self._held:
            held, self._held = self._held, []
            return held
        return self._read(until)

    def _wait_for_answer(self, wait: float, answers: Callable[[object], bool]) -> bool:
        """Reads until ``answers`` accepts a packet or ``wait`` seconds pass. Anything else that comes
        meanwhile (a test sent unasked, a ping) is put aside for the download, never read twice."""
        deadline = self._clock() + wait
        aside = []
        answered = False
        while not answered and self._clock() < deadline:
            for event in self._read(deadline):
                if isinstance(event, p.Frame) and event.kind != p.KIND_PATIENT:
                    # Answers, acks and pings are taken now and never kept: an ack kept for later
                    # would read as the end of the download.
                    self._note(event)
                    answered = answered or answers(event)
                else:
                    aside.append(event)  # a test or patient sent unasked: kept for the download
        self._held.extend(aside)
        return answered

    def _holds_tests(self) -> bool:
        return any(isinstance(event, TESTS) for event in self._held)

    # --- steps -------------------------------------------------------------------------

    def connect(self, wait: float = DEVICE_WAIT, waiting: Callable[[int], None] | None = None) -> str:
        """Asks who the device is until it answers or starts sending tests by itself, for up to
        ``wait`` seconds. ``waiting`` is told the whole seconds left each time it has not yet.
        Returns ANSWERED or PUSHED; raises SessionError when the device says nothing."""
        deadline = self._clock() + wait
        while True:
            self.send(p.identify(usb=self._usb))
            if self._wait_for_answer(min(ANSWER_WAIT, max(deadline - self._clock(), 0.1)), _is_answer):
                self.answered = True
                return ANSWERED
            if self._holds_tests():
                return PUSHED
            left = deadline - self._clock()
            if left <= 0:
                raise SessionError("청력검사기가 답하지 않습니다")
            if waiting:
                waiting(math.ceil(left))

    def identify(self, wait: float = ANSWER_WAIT) -> str:
        """Asks who the device is, twice at most as the maker does. Returns the serial number."""
        for _ in range(2):
            self.send(p.identify(usb=self._usb))
            if self._wait_for_answer(wait, lambda event: event.kind == p.KIND_IDENTIFICATION):
                self.answered = True
                return self.serial
        raise SessionError("청력검사기가 답하지 않습니다")

    def download(self, quiet: float = QUIET_END, limit: float = DOWNLOAD_LIMIT,
                 progress: Callable[[int], None] | None = None) -> Download:
        """Fetches every test in the device's memory; or, when the device has started sending its
        tests by itself without answering, takes those until it goes quiet."""
        pushed = not self.answered and self._holds_tests()
        if not pushed:
            self.send(p.identify(usb=self._usb))
            self._wait_for_answer(ANSWER_WAIT, lambda event: isinstance(event, p.Frame) and (
                event.command == p.ACK or event.kind == p.KIND_IDENTIFICATION))
            self.send(p.command(p.GET_ALL))
            self._asked_all = True
            self._patients = False
        groups: list[DeviceGroup] = []
        corrupt: list[p.Corrupt] = []
        received = bytearray()
        kept: dict[str, DeviceGroup] = {}
        repeats = 0
        start = last = self._clock()
        finished = False

        def take(event) -> bool:
            """Files one event; True when it is the device's 'that was everything'."""
            nonlocal repeats
            if isinstance(event, p.Corrupt):
                corrupt.append(event)
                return False
            received.extend(event.packet.raw if hasattr(event, "packet") else event.raw)
            if isinstance(event, p.Frame) and event.kind == p.KIND_PATIENT:
                groups.append(DeviceGroup(p.device_patient(event)))
            elif isinstance(event, TESTS):
                mark = fingerprint(event)
                if not groups:
                    groups.append(DeviceGroup(None))
                if mark in kept:
                    repeats += 1
                    first = kept[mark]
                    if first.patient is None and groups[-1].patient is not None:
                        # sent unasked before anything else, then again under its patient: kept there
                        first.tests[:] = [t for t in first.tests if fingerprint(t) != mark]
                        groups[-1].tests.append(event)
                        kept[mark] = groups[-1]
                    return False
                kept[mark] = groups[-1]
                groups[-1].tests.append(event)
                if not self.serial:
                    self.serial = event.packet.serial
                if progress:
                    progress(len(kept))
            elif isinstance(event, p.Frame) and event.command in (p.ACK, p.NACK):
                return not pushed
            else:
                self._note(event)
            return False

        while not finished:
            now = self._clock()
            if now - last > quiet or now - start > limit:
                break
            for event in self._events(last + quiet):
                last = self._clock()
                if take(event):
                    finished = True
                    break
        if not finished:
            # The device went quiet without its closing ack: whole packets still waiting behind a
            # broken one are recovered rather than lost.
            for event in self._decoder.flush():
                self._arrived(event)
                take(event)
        return Download(self.serial, [g for g in groups if g.tests], finished, bytes(received), corrupt,
                        pushed, repeats)

    def close(self, goodbye: bool = True) -> None:
        """Tells the device the PC is done, as the maker's program does when its window closes.
        Without ``goodbye`` the link is only closed, as the maker's search for devices does: over
        USB the goodbye is the same KC 07 that answers a test."""
        try:
            if goodbye:
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


def _is_answer(event) -> bool:
    """The identification's answer: the serial number, or an ack from a device that gives only that."""
    return isinstance(event, p.Frame) and (event.kind == p.KIND_IDENTIFICATION or event.command == p.ACK)
