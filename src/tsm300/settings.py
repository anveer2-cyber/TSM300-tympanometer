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

from .clinic import Clinic

FILE_NAME = "설정.json"

EARLIER_CLINIC_NAME = "상쾌한이비인후과"
"""What every sheet was headed before the clinic's details became settings. A settings file
written by those versions has no clinic in it; the sheets of that installation keep this
heading until it is changed, rather than silently losing it in an update."""


@dataclass
class Settings:
    port: str = ""
    """The port the operator chose for the device; empty to leave finding it to the program."""
    port_heard: str = ""
    port_heard_id: str = ""
    """The port the device was last heard on, and the maker, model and serial number of its
    USB serial adapter. With several adapters on the PC they tell the program which one is
    the device's, so that it never opens another instrument's."""
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
    clinic_name: str = ""
    clinic_address: str = ""
    clinic_phone: str = ""
    results_dir: str = ""
    """Where results are kept; empty for the usual folder (see ``results_directory``)."""
    archive_dir: str = ""
    """Where finished results are filed by date; empty for a folder inside the results folder."""

    @property
    def capture_ready(self) -> bool:
        return bool(self.cap_url.strip() and self.cap_key.strip())

    @property
    def clinic(self) -> Clinic:
        return Clinic(self.clinic_name.strip(), self.clinic_address.strip(), self.clinic_phone.strip())


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


def results_directory(chosen: str = "") -> Path:
    """Where the window keeps results: the folder ``chosen`` in the settings, or the usual one.

    From Python the usual one is ``results`` in the folder the program was started from. The
    packaged program can be started from anywhere, a downloads folder for one, so it keeps
    them in a fixed place beside its settings.
    """
    if chosen.strip():
        return Path(chosen.strip())
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
            if "clinic_name" not in values:  # written before 0.10
                values["clinic_name"] = EARLIER_CLINIC_NAME
                # Those versions wrote down whichever port they had found by themselves. It was
                # never the operator's choice, and read as one it would stop the search for the
                # device. It is still the best hint of where the device was.
                values["port_heard"], values["port"] = values.get("port", ""), ""
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
