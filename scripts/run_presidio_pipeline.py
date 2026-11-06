"""Presidio reference pipeline: presidio-analyzer + PyMuPDF word boxes -> real redactions.

Usage: python run_presidio_pipeline.py IN_DIR OUT_DIR [LIMIT]

This is documented glue, not a shipped end-to-end tool. It is the "why not just Presidio?"
baseline: per page, extract words with PyMuPDF `get_text("words")`, rebuild a character-offset
text (words joined by single spaces), run `AnalyzerEngine.analyze(text=..., language="en")`
with all default entities and default settings (spaCy `en_core_web_lg`), map every entity's
character span to the PyMuPDF word rectangles it overlaps, and apply real PyMuPDF redactions
(the text is removed, not just covered).

Requires `presidio-analyzer`, `spacy` and the `en_core_web_lg` model in the current
environment (the project's `[ml]` extra installs them).

Writes OUT_DIR/<doc_id>/<doc_id>_redacted.pdf and OUT_DIR/results.json, the format
`legal-redact bench-h2h --external "label=results.json"` reads.
Not part of the legal-redact package: it is an external-tool harness.
"""
import json
import sys
import time
from pathlib import Path

import pymupdf
from presidio_analyzer import AnalyzerEngine


def redact_page(engine: AnalyzerEngine, page: "pymupdf.Page") -> int:
    words = page.get_text("words")            # (x0, y0, x1, y1, word, block, line, word_no)
    text, spans = "", []                      # spans: (char_start, char_end, Rect)
    for w in words:
        if text:
            text += " "
        start = len(text)
        text += w[4]
        spans.append((start, len(text), pymupdf.Rect(w[0], w[1], w[2], w[3])))
    if not text.strip():
        return 0
    found = engine.analyze(text=text, language="en")     # entities=None -> all defaults
    rects = set()
    for r in found:
        for start, end, rect in spans:
            if start < r.end and end > r.start:
                rects.add((rect.x0, rect.y0, rect.x1, rect.y1))
    for x0, y0, x1, y1 in rects:
        page.add_redact_annot(pymupdf.Rect(x0, y0, x1, y1))
    if rects:
        page.apply_redactions()
    return len(found)


def main() -> int:
    in_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
    out_dir.mkdir(parents=True, exist_ok=True)
    print("loading Presidio AnalyzerEngine (all default entities)...", flush=True)
    t_load = time.perf_counter()
    engine = AnalyzerEngine()
    engine.analyze(text="Alice Example lives in Berlin, phone +1 555 0100.", language="en")
    print(f"engine ready in {time.perf_counter() - t_load:.1f}s", flush=True)
    results = {}
    for src in sorted(in_dir.glob("*.pdf"))[:limit]:
        doc_id = src.stem
        work = out_dir / doc_id
        work.mkdir(exist_ok=True)
        out = work / f"{doc_id}_redacted.pdf"
        t0 = time.perf_counter()
        err = ""
        try:
            doc = pymupdf.open(src)
            for page in doc:
                redact_page(engine, page)
            doc.save(out, garbage=4, deflate=True)
            doc.close()
            if not out.exists() or out.stat().st_size == 0:
                err = "no output written"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        results[doc_id] = {"seconds": round(time.perf_counter() - t0, 3),
                           "outputs": [str(out)], "error": err}
        print(doc_id, results[doc_id]["seconds"], err[:80], flush=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    ok = sum(1 for r in results.values() if not r["error"])
    print(f"done: {ok}/{len(results)} outputs written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
