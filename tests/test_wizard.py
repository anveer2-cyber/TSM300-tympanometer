"""Runs the capture wizard against a pseudo-terminal that plays the part of the device."""

import io
import json
import os
import threading
import time
import zipfile
from datetime import datetime

import pytest

from tsm300 import cli, wizard
from tsm300.console import Console

pty = pytest.importorskip("pty")

ID_KEY = b"ID TSM-300 SCREENING".ljust(76, b"\x00") + b"\n\r" + b"KEY6349".ljust(76, b"\x00") + b"\n\r"
RESULT = bytes(range(200))


class Device:
    def __init__(self):
        self._master, slave = pty.openpty()
        self.port = os.ttyname(slave)

    def send_after(self, delay, data):
        threading.Timer(delay, os.write, (self._master, data)).start()


class Answers:
    """Supplies scripted answers; ``on_ask`` lets a test react to the wizard reaching a prompt."""

    def __init__(self, answers, on_ask=None):
        self.prompts = []
        self._answers = list(answers)
        self._on_ask = on_ask

    def __call__(self, prompt):
        self.prompts.append(prompt)
        answer = self._answers.pop(0)
        if self._on_ask is not None:
            self._on_ask(len(self.prompts))
        return answer


def run_wizard(tmp_path, argv, answers):
    out = io.StringIO()
    args = cli.build_parser().parse_args(["capture"] + argv + ["--out", str(tmp_path), "--idle-gap", "0.3"])
    code = wizard.run(args, Console(answers, out), now=lambda: datetime(2026, 11, 26, 20, 30, 15))
    return code, out.getvalue(), tmp_path / "20261126_203015"


def test_scan_finds_the_baud_then_capture_records_a_noted_burst(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "SCAN_SETTLE", 0.3)
    device = Device()
    device.send_after(0.3, ID_KEY)
    answers = Answers(
        ["오른쪽, 압력 -15", "q"],
        on_ask=lambda count: device.send_after(0.1, b"again") if count == 1 else None,
    )
    threading.Thread(target=_send_when_capturing, args=(device, tmp_path), daemon=True).start()

    code, output, directory = run_wizard(tmp_path, ["--port", device.port, "--scan"], answers)

    assert code == 0
    assert "9600 bps 에서 글자로 읽히는 데이터를 받았습니다" in output
    assert "49 44 20 54 53 4D 2D 33 30 30" in output
    summary = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    assert summary["baud"] == 9600
    assert summary["scan"] == [{"baud": 9600, "file": "scan_9600.bin", "length": len(ID_KEY)}]
    assert (directory / "scan_9600.bin").read_bytes() == ID_KEY
    assert (directory / "burst_001.bin").read_bytes() == RESULT
    assert summary["bursts"][0]["note"] == "오른쪽, 압력 -15"
    assert summary["bursts"][0]["length"] == len(RESULT)
    assert (directory / "raw.bin").read_bytes() == RESULT + b"again"
    with zipfile.ZipFile(str(directory) + ".zip") as archive:
        assert {"session.json", "raw.bin", "burst_001.bin", "scan_9600.bin"} <= set(archive.namelist())


def test_default_run_skips_the_scan_and_ignores_power_noise(tmp_path):
    device = Device()
    device.send_after(0.3, b"\x00")
    device.send_after(1.2, RESULT)
    answers = Answers(["q"])

    code, output, directory = run_wizard(tmp_path, ["--port", device.port], answers)

    assert code == 0
    assert "통신 속도를 찾습니다" not in output
    assert "잡음" in output
    summary = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    assert [burst["length"] for burst in summary["bursts"]] == [1, len(RESULT)]
    assert len(answers.prompts) == 1


def test_unopenable_port_reports_and_exits(tmp_path):
    code, output, directory = run_wizard(tmp_path, ["--port", str(tmp_path / "no-such-port")], Answers([]))

    assert code == 1
    assert "포트를 열 수 없습니다" in output
    assert not directory.exists()


def _send_when_capturing(device, tmp_path):
    """Plays the result once the wizard has left the scan and started capturing."""
    summary_path = tmp_path / "20261126_203015" / "session.json"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if "baud" in json.loads(summary_path.read_text(encoding="utf-8")):
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    time.sleep(0.2)
    os.write(device._master, RESULT)
