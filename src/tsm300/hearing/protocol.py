"""Decoding of what MRS Resonance audiometers (R17A, R27A, R37A, R35C ...) send to a PC.

Written from what was worked out from the maker's PC program (MDS 3.03) to work with its devices,
and checked against the sample packets it ships with. docs/hearing/통신_절차.md sums it up.

Every packet has the same frame:

    'K'  kind  n(2, big-endian)  data(n)  xor  'k'

``xor`` is the exclusive-or of the ``n`` data bytes and the whole packet is ``n + 6`` bytes.
Two kinds break the rule: ``K^`` (live tone status, 19 bytes) and ``K\\`` (16 bytes).

A result is a ``KT`` packet whose data starts with the 13-character serial number, one byte
whose meaning is unknown, the mode and the test number. ``KT`` carries neither the patient
nor the date: the maker's program files a test under the ``KA`` patient packet that came
just before it and dates it by the PC's clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import reduce
from typing import Union

START = 0x4B  # 'K'
END = 0x6B  # 'k'

KIND_TEST = ord("T")
KIND_TEST_R1 = ord("t")
KIND_COMMAND = ord("C")
KIND_PATIENT = ord("A")
KIND_IDENTIFICATION = ord("I")
FIXED_SIZES = {ord("^"): 19, ord("\\"): 16, 0xC1: 10}

HEADER = 20  # 'KT', n, serial(13), one byte, mode, test
SERIAL_LENGTH = 13
MAX_PACKET = 65535 + 6

MODE_AUDIOMETRY = 0
MODE_IMPEDANCE = 1

TEST_TONAL = 0
TEST_SPEECH = 1
TEST_SPEECH_DUTCH = 30
TEST_TONAL_HF = 11

TEST_NAMES = {
    (0, 0): "순음청력", (0, 1): "어음청력", (0, 30): "어음청력(네덜란드어)", (0, 2): "ABLB",
    (0, 3): "자동 순음", (0, 11): "고주파 순음", (0, 4): "다주파수", (0, 5): "MLB", (0, 6): "DLI",
    (0, 7): "SISI", (0, 8): "Tone decay", (0, 10): "Bekesy", (0, 25): "GAP", (0, 27): "DLF", (0, 35): "이명",
    (1, 12): "고막운동도", (1, 13): "등골근 반사", (1, 14): "반사 피로", (1, 15): "ARLT",
    (1, 16): "이관기능(정상 고막)", (1, 17): "이관기능(천공)", (1, 18): "Quick check",
    (1, 19): "Quick screening", (1, 20): "Quick decay", (1, 21): "Non acoustic",
    (1, 22): "Growth DLI", (1, 23): "고주파 고막운동도",
}

# --- pure tone -----------------------------------------------------------------------------

TONE_FREQUENCIES = (125, 250, 500, 750, 1000, 1500, 2000, 3000, 4000, 6000, 8000, 10000, 12500)
HF_FREQUENCIES = (8000, 9000, 10000, 11200, 12500, 14000, 16000, 18000, 20000)


@dataclass(frozen=True)
class Marks:
    """The special byte values of one kind of block. The high-frequency test uses ones 10 lower."""

    none: int
    """Not tested (level), or no masking (masking)."""
    silent: int
    """Masking byte: no response, without masking."""
    silent_masked: int
    """Masking byte: no response, with masking."""


MARKS = Marks(245, 244, 243)
HF_MARKS = Marks(235, 234, 233)

INPUTS = ("TONE", "WARBLE", "NBNOISE", "WNOISE", "SNOISE", "MIKE", "EXT", "SD CARD", "PNOISE")
OUTPUTS = ("AC", "BC", "SPKR", "INS", "AC-U", "SPKR-A", "AC 1", "BC 1", "SPKR 1", "SPKR-A 1")

TONE_BLOCK = 7 + 2 * len(TONE_FREQUENCIES)
HF_BLOCK = 7 + 2 * len(HF_FREQUENCIES)

# The order of the tone blocks in the packet: what was measured, and on which ear.
# "AcU" is the uncomfortable level (UCL), "AcM" the most comfortable level (MCL),
# "Spkr" soundfield without and "SpkrA" with a hearing aid.
TONE_ORDER = ("Ac R", "Bc R", "Spkr R", "AcU R", "SpkrA R", "Ac L", "Bc L", "Spkr L", "AcU L", "SpkrA L")
TONE_ORDER_WITH_MCL = TONE_ORDER + ("AcM R", "AcM L")
HF_ORDER = ("Ac R", "Spkr R", "Ac L", "Spkr L")

# --- speech --------------------------------------------------------------------------------

SPEECH_POINTS = 10
SPEECH_BLOCK = 10 + 3 * SPEECH_POINTS
SRT_BLOCK = 2
SPEECH_ORDER = ("Ac R", "Bc R", "Spkr R", "SpkrA R", "Ac L", "Bc L", "Spkr L", "SpkrA L")
SPEECH_ORDER_LONG = SPEECH_ORDER + ("Ac1 R", "Bc1 R", "Spkr1 R", "SpkrA1 R", "Ac1 L", "Bc1 L", "Spkr1 L", "SpkrA1 L")
SRT_ORDER = ("Ac R", "Bc R", "Spkr R", "SpkrA R", "Ac L", "Bc L", "Spkr L", "SpkrA L")
SPEECH_WITH_ANSWERS = 838


class ProtocolError(ValueError):
    """The bytes do not form a valid Resonance packet."""


def signed(byte: int) -> int:
    return byte - 256 if byte >= 128 else byte


def level(byte: int, marks: Marks = MARKS) -> int | None:
    """A stored level byte in dB HL; None when nothing was stored.

    Levels are signed bytes: 246 is -10 dB HL and 251 is -5, as the maker's charts, tables
    and averages read them. (Its NOAH export alone reads ``246 - byte``.)
    """
    return None if byte == marks.none else signed(byte)


@dataclass(frozen=True)
class Masking:
    level: int | None
    """The masking noise in dB, or None when none was given or not known."""
    masked: bool
    no_response: bool


def masking(byte: int, marks: Marks = MARKS) -> Masking:
    if byte == marks.none:
        return Masking(None, False, False)
    if byte == marks.silent:
        return Masking(None, False, True)
    if byte == marks.silent_masked:
        return Masking(None, True, True)
    return Masking(signed(byte), True, False)


@dataclass(frozen=True)
class TonePoint:
    frequency: int
    level: int
    """The threshold, or for no response the loudest tone given."""
    masking: int | None = None
    masked: bool = False
    no_response: bool = False


@dataclass(frozen=True)
class ToneCurve:
    """One curve of an audiogram: one transducer on one ear."""

    name: str
    ear: str
    """'R' or 'L'."""
    kind: str
    """'Ac', 'Bc', 'Spkr', 'SpkrA', 'AcU', 'AcM'."""
    stimulus: str
    masking_signal: str
    pulsed: bool
    points: tuple[TonePoint, ...]
    raw: bytes = field(repr=False, default=b"")

    def at(self, frequency: int) -> TonePoint | None:
        return next((p for p in self.points if p.frequency == frequency), None)


@dataclass(frozen=True)
class SpeechPoint:
    level: int
    score: int | None
    """Percent of words repeated correctly."""
    masking: int | None = None
    masked: bool = False
    correct: int | None = None
    """How many words were repeated correctly; only in the longest packets."""

    @property
    def words(self) -> int | None:
        """The number of words given, worked out the maker's way from ``correct`` and ``score``."""
        if self.correct is None or not self.score:
            return None
        return round(100 * self.correct / self.score)


@dataclass(frozen=True)
class SpeechCurve:
    name: str
    ear: str
    kind: str
    """'Ac', 'Bc', 'Spkr', 'SpkrA'; with a 1 after it ('Ac1') the second word list."""
    stimulus: str
    masking_signal: str
    material: int
    """The word list's number on the device."""
    points: tuple[SpeechPoint, ...]
    raw: bytes = field(repr=False, default=b"")

    @property
    def best(self) -> SpeechPoint | None:
        """The point with the highest score; at a tie the quieter one."""
        scored = [p for p in self.points if p.score is not None]
        if not scored:
            return None
        return max(scored, key=lambda p: (p.score, -p.level))


@dataclass(frozen=True)
class Srt:
    ear: str
    kind: str
    level: int | None


@dataclass(frozen=True)
class Packet:
    """One result packet as it arrived."""

    serial: str
    mode: int
    test: int
    raw: bytes = field(repr=False)

    @property
    def model(self) -> str:
        """'R37A', 'R27A', ... from the start of the serial number."""
        return self.serial[:4]

    @property
    def name(self) -> str:
        return TEST_NAMES.get((self.mode, self.test), f"알 수 없는 검사 {self.mode}/{self.test}")


@dataclass(frozen=True)
class ToneAudiogram:
    packet: Packet
    curves: tuple[ToneCurve, ...]
    high_frequency: bool = False

    def curve(self, kind: str, ear: str) -> ToneCurve | None:
        return next((c for c in self.curves if c.kind == kind and c.ear == ear), None)


@dataclass(frozen=True)
class SpeechAudiogram:
    packet: Packet
    curves: tuple[SpeechCurve, ...]
    srt: tuple[Srt, ...] = ()

    def curve(self, kind: str, ear: str) -> SpeechCurve | None:
        return next((c for c in self.curves if c.kind == kind and c.ear == ear), None)

    def srt_of(self, kind: str, ear: str) -> int | None:
        return next((s.level for s in self.srt if s.kind == kind and s.ear == ear), None)


@dataclass(frozen=True)
class OtherTest:
    """A test this program does not draw (tympanometry on the audiometer, SISI ...). Kept raw."""

    packet: Packet


@dataclass(frozen=True)
class Frame:
    """Any other packet: a command or answer ('C'), a patient ('A'), the serial number ('I') ..."""

    kind: int
    data: bytes
    raw: bytes = field(repr=False)

    @property
    def letter(self) -> str:
        return chr(self.kind) if 32 <= self.kind < 127 else f"0x{self.kind:02X}"

    @property
    def command(self) -> int | None:
        """For a 'C' packet, its command number (6 ack, 7 nack, 5 ping, 0x16 sync ...)."""
        return self.data[0] if self.kind == KIND_COMMAND and self.data else None


@dataclass(frozen=True)
class Corrupt:
    reason: str
    data: bytes


Result = Union[ToneAudiogram, SpeechAudiogram, OtherTest]


# --- frames --------------------------------------------------------------------------------

def xor(data: bytes) -> int:
    return reduce(lambda a, b: a ^ b, data, 0)


def packet_length(data: bytes) -> int | None:
    """The length of the packet that starts ``data``; None until enough bytes are in."""
    if len(data) < 2:
        return None
    if data[0] != START:
        raise ProtocolError("packet does not start with 'K'")
    if data[1] in FIXED_SIZES:
        return FIXED_SIZES[data[1]]
    if len(data) < 4:
        return None
    return int.from_bytes(data[2:4], "big") + 6


def check_frame(data: bytes) -> bytes:
    """The data of one complete variable-length packet, after checking its frame."""
    data = bytes(data)
    size = packet_length(data)
    if size != len(data):
        raise ProtocolError(f"packet says {size} bytes, has {len(data)}")
    if data[-1] != END:
        raise ProtocolError("packet does not end with 'k'")
    body = data[4:-2]
    if xor(body) != data[-2]:
        raise ProtocolError("check byte does not match")
    return body


def parse_packet(data: bytes) -> Result:
    """One complete ``KT`` packet."""
    data = bytes(data)
    if data[:2] != bytes([START, KIND_TEST]):
        raise ProtocolError("not a result packet")
    check_frame(data)
    if len(data) < HEADER + 2:
        raise ProtocolError("packet too short")
    serial = data[4:4 + SERIAL_LENGTH].decode("latin-1").rstrip("\x00 ")
    packet = Packet(serial=serial, mode=data[18], test=data[19], raw=data)
    body = data[HEADER:-2]
    if packet.mode == MODE_AUDIOMETRY and packet.test == TEST_TONAL:
        return _tonal(packet, body)
    if packet.mode == MODE_AUDIOMETRY and packet.test == TEST_TONAL_HF:
        return _tonal_hf(packet, body)
    if packet.mode == MODE_AUDIOMETRY and packet.test in (TEST_SPEECH, TEST_SPEECH_DUTCH):
        return _speech(packet, body)
    return OtherTest(packet)


def parse_frame(data: bytes) -> Frame:
    data = bytes(data)
    if data[1] in FIXED_SIZES:
        if len(data) != FIXED_SIZES[data[1]]:
            raise ProtocolError("wrong length")
        return Frame(kind=data[1], data=data[2:], raw=data)
    return Frame(kind=data[1], data=check_frame(data), raw=data)


def _ear_and_kind(name: str) -> tuple[str, str]:
    """'SpkrA R' -> ('R', 'SpkrA')."""
    kind, ear = name.split()
    return ear, kind


def _pick(names: tuple[str, ...], index: int) -> str:
    return names[index] if 0 <= index < len(names) else f"?{index}"


def _tone_curve(name: str, block: bytes, frequencies: tuple[int, ...], marks: Marks) -> ToneCurve | None:
    if block[0] != 1:
        return None
    count = len(frequencies)
    levels = block[7:7 + count]
    masks = block[7 + count:7 + 2 * count]
    points = []
    for frequency, value, mask in zip(frequencies, levels, masks):
        db = level(value, marks)
        if db is None:
            continue
        m = masking(mask, marks)
        points.append(TonePoint(frequency, db, m.level, m.masked, m.no_response))
    ear, kind = _ear_and_kind(name)
    return ToneCurve(name=name, ear=ear, kind=kind, stimulus=_pick(INPUTS, block[1]),
                     masking_signal=_pick(INPUTS, block[2]), pulsed=block[5] == 1,
                     points=tuple(points), raw=bytes(block))


def _tonal(packet: Packet, body: bytes) -> ToneAudiogram:
    if len(body) == len(TONE_ORDER_WITH_MCL) * TONE_BLOCK:
        order = TONE_ORDER_WITH_MCL
    elif len(body) == len(TONE_ORDER) * TONE_BLOCK:
        order = TONE_ORDER
    else:
        raise ProtocolError(f"pure tone packet has {len(body)} data bytes")
    curves = []
    for index, name in enumerate(order):
        curve = _tone_curve(name, body[index * TONE_BLOCK:(index + 1) * TONE_BLOCK], TONE_FREQUENCIES, MARKS)
        if curve is not None:
            curves.append(curve)
    return ToneAudiogram(packet, tuple(curves))


def _tonal_hf(packet: Packet, body: bytes) -> ToneAudiogram:
    if len(body) != len(HF_ORDER) * HF_BLOCK:
        raise ProtocolError(f"high-frequency packet has {len(body)} data bytes")
    curves = []
    for index, name in enumerate(HF_ORDER):
        curve = _tone_curve(name, body[index * HF_BLOCK:(index + 1) * HF_BLOCK], HF_FREQUENCIES, HF_MARKS)
        if curve is not None:
            curves.append(curve)
    return ToneAudiogram(packet, tuple(curves), high_frequency=True)


def _speech(packet: Packet, body: bytes) -> SpeechAudiogram:
    long_form = len(SPEECH_ORDER_LONG) * SPEECH_BLOCK + len(SRT_ORDER) * SRT_BLOCK
    if len(body) == len(SPEECH_ORDER) * SPEECH_BLOCK:
        order, srt_order, with_answers = SPEECH_ORDER, (), False
    elif len(body) == long_form:
        order, srt_order, with_answers = SPEECH_ORDER_LONG, SRT_ORDER, False
    elif len(body) == long_form + len(SPEECH_ORDER_LONG) * SPEECH_POINTS:
        order, srt_order, with_answers = SPEECH_ORDER_LONG, SRT_ORDER, True
    else:
        raise ProtocolError(f"speech packet has {len(body)} data bytes")
    position = 0
    blocks = []
    for name in order:
        blocks.append((name, body[position:position + SPEECH_BLOCK]))
        position += SPEECH_BLOCK
    srts = []
    for name in srt_order:
        block = body[position:position + SRT_BLOCK]
        position += SRT_BLOCK
        if block[0] == 1:
            ear, kind = _ear_and_kind(name)
            srts.append(Srt(ear, kind, level(block[1])))
    answers = []
    if with_answers:
        for _ in order:
            answers.append(body[position:position + SPEECH_POINTS])
            position += SPEECH_POINTS
    curves = []
    for index, (name, block) in enumerate(blocks):
        if block[0] != 1:
            continue
        correct = answers[index] if answers else bytes([MARKS.none]) * SPEECH_POINTS
        points = []
        for value, score, mask, right in zip(block[10:20], block[20:30], block[30:40], correct):
            db = level(value)
            if db is None:
                continue
            m = masking(mask)
            points.append(SpeechPoint(db, None if score == MARKS.none else score, m.level, m.masked,
                                      None if right == MARKS.none else right))
        ear, kind = _ear_and_kind(name)
        curves.append(SpeechCurve(name=name, ear=ear, kind=kind, stimulus=_pick(INPUTS, block[1]),
                                  masking_signal=_pick(INPUTS, block[2]), material=block[7],
                                  points=tuple(points), raw=bytes(block)))
    return SpeechAudiogram(packet, tuple(curves), tuple(srts))


# --- the byte stream -----------------------------------------------------------------------

KNOWN_KINDS = frozenset(b"TtCAIQfJdWL MpqrS") | frozenset(FIXED_SIZES)
"""Every packet kind the maker's program knows (docs/hearing/통신_절차.md §4.1)."""
MAX_DATA = {KIND_TEST: 16384, KIND_TEST_R1: 16384, KIND_COMMAND: 64}
MAX_OTHER_DATA = 8192
"""The largest data a packet of each kind may claim. A tympanogram, the largest, is about 8 kB."""


def _start_at(buffer, i: int) -> str:
    """Whether a packet could start at ``i``: 'no', 'wait' (not all of it is in yet) or 'yes'
    (all of it is in and its end mark and check byte are right)."""
    if buffer[i] != START:
        return "no"
    if i + 1 >= len(buffer):
        return "wait"
    kind = buffer[i + 1]
    if kind not in KNOWN_KINDS:
        return "no"
    if kind in FIXED_SIZES:
        return "yes" if i + FIXED_SIZES[kind] <= len(buffer) else "wait"
    if i + 4 > len(buffer):
        return "wait"
    n = int.from_bytes(buffer[i + 2:i + 4], "big")
    if n > MAX_DATA.get(kind, MAX_OTHER_DATA):
        return "no"
    end = i + n + 6
    if end > len(buffer):
        return "wait"
    if buffer[end - 1] != END or xor(buffer[i + 4:end - 2]) != buffer[end - 2]:
        return "no"
    return "yes"


class StreamDecoder:
    """Turns the bytes read from the device into results, other packets and corrupt pieces.

    However the bytes come — noise, a packet cut short when the cable moved, a wrong check
    byte — it never stops for good and never takes a good packet for part of a bad one: a
    bad stretch is dropped up to the next place where a whole, correct packet starts.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list:
        self._buffer += data
        events: list = []
        while self._step(events):
            pass
        return events

    def flush(self) -> list:
        """The link went quiet: whatever is left is not going to be completed. Recovers any whole
        packet still inside it and reports the rest as corrupt."""
        events: list = []
        while self._buffer:
            if not self._step(events):
                self._skip(events, "packet stopped part-way", final=True)
        return events

    def _step(self, events: list) -> bool:
        buffer = self._buffer
        if not buffer:
            return False
        state = _start_at(buffer, 0)
        if state == "no":
            return self._skip(events, self._why_not(buffer))
        if state == "wait":
            return False
        size = packet_length(buffer)
        chunk = bytes(buffer[:size])
        try:
            event = parse_packet(chunk) if chunk[1] == KIND_TEST else parse_frame(chunk)
        except ProtocolError as error:
            return self._skip(events, str(error))
        events.append(event)
        del buffer[:size]
        return True

    @staticmethod
    def _why_not(buffer) -> str:
        if buffer[0] != START:
            return "bytes outside a packet"
        if len(buffer) < 2 or buffer[1] not in KNOWN_KINDS:
            return "unknown packet kind"
        try:
            check_frame(bytes(buffer[:packet_length(buffer)]))
        except ProtocolError as error:
            return str(error)
        return "packet claims more data than any packet holds"

    def _skip(self, events: list, reason: str, final: bool = False) -> bool:
        """Drops a bad stretch up to the next whole, correct packet. When none is in yet, up to the
        first place one could still be arriving (none at all when ``final``)."""
        buffer = self._buffer
        resume = None
        waiting = None
        for i in range(1, len(buffer)):
            state = _start_at(buffer, i)
            if state == "yes":
                resume = i
                break
            if state == "wait" and waiting is None and not final:
                waiting = i
        end = resume if resume is not None else (waiting if waiting is not None else len(buffer))
        events.append(Corrupt(reason, bytes(buffer[:end])))
        del buffer[:end]
        return resume is not None


# --- commands the PC may send --------------------------------------------------------------
# Only commands that read. Never send delete (0x03), serial-number, calibration, stimulus
# or patient-upload commands from this program (docs/hearing/통신_절차.md §6).

SYNC = 0x16
GET_ALL = 0x01
DEVICE_INFO = 0x04
PING = 0x05
ACK = 0x06
NACK = 0x07
READ_ONLY = frozenset({SYNC, GET_ALL, DEVICE_INFO, PING, ACK, NACK})
USB_TEST_ACK = NACK
"""What the maker's program sends over USB to say a test arrived: 0x07, not 0x06.
It sends it after a test that came with no device patient before it, and after a test the
device sent unasked; docs/hearing/통신_절차.md §5.3."""


def command(number: int, *args: int, length_field: int = 0x0A) -> bytes:
    """A 16-byte 'KC' command. Refuses anything that could change the device."""
    if number not in READ_ONLY:
        raise ValueError(f"command 0x{number:02X} is not one this program sends")
    body = bytes([number, *args]).ljust(10, b"\x00")[:10]
    return bytes([START, KIND_COMMAND]) + length_field.to_bytes(2, "big") + body + bytes([xor(body), END])


def identify(usb: bool = True, version: int = 4) -> bytes:
    """Asks the device for its serial number. Over USB the maker sends 0x10 in the length field."""
    return command(SYNC, version, 1, length_field=0x10 if usb else 0x0A)


# --- the device's own patient list ---------------------------------------------------------

@dataclass(frozen=True)
class DevicePatient:
    """A ``KA`` packet: a patient as the audiometer has them. Tests that follow belong to them."""

    number: str
    """The device's own number for the patient."""
    pc_number: str
    """The number a PC program gave when it put the patient on the device (0 when typed on it)."""
    first_name: str
    surname: str
    birth_date: str
    """As the device holds it, dd/MM/yyyy."""
    sex: str
    raw: bytes = field(repr=False, default=b"")

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.surname, self.first_name) if p)


def device_patient(frame: Frame) -> DevicePatient:
    if frame.kind != KIND_PATIENT:
        raise ProtocolError("not a patient packet")
    fields = frame.data.decode("utf-8", "replace").split(";") + [""] * 8
    return DevicePatient(number=fields[0].strip(), pc_number=fields[1].strip(), first_name=fields[2].strip(),
                         surname=fields[3].strip(), birth_date=fields[4].strip(), sex=fields[5].strip(),
                         raw=frame.raw)


def serial_number(frame: Frame) -> str:
    """The serial number in an identification (``KI``) packet."""
    return frame.data.decode("utf-8", "replace").rstrip("\x00 ")


def frame(kind: int, data: bytes) -> bytes:
    """A variable-length packet around ``data`` (the device's side; used by the simulator)."""
    return bytes([START, kind]) + len(data).to_bytes(2, "big") + data + bytes([xor(data), END])
