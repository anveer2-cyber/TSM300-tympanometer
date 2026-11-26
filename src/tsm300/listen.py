"""Waiting on the serial port and saving each measurement the moment the device sends it."""

from __future__ import annotations

import json
import os
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable

import serial

from . import __version__
from .console import Console, choose_port, open_port
from .protocol import Corrupt, EarResult, Measurement, PowerOn, StreamDecoder, tympanogram_pressures
from .render import render

STALE_AFTER = 3.0
RETRY_AFTER = 5.0

READY = """\
검사 결과를 기다리는 중입니다.
  장비에서 검사를 마친 뒤 -> 키(오른쪽 화살표)를 누르면 바로 저장됩니다.
  (장비 설정: MENU -> Right key -> TRANSMIT)
끝내려면 Ctrl+C 를 누르세요.
"""


def measurement_to_dict(measurement: Measurement, received_at: datetime) -> dict:
    return {
        "received_at": received_at.isoformat(timespec="seconds"),
        "device": {"name": measurement.device_name, "code": measurement.device_code},
        "memory_id": measurement.memory_id,
        "right": _ear_to_dict(measurement.right),
        "left": _ear_to_dict(measurement.left),
    }


def save_measurement(measurement: Measurement, directory: Path, received_at: datetime) -> Path:
    """Writes the raw message, the decoded values and the picture. Returns the picture's path."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = _free_stem(directory, received_at.strftime("%Y%m%d_%H%M%S"))
    (directory / f"{stem}.bin").write_bytes(measurement.raw)
    text = json.dumps(measurement_to_dict(measurement, received_at), ensure_ascii=False, indent=2)
    (directory / f"{stem}.json").write_text(text, encoding="utf-8")
    picture = directory / f"{stem}.png"
    render(measurement, picture, received_at)
    return picture


def describe_ear(name: str, ear: EarResult | None) -> str:
    if ear is None:
        return f"  {name}: 측정 없음"
    reflex = f", 반사 {len(ear.reflexes)}개 주파수" if ear.reflexes else ""
    return (
        f"  {name}: 압력 {ear.middle_ear_pressure} daPa, 용적 {ear.ear_volume:.2f} ml, "
        f"컴플라이언스 {ear.compliance:.2f} ml, 폭 {ear.width} daPa{reflex}"
    )


class Listener:
    """Feeds received bytes to the decoder and acts on what comes out."""

    def __init__(self, console: Console, directory: Path, show: bool = False,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.saved: list[Path] = []
        self._console = console
        self._directory = directory
        self._show = show
        self._now = now
        self._decoder = StreamDecoder()

    def run(self, ser, stop: Callable[[], bool] = lambda: False,
            clock: Callable[[], float] = time.monotonic) -> None:
        last_data = clock()
        while not stop():
            data = ser.read(ser.in_waiting or 1)
            if data:
                last_data = clock()
                self.handle(self._decoder.feed(data))
            elif self._decoder.pending and clock() - last_data > STALE_AFTER:
                self.handle(self._decoder.flush())

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
        picture = save_measurement(measurement, self._directory, received_at)
        self.saved.append(picture)
        self._console.say(f"  저장: {picture.resolve()}")
        if self._show:
            _open_file(picture)

    def _corrupt(self, event: Corrupt) -> None:
        received_at = self._now()
        self._directory.mkdir(parents=True, exist_ok=True)
        stem = _free_stem(self._directory, received_at.strftime("corrupt_%Y%m%d_%H%M%S"))
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


def decode_files(paths: list[str], directory: Path, console: Console,
                 now: Callable[[], datetime] = datetime.now) -> int:
    """Decodes saved raw transmissions, as if the device had just sent them."""
    listener = Listener(console, directory, now=now)
    for path in paths:
        console.say(f"\n{path}")
        decoder = StreamDecoder()
        events = decoder.feed(Path(path).read_bytes()) + decoder.flush()
        if not events:
            console.say("  검사 데이터를 찾지 못했습니다.")
        listener.handle(events)
    return 0


def _ear_to_dict(ear: EarResult | None) -> dict | None:
    if ear is None:
        return None
    return {
        "middle_ear_pressure_dapa": ear.middle_ear_pressure,
        "ear_volume_ml": ear.ear_volume,
        "compliance_ml": ear.compliance,
        "width_dapa": ear.width,
        "tympanogram": {
            "pressure_dapa": tympanogram_pressures(),
            "compliance_ml": [round(value, 4) for value in ear.compliance_curve],
        },
        "reflex_ml": {
            str(frequency): [round(value, 4) for value in ear.reflex_curve(frequency)]
            for frequency in sorted(ear.reflexes)
        },
    }


def _free_stem(directory: Path, stem: str) -> str:
    """Returns ``stem``, or ``stem_2``, ``stem_3``... when files with that name already exist."""
    candidate, number = stem, 1
    while any(directory.glob(f"{candidate}.*")):
        number += 1
        candidate = f"{stem}_{number}"
    return candidate


def _open_file(path: Path) -> None:
    if hasattr(os, "startfile"):
        os.startfile(path)
    else:
        webbrowser.open(path.resolve().as_uri())
