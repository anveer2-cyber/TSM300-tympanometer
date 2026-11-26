"""Raw capture wizard: pick the port, optionally find the baud rate, record what the device sends."""

from __future__ import annotations

import queue
import shutil
from datetime import datetime
from pathlib import Path

import serial

from . import __version__
from .analyze import ascii_preview, guess_baud, hexdump, is_line_noise, looks_like_text, printable_ratio
from .capture import CaptureEngine, SessionWriter, listen_once
from .console import Console, choose_port, open_port

SCAN_BAUDS = (9600, 19200, 38400, 57600, 115200, 4800, 2400, 1200)
SCAN_WAIT = 25.0
SCAN_SETTLE = 4.0
HEXDUMP_LIMIT = 512

PREPARATION = """\
준비 (장비 쪽 설정):
  1. MENU -> Right key -> TRANSMIT 로 설정
  2. MENU -> Printer setup -> Printer Port -> Parallel 로 설정
     (Serial 로 되어 있으면 장비가 PC로 식별 신호를 보내지 않습니다)
  3. 장비 뒷면 SERIAL 단자와 PC가 케이블로 연결되어 있는지 확인
"""

CAPTURE_GUIDE = """\
캡처를 시작합니다. 장비가 보내는 데이터가 올 때마다 아래에 표시됩니다.
  1) 장비 전원을 껐다 켜기 (식별 신호)
  2) 검사 결과가 화면에 떠 있는 상태에서 -> 키(오른쪽 화살표) 누르기
  3) 가능하면 오른쪽/왼쪽 귀, 서로 다른 결과로 몇 번 더 전송
데이터를 받으면 '메모'를 묻습니다. 장비 화면의 값을 적어 주세요.
  예: 오른쪽, 압력 -15, 용적 1.2, 컴플라이언스 0.6, 폭 80
-> 키를 눌러도 아무것도 표시되지 않으면 그 사실만 알려 주셔도 됩니다.
끝내려면 메모 칸에 q 를 입력하거나 Ctrl+C 를 누르세요.
"""

NO_DATA_HELP = """\
어느 속도에서도 데이터를 받지 못했습니다. 확인해 주세요:
  - 장비 MENU -> Printer setup -> Printer Port 가 Parallel 인지
  - 케이블이 장비 뒷면 SERIAL 단자에 꽂혀 있는지 (PRINTER 단자가 아님)
  - 선택한 포트가 USB-시리얼 어댑터가 맞는지
  - 케이블이 널모뎀(크로스) 방식인지
"""


def describe(console: Console, data: bytes) -> None:
    console.say(f"    {len(data)}바이트, 글자로 읽히는 비율 {printable_ratio(data):.0%}, 미리보기: {ascii_preview(data)}")
    console.say(_indent(hexdump(data, limit=HEXDUMP_LIMIT)))


def scan(console: Console, ser, writer: SessionWriter, wait: float = SCAN_WAIT,
         settle: float = SCAN_SETTLE) -> int | None:
    """Finds the baud rate by listening for the power-on identification key at each candidate."""
    console.say("통신 속도를 찾습니다. 장비는 전원을 켤 때 PC로 식별 신호를 보냅니다.")
    console.say("안내가 나올 때마다 장비 전원을 끄고, 2초 뒤 다시 켜 주세요.\n")
    results: list[tuple[int, bytes]] = []
    for number, baud in enumerate(SCAN_BAUDS, 1):
        ser.baudrate = baud
        ser.reset_input_buffer()
        console.say(f"[{number}/{len(SCAN_BAUDS)}] {baud} bps: 지금 전원을 껐다 켜 주세요.")
        data = listen_once(
            ser, wait=wait, settle=settle,
            on_wait=lambda left, got: console.status(f"    기다리는 중... 남은 시간 {left:3.0f}초, 받은 바이트 {got}  "),
        )
        console.clear_status()
        writer.scan_result(baud, data)
        results.append((baud, data))
        if not data or is_line_noise(data):
            console.say("    받은 데이터 없음")
            continue
        describe(console, data)
        if looks_like_text(data):
            console.say(f"-> {baud} bps 에서 글자로 읽히는 데이터를 받았습니다. 이 속도로 진행합니다.\n")
            return baud
    guess = guess_baud(results)
    if guess is None:
        console.say(NO_DATA_HELP)
        answer = console.ask("그래도 계속하려면 속도를 입력 (예: 9600, 그만두려면 Enter): ")
        return int(answer) if answer.isdigit() else None
    console.say(f"글자로 읽히는 데이터는 없었습니다. 받은 모양으로 보면 {guess} bps 가 가장 유력합니다 (추정).")
    answer = console.ask(f"사용할 속도 [{guess}]: ")
    return int(answer) if answer.isdigit() else guess


def capture(console: Console, ser, writer: SessionWriter, idle_gap: float) -> int:
    """Records bursts until the user quits, asking for a note after each one. Returns the burst count."""
    console.say(CAPTURE_GUIDE)
    engine = CaptureEngine(ser, writer, idle_gap)
    engine.start()
    count = 0
    try:
        console.say("대기 중...")
        while True:
            try:
                burst = engine.bursts.get(timeout=0.2)
            except queue.Empty:
                if engine.error is not None:
                    console.say(f"포트에서 읽는 중 오류가 발생했습니다: {engine.error}")
                    break
                continue
            count += 1
            console.say(f"\n[전송 {burst.index}] 시작 후 {burst.started:.1f}초")
            if is_line_noise(burst.data):
                console.say("    전원 켜짐/꺼짐 때 생기는 잡음 1~2바이트 (무시)")
                continue
            describe(console, burst.data)
            note = console.ask("메모 (없으면 Enter, 끝내려면 q): ")
            if note.lower() == "q":
                break
            writer.note(burst.index, note)
            console.say("대기 중...")
    except (KeyboardInterrupt, EOFError):
        console.say("\n중단했습니다.")
    finally:
        engine.stop()
    return count


def run(args, console: Console, port_opener=open_port, now=datetime.now) -> int:
    console.say(f"TSM-300 원시 데이터 캡처 v{__version__}\n")
    console.say(PREPARATION)
    port, port_info = (args.port, {}) if args.port else choose_port(console)
    if not port:
        return 1
    try:
        ser = port_opener(port, args.baud)
    except serial.SerialException as exc:
        console.say(f"{port} 포트를 열 수 없습니다: {exc}")
        console.say("다른 프로그램이 이 포트를 쓰고 있지 않은지 확인해 주세요.")
        return 1
    directory = Path(args.out) / now().strftime("%Y%m%d_%H%M%S")
    writer = SessionWriter(directory, {
        "tool_version": __version__,
        "started": now().isoformat(timespec="seconds"),
        "port": port,
        **port_info,
        "framing": "8N1",
        "idle_gap": args.idle_gap,
    })
    try:
        baud = scan(console, ser, writer) if args.scan else args.baud
        if baud is None:
            return 1
        ser.baudrate = baud
        ser.reset_input_buffer()
        writer.set_meta(baud=baud)
        console.say(f"{port} / {baud} bps / 8N1\n")
        capture(console, ser, writer, args.idle_gap)
    except (KeyboardInterrupt, EOFError):
        console.say("\n중단했습니다.")
    finally:
        ser.close()
        writer.close()
        archive = shutil.make_archive(str(directory), "zip", root_dir=directory)
        console.say(f"\n저장 위치: {directory.resolve()}")
        console.say(f"압축 파일: {Path(archive).resolve()}")
        console.say("이 zip 파일을 Claude 대화에 올리거나, 위 화면 내용을 복사해서 붙여 주세요.")
    return 0


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
