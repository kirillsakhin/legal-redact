"""The .docx pipeline: pre-flight -> detect -> decide -> write -> verify -> release.

Same gate and same fail-closed release rule as the PDF pipeline (`..pipeline`):
the output only reaches its final path if the independent verifier finds no
leak; otherwise the temp file is removed (or quarantined on request) and the
caller gets the report. The review-gate object (`ReviewSession`) is shared, so
"every suggestion decided, every REVIEW finding acknowledged" means the same
thing for both formats.

The decision file stores container numbers and character offsets, never
document text. Export re-reads the source and re-derives the needles from the
text under the accepted offsets, so a file edited between review and export
cannot silently redact the wrong passage: the span must still exist.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..detect.base import Detector
from ..models import Finding, Severity
from ..pipeline import ExportRefused          # the same user-facing refusal type
from ..policy import Decision, Policy, propose
from ..review import ReviewSession
from ..verify import VerifyReport
from .extract import extract_text, to_page_texts
from .package import DocxError, read_package
from .preflight import blocked, preflight_docx
from .verify import verify_docx
from .write import redact_docx

EXTRACTION_REFUSAL = "The document could not be read for detection; nothing was exported."


def summary_docx(src: str | Path) -> dict:
    """Counts only (no document text) for `inspect`."""
    package = read_package(src)
    docx_text = extract_text(package)
    return {
        "text_parts": len(package.layout.text_parts),
        "containers": len(docx_text.containers),
        "characters": sum(len(c.text) for c in docx_text.containers),
        "media_parts": len(package.layout.media_parts),
    }


def suggest_docx(src: str, detector: Detector, policy: Policy,
                 ocr: bool = True) -> ReviewSession:
    """Pre-flight, then run the shared detectors on the synthetic page texts.

    `ocr` is accepted so the CLI can call both pipelines the same way; it has no
    meaning for a .docx (no scanned pages, text inside images is never read).
    """
    findings = preflight_docx(src)
    decisions: list[Decision] = []
    if not blocked(findings):
        try:
            docx_text = extract_text(read_package(src))
        except DocxError as exc:
            findings = list(findings) + [Finding(exc.code, Severity.REFUSE,
                                                EXTRACTION_REFUSAL, None)]
        else:
            decisions = propose(detector.detect(to_page_texts(docx_text)), policy)
    return ReviewSession(decisions, findings)


def load_decisions_docx(src: str, path: str, ocr: bool = True) -> ReviewSession:
    """Load a decision file; findings are recomputed from the source, never trusted."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    decisions: list[Decision] = []
    for raw in data.get("decisions", []):
        raw = dict(raw)
        raw["rects"] = tuple(tuple(r) for r in raw.get("rects", ()))
        decisions.append(Decision(**raw))
    session = ReviewSession(decisions, preflight_docx(src))
    session.acknowledged = set(data.get("acknowledged", []))
    return session


def _validated_spans(accepted, containers) -> list[tuple[int, int, int]]:
    """Coerce decision offsets to ints and refuse anything that does not map to text."""
    spans: list[tuple[int, int, int]] = []
    for decision in accepted:
        try:
            page, start, end = int(decision.page), int(decision.start), int(decision.end)
        except (TypeError, ValueError):
            raise ExportRefused("a decision has malformed text offsets; output withheld") from None
        if not (0 <= page < len(containers)):
            raise ExportRefused("a decision points at a text container that no longer "
                                "exists (was the file changed after review?)")
        text = containers[page].text
        if start < 0 or end > len(text) or end <= start:
            raise ExportRefused("a decision does not map to text in this document "
                                "(was the file changed after review?)")
        spans.append((page, start, end))
    return spans


def _needles(accepted, containers) -> list[tuple[str, str]]:
    needles: list[tuple[str, str]] = []
    for decision in accepted:
        if not isinstance(decision.page, int) or not (0 <= decision.page < len(containers)):
            raise ExportRefused("a decision points at a text container that no longer "
                                "exists (was the file changed after review?)")
        text = containers[decision.page].text
        if not (0 <= decision.start < decision.end <= len(text)):
            raise ExportRefused("a decision does not map to text in this document "
                                "(was the file changed after review?)")
        needle = text[decision.start:decision.end]
        if needle.strip():
            needles.append((decision.category, needle))
    return needles


def export_docx(src: str, out: str, session: ReviewSession,
                keep_failed: bool = False, ocr: bool = True) -> VerifyReport:
    """Write, verify, and release only when the verifier passes."""
    src_path, out_path = Path(src).resolve(), Path(out).resolve()
    if src_path == out_path:
        raise ExportRefused("output must be a different file from the source")
    try:
        accepted = session.redactions()          # raises PermissionError while the gate is closed
    except PermissionError as exc:
        raise ExportRefused(str(exc)) from None
    if not accepted:
        raise ExportRefused("no redactions accepted; nothing to export")
    findings = preflight_docx(src)               # recomputed, never trusted from the file
    if blocked(findings):
        raise ExportRefused("pre-flight refuses this file; nothing was written")
    try:
        package = read_package(src)
        docx_text = extract_text(package)
    except DocxError as exc:
        raise ExportRefused(EXTRACTION_REFUSAL) from exc
    spans = _validated_spans(accepted, docx_text.containers)
    needles = _needles(accepted, docx_text.containers)
    if not needles:
        raise ExportRefused("no accepted span maps to document text; nothing to export")

    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=str(out_path.parent))
    os.close(fd)
    try:
        try:
            redact_docx(src, tmp, spans)
        except DocxError as exc:
            raise ExportRefused("the redacted file could not be written securely; "
                                "output withheld") from exc
        report = verify_docx(src, tmp, needles)
        if not report.passed:
            if keep_failed:
                os.replace(tmp, str(out) + ".FAILED-VERIFICATION")
            raise ExportRefused("verification found a leak; output withheld", report)
        os.replace(tmp, out)
        return report
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
