"""Seeded-leak suite: can the verifier actually fail?

For each synthetic document we make
  - one CLEAN export (oracle decisions -> real writer): the verifier must NOT fail it;
  - one LEAKY variant per injector, each leaking the personal data through one
    specific route: the verifier MUST fail every one.
Both halves matter: a verifier that rejects everything is useless, one that
accepts everything is dangerous. Injectors use PyMuPDF while the verifier reads
with pdfminer.six, pypdfium2 and pikepdf, so the two sides share no code.

Scope: this measures detection of ROUTES we thought of. It cannot show that no
unthought-of route exists; LIMITS in verify.py lists the known blind spots.
"""
from __future__ import annotations

import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf

from ..extract import extract_pages
from ..redact import rects_for_spans, write_redacted
from ..verify import needles_from_source, verify_export
from .synth import SynthDoc, corpus, render_pdf


def _boxes(src, doc: SynthDoc):
    pages = extract_pages(pymupdf.open(src))
    spans = [(0, g.start, g.end) for g in doc.gold]
    return rects_for_spans(pages, spans), [g.category for g in doc.gold], spans


def _name(doc):
    g = next(g for g in doc.gold if g.category == "PERSON")
    return doc.text[g.start:g.end]


def _resave(path, fn):
    d = pymupdf.open(path)
    fn(d)
    tmp = str(path) + ".tmp"
    d.save(tmp, garbage=4, deflate=True)
    d.close()
    shutil.move(tmp, path)


# ---- injectors: each takes (clean_out_path, src_path, doc, boxes) and mutates/creates the leaky file
def inj_metadata(out, src, doc, boxes):
    _resave(out, lambda d: d.set_metadata({"author": _name(doc), "title": "contract"}))


def inj_xmp(out, src, doc, boxes):
    _resave(out, lambda d: d.set_xml_metadata(f"<x:xmpmeta xmlns:x='adobe:ns:meta/'>{_name(doc)}</x:xmpmeta>"))


def inj_annotation(out, src, doc, boxes):
    _resave(out, lambda d: d[0].add_text_annot((300, 20), f"note: {_name(doc)}"))


def inj_embedded(out, src, doc, boxes):
    _resave(out, lambda d: d.embfile_add("n.txt", _name(doc).encode()))


def inj_hidden_white(out, src, doc, boxes):
    _resave(out, lambda d: d[0].insert_text((72, 800), _name(doc), fontsize=8, color=(1, 1, 1)))


def inj_invisible_text(out, src, doc, boxes):
    _resave(out, lambda d: d[0].insert_text((72, 810), _name(doc), fontsize=8, render_mode=3))


def inj_bookmark(out, src, doc, boxes):
    _resave(out, lambda d: d.set_toc([[1, _name(doc), 1]]))


def inj_link(out, src, doc, boxes):
    slug = _name(doc).replace(" ", "")
    _resave(out, lambda d: d[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 40, 20),
                                             "uri": f"https://x.test/{slug}"}))


def inj_javascript(out, src, doc, boxes):
    _resave(out, lambda d: d.xref_set_key(
        d.pdf_catalog(), "OpenAction", f"<</S/JavaScript/JS(app.alert\\('{_name(doc)}'\\))>>"))


def inj_form_field(out, src, doc, boxes):
    def add(d):
        w = pymupdf.Widget()
        w.field_type, w.field_name = pymupdf.PDF_WIDGET_TYPE_TEXT, "f1"
        w.rect, w.field_value = pymupdf.Rect(300, 20, 500, 40), _name(doc)
        d[0].add_widget(w)
    _resave(out, add)


def inj_cosmetic(out, src, doc, boxes):
    """Black rectangles drawn over the text; text left in the file."""
    d = pymupdf.open(src)
    for pno, r in boxes:
        d[pno].draw_rect(pymupdf.Rect(*r), color=(0, 0, 0), fill=(0, 0, 0))
    d.save(out, garbage=4, deflate=True)


def inj_misaligned(out, src, doc, boxes):
    """Boxes shifted off the text: a classic coordinate bug."""
    write_redacted(src, out, [(p, (r[0], r[1] + 14, r[2], r[3] + 14)) for p, r in boxes])


def inj_incremental(out, src, doc, boxes):
    """Redaction saved as an incremental update: the original page stays in the file."""
    shutil.copy(src, out)
    d = pymupdf.open(out)
    for pno, r in boxes:
        d[pno].add_redact_annot(pymupdf.Rect(*r), fill=(0, 0, 0))
    for pno in {p for p, _ in boxes}:
        d[pno].apply_redactions()
    d.saveIncr()
    d.close()


INJECTORS = {
    "cosmetic box only": inj_cosmetic, "misaligned box": inj_misaligned,
    "metadata field": inj_metadata, "XMP metadata": inj_xmp, "annotation": inj_annotation,
    "embedded file": inj_embedded, "white hidden text": inj_hidden_white,
    "invisible text (mode 3)": inj_invisible_text, "bookmark": inj_bookmark,
    "link URI": inj_link, "JavaScript": inj_javascript, "form field value": inj_form_field,
    "incremental save": inj_incremental,
}


def run(n_docs: int = 12, seed0: int = 0) -> dict:
    docs = [d for d in corpus(per_template=n_docs, seed0=seed0)][::max(1, 4 * n_docs // n_docs)]
    docs = docs[: n_docs * 4]
    tmp = Path(tempfile.mkdtemp())
    clean_fail, clean_warn = [], Counter()
    caught, total = Counter(), Counter()
    by_check = defaultdict(Counter)
    for i, doc in enumerate(docs):
        src = tmp / f"s{i}.pdf"
        src.write_bytes(render_pdf(doc))
        boxes, cats, _ = _boxes(src, doc)
        needles = needles_from_source(src, boxes, cats)
        clean = tmp / f"c{i}.pdf"
        write_redacted(src, clean, boxes)
        rep = verify_export(src, clean, boxes, needles)
        if not rep.passed:
            clean_fail.append((doc.doc_id, [c.id for c in rep.failed()]))
        for c in rep.checks:
            if c.status == "WARN":
                clean_warn[c.id] += 1
        for name, inj in INJECTORS.items():
            leaky = tmp / f"l{i}_{abs(hash(name)) % 10**6}.pdf"
            if name in ("cosmetic box only", "misaligned box", "incremental save"):
                inj(leaky, src, doc, boxes)
            else:
                shutil.copy(clean, leaky)
                inj(leaky, src, doc, boxes)
            r = verify_export(src, leaky, boxes, needles)
            total[name] += 1
            if not r.passed:
                caught[name] += 1
                for c in r.failed():
                    by_check[name][c.id] += 1
    shutil.rmtree(tmp, ignore_errors=True)
    return {"docs": len(docs), "clean_fail": clean_fail, "clean_warn": dict(clean_warn),
            "caught": caught, "total": total, "by_check": by_check}


def format_result(res: dict) -> str:
    out = [f"export-verifier suite: {res['docs']} synthetic documents", "",
           f"clean exports wrongly failed: {len(res['clean_fail'])}/{res['docs']}"
           + (f"  {res['clean_fail'][:5]}" if res["clean_fail"] else ""),
           f"clean exports with warnings by check: {res['clean_warn'] or 'none'}", "",
           f"{'leak injected':26} {'caught':>7}  caught by"]
    for name in INJECTORS:
        t = res["total"][name]
        by = ", ".join(f"{k}x{v}" for k, v in sorted(res["by_check"][name].items()))
        out.append(f"{name:26} {res['caught'][name]:>3}/{t:<3}  {by}")
    miss = sum(res["total"][n] - res["caught"][n] for n in INJECTORS)
    out.append("")
    out.append("every injected leak caught" if miss == 0 else f"MISSED LEAKS: {miss}")
    return "\n".join(out)
