"""Architecture B: run several detectors and merge their suggestions.

Ideas, each one visible in the benchmark:

1. Checksums beat model scores. A card number that fails Luhn, or an IBAN that
   fails mod-97, is demoted to a low confidence (so policy sends it to a human)
   no matter how sure a model was; one that passes is raised to 0.99. A model that
   calls an IBAN a "card number" is relabelled when the IBAN checksum passes.
2. Conflict resolution (resolve=True). Currency amounts are not IDs or phone
   numbers; a suggestion lying entirely inside a more confident one of another
   category is redundant (coverage cannot shrink); a PERSON mostly covered by an
   organisation or place name is demoted to the review queue, not dropped.
3. Resolution also removes suggestions that the surrounding text contradicts:
   a date with no birth keyword is a contract date, not a date of birth (the
   rules detector has always required the keyword; a model may guess it from
   context); a personal-name box inside a firm name is the firm, not a person.
   Both are false positives that caused most of the measured over-redaction.
4. Merge by union. Suggestions of the SAME category that overlap become one
   covering the union of their characters (recall-safe: a shorter span from one
   detector cannot leave the tail of a name behind). The merged reason lists which
   detectors agreed, and `support` carries that count to the policy.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Sequence

from ..models import PageText
from .base import Category as C
from .base import Detector, Suggestion
from .rules import iban_ok, luhn_ok

DEMOTED = 0.3

# A currency amount may be written "USD 1,000" or "1,000 USD". The amount guard
# accepts a currency token either inside the span or immediately next to it.
_CURRENCY = (r"(?:USD|EUR|GBP|CHF|JPY|CAD|AUD|NZD|SEK|NOK|DKK|PLN|CZK|HUF|RUB|INR|CNY|"
             r"BRL|MXN|ZAR|TRY|AED|SAR|ILS|KRW|SGD|HKD|[$€£])")
_AMOUNT = re.compile(
    r"^(?:[A-Z]{3}|[$€£])\s?\d[\d,.\s]*$"
    r"|^\d[\d,.\s]*\s?(?:[A-Z]{3}|[$€£])$")
_NUMBER = re.compile(r"^\d[\d,.\s]*$")
_AMOUNT_BEFORE = re.compile(_CURRENCY + r"\s?$")
_AMOUNT_AFTER = re.compile(r"^\s?" + _CURRENCY + r"\b")

# A DOB claim needs birth evidence: the rules detector only matches a date
# introduced by a birth keyword, and this is the same contract for the models.
# The evidence may sit anywhere on the date's own line (a table header such as
# "DOB   12 May 1985" qualifies) or within DOB_CONTEXT characters of the span.
# Known limitation: a DOB value printed on its own line, with its keyword on a
# previous line and further than DOB_CONTEXT characters away, is not proposed.
_DOB_KEYWORD = re.compile(
    r"(?i)\b(?:date\s+of\s+birth|birth\s*date|born(?: on)?|d\.?\s?o\.?\s?b\.?)\b")
DOB_CONTEXT = 28

# A PERSON box that belongs to an organisation name is the wrong category: a
# firm named after people must survive, and a model often boxes the surnames.
# The evidence is a designator adjacent to the span (after it, before it, or
# inside it) - never a benchmark label and never the old NOT_PERSON veto.
_ORG_DESIGNATOR = (
    r"(?:LLP|Ltd|Limited|LLC|Inc|Corp|Corporation|GmbH|AG|PLC|Holdings|Partners|"
    r"Group|Bank|Capital|Associates|Studio|S\.A\.|S\.L\.|B\.V\.|BV|Law\s+Offices|"
    r"Offices|Bureau|Firm|Practice|& Co)")
_ORG_INSIDE = re.compile(r"(?:^|[\s,.(])" + _ORG_DESIGNATOR + r"(?:$|[\s,.)])")
_ORG_AFTER = re.compile(r"^\s*(?:&|" + _ORG_DESIGNATOR + r")\b")
_ORG_BEFORE = re.compile(
    r"(?:^|[^A-Za-z])(?:Studio|Bureau|Firm|Practice|Law\s+Offices|LLP|Ltd|LLC|Inc|GmbH|"
    r"Holdings|Partners|Group|Bank|Capital|Associates)[\s,]+"
    r"(?:(?:von|van|de|da|del|der|la|le|al|el)\s+)?$")


def _is_amount(text: str, start: int, end: int) -> bool:
    raw = text[start:end].strip()
    if _AMOUNT.match(raw):
        return True
    return bool(_NUMBER.match(raw) and (
        _AMOUNT_BEFORE.search(text[max(0, start - 8):start])
        or _AMOUNT_AFTER.match(text[end:end + 8])))


def _is_firm_person(text: str, start: int, end: int) -> bool:
    return bool(_ORG_INSIDE.search(text[start:end])
                or _ORG_AFTER.match(text[end:end + 40])
                or _ORG_BEFORE.search(text[max(0, start - 40):start]))


def _dob_context(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    lo = max(0, min(line_start, start - DOB_CONTEXT))
    hi = min(len(text), max(line_end, end + DOB_CONTEXT))
    return text[lo:hi]


def check_structured(s: Suggestion, text: str) -> Suggestion:
    raw = text[s.start:s.end]
    if s.category in (C.CARD, C.IBAN) and iban_ok(raw):
        return replace(s, category=C.IBAN, confidence=0.99,
                       reason="Valid IBAN (mod-97 checksum passes).")
    if s.category == C.CARD:
        if luhn_ok(raw):
            return replace(s, confidence=0.99, reason="Valid card number (Luhn checksum passes).")
        return replace(s, confidence=min(s.confidence, DEMOTED),
                       reason=s.reason + " Fails the Luhn checksum, so it is probably not a card number.")
    if s.category == C.IBAN:
        return replace(s, confidence=min(s.confidence, DEMOTED),
                       reason=s.reason + " Fails the IBAN checksum.")
    return s


def drop_amounts(items: list[Suggestion], text: dict) -> list[Suggestion]:
    """A currency amount is not an ID, phone or card number."""
    return [s for s in items
            if not (s.category in (C.ID, C.PHONE, C.CARD) and _is_amount(text[s.page], s.start, s.end))]


def drop_unfounded_dob(items: list[Suggestion], text: dict) -> list[Suggestion]:
    """A date with no birth keyword next to it is a contract date, not a date of birth."""
    return [s for s in items
            if not (s.category == C.DOB and not _DOB_KEYWORD.search(
                _dob_context(text[s.page], s.start, s.end)))]


def drop_firm_persons(items: list[Suggestion], text: dict) -> list[Suggestion]:
    """A PERSON box inside an organisation name (a designator is adjacent) is the firm."""
    return [s for s in items
            if not (s.category == C.PERSON and _is_firm_person(text[s.page], s.start, s.end))]


def drop_persons_inside_addresses(items: list[Suggestion]) -> list[Suggestion]:
    """A PERSON box fully inside an ADDRESS box is the street/town part of that address
    (a street or place name can look like a personal name). The address box redacts the
    same characters, so character coverage cannot shrink; only review noise goes."""
    addresses = [s for s in items if s.category == C.ADDRESS]
    return [s for s in items
            if not (s.category == C.PERSON and any(
                a.page == s.page and a.start <= s.start and s.end <= a.end for a in addresses))]



def suppress_contained(items: list[Suggestion]) -> list[Suggestion]:
    """Drop a suggestion that lies entirely inside a MORE confident one of another category.
    Character coverage never shrinks, so recall cannot drop; only duplicate noise goes."""
    return [s for s in items
            if not any(t is not s and t.page == s.page and t.category != s.category
                       and t.start <= s.start and s.end <= t.end and t.confidence > s.confidence
                       for t in items)]


def veto_persons(items: list[Suggestion], veto: list[Suggestion]) -> list[Suggestion]:
    """A PERSON mostly covered by an organisation/place span is demoted to the review queue."""
    out = []
    for s in items:
        if s.category == C.PERSON and veto:
            cov = sum(max(0, min(s.end, v.end) - max(s.start, v.start))
                      for v in veto if v.page == s.page)
            if cov >= 0.5 * (s.end - s.start):
                s = replace(s, confidence=min(s.confidence, DEMOTED),
                            reason=s.reason + " Also looks like an organisation or place name.")
        out.append(s)
    return out


_BRIDGE = re.compile(r"^(?:,[ \n]?|[ \n])$")


class EnsembleDetector:
    def __init__(self, parts: list[Detector], name: str = "ensemble", resolve: bool = True,
                 bridge: bool = False):
        """resolve=False reproduces the earlier configuration (no conflict resolution).
        bridge=True joins two PERSON suggestions separated only by ', ' or a space, so
        'Surname, Firstname' becomes one box instead of two boxes with a comma between."""
        self.parts, self.name, self.resolve, self.bridge = parts, name, resolve, bridge
        # Within one run the same page text is sometimes detected several times: the
        # head-to-head harness scores our tool in four modes over the same sources.
        # Detectors are pure functions of the page text, so remember the result.
        self._cache: dict[tuple, list[Suggestion]] = {}

    def fit(self, train_docs) -> None:
        self._cache.clear()           # thresholds may change, so old results are stale
        for p in self.parts:
            if hasattr(p, "fit"):
                p.fit(train_docs)

    def detect(self, pages: Sequence[PageText]) -> list[Suggestion]:
        key = tuple((p.page, p.text) for p in pages)
        if key in self._cache:
            return list(self._cache[key])
        text = {p.page: p.text for p in pages}
        allp = [check_structured(s, text[s.page])
                for part in self.parts for s in part.detect(pages)]
        veto = [s for s in allp if s.category == C.NOT_PERSON]       # evidence only, never output
        allp = [s for s in allp if s.category != C.NOT_PERSON]
        if self.resolve:
            allp = drop_amounts(allp, text)
            allp = drop_unfounded_dob(allp, text)
            allp = drop_firm_persons(allp, text)
            allp = drop_persons_inside_addresses(allp)
            allp = veto_persons(suppress_contained(allp), veto)
        allp.sort(key=lambda s: (s.page, s.category, s.start))
        merged: list[list[Suggestion]] = []
        for s in allp:
            last = merged[-1] if merged else None
            if last and last[0].page == s.page and last[0].category == s.category:
                end = max(x.end for x in last)
                joined = s.start < end or (
                    self.bridge and s.category == C.PERSON
                    and bool(_BRIDGE.match(text[s.page][end:s.start])))
            else:
                joined = False
            if joined:
                last.append(s)
            else:
                merged.append([s])
        out = []
        for grp in merged:
            top = max(grp, key=lambda x: x.confidence)
            who = sorted({x.detector for x in grp})
            out.append(Suggestion(
                top.page, min(x.start for x in grp), max(x.end for x in grp), top.category,
                f"{top.reason} Agreed by {len(who)} detector(s): {', '.join(who)}.",
                max(x.confidence for x in grp), self.name, len(who)))
        result = sorted(out, key=lambda s: (s.page, s.start))
        self._cache[key] = result
        return list(result)
