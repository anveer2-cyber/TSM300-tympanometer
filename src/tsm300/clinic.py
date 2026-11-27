"""The clinic a result sheet comes from, as printed on the sheet."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Clinic:
    name: str = ""
    address: str = ""
    phone: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.name or self.address or self.phone)

    def to_dict(self) -> dict:
        return {"name": self.name, "address": self.address, "phone": self.phone}

    @classmethod
    def from_dict(cls, saved: dict) -> Clinic:
        return cls(str(saved.get("name") or ""), str(saved.get("address") or ""), str(saved.get("phone") or ""))
