"""Reading a serial port into bursts and persisting a capture session to disk."""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .analyze import is_line_noise

_LINE_NAMES = ("cts", "dsr", "cd", "ri")


@dataclass
class Burst:
    index: int
    started: float
    ended: float
    data: bytes


class BurstSplitter:
    """Groups a byte stream into bursts separated by at least ``idle_gap`` seconds of silence."""

    def __init__(self, idle_gap: float) -> None:
        self.idle_gap = idle_gap
        self._buffer = bytearray()
        self._started = 0.0
        self._last = 0.0
        self._count = 0

    def feed(self, data: bytes, now: float) -> None:
        if not self._buffer:
            self._started = now
        self._buffer += data
        self._last = now

    def poll(self, now: float) -> Burst | None:
        if self._buffer and now - self._last >= self.idle_gap:
            return self.flush()
        return None

    def flush(self) -> Burst | None:
        if not self._buffer:
            return None
        self._count += 1
        burst = Burst(self._count, self._started, self._last, bytes(self._buffer))
        self._buffer.clear()
        return burst


class SessionWriter:
    """Persists a session: the raw stream, a timeline, one file per burst and a summary."""

    def __init__(self, directory: Path, meta: dict) -> None:
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self._lock = threading.Lock()
        self._raw = open(directory / "raw.bin", "ab")
        self._timeline = open(directory / "timeline.jsonl", "a", encoding="utf-8")
        self._offset = 0
        self._summary = dict(meta, scan=[], bursts=[])
        self._write_summary()

    def set_meta(self, **fields) -> None:
        with self._lock:
            self._summary.update(fields)
            self._write_summary()

    def scan_result(self, baud: int, data: bytes) -> None:
        with self._lock:
            name = f"scan_{baud}.bin"
            (self.directory / name).write_bytes(data)
            self._summary["scan"].append({"baud": baud, "file": name, "length": len(data)})
            self._write_summary()

    def rx(self, t: float, data: bytes) -> None:
        with self._lock:
            self._raw.write(data)
            self._raw.flush()
            self._log(t, "rx", offset=self._offset, length=len(data))
            self._offset += len(data)

    def event(self, t: float, name: str, **fields) -> None:
        with self._lock:
            self._log(t, name, **fields)

    def burst(self, burst: Burst) -> None:
        with self._lock:
            name = f"burst_{burst.index:03d}.bin"
            (self.directory / name).write_bytes(burst.data)
            self._summary["bursts"].append({
                "index": burst.index,
                "file": name,
                "started": round(burst.started, 3),
                "ended": round(burst.ended, 3),
                "length": len(burst.data),
                "note": "",
            })
            self._write_summary()

    def note(self, index: int, text: str) -> None:
        with self._lock:
            for entry in self._summary["bursts"]:
                if entry["index"] == index:
                    entry["note"] = text
            self._write_summary()

    def close(self) -> None:
        with self._lock:
            self._raw.close()
            self._timeline.close()

    def _log(self, t: float, name: str, **fields) -> None:
        record = {"t": round(t, 4), "event": name, **fields}
        self._timeline.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._timeline.flush()

    def _write_summary(self) -> None:
        text = json.dumps(self._summary, ensure_ascii=False, indent=2)
        (self.directory / "session.json").write_text(text, encoding="utf-8")


def read_control_lines(ser) -> dict | None:
    """Returns the modem status inputs, or None where the port cannot report them."""
    try:
        return {name: bool(getattr(ser, name)) for name in _LINE_NAMES}
    except (OSError, ValueError):
        return None


class CaptureEngine:
    """Reads the port on a background thread, persists every byte and queues finished bursts."""

    def __init__(self, ser, writer: SessionWriter, idle_gap: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.bursts: queue.Queue[Burst] = queue.Queue()
        self.error: Exception | None = None
        self._ser = ser
        self._writer = writer
        self._splitter = BurstSplitter(idle_gap)
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tsm300-capture", daemon=True)

    def start(self) -> None:
        self._t0 = self._clock()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        lines = read_control_lines(self._ser)
        if lines is not None:
            self._writer.event(0.0, "lines", **lines)
        while not self._stop.is_set():
            try:
                data = self._ser.read(self._ser.in_waiting or 1)
            except Exception as exc:  # the adapter was unplugged or the driver failed
                self.error = exc
                break
            now = self._clock() - self._t0
            if data:
                self._writer.rx(now, data)
                self._splitter.feed(data, now)
            self._emit(self._splitter.poll(now))
            if lines is not None:
                current = read_control_lines(self._ser)
                if current is not None and current != lines:
                    lines = current
                    self._writer.event(now, "lines", **lines)
        self._emit(self._splitter.flush())

    def _emit(self, burst: Burst | None) -> None:
        if burst is not None:
            self._writer.burst(burst)
            self.bursts.put(burst)


def listen_once(ser, wait: float, settle: float,
                clock: Callable[[], float] = time.monotonic,
                on_wait: Callable[[float, int], None] | None = None) -> bytes:
    """Collects one message: waits up to ``wait`` seconds for data, then for ``settle`` seconds of silence.

    Line noise from the device powering off does not count as the message, so listening
    continues through it for the full ``wait``.
    """
    start = clock()
    last = start
    buffer = bytearray()
    hard_limit = wait + 30.0
    while True:
        chunk = ser.read(ser.in_waiting or 1)
        now = clock()
        if chunk:
            buffer += chunk
            last = now
        quiet = now - last >= settle
        elapsed = now - start
        if buffer and not is_line_noise(bytes(buffer)) and quiet:
            break
        if elapsed >= wait and (quiet or not buffer):
            break
        if elapsed >= hard_limit:
            break
        if on_wait is not None:
            on_wait(max(0.0, wait - elapsed), len(buffer))
    return bytes(buffer)
