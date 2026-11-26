"""The icon and the desktop shortcut."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from tsm300 import cli
from tsm300.shortcut import ICON, SHORTCUT_NAME, create_shortcut, windowless_python

ON_WINDOWS = sys.platform == "win32"


def test_icon_holds_every_size_windows_asks_for():
    with Image.open(ICON) as icon:
        assert icon.format == "ICO"
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(icon.info["sizes"])
    with Image.open(ICON.with_suffix(".png")) as picture:
        assert picture.size == (256, 256)


def test_icon_is_set_on_the_program_and_shown_on_its_windows():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtGui import QIcon

    icon = QIcon(str(ICON))
    assert not icon.isNull() and not icon.pixmap(32, 32).isNull()


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
    read_back = (
        "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:TSM300_LINK); "
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Write-Output $link.TargetPath $link.Arguments $link.WorkingDirectory $link.IconLocation"
    )
    finished = subprocess.run(["powershell", "-NoProfile", "-Command", read_back],
                              env=dict(os.environ, TSM300_LINK=str(created)), capture_output=True, check=True)
    target, arguments, working_directory, icon = finished.stdout.decode("utf-8").split("\r\n")[:4]
    assert Path(target) == windowless_python()
    assert arguments == "-m tsm300"
    assert Path(working_directory) == work
    assert Path(icon.split(",")[0]) == ICON
