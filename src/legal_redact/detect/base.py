"""The contract every detector implements.

A detector only SUGGESTS. It returns character spans on the extracted page text
plus a category and a human-readable reason. Policy (what to do about a
category for a given disclosure purpose) and the final decision live elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from ..models import PageText


class Category:
    PERSON = "PERSON"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    ADDRESS = "ADDRESS"
    ID = "ID"            # passport, tax and national identifiers
    IBAN = "IBAN"        # bank account numbers
    CARD = "CARD"        # payment card numbers
    DOB = "DOB"          # date of birth
    ALL = (PERSON, EMAIL, PHONE, ADDRESS, ID, IBAN, CARD, DOB)


@dataclass(frozen=True)
class Suggestion:
    page: int
    start: int          # char offsets into PageText.text, end exclusive
    end: int
    category: str
    reason: str         # why we suggest it; shown to the lawyer
    confidence: float   # 0..1; rules with a checksum are near 1, heuristics lower
    detector: str
    support: int = 1    # how many detectors agreed (set by the ensemble)


class Detector(Protocol):
    name: str

    def detect(self, pages: Sequence[PageText]) -> list[Suggestion]: ...
