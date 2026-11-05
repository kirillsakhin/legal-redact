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
    pages = extract_pages(pymupdf.open(stream=doc.pdf or render_pdf(doc), filetype="pdf"))
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


def registry():
    """name -> zero-argument factory. Imports are lazy so `rules` needs no ML stack."""
    from ..detect.ensemble import EnsembleDetector
    from ..detect.gliner_det import GlinerDetector
    from ..detect.presidio_det import PresidioDetector
    from ..detect.rules import RulesDetector
    kn, ur = "knowledgator/gliner-pii-base-v1.0", "urchade/gliner_multi_pii-v1"
    return {
        "rules": RulesDetector,
        "presidio": PresidioDetector,
        "gliner-kn": lambda: GlinerDetector(kn),
        "gliner-ur": lambda: GlinerDetector(ur),
        "ensemble": lambda: EnsembleDetector(
            [RulesDetector(), PresidioDetector(), GlinerDetector(kn)], "ensemble(rules+presidio+gliner-kn)"),
    }


def main_bench(detectors: list[str], n: int, seed: int, show_misses: bool) -> int:
    import time
    reg = registry()
    docs = corpus(per_template=n, seed0=seed, templates=TEMPLATES)
    rows = []
    for name in detectors:
        t0 = time.perf_counter()
        summary, misses = leave_template_out(reg[name], docs)
        dt = time.perf_counter() - t0
        print(format_summary(summary))
        print(f"wall time: {dt:.1f}s for {len(docs)} docs\n")
        if show_misses:
            print("leaked spans (SYNTHETIC text):")
            for d, (cat, a, b) in misses[:40]:
                print(f"  {d.doc_id} {cat:8} {d.text[a:b]!r}")
            print()
        rows.append((summary, dt))
    if len(rows) > 1:
        print("comparison")
        print(f"{'detector':44} {'PERSON P':>8} {'PERSON R':>8} {'leak docs':>9} {'prot dmg':>8}")
        for s, _ in rows:
            pm = s.per_category.get("PERSON", {})
            print(f"{s.detector:44} {pm.get('precision', float('nan')):8.2f} "
                  f"{pm.get('recall', float('nan')):8.2f} {s.doc_leak_rate:9.0%} {s.protected_damage_rate:8.1%}")
    return 0
