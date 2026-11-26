"""The program's settings file, kept outside the code so that keys and addresses never enter the repository.

The file is ``%APPDATA%\\TSM300\\설정.json`` on Windows. Key names follow the clinic's VNGLive
program where the meaning is the same.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

FILE_NAME = "설정.json"


@dataclass
class Settings:
    port: str = ""
    emr_via: str = ""
    """How patients are looked up: ``odbc`` (the records database on this PC), ``server``
    (the clinic's relay server) or empty for typing them in only."""
    emr_dsn: str = ""
    emr_uid: str = ""
    emr_pwd: str = ""
    emr_server: str = ""
    emr_token: str = ""
    cap_on: bool = False
    cap_url: str = ""
    cap_key: str = ""

    @property
    def capture_ready(self) -> bool:
        return bool(self.cap_url.strip() and self.cap_key.strip())


def settings_directory() -> Path:
    override = os.environ.get("TSM300_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "TSM300"
    return Path.home() / ".config" / "TSM300"


def is_packaged() -> bool:
    """True when running as the single-file program (TSM300.exe) rather than from Python."""
    return bool(getattr(sys, "frozen", False))


def results_directory() -> Path:
    """Where the window keeps results when it is not told a folder.

    From Python that is ``results`` in the folder the program was started from. The packaged
    program can be started from anywhere, a downloads folder for one, so it keeps them in a
    fixed place beside its settings.
    """
    return settings_directory() / "results" if is_packaged() else Path("results")


def load(directory: Path | None = None) -> Settings:
    """Reads the settings, falling back to the previous save when the file is damaged."""
    path = (directory or settings_directory()) / FILE_NAME
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            known = {field.name: field.type for field in fields(Settings)}
            values = {}
            for name, value in data.items():
                if name in known:
                    values[name] = bool(value) if known[name] == "bool" else str(value or "")
            return Settings(**values)
    return Settings()


def save(settings: Settings, directory: Path | None = None) -> None:
    """Writes the settings through a temporary file, keeping the previous save as a backup."""
    path = (directory or settings_directory()) / FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=1), encoding="utf-8")
    if path.exists():
        os.replace(path, path.with_name(path.name + ".bak"))
    os.replace(part, path)
