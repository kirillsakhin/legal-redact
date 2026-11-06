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

Engine pinning (docs/OCR_BACKENDS.md): suggest records which OCR engine read the
pages. Export refuses, with no output, when the current invocation's engine (or
its declared locality) differs, or when the decision file has no record at all
while the document has OCR pages: re-run suggest. A non-local engine additionally
requires --allow-cloud-ocr on every invocation, even if the decision file was
produced with it earlier, and the per-file cloud finding must be acknowledged.
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
from .models import Finding, Severity
from .ocr import OcrError, OcrUnavailable, needs_ocr
from .ocr_backends import EngineConfig, EngineConfigError, default_config
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


def _config(engine_config: EngineConfig | None) -> EngineConfig:
    return engine_config or default_config()


def _ocr_page_numbers(src: str) -> list[int]:
    doc = pymupdf.open(src)
    try:
        return [i for i in range(doc.page_count) if needs_ocr(doc[i])]
    finally:
        doc.close()


def _short_spec(spec: object, limit: int = 80) -> str:
    text = str(spec or "?")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _engine_label(pin: dict) -> str:
    spec = _short_spec(pin.get("engine"))
    locality = "local" if pin.get("local") else "cloud/third-party"
    return f"{spec} ({locality})"


def _engine_mismatch(recorded, config: EngineConfig, ocr_pages: list[int]) -> str | None:
    """Why this decision file cannot be exported with the current engine, if it cannot.

    The decision file pins the engine that read the pages; boxes produced under a
    different reader are not trusted, so the lawyer must re-run suggest rather
    than have the export silently reinterpret old decisions.
    """
    if not ocr_pages:
        return None
    if not isinstance(recorded, dict) or not str(recorded.get("engine") or "").strip():
        return ("this decision file has no OCR engine record and the document has pages "
                "without a text layer; re-run `suggest` with the engine you want to export "
                "with (see docs/OCR_BACKENDS.md)")
    recorded_spec = str(recorded.get("engine")).strip()
    recorded_local = bool(recorded.get("local"))
    if recorded_spec != config.spec or recorded_local != config.local:
        return (f"the decision file was made with OCR engine {_engine_label(recorded)} but "
                f"this run uses {config.spec} ({'local' if config.local else 'cloud/third-party'}"
                "); re-run `suggest` with the same engine (or export with the recorded one)")
    return None


def _pin(config: EngineConfig) -> dict:
    recorded = config.pinned()
    recorded["cloud"] = not config.local
    return recorded


def suggest(src: str, detector: Detector, policy: Policy, ocr: bool = True,
            engine_config: EngineConfig | None = None) -> ReviewSession:
    config = _config(engine_config)
    config.check_gate()          # before opening or rendering the document
    doc = pymupdf.open(src)
    try:
        findings = preflight(doc, ocr=ocr, engine_config=config)
        decisions: list[Decision] = []
        if not any(f.severity.value == "refuse" for f in findings):
            pages = extract_pages(doc, ocr=ocr, engine_config=config)
            decisions = propose(detector.detect(pages), policy)
            for d in decisions:
                d.rects = tuple((r.x0, r.y0, r.x1, r.y1) for r in pages[d.page].locate(d.start, d.end))
    finally:
        doc.close()
    return ReviewSession(decisions, findings, ocr_info=_pin(config) if ocr else None)


def add_repeats(src: str, session: ReviewSession, accept: bool = False,
                ocr: bool = True, engine_config: EngineConfig | None = None) -> int:
    """Propose (or, if `accept`, accept) every other occurrence of each accepted text.
    Accepting automatically is an explicit bulk action: the CLI exposes it as a flag."""
    config = _config(engine_config)
    config.check_gate()
    doc = pymupdf.open(src)
    try:
        new = find_repeats(session.decisions,
                           extract_pages(doc, ocr=ocr, engine_config=config))
    finally:
        doc.close()
    for d in new:
        if accept:
            d.status = "accepted"
        session.decisions.append(d)
    return len(new)


def save_decisions(session: ReviewSession, path: str) -> None:
    """Decision file: offsets, boxes, categories, reasons, engine pin. No document text."""
    data = {"decisions": [asdict(d) for d in session.decisions],
            "acknowledged": sorted(session.acknowledged)}
    if session.ocr_info is not None:
        data["ocr"] = dict(session.ocr_info)
    Path(path).write_text(json.dumps(data, indent=1), encoding="utf-8")


def load_decisions(src: str, path: str, ocr: bool = True,
                   engine_config: EngineConfig | None = None) -> ReviewSession:
    config = _config(engine_config)
    config.check_gate()          # a cloud engine needs approval on every invocation
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    decisions = []
    for d in data["decisions"]:
        d["rects"] = tuple(tuple(r) for r in d["rects"])
        decisions.append(Decision(**d))
    recorded = data.get("ocr")
    doc = pymupdf.open(src)
    try:
        findings = preflight(doc, ocr=ocr, engine_config=config)   # never trusted from the file
        ocr_pages = [i for i in range(doc.page_count) if needs_ocr(doc[i])] if ocr else []
    finally:
        doc.close()
    mismatch = _engine_mismatch(recorded, config, ocr_pages)
    if mismatch:
        findings = list(findings) + [Finding("PF022", Severity.REFUSE, mismatch)]
    s = ReviewSession(decisions, findings, ocr_info=recorded if isinstance(recorded, dict) else None)
    s.acknowledged = set(data.get("acknowledged", []))
    return s


def _source_pages(src: str, ocr: bool, config: EngineConfig):
    """Open the source once: pages (OCR'd where needed), page numbers needing OCR."""
    doc = pymupdf.open(src)
    try:
        needed = [i for i in range(doc.page_count) if needs_ocr(doc[i])]
        pages = extract_pages(doc, ocr=ocr, engine_config=config)
    finally:
        doc.close()
    return pages, needed


def export(src: str, out: str, session: ReviewSession, keep_failed: bool = False,
           ocr: bool = True, engine_config: EngineConfig | None = None) -> VerifyReport:
    config = _config(engine_config)
    config.check_gate()          # cloud approval is required on every invocation
    if Path(src).resolve() == Path(out).resolve():
        raise ExportRefused("output must be a different file from the source")
    # Engine pin first: a decision file produced by another OCR reader (or an
    # old file with no pin at all) must be re-run through suggest, not exported
    # with boxes the verifier would be searching for from a different reader.
    ocr_pages = _ocr_page_numbers(src) if ocr else []
    mismatch = _engine_mismatch(session.ocr_info, config, ocr_pages)
    if mismatch:
        # Refuse before the temporary output exists; nothing is written.
        raise ExportRefused(mismatch)
    try:
        accepted = session.redactions()          # raises PermissionError while the gate is closed
    except PermissionError as exc:
        raise ExportRefused(str(exc)) from None
    if not accepted:
        raise ExportRefused("no redactions accepted; nothing to export")
    try:
        pages, needs_ocr_pages = _source_pages(src, ocr, config)
    except (OcrError, OcrUnavailable, EngineConfigError) as exc:
        raise ExportRefused(
            f"the OCR engine failed while re-reading the source ({type(exc).__name__}: {exc}); "
            "nothing was written. Run the wrapper manually to diagnose (docs/OCR_BACKENDS.md)."
        ) from None
    if needs_ocr_pages and not ocr:
        raise ExportRefused("page(s) without a usable text layer need OCR, which is disabled "
                            "for this run (--no-ocr); refusing to export a scan that cannot be redacted")
    unread = [i for i in needs_ocr_pages if not pages[i].ocr]
    if unread:
        raise ExportRefused("the configured OCR engine is not available, so page(s) without "
                            "a text layer cannot be redacted; nothing was written (fail closed)")
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
                               raster_needles=raster_needles, source_pages=pages,
                               engine_config=config)
        if not report.passed:
            if keep_failed:
                os.replace(tmp, str(out) + ".FAILED-VERIFICATION")
            raise ExportRefused("verification found a leak; output withheld", report)
        os.replace(tmp, out)
        return report
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
