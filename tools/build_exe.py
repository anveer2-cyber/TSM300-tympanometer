"""Builds dist/TSM300.exe: the whole program in one file, for PCs without Python.

Run on Windows, from the repository folder:

    python -m pip install . pyinstaller
    python tools/build_exe.py

The program is built without a console window. ``--collect-data`` takes the icon from the
installed package, which is why the package is installed first.
"""

from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent

PyInstaller.__main__.run([
    str(ROOT / "tools" / "exe_entry.py"),
    "--name", "TSM300",
    "--onefile",
    "--windowed",
    "--noconfirm",
    "--clean",
    "--icon", str(ROOT / "src" / "tsm300" / "assets" / "tsm300.ico"),
    "--collect-data", "tsm300",
    # Optional extras of the drawing library that this program never touches. They only
    # get in when they happen to be installed on the building PC, and they are large.
    "--exclude-module", "tkinter",
    "--exclude-module", "pandas",
    "--exclude-module", "scipy",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build" / "pyinstaller"),
    "--specpath", str(ROOT / "build"),
])
