"""Core data types shared by every pipeline stage.

Design rule: coordinates are PDF points with a TOP-LEFT origin (PyMuPDF's
convention), one page at a time. A detector works on text offsets; this module
is what lets us turn an offset range back into rectangles on the page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def union(self, other: "Rect") -> "Rect":
        return Rect(min(self.x0, other.x0), min(self.y0, other.y0),
                    max(self.x1, other.x1), max(self.y1, other.y1))


@dataclass(frozen=True)
class Word:
    text: str
    rect: Rect
    line_id: int      # (block, line) pair collapsed to one id per page
    start: int        # char offset of this word in PageText.text
    end: int          # exclusive
    confidence: float | None = None   # 0..1 from OCR engines; None for a real text layer


@dataclass
class PageText:
    """Reading-order text of one page plus the geometry behind every word."""
    page: int         # 0-based
    width: float
    height: float
    text: str
    words: list[Word] = field(default_factory=list)
    ocr: bool = False  # True when the text came from OCR of the page image, not a text layer

    def locate(self, start: int, end: int) -> list[Rect]:
        """Rectangles covering text[start:end], one per line.

        Granularity is the WORD: a span that covers half a word still gets the
        whole word's box. For redaction that errs on the safe side (we remove
        more, never less); the cost is some over-redaction, which the
        benchmark measures.
        """
        per_line: dict[int, Rect] = {}
        for w in self.words:
            if w.end <= start or w.start >= end:
                continue
            cur = per_line.get(w.line_id)
            per_line[w.line_id] = w.rect if cur is None else cur.union(w.rect)
        return list(per_line.values())


class Severity(str, Enum):
    REFUSE = "refuse"   # export is blocked
    REVIEW = "review"   # export allowed only after explicit human acknowledgement
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    code: str           # stable id, e.g. "PF003"
    severity: Severity
    message: str        # never contains document text
    page: int | None = None
