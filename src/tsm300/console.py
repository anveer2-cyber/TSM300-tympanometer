"""Console interaction and serial port selection shared by the commands."""

from __future__ import annotations

import re
import sys
from typing import Callable

import serial
from serial.tools import list_ports


class Console:
    def __init__(self, input_fn: Callable[[str], str] = input, out=None) -> None:
        self._input = input_fn
        self._out = out if out is not None else sys.stdout

    def say(self, text: str = "") -> None:
        print(text, file=self._out, flush=True)

    def status(self, text: str) -> None:
        self._out.write("\r" + text)
        self._out.flush()

    def clear_status(self) -> None:
        self.status(" " * 70 + "\r")

    def ask(self, prompt: str) -> str:
        return self._input(prompt).strip()


def open_port(port: str, baud: int):
    return serial.Serial(port, baudrate=baud, bytesize=8, parity="N", stopbits=1, timeout=0.05)


def available_ports() -> list:
    """The PC's serial ports in the order a person counts them: COM3 before COM10."""
    return sorted(list_ports.comports(), key=lambda port: _natural(port.device))


def _natural(name: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def choose_port(console: Console) -> tuple[str | None, dict]:
    ports = available_ports()
    if not ports:
        console.say("연결된 시리얼(COM) 포트를 찾지 못했습니다.")
        console.say("장치 관리자의 '포트(COM & LPT)'에 USB-시리얼 어댑터가 보이는지 확인해 주세요.")
        name = console.ask("포트 이름을 직접 입력 (예: COM3, 그만두려면 Enter): ")
        return (name or None), {}
    console.say("발견된 포트:")
    for number, port in enumerate(ports, 1):
        console.say(f"  {number}) {port.device} - {port.description}")
    default = next((n for n, p in enumerate(ports, 1) if "USB" in (p.hwid or "").upper()), 1)
    chosen = ports[default - 1]
    if len(ports) > 1:
        while True:
            answer = console.ask(f"사용할 포트 번호 [{default}]: ") or str(default)
            if answer.isdigit() and 1 <= int(answer) <= len(ports):
                chosen = ports[int(answer) - 1]
                break
            console.say("목록에 있는 번호를 입력해 주세요.")
    console.say(f"-> {chosen.device} 사용")
    return chosen.device, {"port_description": chosen.description, "port_hwid": chosen.hwid}
