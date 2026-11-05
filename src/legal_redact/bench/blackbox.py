"""Black-box scoring of a redacted PDF, for comparing different tools on equal terms.

A tool under test is given a source PDF and produces an output PDF. We know what
personal data the document contains (gold) and what legally important text must
survive (protected). We score ONLY the output file, with two independent readers
(pdfminer.six and pypdfium2), and never look inside the tool:

- leaked: a gold string that is still extractable from the output (full-string match,
  whitespace and punctuation normalised; a compact match catches strings split by
  line breaks);
- lost: a protected string that is no longer extractable (collateral damage);
- doc leak: at least one leaked gold string.

If the output has (almost) no extractable text, it was probably flattened to images, so
text checks cannot judge it. Such outputs are counted as NOT_CHECKED, never as clean.

Limits: string presence is a weaker test than the span-coverage metric used elsewhere
(it cannot see a half-removed name that no longer matches the whole string), and a
repeated name counts once per distinct string. It is the same test for every tool.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..verify import _compact, _has, _pdfium_pages, _pdfminer_pages, norm
from .metrics import wilson
from .synth import SynthDoc

MIN_TEXT_CHARS = 40     # below this, an output counts as having no text layer


@dataclass
class BBScore:
    doc_id: str
    checked: bool = True
    leaked: Counter = field(default_factory=Counter)        # category -> distinct strings
    total_gold: Counter = field(default_factory=Counter)    # category -> distinct strings
    lost: int = 0
    total_protected: int = 0
    error: str = ""


def output_text(path: str | Path) -> str:
    """Normalised text of the output as read by BOTH readers, concatenated."""
    return norm(" ".join(_pdfminer_pages(path) + _pdfium_pages(path)))


def _present(whole: str, text: str) -> bool:
    n = norm(text)
    return bool(n) and (_has(whole, n) or (len(_compact(n)) >= 8 and _compact(n) in _compact(whole)))


def score_output(doc: SynthDoc, out_path: str | Path) -> BBScore:
    sc = BBScore(doc.doc_id)
    try:
        whole = output_text(out_path)
    except Exception as exc:                    # unreadable output is never "clean"
        sc.checked, sc.error = False, f"{type(exc).__name__}"
        return sc
    if len(whole) < MIN_TEXT_CHARS:
        sc.checked, sc.error = False, "no text layer"
        return sc
    seen = set()
    for g in doc.gold:
        s = doc.text[g.start:g.end]
        if (g.category, norm(s)) in seen:
            continue
        seen.add((g.category, norm(s)))
        sc.total_gold[g.category] += 1
        if _present(whole, s):
            sc.leaked[g.category] += 1
    seen_p = set()
    for p in doc.protected:
        s = doc.text[p.start:p.end]
        if norm(s) in seen_p:
            continue
        seen_p.add(norm(s))
        sc.total_protected += 1
        sc.lost += not _present(whole, s)
    return sc


@dataclass
class BBSummary:
    tool: str
    docs: int
    checked: int
    doc_leak_rate: float
    doc_leak_ci: tuple
    per_category_leak: dict       # category -> share of gold strings still extractable
    protected_lost_rate: float
    unchecked: Counter
    errors: int


def summarize(tool: str, scores: list[BBScore], categories=None) -> BBSummary:
    """categories: count a document as leaking only if a string of one of these categories leaked
    (used to compare a tool on the categories it claims to support). None = all categories."""
    ok = [s for s in scores if s.checked]
    keep = (lambda c: True) if categories is None else (lambda c: c in categories)
    leaky = sum(1 for s in ok if sum(n for c, n in s.leaked.items() if keep(c)) > 0)
    cats = defaultdict(lambda: [0, 0])
    for s in ok:
        for c, n in s.total_gold.items():
            cats[c][0] += s.leaked[c]
            cats[c][1] += n
    lost = sum(s.lost for s in ok)
    prot = sum(s.total_protected for s in ok)
    return BBSummary(
        tool, len(scores), len(ok), leaky / len(ok) if ok else float("nan"), wilson(leaky, len(ok)),
        {c: k / n for c, (k, n) in sorted(cats.items())}, lost / prot if prot else float("nan"),
        Counter(s.error for s in scores if not s.checked), 0)


def format_table(rows: list[BBSummary]) -> str:
    cats = sorted({c for r in rows for c in r.per_category_leak})
    head = f"{'tool':34} {'docs':>5} {'doc leak (95% CI)':>20} {'protected lost':>14} " + \
        " ".join(f"{c[:6]:>6}" for c in cats)
    out = [head]
    for r in rows:
        lo, hi = r.doc_leak_ci
        leak = f"{r.doc_leak_rate:.0%} ({lo:.0%}-{hi:.0%})" if r.checked else "n/a"
        out.append(f"{r.tool:34} {r.checked:>2}/{r.docs:<2} {leak:>20} {r.protected_lost_rate:>14.1%} " +
                   " ".join(f"{r.per_category_leak.get(c, float('nan')):>6.0%}" for c in cats))
        if r.unchecked:
            out.append(f"{'':34} not checked: {dict(r.unchecked)}")
    out.append("columns after 'protected lost' = share of gold strings of that category still extractable")
    return "\n".join(out)
