"""Architecture B: run several detectors and merge their suggestions.

Two ideas, both visible in the benchmark:

1. Checksums beat model scores. A card number that fails Luhn, or an IBAN that
   fails mod-97, is demoted to a low confidence (so policy sends it to a human)
   no matter how sure a model was; one that passes is raised to 0.99. A model that
   calls an IBAN a "card number" is relabelled when the IBAN checksum passes.
2. Merge by union. Suggestions of the SAME category that overlap become one
   covering the union of their characters (recall-safe: a shorter span from one
   detector cannot leave the tail of a name behind). The merged reason lists which
   detectors agreed, and `support` carries that count to the policy.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from ..models import PageText
from .base import Category as C
from .base import Detector, Suggestion
from .rules import iban_ok, luhn_ok

DEMOTED = 0.3


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


class EnsembleDetector:
    def __init__(self, parts: list[Detector], name: str = "ensemble"):
        self.parts, self.name = parts, name

    def fit(self, train_docs) -> None:
        for p in self.parts:
            if hasattr(p, "fit"):
                p.fit(train_docs)

    def detect(self, pages: Sequence[PageText]) -> list[Suggestion]:
        text = {p.page: p.text for p in pages}
        allp = [check_structured(s, text[s.page])
                for part in self.parts for s in part.detect(pages)]
        allp.sort(key=lambda s: (s.page, s.category, s.start))
        merged: list[list[Suggestion]] = []
        for s in allp:
            last = merged[-1] if merged else None
            if (last and last[0].page == s.page and last[0].category == s.category
                    and s.start < max(x.end for x in last)):
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
