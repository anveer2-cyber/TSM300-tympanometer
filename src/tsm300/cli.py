"""Command line: the operator's window (the default), updating, a desktop shortcut, console listening, raw capture, ports, decoding."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from . import settings as settings_file
from .console import Console, available_ports
from .patient import Patient, parse_birth_date, parse_sex
from .protocol import BAUD_RATE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tsm300", description="Oscilla TSM-300 검사 결과를 받아 환자 정보와 함께 A4 결과지로 저장합니다.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_port_options(parser)
    parser.add_argument("--out", help="결과를 저장할 폴더 (기본: 설정에서 정한 폴더)")
    commands = parser.add_subparsers(dest="command", title="그 밖의 명령")

    listen = commands.add_parser("listen", help="창 없이 명령 프롬프트에서 결과를 받아 저장합니다")
    _add_port_options(listen)
    listen.add_argument("--out", default="results", help="결과를 저장할 폴더 (기본: results)")
    listen.add_argument("--show", action="store_true", help="결과지를 받자마자 화면에 엽니다")

    commands.add_parser("ports", help="시리얼 포트 목록을 보여 줍니다")
    commands.add_parser("shortcut", help="바탕 화면에 프로그램 바로가기를 만듭니다")

    update = commands.add_parser("update", help="새 버전이 있으면 받아서 바꿔 넣습니다 (exe 로 쓸 때)")
    update.add_argument("--check", action="store_true", help="새 버전이 있는지만 알려 줍니다")
    update.add_argument("--again", action="store_true", help="새 버전이 아니어도 올라와 있는 버전을 다시 받습니다")
    update.add_argument("--restart", action="store_true", help="바꿔 넣은 뒤 프로그램을 켭니다")

    capture = commands.add_parser("capture", help="장비가 보내는 원시 데이터를 그대로 기록합니다 (분석용)")
    _add_port_options(capture)
    capture.add_argument("--scan", action="store_true", help="통신 속도를 모를 때 자동으로 찾습니다")
    capture.add_argument("--out", default="captures", help="저장할 폴더 (기본: captures)")
    capture.add_argument("--idle-gap", type=float, default=1.5,
                         help="이 시간(초) 동안 조용하면 한 번의 전송이 끝난 것으로 봅니다")

    decode = commands.add_parser("decode", help="저장해 둔 원본 데이터(.bin)로 결과지를 다시 만듭니다")
    decode.add_argument("files", nargs="+", help="해독할 .bin 파일")
    decode.add_argument("--out", default="results", help="결과를 저장할 폴더 (기본: results)")
    decode.add_argument("--chart", default="", help="차트번호")
    decode.add_argument("--name", default="", help="환자 이름")
    decode.add_argument("--sex", default="", help="성별 (남 또는 여)")
    decode.add_argument("--birth", default="", help="생년월일 (예: 1980-01-31)")
    decode.add_argument("--note", default="", help="결과지에 인쇄할 메모")
    decode.add_argument("--tested", default="", help="결과지에 인쇄할 검사일시 (예: \"2026-11-26 14:30\"). 생략하면 지금")
    return parser


def main(argv: list[str] | None = None) -> int:
    _give_missing_streams_somewhere_to_go()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if settings_file.is_packaged():
        from . import update
        update.clean_up(Path(sys.executable))
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    if args.command == "ports":
        ports = available_ports()
        for port in ports:
            console.say(f"{port.device} - {port.description} [{port.hwid}]")
        if not ports:
            console.say("시리얼 포트 없음")
        return 0
    if args.command == "shortcut":
        from .shortcut import create_shortcut
        try:
            created = create_shortcut(Path.cwd())
        except RuntimeError as error:
            console.say(str(error))
            return 1
        console.say(f"바로가기를 만들었습니다: {created}")
        chosen = settings_file.load().results_dir
        console.say(f"바로가기로 실행하면 결과는 {settings_file.results_directory(chosen).resolve()} 에 저장됩니다.")
        return 0
    if args.command == "update":
        return _update(args, console)
    if args.command == "capture":
        from . import wizard
        return wizard.run(args, console)
    if args.command == "listen":
        from . import listen
        return listen.run(args, console)
    if args.command == "decode":
        from . import listen
        try:
            patient = Patient(args.chart, args.name, parse_sex(args.sex), parse_birth_date(args.birth))
            tested_at = parse_moment(args.tested)
        except ValueError as error:
            parser.error(str(error))
        return listen.decode_files(args.files, Path(args.out), console, patient, args.note,
                                   clinic=settings_file.load().clinic, tested_at=tested_at)
    from . import app
    return app.run(args)


def parse_moment(text: str) -> datetime | None:
    """Reads a date with or without a time, such as 2026-11-26 14:30. Returns None when ``text`` is blank."""
    cleaned = text.strip()
    if not cleaned:
        return None
    for layout in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, layout)
        except ValueError:
            continue
    raise ValueError(f"검사일시를 읽을 수 없습니다: {text!r} (예: 2026-11-26 14:30)")


def _update(args, console: Console) -> int:
    from . import update
    try:
        release = update.latest_release()
        console.say(f"지금 버전: {__version__}")
        console.say(f"올라와 있는 버전: {release.version}")
        if args.check:
            return 0
        if not (args.again or update.is_newer(release.version)):
            console.say("이미 최신 버전입니다.")
            return 0
        program = update.program_file()
        if program is None:
            console.say("Python 으로 설치한 프로그램은 아래 명령으로 업데이트합니다.")
            console.say(update.PYTHON_COMMAND)
            return 1
        update.install(release, program)
    except update.UpdateError as error:
        console.say(str(error))
        return 1
    console.say(f"v{release.version} 으로 바꿨습니다.")
    if args.restart:
        update.restart(program, [])
    else:
        console.say("프로그램을 다시 켜면 새 버전입니다.")
    return 0


def _give_missing_streams_somewhere_to_go() -> None:
    """The packaged program is started without a console, where Python has no output streams
    at all and the first write would stop it. Output redirected to a file still arrives."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _add_port_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", help="사용할 포트 (예: COM5). 생략하면 자동으로 고릅니다")
    parser.add_argument("--baud", type=int, default=BAUD_RATE, help=f"통신 속도 (기본: {BAUD_RATE})")
