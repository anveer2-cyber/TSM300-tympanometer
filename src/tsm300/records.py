"""The clinic's systems as this program sees them: patient lookup, today's orders, sending to 상쾌한캡처.

``links`` holds the queries and requests ported from the clinic's VNGLive program; this
module turns its dictionaries into ``Patient`` and ``Order`` and its errors into text an
operator can act on.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from . import links
from .patient import Order, Patient, parse_birth_date
from .settings import Settings

EXAM_KIND = "tymp"


class Records:
    """Looks patients up through ``backend``, a ``links.MagicDb`` or ``links.YejinServer``."""

    def __init__(self, backend, today: Callable[[], date] = date.today) -> None:
        self._backend = backend
        self._today = today
        self._internal: dict[str, str] = {}

    def find(self, chart_no: str) -> Patient | None:
        """The patient with this chart number. Raises LookupError when two patients share it."""
        try:
            found = self._backend.find_patient(chart_no)
        except LookupError:
            raise
        except Exception as error:
            raise RuntimeError(error_text(error)) from None
        return self._patient(found) if found else None

    def todays_orders(self) -> list[Order]:
        try:
            rows = self._backend.todays_orders(links.TYMP_CODES, self._today().isoformat())
        except Exception as error:
            raise RuntimeError(error_text(error)) from None
        return [
            Order(self._patient(row), time=row.get("time") or "", label=row.get("tests") or "", room=row.get("room") or "")
            for row in rows
        ]

    def internal_number(self, chart_no: str) -> str:
        """The records' internal number for a chart number looked up earlier, or "" when not known.

        It is never sent anywhere: 상쾌한캡처 is addressed by the chart number on screen. It only
        serves to check afterwards that the sheet was filed under the same patient.
        """
        return self._internal.get(chart_no, "")

    def _patient(self, found: dict) -> Patient:
        patient = to_patient(found)
        if patient.chart_no:
            if found.get("code") is None:
                self._internal.pop(patient.chart_no, None)
            else:
                self._internal[patient.chart_no] = str(found["code"]).strip()
        return patient

    def check(self) -> str:
        """Tries the connection and describes it. Raises RuntimeError with the reason on failure."""
        try:
            if hasattr(self._backend, "test"):
                return self._backend.test()
            self._backend.search("0")
            return "예진이 서버에 연결됨"
        except Exception as error:
            raise RuntimeError(error_text(error)) from None

    def close(self) -> None:
        self._backend.close()


def records_from_settings(settings: Settings) -> Records | None:
    if settings.emr_via == "odbc":
        return Records(links.MagicDb(settings.emr_dsn, settings.emr_uid, settings.emr_pwd))
    if settings.emr_via == "server":
        return Records(links.YejinServer(settings.emr_server, settings.emr_token))
    return None


def to_patient(found: dict) -> Patient:
    birth = found.get("birth") or ""
    try:
        birth_date = parse_birth_date(birth)
    except ValueError:
        birth_date = None
    name = found.get("name") or ""
    return Patient(found.get("chart") or "", name, found.get("sex") or "", birth_date)


def error_text(error: Exception) -> str:
    """Says what went wrong reaching the records database, without connection details."""
    text = str(error)
    if isinstance(error, ModuleNotFoundError):
        return "이 PC 에 pyodbc 가 설치되어 있지 않습니다. (python -m pip install pyodbc)"
    if "IM002" in text:
        return "ODBC 데이터 원본을 찾지 못했습니다. 설정의 DSN 이름을 확인해 주세요."
    if "28000" in text or "password" in text.lower():
        return "매직차트 DB 아이디나 비밀번호가 맞지 않습니다."
    if "IM014" in text or "architecture" in text.lower():
        return "64비트용 ODBC 데이터 원본이 없습니다."
    if type(error).__module__.startswith("pyodbc"):
        return "매직차트 DB 에서 읽지 못했습니다."
    return text


def send_sheet(sheet: Path, patient: Patient, tested_on: date, settings: Settings, internal_number: str = "",
               send: Callable[..., dict] = links.save_to_entcapture, now: Callable[[], datetime] = datetime.now) -> dict:
    """Sends the sheet to 상쾌한캡처 and notes the outcome in the result's JSON file.

    The receiver is given the chart number on screen, the name and the birth date, and checks
    them against the records itself. Returns ``{"ok": True, "imageDataName": ..., "chartId": ...}``
    or ``{"ok": False, "code": ..., "error": ..., "retry": ...}``. When ``internal_number`` is
    known and the receiver filed the sheet under a different one, the outcome also carries
    ``"expected_chartId"``.
    """
    details = {"chart": patient.chart_no, "name": patient.name,
               "birth": patient.birth_date.isoformat() if patient.birth_date else ""}
    try:
        reply = send(str(sheet), details, tested_on, EXAM_KIND, url=settings.cap_url, key=settings.cap_key)
        outcome = {"ok": True, "duplicate": bool(reply.get("duplicate")), "id": reply.get("id"),
                   "imageDataName": reply.get("imageDataName"), "chartId": str(reply.get("chartId") or "")}
        if internal_number and outcome["chartId"] and not _same_number(internal_number, outcome["chartId"]):
            outcome["expected_chartId"] = internal_number
    except links.SendError as error:
        outcome = {"ok": False, "code": error.code, "error": str(error), "retry": error.retry}
    _note_outcome(sheet.with_suffix(".json"), dict(outcome, at=now().isoformat(timespec="seconds")))
    return outcome


def _same_number(one: str, other: str) -> bool:
    one, other = one.strip(), other.strip()
    if one.isdigit() and other.isdigit():
        return int(one) == int(other)
    return one == other


def charts_tested_on(directory: Path, day: date) -> set[str]:
    """Chart numbers that already have a saved result on ``day``."""
    charts = set()
    for path in directory.glob(f"{day:%Y%m%d}_*.json"):
        try:
            chart = json.loads(path.read_text(encoding="utf-8"))["patient"]["chart_no"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if chart:
            charts.add(chart)
    return charts


def _note_outcome(record: Path, outcome: dict) -> None:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data.setdefault("capture", []).append(outcome)
    record.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
