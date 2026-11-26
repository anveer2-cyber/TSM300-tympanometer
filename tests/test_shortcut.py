"""The icon and the desktop shortcut."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from tsm300 import cli
from tsm300.shortcut import ICON, SHORTCUT_NAME, create_shortcut, start_command, windowless_python

ON_WINDOWS = sys.platform == "win32"


def test_icon_holds_every_size_windows_asks_for():
    with Image.open(ICON) as icon:
        assert icon.format == "ICO"
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(icon.info["sizes"])
    with Image.open(ICON.with_suffix(".png")) as picture:
        assert picture.size == (256, 256)


def test_icon_loads_as_a_window_icon():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])  # Qt refuses to make pictures without one
    icon = QIcon(str(ICON))
    assert not icon.isNull() and not icon.pixmap(32, 32).isNull()
    assert application is QApplication.instance()


@pytest.mark.skipif(ON_WINDOWS, reason="other systems cannot make a Windows shortcut")
def test_shortcut_command_explains_itself_off_windows(tmp_path, capsys):
    with pytest.raises(RuntimeError, match="Windows 에서만"):
        create_shortcut(tmp_path)

    assert cli.main(["shortcut"]) == 1
    assert "Windows 에서만" in capsys.readouterr().out


@pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows shell")
def test_shortcut_starts_the_program_without_a_console_from_the_chosen_folder(tmp_path):
    work = tmp_path / "검사 결과"
    work.mkdir()
    folder = tmp_path / "바탕 화면"
    folder.mkdir()

    created = create_shortcut(work, folder)

    assert created == folder / f"{SHORTCUT_NAME}.lnk" and created.exists()
    link = read_back(created)
    assert Path(link["target"]) == windowless_python()
    assert link["arguments"] == "-m tsm300"
    assert Path(link["working_directory"]) == work
    assert Path(link["icon"]) == ICON
    assert link["description"] == SHORTCUT_NAME


def test_from_python_a_shortcut_runs_the_module_and_the_packaged_program_runs_itself(tmp_path, monkeypatch):
    assert start_command() == (windowless_python(), "-m tsm300", ICON)

    program = tmp_path / "TSM300.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(program))

    assert start_command() == (program, "", program)


@pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows shell")
def test_shortcut_of_the_packaged_program_starts_it_directly(tmp_path, monkeypatch):
    program = tmp_path / "TSM300.exe"
    program.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(program))

    link = read_back(create_shortcut(tmp_path, tmp_path))

    assert Path(link["target"]) == program
    assert link["arguments"] == ""
    assert Path(link["icon"]) == program


@pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows shell")
def test_shortcut_goes_to_the_desktop_by_default(tmp_path, monkeypatch, capsys):
    import tsm300.shortcut as shortcut

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(shortcut, "_desktop", lambda ole32: desktop)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["shortcut"]) == 0

    assert (desktop / f"{SHORTCUT_NAME}.lnk").exists()
    said = capsys.readouterr().out
    assert "바로가기를 만들었습니다" in said and str(tmp_path / "results") in said


@pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows shell")
def test_the_real_desktop_folder_is_found():
    import ctypes

    import tsm300.shortcut as shortcut

    assert shortcut._desktop(ctypes.OleDLL("ole32")).is_dir()


def read_back(link: Path) -> dict:
    """Reads a shortcut with the Windows shell itself, independently of the code that wrote it."""
    script = (
        "$item = (New-Object -ComObject Shell.Application).NameSpace($env:TSM300_FOLDER).ParseName($env:TSM300_FILE); "
        "$link = $item.GetLink; $icon = ''; [void]$link.GetIconLocation([ref]$icon); "
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Write-Output $link.Path $link.Arguments $link.WorkingDirectory $icon $link.Description"
    )
    finished = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], capture_output=True, check=True,
        env=dict(os.environ, TSM300_FOLDER=str(link.parent), TSM300_FILE=link.name),
    )
    lines = finished.stdout.decode("utf-8").split("\r\n")
    return dict(zip(("target", "arguments", "working_directory", "icon", "description"), lines))
