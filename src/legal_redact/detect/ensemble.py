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
3. Merge by union. Suggestions of the SAME category that overlap become one
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
_AMOUNT = re.compile(r"^(?:[A-Z]{3}|[$€£])\s?\d[\d,.\s]*$|^\d[\d,.\s]*\s?(?:EUR|USD|GBP)$")


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
            if not (s.category in (C.ID, C.PHONE, C.CARD)
                    and _AMOUNT.match(text[s.page][s.start:s.end].strip()))]


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

    def fit(self, train_docs) -> None:
        for p in self.parts:
            if hasattr(p, "fit"):
                p.fit(train_docs)

    def detect(self, pages: Sequence[PageText]) -> list[Suggestion]:
        text = {p.page: p.text for p in pages}
        allp = [check_structured(s, text[s.page])
                for part in self.parts for s in part.detect(pages)]
        veto = [s for s in allp if s.category == C.NOT_PERSON]       # evidence only, never output
        allp = [s for s in allp if s.category != C.NOT_PERSON]
        if self.resolve:
            allp = veto_persons(suppress_contained(drop_amounts(allp, text)), veto)
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
        return sorted(out, key=lambda s: (s.page, s.start))
