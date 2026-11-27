"""The audiometer's commands: ``hearing-check`` for the first visit to the device, ``hearing-demo`` for a sample sheet.

``hearing-check`` is what to run at the clinic the first time: it lists the audiometers
Windows sees on USB, asks the first one who it is, reads every test in its memory, prints
what it found and keeps a log of every byte. It sends only commands that read.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

from .. import settings as settings_file
from ..console import Console
from ..patient import Patient
from . import device
from .pta import average
from .record import EAR_NAMES, KIND_NAMES, HearingResult, Part, can_be_aided, describe, type_of

DEMO_PATIENT = Patient("00000", "견본 환자", "여", date(1955, 5, 20))
DEMO_NOTE = ("견본 데이터입니다. 실제 환자가 아닙니다.\n"
             "보청기 착용 전 · 후 음장검사를 한 장에 담은 예입니다 (보청기 착용 검사를 따로 받아 합침).")


def check(args, console: Console) -> int:
    console.say("청력검사기 점검 (MRS Resonance): 기기의 검사를 읽기만 하고 아무것도 바꾸지 않습니다.\n")
    if args.simulator:
        console.say("모의 장비로 합니다. 실제 청력검사기가 아닙니다.")
    else:
        try:
            paths = device.usb_devices()
        except device.DeviceError as error:
            console.say(f"※ {error}")
            return 1
        from .winusb import INTERFACE_GUID
        console.say(f"USB 에 연결된 청력검사기: {len(paths)}대 (MRS 드라이버의 장치 GUID {INTERFACE_GUID})")
        for path in paths:
            console.say(f"  {path}")
        if not paths:
            console.say("※ 청력검사기를 찾지 못했습니다. 기기 전원, USB 선, MRS 드라이버 설치를 확인해 주세요.")
            return 1
    folder = Path(args.out) if args.out else log_folder()
    opener = device.opener_for(device.SIMULATOR if args.simulator else device.USB)
    live = getattr(sys.stdout, "isatty", lambda: False)()  # no running count in a file the output is sent to
    try:
        fetched = device.fetch(opener, folder, progress=(lambda count: console.status(f"받는 중... 검사 {count}건"))
                               if live else None)
    except device.DeviceError as error:
        if live:
            console.clear_status()
        console.say(f"※ {error}")
        return 1
    if live:
        console.clear_status()
    for line in summary(fetched):
        console.say(line)
    return 0


def log_folder() -> Path:
    """The byte logs' folder inside the results folder chosen in the settings."""
    chosen = settings_file.load().results_dir
    return settings_file.results_directory(chosen).resolve() / device.LOG_FOLDER


def summary(fetched: device.Fetched) -> list[str]:
    """What was read from the device, for a person to look over."""
    download = fetched.download
    lines = [f"일련번호: {download.serial or '알려 주지 않음'}",
             f"기기 환자 {len(download.groups)}명, 검사 {len(download.tests)}건, "
             f"끝 신호 {'받음' if download.finished else '못 받음'}, 알아보지 못한 조각 {len(download.corrupt)}개"]
    lines += [f"※ {warning}" for warning in fetched.warnings]
    order = 0
    for group in download.groups:
        who = group.patient
        heading = (f"기기 환자 {who.number} · {who.name or '(이름 없음)'} · {who.birth_date or '생일 없음'} · {who.sex or '-'}"
                   if who else "기기 환자 정보 없음")
        lines.append("")
        lines.append(heading)
        for test in group.tests:
            order += 1
            mark = " (보청기 착용 표시 가능: 음장만 있음)" if can_be_aided(test) else ""
            lines.append(f"  {order}. {describe(test)}{mark}")
            lines += [f"     {line}" for line in _values(test)]
    lines.append("")
    lines.append(f"주고받은 기록: {fetched.log}" if fetched.log else "주고받은 기록은 남기지 않았습니다.")
    return lines


def _values(test) -> list[str]:
    kind = type_of(test)
    if not kind:
        return []
    result = HearingResult((Part(test),))
    lines = []
    if result.tone:
        averages = []
        for kind_, name in (("Ac", "기도"), ("Bc", "골도"), ("Spkr", "음장"), ("SpkrA", "음장 보청기")):
            shown = [f"{ear} {average(result.tone_curve(kind_, ear)).text()}" for ear in ("R", "L")
                     if result.tone_curve(kind_, ear) is not None]
            if shown:
                averages.append(f"{name} {' · '.join(shown)}")
        if averages:
            lines.append("평균(6분법) " + " / ".join(averages))
    if result.speech or result.srt:
        for (kind_, ear), placed in sorted(result.speech.items(), key=lambda item: (item[0][1] != "R", item[0][0])):
            best = placed.curve.best
            srt = result.srt_of(kind_, ear)
            lines.append(f"어음 {KIND_NAMES.get(kind_, kind_)} {EAR_NAMES[ear]}: SRT {srt if srt is not None else '-'} dB, "
                         f"최고 명료도 {best.score if best else '-'}% ({best.level if best else '-'} dB)")
    if result.high_frequency:
        for (kind_, ear), placed in sorted(result.high_frequency.items(), key=lambda item: (item[0][1] != "R", item[0][0])):
            points = ", ".join(f"{point.frequency // 1000 if point.frequency % 1000 == 0 else point.frequency / 1000}k "
                               f"{point.level}" for point in placed.curve.points)
            lines.append(f"고주파 {KIND_NAMES.get(kind_, kind_)} {EAR_NAMES[ear]}: {points}")
    return lines


def demo_result() -> HearingResult:
    """A sheet's worth of tests read from the simulated device: the made-up patient tested with
    and without a hearing aid, whose tests with the aid in are marked as such."""
    fetched = device.fetch(device.opener_for(device.SIMULATOR))
    group = fetched.download.groups[-1]
    # In this made-up memory, the tests with soundfield curves only were done with the aid in.
    return HearingResult(tuple(Part(test, group.patient, can_be_aided(test)) for test in group.tests))


def demo(args, console: Console, now=datetime.now) -> int:
    from .sheet import render
    patient = args.patient or DEMO_PATIENT
    target = Path(args.file)
    target.parent.mkdir(parents=True, exist_ok=True)
    render(demo_result(), target, args.tested or now().replace(second=0, microsecond=0), patient,
           DEMO_NOTE if args.note is None else args.note, settings_file.load().clinic)
    console.say(f"견본 청력검사 결과지를 그렸습니다: {target.resolve()}")
    return 0
