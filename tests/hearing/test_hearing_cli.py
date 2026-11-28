"""The audiometer's commands: the check for the first visit to the device, and a sample sheet."""

from __future__ import annotations

import pytest
from PIL import Image

from tsm300 import cli
from tsm300.hearing import device


def test_hearing_check_reads_the_simulated_device_and_keeps_the_byte_log(tmp_path, capsys):
    assert cli.main(["hearing-check", "--simulator", "--out", str(tmp_path)]) == 0

    said = capsys.readouterr().out
    assert "읽기만 하고 아무것도 바꾸지 않습니다" in said
    assert "일련번호: R37A21C000777" in said and "기기 환자 4명, 검사 9건, 끝 신호 받음" in said
    assert "기기 환자 4 · 라 견본 · 20/05/1955 · F" in said
    assert "평균(6분법) 기도 R 60 · L 50" in said
    assert "보청기 착용 표시 가능" in said
    (log,) = tmp_path.glob("*.txt")
    assert f"주고받은 기록: {log}" in said


def test_hearing_check_without_a_device_says_what_to_check(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # the byte log goes in the results folder, here the usual one beside the program
    monkeypatch.setattr(device, "usb_devices", lambda: [])
    monkeypatch.setattr(device, "DEVICE_WAIT", 0.0)  # the operator never presses Connect to PC

    def missing():
        raise device.DeviceMissing(device.NO_DEVICE)

    monkeypatch.setattr(device, "opener_for", lambda link: missing)
    assert cli.main(["hearing-check"]) == 1
    said = capsys.readouterr().out
    assert "USB 에 보이지 않습니다. 장비에서 [Connect to PC]를 눌러 주세요" in said and device.NO_DEVICE in said

    def busy():
        raise device.DeviceError("청력검사기를 다른 프로그램이 쓰고 있습니다. MDS 가 켜져 있으면 닫아 주세요.")

    monkeypatch.setattr(device, "usb_devices", lambda: [r"\\?\usb#vid_045e&pid_00ce#1"])
    monkeypatch.setattr(device, "open_usb", busy)
    monkeypatch.setattr(device, "opener_for", lambda link: busy)
    assert cli.main(["hearing-check"]) == 1
    said = capsys.readouterr().out
    assert "1대" in said and "MDS 가 켜져 있으면 닫아 주세요" in said
    logs = list((tmp_path / "results" / device.LOG_FOLDER).glob("*.txt"))  # one from each try
    assert len(logs) == 2 and any(f"주고받은 기록: {log.resolve()}" in said for log in logs)


def test_hearing_demo_draws_a_sample_sheet(tmp_path, capsys):
    target = tmp_path / "견본.jpg"

    assert cli.main(["hearing-demo", str(target), "--chart", "00512", "--name", "홍길동", "--tested", "2026-11-27 10:30"]) == 0

    with Image.open(target) as image:
        assert image.size == (2480, 3508)
    assert "견본 청력검사 결과지를 그렸습니다" in capsys.readouterr().out


def test_hearing_demo_refuses_an_unreadable_date(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["hearing-demo", str(tmp_path / "x.jpg"), "--tested", "어제"])
