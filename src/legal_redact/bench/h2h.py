"""Head-to-head: our tool and external tools, scored identically from output files only.

Our tool runs here (with a pinned detector configuration). An external tool is run by
its own harness in its own environment and leaves output PDFs plus a results.json;
this module scores those outputs with bench/blackbox.py, the same way as ours.

Controls in every table: the unredacted source (must leak everything) and an oracle
export built from the gold labels (must leak nothing and lose nothing). A comparison
without both controls would not show that the scorer can tell good from bad.

Our two modes:
- accept-all: every suggestion is accepted, including the ones sent to the review queue.
  This is "no human decisions", the closest match to a tool run in batch.
- auto-only: only suggestions the policy proposes to redact are accepted; the review queue
  is left undecided and therefore NOT redacted. This is what happens if the queue is skipped.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pymupdf

from ..extract import extract_pages
from ..pipeline import ExportRefused, export, suggest
from ..policy import load_policy
from ..redact import rects_for_spans, write_redacted
from .blackbox import BBScore, format_table, score_output, summarize
from .synth import SynthDoc


def write_sources(docs: list[SynthDoc], folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    for d in docs:
        p = folder / f"{d.doc_id}.pdf"
        p.write_bytes(d.pdf)
        out[d.doc_id] = p
    return out


def make_oracle(docs, sources, out_dir: Path) -> dict[str, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {}
    for d in docs:
        pages = extract_pages(pymupdf.open(sources[d.doc_id]))
        boxes = rects_for_spans(pages, [(0, g.start, g.end) for g in d.gold])
        p = out_dir / f"{d.doc_id}.pdf"
        write_redacted(sources[d.doc_id], p, boxes)
        res[d.doc_id] = p
    return res


def make_ours(docs, sources, out_dir: Path, mode: str, detector, policy_name="counterparty"):
    """mode: 'all' or 'auto'. Returns ({doc_id: path or None}, {'refused': n, 'seconds': s})."""
    assert mode in ("all", "auto")
    out_dir.mkdir(parents=True, exist_ok=True)
    pol, res, refused, t0 = load_policy(policy_name), {}, 0, time.perf_counter()
    for d in docs:
        s = suggest(str(sources[d.doc_id]), detector, pol)
        for i, dec in enumerate(s.decisions):
            take = dec.status == "pending" and (mode == "all" or dec.proposed == "redact")
            (s.accept if take else s.reject)(i)
        for f in s.findings:
            s.acknowledge(f.code)
        p = out_dir / f"{d.doc_id}.pdf"
        try:
            export(str(sources[d.doc_id]), str(p), s)
            res[d.doc_id] = p
        except ExportRefused:
            res[d.doc_id] = None
            refused += 1
    return res, {"refused": refused, "seconds": round(time.perf_counter() - t0, 1)}


def load_external(results_json: Path) -> dict[str, Path | None]:
    data = json.loads(results_json.read_text())
    out = {}
    for doc_id, r in data.items():
        pdfs = [Path(x) for x in r.get("outputs", []) if x.lower().endswith(".pdf")]
        # prefer the file that looks like the redacted PDF, not a review or image copy
        red = [p for p in pdfs if "redacted" in p.name.lower() and "review" not in p.name.lower()]
        out[doc_id] = (red or pdfs or [None])[0]
    return out


def score_arm(name: str, docs, mapping: dict[str, Path | None], categories=None):
    scores = []
    for d in docs:
        p = mapping.get(d.doc_id)
        if p is None or not Path(p).exists():
            sc = BBScore(d.doc_id, checked=False, error="no output produced")
        else:
            sc = score_output(d, p)
        scores.append(sc)
    return summarize(name, scores, categories)


def table(arms: list) -> str:
    return format_table(arms)
