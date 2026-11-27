"""Bringing the single-file program (TSM300.exe) up to date from the project's releases page.

The newest release is found by following the page's ``latest`` address, which leads to the
address of that release and so names its version. The new program file is downloaded beside
the running one, started once to see that it is whole and is the version expected, and only
then put in its place. Windows lets a running program file be renamed but not overwritten, so
the running one is moved aside and removed when the program next starts.
"""

from __future__ import annotations

import http.client
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import __version__
from .settings import is_packaged

RELEASES = "https://github.com/anveer2-cyber/TSM300-tympanometer/releases"
PROGRAM_NAME = "TSM300.exe"
PYTHON_COMMAND = ("python -m pip install --upgrade "
                  "https://github.com/anveer2-cyber/TSM300-tympanometer/archive/refs/heads/claude/cool-hamilton-bdp178.zip")

RESTARTED = "TSM300_RESTARTED"
"""Set in the environment of a program started by ``restart``: the one that started it is
still closing, so the new one waits for it rather than report that it is already running."""

_TAG = re.compile(r"/releases/tag/(v?(\d+(?:\.\d+)*))/?$")
_HEADERS = {"User-Agent": f"TSM300/{__version__}"}
_CHUNK = 256 * 1024

Progress = Callable[[int, int], None]
"""Called with the bytes received so far and the size of the whole file (0 when not known)."""


class UpdateError(Exception):
    """An update could not be looked for or installed. The text is written for the operator."""


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    """Where this release's program file is downloaded from."""


def program_file() -> Path | None:
    """The program file that is running, or None when the program runs from Python."""
    return Path(sys.executable) if is_packaged() else None


def latest_release(releases: str = RELEASES, timeout: float = 20) -> Release:
    """The newest published release. Raises UpdateError when it cannot be found out."""
    request = urllib.request.Request(f"{releases}/latest", method="HEAD", headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            landed = response.geturl()
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise UpdateError(f"새 버전이 있는지 확인하지 못했습니다. 인터넷 연결을 확인해 주세요.\n({_reason(error)})") from None
    found = _TAG.search(urlsplit(landed).path)
    if found is None:
        raise UpdateError("내려받을 수 있는 버전이 아직 올라와 있지 않습니다.")
    tag, version = found.groups()
    return Release(version, f"{releases}/download/{tag}/{PROGRAM_NAME}")


def is_newer(version: str, than: str = __version__) -> bool:
    return _numbers(version) > _numbers(than)


def download(release: Release, target: Path, progress: Progress | None = None, timeout: float = 30) -> None:
    """Fetches the release's program file into ``target``. Raises UpdateError when it does not all arrive."""
    request = urllib.request.Request(release.url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, open(target, "wb") as file:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                file.write(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received, total)
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise UpdateError(f"새 버전을 내려받지 못했습니다.\n({_reason(error)})") from None
    if total and received != total:
        raise UpdateError("내려받기가 중간에 끊겼습니다. 잠시 뒤에 다시 해 주세요.")


def install(release: Release, program: Path, progress: Progress | None = None, run=subprocess.run) -> None:
    """Downloads ``release`` and puts it in the place of ``program``, which may be running.

    ``program`` is left as it was unless the downloaded file starts and names the version
    expected. Raises UpdateError when anything goes wrong.
    """
    fresh = program.with_name(f"{program.stem}.new{program.suffix}")
    try:
        download(release, fresh, progress)
        _check(fresh, release.version, run)
        _swap(program, fresh)
    finally:
        _remove(fresh)


def restart(program: Path, arguments: list[str], start=subprocess.Popen) -> None:
    """Starts ``program`` afresh, apart from this process, which the caller then ends."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    start([str(program), *arguments], env=dict(_fresh_environment(), **{RESTARTED: "1"}), close_fds=True, creationflags=flags,
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def clean_up(program: Path) -> None:
    """Removes what an earlier update left beside ``program``; a file still in use stays for next time."""
    _remove(_aside(program))
    _remove(program.with_name(f"{program.stem}.new{program.suffix}"))


def _check(fresh: Path, version: str, run) -> None:
    """Starts the downloaded file once: a cut-off, damaged or wrong file must not replace a working program."""
    try:
        finished = run([str(fresh), "--version"], capture_output=True, timeout=180, stdin=subprocess.DEVNULL,
                       env=_fresh_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as error:
        raise UpdateError(f"내려받은 파일을 실행해 보지 못했습니다. 백신 프로그램이 막았을 수 있습니다.\n({_reason(error)})") from None
    said = finished.stdout.decode("utf-8", "replace").strip()
    if finished.returncode != 0 or said != version:
        raise UpdateError(f"내려받은 파일이 v{version} 이 아닙니다. 프로그램은 바꾸지 않았습니다.")


def _swap(program: Path, fresh: Path) -> None:
    aside = _aside(program)
    try:
        _remove(aside)
        os.replace(program, aside)
    except OSError as error:
        raise UpdateError(f"프로그램 파일을 바꾸지 못했습니다. 이 폴더에 쓸 수 있는지 확인해 주세요.\n({_reason(error)})") from None
    try:
        os.replace(fresh, program)
    except OSError as error:
        os.replace(aside, program)
        raise UpdateError(f"새 프로그램 파일을 제자리에 두지 못했습니다.\n({_reason(error)})") from None


def _aside(program: Path) -> Path:
    """Where the replaced program file waits to be removed. The name does not look like a program to start."""
    return program.with_name(program.name + ".old")


def _fresh_environment() -> dict:
    """The environment for starting a program file that is not this one.

    A single-file program tells the programs it starts where it unpacked itself; without
    this the new file would run the old program's unpacked code.
    """
    return dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _reason(error: Exception) -> str:
    return str(getattr(error, "reason", None) or error) or type(error).__name__
