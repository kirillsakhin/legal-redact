import json
import os

import pymupdf
import pytest

from legal_redact import pipeline
from legal_redact.bench import export_suite
from legal_redact.bench.synth import generate, render_pdf
from legal_redact.detect.rules import RulesDetector
from legal_redact.extract import extract_pages
from legal_redact.pipeline import ExportRefused, export, load_decisions, save_decisions, suggest
from legal_redact.policy import load_policy
from legal_redact.redact import rects_for_spans, write_redacted


@pytest.fixture
def src(tmp_path):
    d = generate("loan", 2)
    p = tmp_path / "in.pdf"
    p.write_bytes(render_pdf(d))
    return str(p), d


def test_writer_removes_text_keeps_other_text_and_refuses_overwrite(src, tmp_path):
    path, d = src
    pages = extract_pages(pymupdf.open(path))
    g = next(x for x in d.gold if x.category == "IBAN")
    boxes = rects_for_spans(pages, [(0, g.start, g.end)])
    out = str(tmp_path / "out.pdf")
    write_redacted(path, out, boxes)
    text = pymupdf.open(out)[0].get_text()
    assert d.text[g.start:g.end].replace(" ", "") not in text.replace(" ", "")
    assert "LOAN AGREEMENT" in text
    with pytest.raises(ValueError):
        write_redacted(path, path, boxes)


def test_verifier_passes_clean_exports_and_catches_every_seeded_leak():
    """Both halves: no false rejects on good output, no misses on leaky output."""
    res = export_suite.run(n_docs=1)
    assert res["clean_fail"] == []
    missed = {n: res["total"][n] - res["caught"][n] for n in export_suite.INJECTORS}
    assert not any(missed.values()), missed


def _accept_everything(session):
    for i in range(len(session.decisions)):
        session.accept(i)
    for f in session.findings:
        session.acknowledge(f.code)


def test_end_to_end_export_and_report_contains_no_document_text(src, tmp_path):
    path, d = src
    s = suggest(path, RulesDetector(names=False), load_policy("counterparty"))
    _accept_everything(s)
    out = str(tmp_path / "out.pdf")
    rep = export(path, out, s)
    assert rep.passed and os.path.exists(out)
    blob = json.dumps(rep.to_dict()) + json.dumps(s.report())
    dec = str(tmp_path / "dec.json")
    save_decisions(s, dec)
    blob += open(dec, encoding="utf-8").read()
    for g in d.gold:
        v = d.text[g.start:g.end]
        assert v not in blob, "document text leaked into a report or decision file"


def test_gate_blocks_export_while_suggestions_are_undecided(src, tmp_path):
    path, _ = src
    s = suggest(path, RulesDetector(names=False), load_policy("counterparty"))
    out = str(tmp_path / "out.pdf")
    with pytest.raises(ExportRefused):
        export(path, out, s)
    assert not os.path.exists(out)


def test_failed_verification_withholds_the_output(src, tmp_path, monkeypatch):
    path, _ = src
    s = suggest(path, RulesDetector(names=False), load_policy("counterparty"))
    _accept_everything(s)

    def cosmetic_only(src_, out_, boxes):           # a broken writer: draws boxes, removes nothing
        doc = pymupdf.open(src_)
        for p, r in boxes:
            doc[p].draw_rect(pymupdf.Rect(*r), fill=(0, 0, 0))
        doc.save(out_)
    monkeypatch.setattr(pipeline, "write_redacted", cosmetic_only)
    out = str(tmp_path / "out.pdf")
    with pytest.raises(ExportRefused) as e:
        export(path, out, s)
    assert e.value.report and not e.value.report.passed
    assert not os.path.exists(out)
    assert [f for f in os.listdir(tmp_path) if f.endswith(".pdf")] == ["in.pdf"]   # no temp leftovers


def test_decisions_roundtrip_recomputes_findings(src, tmp_path):
    path, _ = src
    s = suggest(path, RulesDetector(names=False), load_policy("counterparty"))
    dec = str(tmp_path / "d.json")
    save_decisions(s, dec)
    s2 = load_decisions(path, dec)
    assert len(s2.decisions) == len(s.decisions) and s2.decisions[0].rects


def test_ensemble_without_ml_extra_gives_a_clear_message_not_a_traceback(monkeypatch, capsys):
    import sys
    from legal_redact import cli
    monkeypatch.setitem(sys.modules, "gliner", None)      # makes `import gliner` raise ImportError
    assert cli._detector("ensemble") is None
    out = capsys.readouterr().out
    assert "ml" in out and "--detector rules" in out
    assert cli._detector("rules") is not None
