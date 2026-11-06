"""OCR path: scanned pages are read, redacted destructively, verified fail-closed.

Everything here is synthetic, like the rest of the suite. The scan fixture is a
synthetic contract page rendered to an image at 300 dpi and wrapped in an
image-only PDF: a stand-in for a scanned or photographed page with no text layer.

The seeded leaks are at the WRITER level, which is where this path can leak:
  - a writer that copies the scan without burning the boxes (leaks everything);
  - a writer whose boxes under-cover the words (half a name stays readable).
Both must be refused by the verifier; the clean scan must pass.
"""
import json
import os
import shutil

import pymupdf
import pytest

from legal_redact import pipeline
from legal_redact.bench.synth import generate, render_pdf
from legal_redact.cli import main
from legal_redact.detect.rules import RulesDetector
from legal_redact.extract import extract_pages
from legal_redact.models import Severity
from legal_redact.ocr import (MIN_TEXT_CHARS, needs_ocr, ocr_available, ocr_page,
                              ocr_version)
from legal_redact.pipeline import (ExportRefused, export, load_decisions,
                                   save_decisions, suggest)
from legal_redact.policy import load_policy
from legal_redact.preflight import blocked, preflight
from legal_redact.redact import write_redacted

pytestmark = pytest.mark.skipif(not ocr_available(), reason="Tesseract is not installed")

SCAN_DPI = 300


def make_scan(text_pdf_bytes: bytes, dpi: int = SCAN_DPI) -> bytes:
    """Render every page of a text PDF to an image and rebuild it as image-only."""
    src = pymupdf.open("pdf", text_pdf_bytes)
    out = pymupdf.open()
    try:
        for i in range(src.page_count):
            pix = src[i].get_pixmap(dpi=dpi)
            page = out.new_page(width=src[i].rect.width, height=src[i].rect.height)
            page.insert_image(page.rect, pixmap=pix)
        return out.tobytes()
    finally:
        src.close()
        out.close()


@pytest.fixture
def scan(tmp_path):
    doc = generate("loan", 2)
    path = tmp_path / "scan.pdf"
    path.write_bytes(make_scan(render_pdf(doc)))
    return str(path), doc


@pytest.fixture
def reviewed(scan):
    """A scan that has been through suggest -> every decision accepted -> findings acked."""
    path, doc = scan
    session = suggest(path, RulesDetector(names=False), load_policy("counterparty"))
    assert session.decisions
    for i in range(len(session.decisions)):
        session.accept(i)
    for f in session.findings:
        session.acknowledge(f.code)
    assert not session.blockers()
    return path, doc, session


# ---- capability and the model ----------------------------------------------
def test_ocr_capability_reports_the_engine():
    assert ocr_available()
    version = ocr_version()
    assert version and version.startswith("tesseract")


def test_ocr_page_rebuilds_the_text_extractor_word_model(scan):
    path, doc = scan
    pages = extract_pages(pymupdf.open(path), ocr=True)
    assert len(pages) == 1 and pages[0].ocr is True
    page = pages[0]
    # The OCR text is the same reading-order text, with honest offsets and boxes.
    for w in page.words:
        assert page.text[w.start:w.end] == w.text
    for gold in doc.gold:
        if gold.category == "PERSON":
            assert doc.text[gold.start:gold.end] in page.text
    assert "Repayments are collected from IBAN" in page.text
    # Geometry: the rectangle of a recognised word lands on actual ink on the page.
    word = next(w for w in page.words if w.text.startswith("GB"))
    assert 0 <= word.rect.x0 < word.rect.x1 <= page.width
    assert 0 <= word.rect.y0 < word.rect.y1 <= page.height
    src = pymupdf.open(path)
    pix = src[0].get_pixmap(dpi=SCAN_DPI)
    scratch = pymupdf.open()
    sp = scratch.new_page(width=src[0].rect.width, height=src[0].rect.height)
    sp.insert_image(sp.rect, pixmap=pix)
    clip = pymupdf.Rect(word.rect.x0, word.rect.y0, word.rect.x1, word.rect.y1)
    sub = sp.get_pixmap(dpi=SCAN_DPI, clip=clip, colorspace=pymupdf.csGRAY)
    assert min(sub.samples) < 100, "OCR word box does not land on ink"
    src.close()
    scratch.close()


def test_text_pages_are_never_ocrd_and_almost_empty_pages_are(tmp_path):
    doc = pymupdf.open()
    with_text = doc.new_page()
    with_text.insert_text((72, 72), "This page has a real text layer of its own.", fontsize=11)
    almost_empty = doc.new_page()
    almost_empty.insert_text((72, 72), "Page 1", fontsize=11)      # < MIN_TEXT_CHARS
    path = str(tmp_path / "two.pdf")
    doc.save(path)
    pages = extract_pages(pymupdf.open(path), ocr=True)
    assert [p.ocr for p in pages] == [False, True]
    assert "real text layer" in pages[0].text
    assert len("Page 1") < MIN_TEXT_CHARS and needs_ocr(pymupdf.open(path)[1])


# ---- pre-flight ------------------------------------------------------------
def test_preflight_surfaces_ocr_use_and_its_limits(scan):
    path, _ = scan
    findings = preflight(pymupdf.open(path))
    by = {f.code: f for f in findings}
    assert by["PF003"].severity is Severity.REVIEW
    assert "OCR will be used" in by["PF003"].message
    assert by["PF015"].severity is Severity.INFO
    assert "cannot be redacted" in by["PF015"].message
    assert by["PF016"].severity is Severity.REVIEW
    assert not blocked(findings)
    for f in findings:                       # findings never contain document text
        assert "Oskar" not in f.message and "Kowalczyk" not in f.message


def test_no_tesseract_path_refuses_the_scan(scan, tmp_path, monkeypatch):
    path, _ = scan
    # Simulate a machine without Tesseract: the old fail-closed refusal must return.
    monkeypatch.setattr("legal_redact.preflight.ocr_available", lambda: False)
    monkeypatch.setattr("legal_redact.extract.ocr_available", lambda: False)
    findings = preflight(pymupdf.open(path))
    assert blocked(findings) and {f.code for f in findings} >= {"PF003"}
    pages = extract_pages(pymupdf.open(path), ocr=True)
    assert pages[0].text == "" and pages[0].ocr is False     # extraction never invents text
    session = suggest(path, RulesDetector(names=False), load_policy("counterparty"))
    assert not session.decisions                              # nothing to review on a refused scan
    out = str(tmp_path / "refused.pdf")
    with pytest.raises(ExportRefused):
        export(path, out, session)
    assert not os.path.exists(out)


def test_no_ocr_flag_keeps_the_old_refusal(scan, capsys):
    path, _ = scan
    assert main(["inspect", path, "--no-ocr"]) == 1
    text = capsys.readouterr().out
    assert "REFUSE" in text and "PF003" in text and "ocr_pages=0" in text


def test_cli_inspect_reports_capability_and_ocr_usage_without_text(scan, capsys):
    path, _ = scan
    assert main(["inspect", path]) == 0
    text = capsys.readouterr().out
    assert "ocr=available" in text and "ocr_pages=1" in text
    assert "PF003" in text and "PF015" in text
    assert "Oskar" not in text and "Kowalczyk" not in text and "example.test" not in text


# ---- writer + verifier -----------------------------------------------------
def test_end_to_end_scan_exports_clean_and_verifies(reviewed, tmp_path):
    path, doc, session = reviewed
    out = str(tmp_path / "scan.redacted.pdf")
    report = export(path, out, session)
    assert report.passed and os.path.exists(out)
    # Every raster-page check ran and passed (not silently skipped).
    status = {c.id: c.status for c in report.checks}
    assert {"V13", "V14", "V15", "V16"} <= set(status)
    assert all(status[cid] == "PASS" for cid in ("V13", "V14", "V15"))
    result = pymupdf.open(out)
    try:
        assert result.page_count == 1
        assert abs(result[0].rect.width - 595) < 1 and abs(result[0].rect.height - 842) < 1
        assert result[0].get_text().strip() == ""                   # no text layer survived
        assert len(result[0].get_images(full=True)) == 1            # single rebuilt page image
        meta = {k: v for k, v in (result.metadata or {}).items()
                if v and k not in ("format", "encryption")}
        assert not meta                                             # no metadata survived
    finally:
        result.close()
    blob = json.dumps(report.to_dict()) + json.dumps(session.report())
    for gold in doc.gold:                                           # no document text in reports
        value = doc.text[gold.start:gold.end]
        assert value not in blob, "document text leaked into a report"


def test_unredacted_scan_is_refused_by_the_ocr_leak_check(reviewed, tmp_path, monkeypatch):
    """A writer that removes nothing must be refused: V14 (pixels) and V15 (OCR) fail."""
    path, _, session = reviewed
    out = str(tmp_path / "leak.pdf")

    def remove_nothing(src_, out_, boxes, raster_pages=(), raster_dpi=300):
        shutil.copyfile(src_, out_)

    monkeypatch.setattr(pipeline, "write_redacted", remove_nothing)
    with pytest.raises(ExportRefused) as exc:
        export(path, out, session)
    report = exc.value.report
    assert report is not None and not report.passed
    failed = {c.id for c in report.failed()}
    assert {"V14", "V15"} <= failed, failed
    assert not os.path.exists(out)


def test_under_covering_boxes_are_refused(reviewed, tmp_path, monkeypatch):
    """A box that burns less than the word leaves original pixels: must be refused."""
    path, _, session = reviewed
    out = str(tmp_path / "undercut.pdf")

    def under_covering(src_, out_, boxes, raster_pages=(), raster_dpi=300):
        shrunk = [(p, (r[0] + 6, r[1] + 3, r[2] - 6, r[3] - 3)) for p, r in boxes]
        write_redacted(src_, out_, shrunk, raster_pages=raster_pages, raster_dpi=raster_dpi)

    monkeypatch.setattr(pipeline, "write_redacted", under_covering)
    with pytest.raises(ExportRefused) as exc:
        export(path, out, session)
    report = exc.value.report
    assert report is not None and not report.passed
    assert "V14" in {c.id for c in report.failed()}
    assert not os.path.exists(out)


def test_export_refuses_a_scan_when_ocr_is_turned_off_after_review(reviewed, tmp_path):
    path, _, session = reviewed
    out = str(tmp_path / "nocrop.pdf")
    with pytest.raises(ExportRefused) as exc:
        export(path, out, session, ocr=False)
    assert "OCR" in str(exc.value)
    assert not os.path.exists(out)


def test_decision_file_roundtrip_on_a_scan_still_exports(scan, tmp_path):
    path, _, = scan
    session = suggest(path, RulesDetector(names=False), load_policy("counterparty"))
    for i in range(len(session.decisions)):
        session.accept(i)
    for f in session.findings:
        session.acknowledge(f.code)
    decision_path = str(tmp_path / "dec.json")
    save_decisions(session, decision_path)
    with open(decision_path, encoding="utf-8") as fh:
        saved = fh.read()
    assert session.decisions[0].category in saved
    assert "Oskar" not in saved and "Kowalczyk" not in saved      # text-free decision file
    loaded = load_decisions(path, decision_path)
    out = str(tmp_path / "roundtrip.pdf")
    report = export(path, out, loaded)
    assert report.passed and os.path.exists(out)


# ---- mixed documents ------------------------------------------------------
def test_mixed_text_and_scan_pages_preserve_order_count_and_size(tmp_path):
    text_a = generate("services", 1)
    scan_doc = generate("loan", 2)
    text_b = generate("nda", 5)
    a = pymupdf.open("pdf", render_pdf(text_a))
    b = pymupdf.open("pdf", make_scan(render_pdf(scan_doc)))
    mixed = pymupdf.open()
    mixed.insert_pdf(a, from_page=0, to_page=0)                      # text page
    scanned = mixed.new_page(width=b[0].rect.width, height=b[0].rect.height)
    scanned.insert_image(scanned.rect, pixmap=b[0].get_pixmap(dpi=SCAN_DPI))
    mixed.insert_pdf(pymupdf.open("pdf", render_pdf(text_b)), from_page=0, to_page=0)
    src = str(tmp_path / "mixed.pdf")
    mixed.save(src)
    sizes = [(round(p.rect.width, 1), round(p.rect.height, 1)) for p in mixed]
    mixed.close()
    a.close()
    b.close()

    findings = preflight(pymupdf.open(src))
    codes = {f.code: f for f in findings}
    assert "PF003" not in codes and "PF004" in codes and codes["PF004"].page == 1
    assert not blocked(findings)

    session = suggest(src, RulesDetector(names=False), load_policy("counterparty"))
    pages = extract_pages(pymupdf.open(src), ocr=True)
    assert [p.ocr for p in pages] == [False, True, False]
    assert not pages[0].ocr and pages[0].text
    for i in range(len(session.decisions)):
        session.accept(i)
    for f in session.findings:
        session.acknowledge(f.code)
    out = str(tmp_path / "mixed.redacted.pdf")
    report = export(src, out, session)
    assert report.passed
    result = pymupdf.open(out)
    try:
        assert result.page_count == 3                                    # count preserved
        assert [(round(p.rect.width, 1), round(p.rect.height, 1)) for p in result] == sizes
        assert "SERVICES AGREEMENT" in result[0].get_text()              # text page stays text
        assert result[1].get_text().strip() == ""                        # scan page is image-only
        assert len(result[1].get_images(full=True)) == 1
        assert "NON-DISCLOSURE" in result[2].get_text()                  # order preserved
    finally:
        result.close()
