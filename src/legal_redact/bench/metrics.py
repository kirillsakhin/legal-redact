"""Scoring. Pure functions, no I/O.

Definitions (state them in every report):
- Overlap match: a prediction and a gold span of the SAME category share at least
  one character. Used for per-category precision/recall.
- Strict match: same category and identical start/end.
- Covered: every character of a gold span lies inside the union of ALL predicted
  spans (any category). A gold span that is not covered is a LEAK: part of the
  personal data would survive redaction.
- Doc leak: the document has at least one leaked gold span.
- Damaged protected span: a protected span overlapping any prediction.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field

from .synth import SynthDoc


def _overlap(a0, a1, b0, b1) -> bool:
    return min(a1, b1) > max(a0, b0)


def _covered(gs, ge, preds) -> bool:
    pos = gs
    for s, e, _ in sorted((p for p in preds if p[0] < ge and p[1] > gs)):
        if s > pos:
            return False
        pos = max(pos, e)
        if pos >= ge:
            return True
    return pos >= ge


@dataclass
class DocScore:
    doc_id: str
    template: str
    # per category counters
    tp_p: dict = field(default_factory=lambda: defaultdict(int))   # predictions that hit
    n_p: dict = field(default_factory=lambda: defaultdict(int))    # predictions
    tp_r: dict = field(default_factory=lambda: defaultdict(int))   # gold hit (overlap)
    strict_r: dict = field(default_factory=lambda: defaultdict(int))
    n_g: dict = field(default_factory=lambda: defaultdict(int))
    leaks: list = field(default_factory=list)      # (category, start, end) gold not covered
    protected_total: int = 0
    protected_damaged: int = 0


def score_doc(doc: SynthDoc, suggestions) -> DocScore:
    sc = DocScore(doc.doc_id, doc.template)
    preds = [(s.start, s.end, s.category) for s in suggestions if s.page == 0]
    for g in doc.gold:
        sc.n_g[g.category] += 1
        same = [p for p in preds if p[2] == g.category and _overlap(g.start, g.end, p[0], p[1])]
        if same:
            sc.tp_r[g.category] += 1
        if any(p[0] == g.start and p[1] == g.end for p in same):
            sc.strict_r[g.category] += 1
        if not _covered(g.start, g.end, preds):
            sc.leaks.append((g.category, g.start, g.end))
    for p in preds:
        sc.n_p[p[2]] += 1
        if any(g.category == p[2] and _overlap(g.start, g.end, p[0], p[1]) for g in doc.gold):
            sc.tp_p[p[2]] += 1
    for q in doc.protected:
        sc.protected_total += 1
        if any(_overlap(q.start, q.end, p[0], p[1]) for p in preds):
            sc.protected_damaged += 1
    return sc


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


@dataclass
class Summary:
    detector: str
    n_docs: int
    per_category: dict       # cat -> dict(precision, recall, strict_recall, n_gold, n_pred)
    doc_leak_rate: float
    doc_leak_ci: tuple[float, float]
    leaked_spans: int
    total_gold: int
    protected_damage_rate: float
    per_template: dict       # template -> dict(docs, leak_rate)


def summarize(detector: str, scores: list[DocScore]) -> Summary:
    cats = sorted({c for s in scores for c in list(s.n_g) + list(s.n_p)})
    per = {}
    for c in cats:
        tp_p = sum(s.tp_p[c] for s in scores)
        n_p = sum(s.n_p[c] for s in scores)
        tp_r = sum(s.tp_r[c] for s in scores)
        st_r = sum(s.strict_r[c] for s in scores)
        n_g = sum(s.n_g[c] for s in scores)
        per[c] = dict(precision=tp_p / n_p if n_p else float("nan"),
                      recall=tp_r / n_g if n_g else float("nan"),
                      strict_recall=st_r / n_g if n_g else float("nan"),
                      n_gold=n_g, n_pred=n_p)
    leaky = sum(1 for s in scores if s.leaks)
    tpl = defaultdict(lambda: [0, 0])
    for s in scores:
        tpl[s.template][0] += 1
        tpl[s.template][1] += 1 if s.leaks else 0
    pt = sum(s.protected_total for s in scores)
    pd_ = sum(s.protected_damaged for s in scores)
    return Summary(
        detector, len(scores), per, leaky / len(scores) if scores else 0.0,
        wilson(leaky, len(scores)), sum(len(s.leaks) for s in scores),
        sum(sum(s.n_g.values()) for s in scores), pd_ / pt if pt else 0.0,
        {t: dict(docs=n, leak_rate=k / n) for t, (n, k) in tpl.items()})
