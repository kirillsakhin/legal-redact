"""R0: the rule-based baseline.

Deliberately simple. It is the yardstick every later detector must beat, and it
is also the part of the final system that handles structured identifiers well,
because checksums (IBAN mod-97, Luhn) give real certainty that a model cannot.
Names and addresses are handled by weak heuristics so the benchmark can show
exactly why a learned detector is needed.
"""
from __future__ import annotations

import re
from typing import Sequence

from ..models import PageText
from .base import Category as C
from .base import Suggestion


def iban_ok(raw: str) -> bool:
    s = re.sub(r"\s", "", raw).upper()
    if not (15 <= len(s) <= 34) or not s.isalnum():
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def luhn_ok(raw: str) -> bool:
    ds = [int(c) for c in re.sub(r"\D", "", raw)]
    if not (13 <= len(ds) <= 19):
        return False
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
_DATE = (rf"(?:\d{{1,2}}\s+{_MONTH}\s+\d{{4}}|{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}"
         r"|\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{4}-\d{2}-\d{2})")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_INTL_RE = re.compile(r"\+\d{1,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{1,4}(?:[ .-]?\d{2,4}){1,4}")
PHONE_KW_RE = re.compile(r"(?i)\b(?:tel|phone|mobile|fax)\b\.?:?\s*(\+?[\d(][\d ().-]{6,18}\d)")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
CARD_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
DOB_RE = re.compile(rf"(?i)\b(?:date of birth|born(?: on)?|DOB)\b[:\s]+({_DATE})")
ID_RE = re.compile(
    r"(?i)\b(?:passport|tax id|tax identification|TIN|national id|id)(?:\s+(?:no|number|#))?\.?:?\s+"
    r"([A-Z0-9][A-Z0-9-]{5,})")
STREET = r"(?:Street|St\.|Road|Rd\.|Avenue|Ave\.|Lane|Drive|Boulevard)"
ADDRESS_RE = re.compile(
    rf"\b\d{{1,5}}[A-Za-z]? [A-Z][a-z]+(?: [A-Z][a-z]+)* {STREET}"
    r"(?:, [A-Z][a-z]+(?: [A-Z][a-z]+)*)?"
    r"(?:, [A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2})?")

_NAME_RE = re.compile(
    r"\b(?:(?:Mr|Ms|Mrs|Dr|Prof)\.? )?"
    r"([A-Z][a-z]+(?: [A-Z]\.)? [A-Z][a-z]+(?:-[A-Z][a-z]+)?)\b")
_STOP = {"The", "This", "That", "These", "Agreement", "Party", "Parties", "Section",
         "Clause", "Services", "Service", "Confidential", "Information", "Effective",
         "Date", "Governing", "Law", "Payment", "Terms", "Notices", "Term", "Schedule",
         "Signed", "Definitions", "Fees", "Borrower", "Lender", "Tenant", "Landlord",
         "Provider", "Client", "Guarantor", "Property", "Rent", "Deposit", "Loan"}
_ORG_AFTER = re.compile(r"^\s*(?:&|LLP|Ltd|LLC|Inc|GmbH|Holdings|Partners|Group|Bank|Capital)\b")


def _digits_only_neighbours_ok(text: str, s: int, e: int) -> bool:
    return not (s > 0 and text[s - 1].isalnum()) and not (e < len(text) and text[e].isalnum())


class RulesDetector:
    name = "rules"

    def detect(self, pages: Sequence[PageText]) -> list[Suggestion]:
        out: list[Suggestion] = []
        for p in pages:
            out += self._page(p)
        return out

    def _page(self, p: PageText) -> list[Suggestion]:
        t, n = p.text, self.name
        res: list[Suggestion] = []

        def add(s, e, cat, why, conf):
            res.append(Suggestion(p.page, s, e, cat, why, conf, n))

        for m in EMAIL_RE.finditer(t):
            add(m.start(), m.end(), C.EMAIL, "Matches an e-mail address pattern.", 0.99)
        for m in IBAN_RE.finditer(t):
            if iban_ok(m.group()):
                add(m.start(), m.end(), C.IBAN, "Valid IBAN (mod-97 checksum passes).", 0.99)
        for m in CARD_RE.finditer(t):
            if luhn_ok(m.group()):
                add(m.start(), m.end(), C.CARD, "Valid card number (Luhn checksum passes).", 0.95)
        for m in PHONE_INTL_RE.finditer(t):
            add(m.start(), m.end(), C.PHONE, "International phone number format.", 0.85)
        for m in PHONE_KW_RE.finditer(t):
            add(m.start(1), m.end(1), C.PHONE, "Number introduced by a phone keyword.", 0.8)
        for m in DOB_RE.finditer(t):
            add(m.start(1), m.end(1), C.DOB, "Date introduced by a date-of-birth keyword.", 0.9)
        for m in ID_RE.finditer(t):
            add(m.start(1), m.end(1), C.ID, "Identifier introduced by a passport/tax/ID keyword.", 0.8)
        for m in ADDRESS_RE.finditer(t):
            add(m.start(), m.end(), C.ADDRESS, "Street-address pattern.", 0.6)
        for m in _NAME_RE.finditer(t):
            s, e = m.start(1), m.end(1)
            toks = m.group(1).split()
            if any(tok in _STOP for tok in toks) or _ORG_AFTER.match(t[e:]):
                continue
            if s >= 2 and t[s - 2:s].strip() == "&":
                continue
            add(s, e, C.PERSON, "Two capitalised words that look like a personal name.", 0.4)
        return _dedupe(res)


def _dedupe(items: list[Suggestion]) -> list[Suggestion]:
    """Drop a suggestion fully inside a higher-confidence one of any category."""
    items = sorted(items, key=lambda s: (-s.confidence, s.start))
    kept: list[Suggestion] = []
    for s in items:
        if any(k.page == s.page and k.start <= s.start and s.end <= k.end for k in kept):
            continue
        kept.append(s)
    return sorted(kept, key=lambda s: (s.page, s.start))
