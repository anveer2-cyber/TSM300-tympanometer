"""The program's icon and a desktop shortcut that starts it without a console window."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ICON = Path(__file__).parent / "assets" / "tsm300.ico"
SHORTCUT_NAME = "TSM-300 임피던스 검사"
APP_ID = "TSM300.Tympanometry"

# Paths reach PowerShell through environment variables, so Hangul in them needs no quoting or code page.
_CREATE = (
    "$folder = if ($env:TSM300_FOLDER) { $env:TSM300_FOLDER } else { [Environment]::GetFolderPath('Desktop') }; "
    "$path = Join-Path $folder ($env:TSM300_NAME + '.lnk'); "
    "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($path); "
    "$link.TargetPath = $env:TSM300_TARGET; "
    "$link.Arguments = $env:TSM300_ARGUMENTS; "
    "$link.WorkingDirectory = $env:TSM300_WORKDIR; "
    "$link.IconLocation = $env:TSM300_ICON; "
    "$link.Description = $env:TSM300_NAME; "
    "$link.Save(); "
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "Write-Output $path"
)


def windowless_python() -> Path:
    """The interpreter that runs without opening a console, where the installation has one."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def create_shortcut(working_directory: Path, folder: Path | None = None) -> Path:
    """Creates the shortcut on the desktop, or in ``folder``, and returns its path.

    The program started from it keeps its results under ``working_directory``.
    """
    if sys.platform != "win32":
        raise RuntimeError("바로가기는 Windows 에서만 만들 수 있습니다.")
    environment = dict(
        os.environ,
        TSM300_FOLDER=str(folder) if folder else "",
        TSM300_NAME=SHORTCUT_NAME,
        TSM300_TARGET=str(windowless_python()),
        TSM300_ARGUMENTS="-m tsm300",
        TSM300_WORKDIR=str(working_directory),
        TSM300_ICON=str(ICON),
    )
    finished = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _CREATE],
        env=environment, capture_output=True,
    )
    if finished.returncode != 0:
        raise RuntimeError("바로가기를 만들지 못했습니다: " + finished.stderr.decode("utf-8", "replace").strip())
    return Path(finished.stdout.decode("utf-8", "replace").strip())


def claim_taskbar_identity() -> None:
    """Makes Windows show this program's own icon on the taskbar instead of Python's."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
