"""Who a measurement belongs to. The device sends no patient details, so they come from the operator."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

_SEX = {"남": "남", "m": "남", "male": "남", "여": "여", "f": "여", "female": "여"}
_BIRTH = re.compile(r"(\d{4})(\d{2})(\d{2})|(\d{4})\D(\d{1,2})\D(\d{1,2})")
_UNSAFE_IN_FILE_NAME = re.compile(r'[\\/:*?"<>|\s]+')


@dataclass(frozen=True)
class Patient:
    chart_no: str = ""
    name: str = ""
    sex: str = ""
    birth_date: date | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.chart_no or self.name or self.sex or self.birth_date)

    def age_on(self, day: date) -> int | None:
        """Age in completed years on ``day``."""
        if self.birth_date is None:
            return None
        before_birthday = (day.month, day.day) < (self.birth_date.month, self.birth_date.day)
        return day.year - self.birth_date.year - before_birthday

    def file_tag(self) -> str:
        """The chart number in a form that is safe inside a file name. Names are kept out of file names."""
        return _UNSAFE_IN_FILE_NAME.sub("", self.chart_no)

    def to_dict(self) -> dict:
        return {
            "chart_no": self.chart_no,
            "name": self.name,
            "sex": self.sex,
            "birth_date": self.birth_date.isoformat() if self.birth_date else None,
        }

    @classmethod
    def from_dict(cls, saved: dict) -> Patient:
        """Rebuilds a patient from ``to_dict``; an unreadable birth date is dropped rather than refused."""
        try:
            birth_date = date.fromisoformat(saved.get("birth_date") or "")
        except ValueError:
            birth_date = None
        return cls(saved.get("chart_no") or "", saved.get("name") or "", saved.get("sex") or "", birth_date)


@dataclass(frozen=True)
class Order:
    """A test ordered for a patient today, as shown in the pick list."""

    patient: Patient
    time: str = ""
    label: str = ""
    room: str = ""

    @property
    def selectable(self) -> bool:
        """False for an order whose patient the records system could not identify."""
        return bool(self.patient.chart_no)


class PatientSource(Protocol):
    """Where patient details can be looked up, such as the clinic's records system."""

    def find(self, chart_no: str) -> Patient | None:
        """The patient with this chart number, or None when there is no such chart."""

    def todays_orders(self) -> list[Order]:
        """Patients with a tympanometry order today."""

    def internal_number(self, chart_no: str) -> str:
        """The records' own number for a chart number looked up earlier, or "" when not known."""


def parse_sex(text: str) -> str:
    """Returns 남 or 여, or an empty string when ``text`` is blank. Raises ValueError otherwise."""
    cleaned = text.strip().lower()
    if not cleaned:
        return ""
    if cleaned not in _SEX:
        raise ValueError(f"성별을 알 수 없습니다: {text!r} (남 또는 여)")
    return _SEX[cleaned]


def parse_birth_date(text: str) -> date | None:
    """Reads 1980-01-31, 1980.1.31, 1980/01/31 or 19800131. Returns None when ``text`` is blank."""
    cleaned = text.strip()
    if not cleaned:
        return None
    match = _BIRTH.fullmatch(cleaned)
    if match is None:
        raise ValueError(f"생년월일을 읽을 수 없습니다: {text!r} (예: 1980-01-31 또는 19800131)")
    try:
        birth = date(*(int(part) for part in match.groups() if part is not None))
    except ValueError:
        raise ValueError(f"없는 날짜입니다: {text!r}") from None
    if not date(1900, 1, 1) <= birth <= date.today():
        raise ValueError(f"생년월일로 볼 수 없는 날짜입니다: {text!r}")
    return birth
