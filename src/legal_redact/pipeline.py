"""End-to-end export: review gate -> write -> verify -> release or refuse.

The output only reaches its final path if verification finds no leak. If it does,
the file is deleted (or quarantined on request) and the caller gets the report.
Nothing in the report or in the decision file contains document text.
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
from .policy import Decision, Policy, propose
from .preflight import preflight
from .redact import write_redacted
from .review import ReviewSession
from .verify import VerifyReport, needles_from_source, verify_export


class ExportRefused(Exception):
    def __init__(self, message: str, report: VerifyReport | None = None):
        super().__init__(message)
        self.report = report


def suggest(src: str, detector: Detector, policy: Policy) -> ReviewSession:
    doc = pymupdf.open(src)
    findings = preflight(doc)
    decisions: list[Decision] = []
    if not any(f.severity.value == "refuse" for f in findings):
        pages = extract_pages(doc)
        decisions = propose(detector.detect(pages), policy)
        for d in decisions:
            d.rects = tuple((r.x0, r.y0, r.x1, r.y1) for r in pages[d.page].locate(d.start, d.end))
    return ReviewSession(decisions, findings)


def save_decisions(session: ReviewSession, path: str) -> None:
    """Decision file: offsets, boxes, categories, reasons. No document text."""
    data = {"decisions": [asdict(d) for d in session.decisions],
            "acknowledged": sorted(session.acknowledged)}
    Path(path).write_text(json.dumps(data, indent=1), encoding="utf-8")


def load_decisions(src: str, path: str) -> ReviewSession:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    decisions = []
    for d in data["decisions"]:
        d["rects"] = tuple(tuple(r) for r in d["rects"])
        decisions.append(Decision(**d))
    s = ReviewSession(decisions, preflight(pymupdf.open(src)))   # findings are recomputed, never trusted
    s.acknowledged = set(data.get("acknowledged", []))
    return s


def export(src: str, out: str, session: ReviewSession, keep_failed: bool = False) -> VerifyReport:
    if Path(src).resolve() == Path(out).resolve():
        raise ExportRefused("output must be a different file from the source")
    try:
        accepted = session.redactions()          # raises PermissionError while the gate is closed
    except PermissionError as exc:
        raise ExportRefused(str(exc)) from None
    boxes, cats = [], []
    for d in accepted:
        for r in d.rects:
            boxes.append((d.page, r))
            cats.append(d.category)
    if not boxes:
        raise ExportRefused("no redactions accepted; nothing to export")
    needles = needles_from_source(src, boxes, cats)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=str(Path(out).resolve().parent))
    os.close(fd)
    try:
        write_redacted(src, tmp, boxes)
        report = verify_export(src, tmp, boxes, needles)
        if not report.passed:
            if keep_failed:
                os.replace(tmp, str(out) + ".FAILED-VERIFICATION")
            raise ExportRefused("verification found a leak; output withheld", report)
        os.replace(tmp, out)
        return report
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
