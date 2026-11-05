"""Repeat occurrences: the same text, elsewhere in the document.

A name accepted on page 1 usually appears again in the signature block. Detectors are
not consistent about repeats, and the export verifier correctly refuses a file in which a
redacted string is still readable somewhere else. This module turns that refusal into a
step the reviewer can take: it PROPOSES every other occurrence of each accepted text.

Rules, chosen to be conservative:
- only text the reviewer ACCEPTED is searched for; nothing is accepted automatically here;
- a text that was also REJECTED anywhere is skipped (the reviewer drew a distinction);
- an occurrence already covered by any decision is skipped;
- matching ignores case and treats any whitespace (including a line break) as one space,
  and requires word boundaries, so "Kim" does not match inside "Kimberly";
- very short texts (under 3 characters, or digits only and under 4) are skipped.

Known limit: a text can mean different things in different places (a surname that is also
part of a firm name). The reviewer sees each proposal and decides; the benchmark reports how
often auto-accepting them would damage protected text.
"""
from __future__ import annotations

import re

from .models import PageText
from .policy import Decision

_TRIM = " \t\n.,;:()\"'"


def _norm(text: str) -> str:
    return " ".join(text.split()).strip(_TRIM)


def _span_of(d: Decision, pages: list[PageText]) -> tuple[int, int] | None:
    """Character range a decision covers on its page (None if it covers no text)."""
    if d.end > d.start:
        return d.start, d.end
    words = []                                   # manual box: words whose centre is inside it
    for r in d.rects:
        words += [w for w in pages[d.page].words
                  if r[0] <= (w.rect.x0 + w.rect.x1) / 2 <= r[2] and r[1] <= (w.rect.y0 + w.rect.y1) / 2 <= r[3]]
    return (min(w.start for w in words), max(w.end for w in words)) if words else None


def _text_of(d: Decision, pages: list[PageText]) -> str:
    span = _span_of(d, pages)
    return _norm(pages[d.page].text[span[0]:span[1]]) if span else ""


def _usable(text: str) -> bool:
    if len(text) < 3 or not any(ch.isalnum() for ch in text):
        return False
    return not (text.replace(" ", "").isdigit() and len(text) < 4)


def find_repeats(decisions: list[Decision], pages: list[PageText]) -> list[Decision]:
    accepted: dict[str, Decision] = {}
    rejected: set[str] = set()
    covered: dict[int, list[tuple[int, int]]] = {}
    for d in decisions:
        span = _span_of(d, pages)
        if span:
            covered.setdefault(d.page, []).append(span)
        t = _text_of(d, pages).casefold()
        if not _usable(t):
            continue
        if d.status == "accepted":
            accepted.setdefault(t, d)
        elif d.status == "rejected":
            rejected.add(t)
    out: list[Decision] = []
    seen: set[tuple[int, int, int]] = set()
    for key, src in accepted.items():
        if key in rejected:
            continue
        pat = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(tok) for tok in key.split()) + r"(?!\w)",
                         re.IGNORECASE)
        for pno, page in enumerate(pages):
            for m in pat.finditer(page.text):
                s, e = m.start(), m.end()
                if any(s < ce and cs < e for cs, ce in covered.get(pno, [])) or (pno, s, e) in seen:
                    continue
                seen.add((pno, s, e))
                rects = tuple((r.x0, r.y0, r.x1, r.y1) for r in page.locate(s, e))
                out.append(Decision(
                    pno, s, e, src.category,
                    f"Same text as an item you accepted (page {src.page + 1}).", "repeat", 1.0,
                    "redact", "pending", "repeat", rects))
    return sorted(out, key=lambda d: (d.page, d.start))
