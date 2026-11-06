"""Adopted OCR improvements: grayscale rendering, OSD orientation correction.

Evaluation table and the rejected alternatives are in docs/OCR_EVALUATION.md.
These tests pin the two adopted behaviours and mutation-check the orientation
guard: if OSD is disabled, a rotated scan must NOT look readable.
"""
import pymupdf
import pytest

from legal_redact import ocr as ocr_mod
from legal_redact.bench.synth import generate, render_pdf
from legal_redact.extract import extract_pages
from legal_redact.models import Severity
from legal_redact.ocr import OSD_MIN_CONFIDENCE, ocr_available, ocr_orientation
from legal_redact.preflight import blocked, preflight

pytestmark = pytest.mark.skipif(not ocr_available(), reason="Tesseract is not installed")

PAGE_W, PAGE_H = 595.0, 842.0
PLANTED = [
    "Oskar Kowalczyk", "4 January 1980", "D88065013",
    "82 Station Lane, Northvale, GF8 7TR", "GB53 PSCR 5001 9062 0132 78",
    "4513 7823 7732 4327", "Dmitri B. Sample", "F43343274", "+49 30 648823",
]


def _rotated_scan(tmp_path_factory, angle: int) -> str:
    """Build a scan whose content needs `angle` degrees of clockwise correction.

    PyMuPDF's page rotation is clockwise, so to create content that OSD will
    report as `angle`, the upright page is displayed rotated by 360-angle.
    A page needing 90 deg correction is the case Tesseract cannot read natively
    (the 270 deg case can be read as-is and is not a useful failure fixture).
    """
    doc = generate("loan", 2)
    src = pymupdf.open("pdf", render_pdf(doc))
    pix = src[0].get_pixmap(dpi=300)
    scratch = pymupdf.open()
    sp = scratch.new_page(width=src[0].rect.width, height=src[0].rect.height)
    sp.insert_image(sp.rect, pixmap=pix)
    sp.set_rotation((360 - angle) % 360)
    rotated = sp.get_pixmap(dpi=300)
    out = pymupdf.open()
    page = out.new_page(width=rotated.width * 72 / 300, height=rotated.height * 72 / 300)
    page.insert_image(page.rect, pixmap=rotated)
    path = tmp_path_factory.mktemp("ocr-rot") / f"scan_{angle}.pdf"
    out.save(path)
    out.close()
    scratch.close()
    src.close()
    return str(path)


def _flat_scan(tmp_path_factory) -> str:
    doc = generate("loan", 2)
    src = pymupdf.open("pdf", render_pdf(doc))
    pix = src[0].get_pixmap(dpi=300)
    out = pymupdf.open()
    page = out.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_image(page.rect, pixmap=pix)
    path = tmp_path_factory.mktemp("ocr-flat") / "scan.pdf"
    out.save(path)
    out.close()
    src.close()
    return str(path)


@pytest.fixture(scope="module")
def flat_scan(tmp_path_factory):
    return _flat_scan(tmp_path_factory)


@pytest.fixture(scope="module")
def rot90_scan(tmp_path_factory):
    return _rotated_scan(tmp_path_factory, 90)


def _planted_found(text: str) -> int:
    compact = text.replace(" ", "")
    return sum(1 for value in PLANTED
               if value in text or value.replace(" ", "") in compact)


def _transform_has_landed(truth_rect, mapped_rect, angle, w, h) -> bool:
    """Does a mapped box sit where the known text position rotates to?"""
    p0, q0 = truth_rect[0] / PAGE_W, truth_rect[1] / PAGE_H
    p1, q1 = truth_rect[2] / PAGE_W, truth_rect[3] / PAGE_H
    if angle == 90:
        expected = [q0 * w, (1 - p1) * h, q1 * w, (1 - p0) * h]
    elif angle == 180:
        expected = [(1 - p1) * w, (1 - q1) * h, (1 - p0) * w, (1 - q0) * h]
    elif angle == 270:
        expected = [(1 - q1) * w, p0 * h, (1 - q0) * w, p1 * h]
    else:
        expected = truth_rect
    ix = min(expected[2], mapped_rect[2]) - max(expected[0], mapped_rect[0])
    iy = min(expected[3], mapped_rect[3]) - max(expected[1], mapped_rect[1])
    if ix <= 0 or iy <= 0:
        return False
    inter = ix * iy
    area_m = (mapped_rect[2] - mapped_rect[0]) * (mapped_rect[3] - mapped_rect[1])
    return inter / area_m >= 0.5


def test_grayscale_read_recovers_all_planted_values(flat_scan):
    page = extract_pages(pymupdf.open(flat_scan), ocr=True)[0]
    assert page.ocr is True
    assert _planted_found(page.text) == len(PLANTED)
    for w in page.words:
        assert page.text[w.start:w.end] == w.text
        assert 0 <= w.rect.x0 < w.rect.x1 <= page.width
        assert 0 <= w.rect.y0 < w.rect.y1 <= page.height


@pytest.mark.parametrize("angle", [90, 180])
def test_osd_detects_rotation(tmp_path_factory, angle):
    path = _rotated_scan(tmp_path_factory, angle)
    page = pymupdf.open(path)[0]
    detected, confidence = ocr_orientation(page)
    assert detected == angle and confidence >= OSD_MIN_CONFIDENCE


def test_clean_page_osd_reports_upright_and_confident(flat_scan):
    detected, confidence = ocr_orientation(pymupdf.open(flat_scan)[0])
    assert detected == 0 and confidence >= OSD_MIN_CONFIDENCE


def test_rotated_scan_is_auto_corrected_and_boxes_map_back(rot90_scan):
    doc = pymupdf.open(rot90_scan)
    page = extract_pages(doc, ocr=True)[0]
    assert page.ocr is True
    assert _planted_found(page.text) == len(PLANTED)
    # Every word box must be inside the ORIGINAL (rotated) page, not the corrected image.
    assert all(0 <= w.rect.x0 < w.rect.x1 <= page.width for w in page.words)
    assert all(0 <= w.rect.y0 < w.rect.y1 <= page.height for w in page.words)
    # A known name's box must land where that name rotates to on the scan page.
    source = generate("loan", 2)
    gold = source.gold[0]                                   # first planted PERSON
    truth = extract_pages(pymupdf.open("pdf", render_pdf(source)))[0]
    truth_rect = truth.locate(gold.start, gold.end)[0]
    truth_rect = [truth_rect.x0, truth_rect.y0, truth_rect.x1, truth_rect.y1]
    name_tokens = set(source.text[gold.start:gold.end].split())
    words = [w for w in page.words if w.text in name_tokens]
    assert words, "corrected read did not produce the planted name"
    union = words[0].rect
    for w in words[1:]:
        union = union.union(w.rect)
    mapped = [union.x0, union.y0, union.x1, union.y1]
    assert _transform_has_landed(truth_rect, mapped, 90, page.width, page.height), mapped


def test_orientation_correction_is_load_bearing(rot90_scan, monkeypatch):
    """Mutation check: without OSD the same rotated scan is garbage (recall drops)."""
    monkeypatch.setattr(ocr_mod, "ocr_orientation", lambda page, dpi=200: (0, 0.0))
    page = extract_pages(pymupdf.open(rot90_scan), ocr=True)[0]
    assert _planted_found(page.text) <= 1       # baseline rotated read: 0/9
    # ...while the real path finds all of them (test above). The guard is load-bearing.


def test_end_to_end_rotated_scan_exports_and_verifies(rot90_scan, tmp_path):
    """The adopted path must survive the whole fail-closed pipeline, not just OCR."""
    from legal_redact.detect.rules import RulesDetector
    from legal_redact.pipeline import export, suggest
    from legal_redact.policy import load_policy

    session = suggest(rot90_scan, RulesDetector(names=False), load_policy("counterparty"))
    assert session.decisions
    for i in range(len(session.decisions)):
        session.accept(i)
    for finding in session.findings:
        session.acknowledge(finding.code)
    out = str(tmp_path / "rot90.redacted.pdf")
    report = export(rot90_scan, out, session)
    assert report.passed
    status = {c.id: c.status for c in report.checks}
    assert status["V15"] == "PASS"                 # OCR of the rebuilt image found nothing
    source = pymupdf.open(rot90_scan)
    result = pymupdf.open(out)
    try:
        assert result.page_count == source.page_count
        assert abs(result[0].rect.width - source[0].rect.width) < 0.5
        assert abs(result[0].rect.height - source[0].rect.height) < 0.5
        assert result[0].get_text().strip() == ""   # no text layer survived
        assert len(result[0].get_images(full=True)) == 1
    finally:
        source.close()
        result.close()


def test_preflight_flags_rotated_and_uncertain_orientation(rot90_scan, flat_scan, monkeypatch):
    findings = preflight(pymupdf.open(rot90_scan))
    codes = {f.code: f for f in findings}
    assert "PF017" in codes and codes["PF017"].severity is Severity.REVIEW
    assert not blocked(findings)
    # Clean page: orientation is known-upright, no orientation finding.
    clean_codes = {f.code for f in preflight(pymupdf.open(flat_scan))}
    assert "PF017" not in clean_codes and "PF018" not in clean_codes
    # Mutation: if OSD cannot decide, the page must become an explicit REVIEW,
    # never a silent guess.
    monkeypatch.setattr("legal_redact.preflight.ocr_orientation", lambda page, dpi=200: (0, 0.0))
    codes = {f.code: f for f in preflight(pymupdf.open(rot90_scan))}
    assert "PF018" in codes and codes["PF018"].severity is Severity.REVIEW
    assert "PF017" not in codes
