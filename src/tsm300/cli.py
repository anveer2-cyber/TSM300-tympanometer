"""Command line: receive results (the default), capture raw bytes, list ports, decode saved files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, listen, wizard
from .console import Console, available_ports
from .protocol import BAUD_RATE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tsm300", description="Oscilla TSM-300 검사 결과를 PC로 받아 저장합니다.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_port_options(parser)
    parser.add_argument("--out", default="results", help="결과를 저장할 폴더 (기본: results)")
    parser.add_argument("--show", action="store_true", help="결과 이미지를 받자마자 화면에 엽니다")
    commands = parser.add_subparsers(dest="command", title="그 밖의 명령")

    commands.add_parser("ports", help="시리얼 포트 목록을 보여 줍니다")

    capture = commands.add_parser("capture", help="장비가 보내는 원시 데이터를 그대로 기록합니다 (분석용)")
    _add_port_options(capture)
    capture.add_argument("--scan", action="store_true", help="통신 속도를 모를 때 자동으로 찾습니다")
    capture.add_argument("--out", default="captures", help="저장할 폴더 (기본: captures)")
    capture.add_argument("--idle-gap", type=float, default=1.5,
                         help="이 시간(초) 동안 조용하면 한 번의 전송이 끝난 것으로 봅니다")

    decode = commands.add_parser("decode", help="저장해 둔 원시 데이터 파일을 해독합니다")
    decode.add_argument("files", nargs="+", help="해독할 .bin 파일")
    decode.add_argument("--out", default="results", help="결과를 저장할 폴더 (기본: results)")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    console = Console()
    if args.command == "ports":
        ports = available_ports()
        for port in ports:
            console.say(f"{port.device} - {port.description} [{port.hwid}]")
        if not ports:
            console.say("시리얼 포트 없음")
        return 0
    if args.command == "capture":
        return wizard.run(args, console)
    if args.command == "decode":
        return listen.decode_files(args.files, Path(args.out), console)
    return listen.run(args, console)


def _add_port_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", help="사용할 포트 (예: COM5). 생략하면 목록에서 고릅니다")
    parser.add_argument("--baud", type=int, default=BAUD_RATE, help=f"통신 속도 (기본: {BAUD_RATE})")
