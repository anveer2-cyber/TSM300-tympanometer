"""Getting the tests out of the audiometer: opening the link, keeping a log of every byte, and
saying in plain words what is in the way when it does not work.

Only commands that read are ever sent (``protocol.command`` refuses the others). The device
keeps its tests after they have been fetched, so each download brings back the old ones too;
``SeenTests`` remembers which have been saved, so that only new ones are offered.

The operator may press Connect to PC on the device before or after pressing [받기] here, so
fetching waits up to ``DEVICE_WAIT`` seconds, both for the device to show up on USB and for it
to answer, and says meanwhile what to press.

Nothing here needs Qt: the window runs ``fetch`` on a thread of its own, and the command line
runs it directly.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import __version__
from .samples import practice_memory
from .session import DEVICE_WAIT, PUSHED, Download, Session, SessionError  # noqa: F401 (DEVICE_WAIT is shown to users)
from .simulator import SimulatedAudiometer

USB, SIMULATOR = "usb", "simulator"
LINKS = {USB: "USB", SIMULATOR: "모의 장비"}
LOG_FOLDER = "청력검사기_기록"
"""The folder, inside the results folder, that the byte logs go in."""
SEEN_FILE = "청력검사기_받은검사.json"
"""The file, beside the settings, that remembers which of the device's tests have been saved."""

CONNECT_KEY = "Connect to PC"
"""What the operator presses on the audiometer so that it talks to the PC."""
WAITING = f"청력검사기를 기다리는 중입니다. 장비에서 [{CONNECT_KEY}]를 눌러 주세요. (남은 시간 {{left}}초)"
NO_ANSWER = (f"청력검사기가 답하지 않습니다. 장비에서 [{CONNECT_KEY}]를 누른 뒤 다시 받아 주세요. "
             "그래도 안 되면 기기가 켜져 있는지, USB 선이 꽂혀 있는지 보고, MDS 가 켜져 있으면 닫아 주세요.")
NO_DEVICE = (f"USB 에서 청력검사기를 찾지 못했습니다. 장비에서 [{CONNECT_KEY}]를 누른 뒤 다시 받아 주세요. "
             "그래도 안 되면 기기 전원, USB 선, MRS 드라이버 설치를 확인해 주세요.")
OPEN_RETRY = 1.0
"""Seconds between looks for the audiometer on USB while waiting for it."""
STOPPED = "청력검사기와 주고받다가 멈췄습니다: {error}. USB 선을 확인하고 다시 눌러 주세요."
NOT_FINISHED = ("청력검사기가 다 보냈다는 신호 없이 조용해졌습니다. 빠진 검사가 있을 수 있으니, "
                "찾는 검사가 없으면 한 번 더 받아 주세요.")


class DeviceError(RuntimeError):
    """Why the audiometer could not be read, in words the operator can act on."""

    def __init__(self, message: str, log: Path | None = None) -> None:
        super().__init__(message)
        self.log = log

    def __str__(self) -> str:
        text = super().__str__()
        return f"{text}\n주고받은 기록: {self.log}" if self.log else text


class DeviceMissing(DeviceError):
    """No audiometer on USB yet: it may appear once Connect to PC is pressed on it."""


@dataclass
class Fetched:
    download: Download
    log: Path | None
    """The byte log of this download, when one was kept."""

    @property
    def warnings(self) -> list[str]:
        said = []
        if not self.download.finished and not self.download.pushed:
            said.append(NOT_FINISHED)
        if self.download.corrupt:
            pieces = len(self.download.corrupt)
            said.append(f"오는 도중 망가진 자료 {pieces}조각은 버렸습니다(주고받은 기록에 남김). 검사가 빠졌으면 "
                        "[청력검사기에서 받기]를 한 번 더 누르세요. 기기가 검사를 그대로 갖고 있어 온전히 다시 옵니다.")
        return said


# --- links --------------------------------------------------------------------------------------

class PacedLink:
    """The simulated device, made to wait a little when it has nothing to say, as a USB read does."""

    name = "모의 장비"

    def __init__(self, device: SimulatedAudiometer, pause: float = 0.01) -> None:
        self.device = device
        self._pause = pause

    def read(self) -> bytes:
        data = self.device.read()
        if not data:
            time.sleep(self._pause)
        return data

    def write(self, data: bytes) -> None:
        self.device.write(data)

    def close(self) -> None:
        self.device.close()


_practice = None


def practice_device() -> SimulatedAudiometer:
    """A simulated audiometer holding made-up patients. Its memory lasts while the program runs,
    so tests saved from it count as received, as on the real device."""
    global _practice
    if _practice is None:
        _practice = practice_memory()
    return SimulatedAudiometer(_practice)


def opener_for(link: str) -> Callable[[], object]:
    """What opens the link chosen in the settings."""
    if link == SIMULATOR:
        return lambda: PacedLink(practice_device())
    return open_usb


def open_usb():
    """The first Resonance audiometer on USB. Raises DeviceError saying what to check."""
    from . import winusb
    try:
        return winusb.open_audiometer()
    except winusb.NoAudiometer:
        raise DeviceMissing(NO_DEVICE) from None
    except (winusb.WinUsbError, OSError) as error:
        raise DeviceError(_sentence(str(error))) from None


def usb_devices() -> list[str]:
    """The Windows device paths of the audiometers on USB."""
    from . import winusb
    try:
        return winusb.device_paths()
    except (winusb.WinUsbError, OSError) as error:
        raise DeviceError(_sentence(str(error))) from None


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "?", "!", ")")) else text + "."


# --- the byte log -------------------------------------------------------------------------------

class ByteLog:
    """Every byte the PC sends and the device answers, with the time, one line per piece:

        2026-11-27 10:30:00.123  PC>     16  4b43001016040100000000000000136b
        2026-11-27 10:30:00.140  <DEV    20  4b49000e...

    The first visit to a clinic's device is the time something unforeseen is most likely; a
    log like this lets it be worked out afterwards from home. Each line is written at once, so
    the log survives the program being stopped half-way.
    """

    def __init__(self, folder: Path, what: str, now: Callable[[], datetime] = datetime.now) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = f"{now():%Y%m%d_%H%M%S}"
        path, number = folder / f"{stamp}.txt", 1
        while path.exists():
            number += 1
            path = folder / f"{stamp}_{number}.txt"
        self.path = path
        self._now = now
        self._file = open(path, "a", encoding="utf-8", newline="\n")
        self.note(f"TSM300 v{__version__} · {what} · {sys.platform}")
        self.note("PC> = 이 PC 가 보낸 것, <DEV = 청력검사기가 보낸 것. 바이트 수와 16진수.")

    def write(self, direction: str, data: bytes) -> None:
        self._line(f"{direction:<5} {len(data):5d}  {bytes(data).hex()}")

    def note(self, text: str) -> None:
        self._line(f"# {text}")

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass

    def _line(self, text: str) -> None:
        try:
            self._file.write(f"{self._now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + f"  {text}\n")
            self._file.flush()
        except (OSError, ValueError):
            pass  # a log that cannot be written must not stop the download it records


def read_log(path: Path) -> list[tuple[str, bytes]]:
    """The pieces in a byte log, as (direction, bytes), for working a download out again."""
    pieces = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[2] in ("PC>", "<DEV"):
            pieces.append((parts[2], bytes.fromhex(parts[4])))
    return pieces


# --- talking to it ------------------------------------------------------------------------------

def _start_log(folder: Path | None, what: str, now) -> ByteLog | None:
    if folder is None:
        return None
    try:
        return ByteLog(folder, what, now)
    except OSError:
        return None


def _open(opener: Callable[[], object], deadline: float, clock: Callable[[], float],
          waiting: Callable[[int], None] | None, sleep: Callable[[float], None]):
    """Opens the link, looking again every second until ``deadline`` while no audiometer is on USB."""
    while True:
        try:
            return opener()
        except DeviceMissing:
            left = deadline - clock()
            if left <= 0:
                raise
            if waiting:
                waiting(math.ceil(left))
            sleep(min(OPEN_RETRY, left))


def _session(opener: Callable[[], object], log: ByteLog | None, clock: Callable[[], float],
             deadline: float, waiting: Callable[[int], None] | None, sleep: Callable[[float], None],
             acknowledge: bool = True) -> Session:
    try:
        link = _open(opener, deadline, clock, waiting, sleep)
    except Exception as error:  # DeviceError says what to do; anything else is passed on as it is
        said = str(error) if isinstance(error, DeviceError) else f"청력검사기를 열지 못했습니다: {error}"
        if log:
            log.note(f"열지 못함: {said}")
            log.close()
        raise DeviceError(said, log.path if log else None) from None
    if log:
        log.note(f"열었음: {getattr(link, 'name', type(link).__name__)} {getattr(link, 'path', '')}".rstrip())
    return Session(link, usb=True, clock=clock, log=log.write if log else None, acknowledge=acknowledge)


def _connect(session: Session, deadline: float, clock: Callable[[], float],
             waiting: Callable[[int], None] | None) -> str:
    """Asks who the device is until it answers or sends tests by itself. A device that answers
    with an ack only, and no serial number, has answered all the same: the tests then carry it."""
    try:
        return session.connect(max(deadline - clock(), 0.0), waiting)
    except SessionError:
        raise DeviceError(NO_ANSWER) from None


def fetch(opener: Callable[[], object], log_folder: Path | None = None,
          progress: Callable[[int], None] | None = None, now: Callable[[], datetime] = datetime.now,
          clock: Callable[[], float] = time.monotonic, waiting: Callable[[int], None] | None = None,
          wait: float | None = None, sleep: Callable[[float], None] | None = None) -> Fetched:
    """Identifies the audiometer and fetches every test in its memory, waiting up to ``wait``
    seconds (``DEVICE_WAIT``) for it; ``waiting`` is told the seconds left meanwhile. Raises DeviceError."""
    deadline = clock() + (DEVICE_WAIT if wait is None else wait)
    sleep = sleep or time.sleep
    log = _start_log(log_folder, "청력검사기에서 받기", now)
    session = _session(opener, log, clock, deadline, waiting, sleep)
    try:
        how = _connect(session, deadline, clock, waiting)
        if log and how == PUSHED:
            log.note("기기가 묻기 전에 검사를 보내 옴: 보내는 대로 받고 더 묻지 않음")
        download = session.download(progress=progress)
        if log:
            log.note(f"받음: 일련번호 {download.serial or '모름'}, 기기 환자 {len(download.groups)}명, "
                     f"검사 {len(download.tests)}건, 끝 신호 {'있음' if download.finished else '없음'}, "
                     f"알아보지 못한 조각 {len(download.corrupt)}"
                     + (", 기기가 스스로 보냄" if download.pushed else "")
                     + (f", 두 번 온 검사 {download.repeats}건은 하나만" if download.repeats else ""))
        return Fetched(download, log.path if log else None)
    except DeviceError as error:
        if log:
            log.note(f"멈춤: {error}")
        raise DeviceError(str(error), log.path if log else None) from None
    except Exception as error:  # the USB cable pulled half-way, or anything else the link reports
        if log:
            log.note(f"멈춤: {type(error).__name__}: {error}")
        raise DeviceError(STOPPED.format(error=str(error).rstrip(".")), log.path if log else None) from None
    finally:
        _close(session, log)


def check(opener: Callable[[], object], log_folder: Path | None = None,
          now: Callable[[], datetime] = datetime.now, clock: Callable[[], float] = time.monotonic,
          waiting: Callable[[int], None] | None = None, wait: float | None = None,
          sleep: Callable[[float], None] | None = None) -> str:
    """Asks the audiometer who it is, and nothing more. Returns what it said. Raises DeviceError.
    Tests the device sends meanwhile are not answered, and the link is closed without the goodbye
    (KC 07, which over USB also answers a test), so that it does not take them as delivered."""
    deadline = clock() + (DEVICE_WAIT if wait is None else wait)
    sleep = sleep or time.sleep
    log = _start_log(log_folder, "청력검사기 연결 시험", now)
    session = _session(opener, log, clock, deadline, waiting, sleep, acknowledge=False)
    try:
        how = _connect(session, deadline, clock, waiting)
    except DeviceError as error:
        raise DeviceError(str(error), log.path if log else None) from None
    except Exception as error:
        raise DeviceError(STOPPED.format(error=str(error).rstrip(".")), log.path if log else None) from None
    finally:
        _close(session, log, goodbye=False)  # nothing it sent may read as delivered
    if how == PUSHED:
        return (f"연결됨: 기기가 묻기 전에 검사를 보내 왔습니다. 장비에서 [{CONNECT_KEY}]를 다시 누른 뒤 "
                "[청력검사기에서 받기]로 받으세요.")
    if session.serial:
        return f"연결됨: MRS Resonance {session.serial[:4]} (일련번호 {session.serial})"
    return f"연결됨: 일련번호는 알려 주지 않았습니다 (통신 판 {session.protocol_version})"


def _close(session: Session, log: ByteLog | None, goodbye: bool = True) -> None:
    try:
        session.close(goodbye)
    except Exception:  # the link may already be gone; closing must not hide what went wrong
        pass
    if log:
        log.note("닫음")
        log.close()


# --- which tests have been saved ----------------------------------------------------------------

class SeenTests:
    """The fingerprints of the device's tests that have been saved as a result, kept in a file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> set[str]:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        seen = saved.get("seen") if isinstance(saved, dict) else None
        return set(seen) if isinstance(seen, dict) else set()

    def add(self, fingerprints, sheet: str = "", when: datetime | None = None) -> None:
        """Notes the tests as saved. Raises OSError when the file cannot be written."""
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        seen = saved.get("seen") if isinstance(saved, dict) and isinstance(saved.get("seen"), dict) else {}
        stamp = (when or datetime.now()).isoformat(timespec="seconds")
        for fingerprint in fingerprints:
            seen.setdefault(fingerprint, {"at": stamp, "sheet": sheet})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_name(self.path.name + ".part")
        part.write_text(json.dumps({"format": 1, "seen": seen}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(part, self.path)
