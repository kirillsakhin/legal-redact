"""End-to-end export: review gate -> write -> verify -> release or refuse.

The output only reaches its final path if verification finds no leak. If it does,
the file is deleted (or quarantined on request) and the caller gets the report.
Nothing in the report or in the decision file contains document text.

Scanned pages: extraction reads them with OCR when it is available (`ocr=False`
forces the old fail-closed refusal). Export re-runs extraction because the
decision file intentionally stores boxes, not document text: the OCR words under
those boxes are the needles the verifier searches for in the rebuilt image page.
If a page needs OCR and OCR is off or unavailable, export refuses before writing
anything -- it never writes a scan it could not redact.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

import pymupdf

from .detect.base import Detector
from .extract import extract_pages
from .ocr import needs_ocr
from .policy import Decision, Policy, propose
from .preflight import preflight
from .redact import write_redacted
from .repeats import find_repeats
from .review import ReviewSession
from .verify import VerifyReport, needles_from_groups, verify_export


class ExportRefused(Exception):
    def __init__(self, message: str, report: VerifyReport | None = None):
        super().__init__(message)
        self.report = report


def suggest(src: str, detector: Detector, policy: Policy, ocr: bool = True) -> ReviewSession:
    doc = pymupdf.open(src)
    try:
        findings = preflight(doc, ocr=ocr)
        decisions: list[Decision] = []
        if not any(f.severity.value == "refuse" for f in findings):
            pages = extract_pages(doc, ocr=ocr)
            decisions = propose(detector.detect(pages), policy)
            for d in decisions:
                d.rects = tuple((r.x0, r.y0, r.x1, r.y1) for r in pages[d.page].locate(d.start, d.end))
    finally:
        doc.close()
    return ReviewSession(decisions, findings)


def add_repeats(src: str, session: ReviewSession, accept: bool = False,
                ocr: bool = True) -> int:
    """Propose (or, if `accept`, accept) every other occurrence of each accepted text.
    Accepting automatically is an explicit bulk action: the CLI exposes it as a flag."""
    doc = pymupdf.open(src)
    try:
        new = find_repeats(session.decisions, extract_pages(doc, ocr=ocr))
    finally:
        doc.close()
    for d in new:
        if accept:
            d.status = "accepted"
        session.decisions.append(d)
    return len(new)


def save_decisions(session: ReviewSession, path: str) -> None:
    """Decision file: offsets, boxes, categories, reasons. No document text."""
    data = {"decisions": [asdict(d) for d in session.decisions],
            "acknowledged": sorted(session.acknowledged)}
    Path(path).write_text(json.dumps(data, indent=1), encoding="utf-8")


def load_decisions(src: str, path: str, ocr: bool = True) -> ReviewSession:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    decisions = []
    for d in data["decisions"]:
        d["rects"] = tuple(tuple(r) for r in d["rects"])
        decisions.append(Decision(**d))
    doc = pymupdf.open(src)
    try:
        findings = preflight(doc, ocr=ocr)   # findings are recomputed, never trusted
    finally:
        doc.close()
    s = ReviewSession(decisions, findings)
    s.acknowledged = set(data.get("acknowledged", []))
    return s


def _source_pages(src: str, ocr: bool):
    """Open the source once: pages (OCR'd where needed), page numbers needing OCR."""
    doc = pymupdf.open(src)
    try:
        needed = [i for i in range(doc.page_count) if needs_ocr(doc[i])]
        pages = extract_pages(doc, ocr=ocr)
    finally:
        doc.close()
    return pages, needed


def export(src: str, out: str, session: ReviewSession, keep_failed: bool = False,
           ocr: bool = True) -> VerifyReport:
    if Path(src).resolve() == Path(out).resolve():
        raise ExportRefused("output must be a different file from the source")
    try:
        accepted = session.redactions()          # raises PermissionError while the gate is closed
    except PermissionError as exc:
        raise ExportRefused(str(exc)) from None
    if not accepted:
        raise ExportRefused("no redactions accepted; nothing to export")
    pages, needs_ocr_pages = _source_pages(src, ocr)
    if needs_ocr_pages and not ocr:
        raise ExportRefused("page(s) without a usable text layer need OCR, which is disabled "
                            "for this run (--no-ocr); refusing to export a scan that cannot be redacted")
    unread = [i for i in needs_ocr_pages if not pages[i].ocr]
    if unread:
        raise ExportRefused("OCR is not available, so page(s) without a text layer cannot be "
                            "redacted; nothing was written (fail closed)")
    raster = {p.page for p in pages if p.ocr}
    boxes, groups = [], []
    for d in accepted:
        mine = [(d.page, r) for r in d.rects]
        boxes += mine
        groups.append((d.category, mine))
    if not boxes:
        raise ExportRefused("no redaction boxes to export")
    needles = needles_from_groups(src, groups, pages=pages, raster_pages=raster)
    raster_groups = [(cat, [(p, r) for p, r in bs if p in raster]) for cat, bs in groups]
    raster_needles = needles_from_groups(src, [(c, b) for c, b in raster_groups if b],
                                         pages=pages, raster_pages=raster)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=str(Path(out).resolve().parent))
    os.close(fd)
    try:
        if raster:
            write_redacted(src, tmp, boxes, raster_pages=raster)
        else:
            write_redacted(src, tmp, boxes)          # text-only documents keep the old writer call
        report = verify_export(src, tmp, boxes, needles, raster_pages=raster,
                               raster_needles=raster_needles, source_pages=pages)
        if not report.passed:
            if keep_failed:
                os.replace(tmp, str(out) + ".FAILED-VERIFICATION")
            raise ExportRefused("verification found a leak; output withheld", report)
        os.replace(tmp, out)
        return report
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
