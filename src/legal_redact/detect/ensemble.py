"""Architecture B: run several detectors and merge their suggestions.

Merge rule: suggestions of the SAME category that overlap become one, covering
the UNION of their characters. Union is the recall-safe choice (a shorter span
from one detector cannot leave the tail of a name behind); its cost is some
over-redaction, which the benchmark measures. The merged reason lists which
detectors agreed, which is useful context for the reviewer.
"""
from __future__ import annotations

from typing import Sequence

from ..models import PageText
from .base import Detector, Suggestion


class EnsembleDetector:
    def __init__(self, parts: list[Detector], name: str = "ensemble"):
        self.parts, self.name = parts, name

    def fit(self, train_docs) -> None:
        for p in self.parts:
            if hasattr(p, "fit"):
                p.fit(train_docs)

    def detect(self, pages: Sequence[PageText]) -> list[Suggestion]:
        allp = [s for part in self.parts for s in part.detect(pages)]
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
