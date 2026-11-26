"""Decoding of what the TSM-300 sends over its serial port.

A transmitted measurement is a run of records, each a four-letter ASCII tag, a
little-endian 16-bit length and that many bytes of payload. It opens with ``PAID``
and closes with a bare ``STOP`` tag. At power-on the device instead sends two
NUL-padded text lines ending in LF CR: ``ID <model>`` and ``KEY<code>``.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

BAUD_RATE = 9600

MESSAGE_START = b"PAID"
MESSAGE_END = b"STOP"
LINE_END = b"\n\r"

TYMPANOGRAM_POINTS = 160
REFLEX_POINTS = 40
REFLEX_FREQUENCIES = (500, 1000, 2000, 3000, 4000)

PRESSURE_STEP = 3.125
PRESSURE_AT_LAST_POINT = 200.0
ML_PER_TYMPANOGRAM_COUNT = 1 / 64
TYMPANOGRAM_OFFSET_ML = 0.5
ML_PER_REFLEX_COUNT = 0.2 / 256

_MAX_RECORD_LENGTH = 1024
_MAX_PENDING_TEXT = 256
_TAG = re.compile(rb"[A-Z0-9]{4}")
_EAR_SCALARS = {"MEP": "<h", "PVO": "<H", "COM": "<H", "TWI": "<H"}


class ProtocolError(ValueError):
    """The bytes do not form a valid TSM-300 message."""


def tympanogram_pressures() -> list[float]:
    """The pressure in daPa of each tympanogram point, from -296.875 up to +200."""
    last = TYMPANOGRAM_POINTS - 1
    return [PRESSURE_AT_LAST_POINT - (last - i) * PRESSURE_STEP for i in range(TYMPANOGRAM_POINTS)]


@dataclass(frozen=True)
class EarResult:
    tympanogram: tuple[int, ...]
    middle_ear_pressure: int
    ear_volume: float
    compliance: float
    width: int
    reflexes: dict[int, tuple[int, ...]] = field(default_factory=dict)

    @property
    def compliance_curve(self) -> list[float]:
        """The tympanogram in ml; the device shifts it so the +200 daPa end sits near zero."""
        return [count * ML_PER_TYMPANOGRAM_COUNT - TYMPANOGRAM_OFFSET_ML for count in self.tympanogram]

    def reflex_curve(self, frequency: int) -> list[float]:
        """The compliance change in ml during the reflex test at ``frequency`` Hz."""
        return [count * ML_PER_REFLEX_COUNT for count in self.reflexes[frequency]]


@dataclass(frozen=True)
class Measurement:
    memory_id: int
    left: EarResult | None
    right: EarResult | None
    device_name: str
    device_code: str
    unknown: dict[str, bytes] = field(default_factory=dict)
    raw: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class PowerOn:
    """The identification the device sends when it is switched on."""

    model: str
    key: str


@dataclass(frozen=True)
class Corrupt:
    """Bytes that started like a message but could not be decoded."""

    reason: str
    data: bytes


def split_records(message: bytes) -> list[tuple[str, bytes]]:
    """Splits one complete message into (tag, payload) pairs, without the closing STOP."""
    length = _message_length(message)
    if length is None:
        raise ProtocolError("message is incomplete")
    if length != len(message):
        raise ProtocolError(f"{len(message) - length} bytes follow the STOP tag")
    records = []
    position = 0
    while position < length - len(MESSAGE_END):
        tag = message[position:position + 4].decode("ascii")
        (size,) = struct.unpack_from("<H", message, position + 4)
        records.append((tag, bytes(message[position + 6:position + 6 + size])))
        position += 6 + size
    return records


def parse_message(message: bytes) -> Measurement:
    records = dict(split_records(message))
    if "PAID" not in records:
        raise ProtocolError("message has no PAID record")
    memory_id = _unpack("PAID", records.pop("PAID"), "<H")
    device_name = records.pop("DNAM", b"").decode("ascii", "replace")
    device_code = records.pop("CODE", b"").decode("ascii", "replace")
    left = _take_ear(records, "L")
    right = _take_ear(records, "R")
    return Measurement(memory_id, left, right, device_name, device_code, unknown=records, raw=bytes(message))


class StreamDecoder:
    """Turns the serial byte stream into Measurement, PowerOn and Corrupt events."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._model: str | None = None

    @property
    def pending(self) -> int:
        """How many received bytes are still waiting for the rest of their message."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[Measurement | PowerOn | Corrupt]:
        self._buffer += data
        events: list[Measurement | PowerOn | Corrupt] = []
        while self._step(events):
            pass
        return events

    def flush(self) -> list[Corrupt]:
        """Gives up on a message that stopped arriving part-way."""
        leftover = bytes(self._buffer)
        self._buffer.clear()
        if leftover.startswith(MESSAGE_START):
            return [Corrupt("message stopped before its STOP tag", leftover)]
        return []

    def _step(self, events: list) -> bool:
        """Consumes one item from the buffer. Returns False when more bytes are needed."""
        buffer = self._buffer
        start = buffer.find(MESSAGE_START)
        if start != 0:
            text_end = len(buffer) if start == -1 else start
            line_end = buffer.find(LINE_END, 0, text_end)
            if line_end != -1:
                self._identification_line(bytes(buffer[:line_end]), events)
                del buffer[:line_end + len(LINE_END)]
                return True
            if start == -1:
                del buffer[:max(0, len(buffer) - _MAX_PENDING_TEXT)]
                return False
            del buffer[:start]
            return True
        try:
            length = _message_length(buffer)
            if length is None:
                return False
            message = bytes(buffer[:length])
            events.append(parse_message(message))
            del buffer[:length]
        except ProtocolError as error:
            resume = buffer.find(MESSAGE_START, len(MESSAGE_START))
            end = len(buffer) if resume == -1 else resume
            events.append(Corrupt(str(error), bytes(buffer[:end])))
            del buffer[:end]
        return True

    def _identification_line(self, line: bytes, events: list) -> None:
        text = line.replace(b"\x00", b"").decode("ascii", "replace").strip()
        if text.startswith("ID "):
            self._model = text[3:]
        elif text.startswith("KEY"):
            events.append(PowerOn(self._model or "", text[3:]))
            self._model = None


def _message_length(data) -> int | None:
    """The length of the message at the start of ``data``, or None while it is incomplete."""
    position = 0
    while True:
        tag = bytes(data[position:position + 4])
        if len(tag) < 4:
            return None
        if tag == MESSAGE_END:
            return position + 4
        if not _TAG.fullmatch(tag):
            raise ProtocolError(f"invalid record tag {tag!r} at byte {position}")
        if len(data) < position + 6:
            return None
        (size,) = struct.unpack_from("<H", data, position + 4)
        if size > _MAX_RECORD_LENGTH:
            raise ProtocolError(f"record {tag.decode()} claims {size} bytes")
        position += 6 + size


def _unpack(tag: str, payload: bytes, layout: str) -> int:
    if len(payload) != struct.calcsize(layout):
        raise ProtocolError(f"record {tag} has {len(payload)} bytes")
    return struct.unpack(layout, payload)[0]


def _take_ear(records: dict[str, bytes], side: str) -> EarResult | None:
    """Removes one ear's records from ``records`` and builds its result; None if not measured."""
    tympanogram = records.pop(f"{side}T02", None)
    if tympanogram is None:
        raise ProtocolError(f"message has no {side}T02 record")
    if len(tympanogram) != TYMPANOGRAM_POINTS:
        raise ProtocolError(f"record {side}T02 has {len(tympanogram)} bytes")
    scalars = {}
    for name, layout in _EAR_SCALARS.items():
        tag = side + name
        if tag not in records:
            raise ProtocolError(f"message has no {tag} record")
        scalars[name] = _unpack(tag, records.pop(tag), layout)
    reflexes = {}
    for frequency in REFLEX_FREQUENCIES:
        tag = f"{side}R{frequency // 100:02d}"
        if tag in records:
            samples = records.pop(tag)
            if len(samples) != REFLEX_POINTS:
                raise ProtocolError(f"record {tag} has {len(samples)} bytes")
            reflexes[frequency] = tuple(samples)
    if not any(tympanogram) and not any(scalars.values()) and not reflexes:
        return None
    return EarResult(
        tympanogram=tuple(tympanogram),
        middle_ear_pressure=scalars["MEP"],
        ear_volume=scalars["PVO"] / 1000,
        compliance=scalars["COM"] / 1000,
        width=scalars["TWI"],
        reflexes=reflexes,
    )
