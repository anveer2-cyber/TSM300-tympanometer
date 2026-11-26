"""Waiting on the serial port and saving each measurement the moment the device sends it."""

from __future__ import annotations

import os
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable

import serial

from . import __version__
from .console import Console, choose_port, open_port
from .patient import Patient
from .protocol import Corrupt, EarResult, Measurement, PowerOn, StreamDecoder
from .report import prepare
from .store import free_stem, save_result

STALE_AFTER = 3.0
RETRY_AFTER = 5.0

READY = """\
검사 결과를 기다리는 중입니다.
  장비에서 검사를 마친 뒤 -> 키(오른쪽 화살표)를 누르면 바로 저장됩니다.
  (장비 설정: MENU -> Right key -> TRANSMIT)
끝내려면 Ctrl+C 를 누르세요.
"""


def describe_ear(name: str, ear: EarResult | None) -> str:
    if ear is None:
        return f"  {name}: 측정 없음"
    reflex = f", 반사 {len(ear.reflexes)}개 주파수" if ear.reflexes else ""
    return (
        f"  {name}: 압력 {ear.middle_ear_pressure} daPa, 용적 {ear.ear_volume:.2f} ml, "
        f"컴플라이언스 {ear.compliance:.2f} ml, 폭 {ear.width} daPa{reflex}"
    )


def read_events(ser, stop: Callable[[], bool] = lambda: False,
                clock: Callable[[], float] = time.monotonic):
    """Yields Measurement, PowerOn and Corrupt events from the port until ``stop()`` is true."""
    decoder = StreamDecoder()
    last_data = clock()
    while not stop():
        data = ser.read(ser.in_waiting or 1)
        if data:
            last_data = clock()
            yield from decoder.feed(data)
        elif decoder.pending and clock() - last_data > STALE_AFTER:
            yield from decoder.flush()


class Listener:
    """Feeds received bytes to the decoder and acts on what comes out."""

    def __init__(self, console: Console, directory: Path, show: bool = False,
                 now: Callable[[], datetime] = datetime.now, patient: Patient = Patient()) -> None:
        self.saved: list[Path] = []
        self._console = console
        self._directory = directory
        self._show = show
        self._now = now
        self._patient = patient

    def run(self, ser, stop: Callable[[], bool] = lambda: False,
            clock: Callable[[], float] = time.monotonic) -> None:
        for event in read_events(ser, stop, clock):
            self.handle([event])

    def handle(self, events: list) -> None:
        for event in events:
            if isinstance(event, Measurement):
                self._measurement(event)
            elif isinstance(event, PowerOn):
                self._console.say(f"장비 켜짐: {event.model} (KEY {event.key})")
            elif isinstance(event, Corrupt):
                self._corrupt(event)

    def _measurement(self, measurement: Measurement) -> None:
        received_at = self._now()
        self._console.say(f"\n[{received_at:%H:%M:%S}] 검사 수신 (ID {measurement.memory_id})")
        if measurement.left is None and measurement.right is None:
            self._console.say("  이 ID 에는 저장된 검사가 없습니다. 검사 후 다시 전송해 주세요.")
            return
        self._console.say(describe_ear("오른쪽", measurement.right))
        self._console.say(describe_ear("왼쪽", measurement.left))
        if measurement.unknown:
            self._console.say(f"  알 수 없는 항목: {', '.join(sorted(measurement.unknown))}")
        sheet = save_result(measurement, self._directory, received_at, self._patient)
        self.saved.append(sheet)
        self._console.say(f"  저장: {sheet.resolve()}")
        if self._show:
            open_file(sheet)

    def _corrupt(self, event: Corrupt) -> None:
        received_at = self._now()
        self._directory.mkdir(parents=True, exist_ok=True)
        stem = free_stem(self._directory, received_at.strftime("corrupt_%Y%m%d_%H%M%S"))
        (self._directory / f"{stem}.bin").write_bytes(event.data)
        self._console.say(f"\n[{received_at:%H:%M:%S}] 전송을 해독하지 못했습니다 ({event.reason}).")
        self._console.say("  장비에서 -> 키를 다시 눌러 주세요.")


def run(args, console: Console, port_opener=open_port, sleep: Callable[[float], None] = time.sleep) -> int:
    console.say(f"TSM-300 결과 수신 v{__version__}\n")
    port = args.port or choose_port(console)[0]
    if not port:
        return 1
    listener = Listener(console, Path(args.out), show=args.show)
    try:
        prepare()
        while True:
            try:
                ser = port_opener(port, args.baud)
            except serial.SerialException as error:
                console.say(f"{port} 포트를 열 수 없습니다: {error}")
                console.say(f"다른 프로그램이 쓰고 있지 않은지 확인해 주세요. {RETRY_AFTER:.0f}초 뒤 다시 시도합니다.")
                sleep(RETRY_AFTER)
                continue
            console.say(f"{port} / {args.baud} bps 연결됨. 저장 폴더: {Path(args.out).resolve()}")
            console.say(READY)
            try:
                listener.run(ser)
            except serial.SerialException as error:
                console.say(f"\n포트 연결이 끊겼습니다: {error}")
                console.say(f"{RETRY_AFTER:.0f}초 뒤 다시 연결합니다.")
            finally:
                ser.close()
            sleep(RETRY_AFTER)
    except KeyboardInterrupt:
        console.say("\n종료합니다.")
    return 0


def decode_files(paths: list[str], directory: Path, console: Console, patient: Patient = Patient(),
                 now: Callable[[], datetime] = datetime.now) -> int:
    """Decodes saved raw transmissions, as if the device had just sent them."""
    listener = Listener(console, directory, now=now, patient=patient)
    for path in paths:
        console.say(f"\n{path}")
        decoder = StreamDecoder()
        events = decoder.feed(Path(path).read_bytes()) + decoder.flush()
        if not events:
            console.say("  검사 데이터를 찾지 못했습니다.")
        listener.handle(events)
    return 0


def open_file(path: Path) -> None:
    if hasattr(os, "startfile"):
        os.startfile(path)
    else:
        webbrowser.open(path.resolve().as_uri())
