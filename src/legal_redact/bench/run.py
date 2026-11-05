"""Benchmark runner with leave-template-out evaluation.

For each template T: build the detector, let it `fit` on documents from all
OTHER templates (if it has a fit method), and score it on T only. Scores are
then pooled. A rules-only baseline has nothing to fit, but the loop is here so
that thresholds tuned in later phases can never see the held-out layout.

Documents go through the real pipeline: render to PDF -> reopen -> extract.
If extraction does not reproduce the generator's text exactly, we stop: the
labels would no longer line up and every number would be meaningless.
"""
from __future__ import annotations

from typing import Callable

import pymupdf

from ..detect.base import Detector
from ..extract import extract_pages
from .metrics import DocScore, Summary, score_doc, summarize
from .synth import SynthDoc, TEMPLATES, corpus, render_pdf


class ExtractionMismatch(RuntimeError):
    pass


def pages_for(doc: SynthDoc):
    pages = extract_pages(pymupdf.open(stream=render_pdf(doc), filetype="pdf"))
    if pages[0].text != doc.text:
        raise ExtractionMismatch(f"{doc.doc_id}: extracted text differs from generated text")
    return pages


def leave_template_out(factory: Callable[[], Detector], docs: list[SynthDoc]):
    scores: list[DocScore] = []
    misses: list[tuple[SynthDoc, tuple]] = []
    name = factory().name
    for held in sorted({d.template for d in docs}):
        det = factory()
        train = [d for d in docs if d.template != held]
        if hasattr(det, "fit"):
            det.fit(train)
        for d in (d for d in docs if d.template == held):
            sc = score_doc(d, det.detect(pages_for(d)))
            scores.append(sc)
            misses += [(d, m) for m in sc.leaks]
    return summarize(name, scores), misses


def format_summary(s: Summary) -> str:
    out = [f"detector={s.detector} docs={s.n_docs} (leave-template-out, synthetic)", ""]
    out.append(f"{'category':9} {'prec':>6} {'recall':>7} {'strict':>7} {'gold':>5} {'pred':>5}")
    f = lambda x: "   n/a" if x != x else f"{x:6.2f}"  # noqa: E731
    for c, m in s.per_category.items():
        out.append(f"{c:9} {f(m['precision'])} {f(m['recall'])}  {f(m['strict_recall'])} "
                   f"{m['n_gold']:5d} {m['n_pred']:5d}")
    lo, hi = s.doc_leak_ci
    out += ["", f"docs with >=1 residual leak: {s.doc_leak_rate:.0%} (95% Wilson CI {lo:.0%}-{hi:.0%})",
            f"leaked gold spans: {s.leaked_spans}/{s.total_gold}",
            f"protected spans damaged (over-redaction): {s.protected_damage_rate:.1%}",
            "leak rate by held-out template: " + ", ".join(
                f"{t}={m['leak_rate']:.0%}" for t, m in sorted(s.per_template.items()))]
    return "\n".join(out)


def main_bench(detector: str, n: int, seed: int, show_misses: bool) -> int:
    from ..detect.rules import RulesDetector
    factories = {"rules": RulesDetector}
    docs = corpus(per_template=n, seed0=seed, templates=TEMPLATES)
    summary, misses = leave_template_out(factories[detector], docs)
    print(format_summary(summary))
    if show_misses:
        print("\nleaked spans (SYNTHETIC text):")
        for d, (cat, a, b) in misses[:40]:
            print(f"  {d.doc_id} {cat:8} {d.text[a:b]!r}")
    return 0
