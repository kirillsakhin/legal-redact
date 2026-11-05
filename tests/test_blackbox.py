import pymupdf

from legal_redact.bench import export_suite
from legal_redact.bench.blackbox import score_output, summarize
from legal_redact.bench.synth import generate, render_pdf
from legal_redact.extract import extract_pages
from legal_redact.redact import rects_for_spans, write_redacted


def _src(tmp_path, doc):
    p = tmp_path / "s.pdf"
    p.write_bytes(render_pdf(doc))
    return str(p)


def test_unredacted_output_leaks_everything_and_loses_nothing(tmp_path):
    d = generate("loan", 3)
    sc = score_output(d, _src(tmp_path, d))
    assert sc.checked and sum(sc.leaked.values()) == sum(sc.total_gold.values()) > 0
    assert sc.lost == 0


def test_oracle_export_has_no_leak_and_keeps_protected_text(tmp_path):
    d = generate("loan", 3)
    src = _src(tmp_path, d)
    boxes = rects_for_spans(extract_pages(pymupdf.open(src)), [(0, g.start, g.end) for g in d.gold])
    out = str(tmp_path / "o.pdf")
    write_redacted(src, out, boxes)
    sc = score_output(d, out)
    assert sc.checked and sum(sc.leaked.values()) == 0 and sc.lost == 0


def test_flattened_output_is_not_checked_never_clean(tmp_path):
    d = generate("loan", 3)
    src = _src(tmp_path, d)
    pix = pymupdf.open(src)[0].get_pixmap(dpi=60)
    img = pymupdf.open()
    pg = img.new_page(width=595, height=842)
    pg.insert_image(pg.rect, pixmap=pix)
    out = str(tmp_path / "flat.pdf")
    img.save(out)
    sc = score_output(d, out)
    assert not sc.checked and "no text" in sc.error
    assert summarize("t", [sc]).checked == 0


def test_load_external_prefers_the_redacted_pdf_over_review_copies(tmp_path):
    import json
    from legal_redact.bench.h2h import load_external
    r = tmp_path / "results.json"
    r.write_text(json.dumps({
        "a": {"outputs": [str(tmp_path / "a_redactions_for_review.pdf"), str(tmp_path / "a_redacted.pdf")]},
        "b": {"outputs": []}}))
    got = load_external(r)
    assert got["a"].name == "a_redacted.pdf" and got["b"] is None


def test_category_restricted_summary_ignores_other_categories(tmp_path):
    from collections import Counter
    from legal_redact.bench.blackbox import BBScore
    s = BBScore("x", leaked=Counter({"ID": 2}), total_gold=Counter({"ID": 2, "PERSON": 1}))
    assert summarize("t", [s]).doc_leak_rate == 1.0
    assert summarize("t", [s], categories={"PERSON"}).doc_leak_rate == 0.0
