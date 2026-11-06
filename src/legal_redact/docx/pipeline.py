"""The .docx pipeline: pre-flight -> detect -> decide -> write -> verify -> release.

Same gate and same fail-closed release rule as the PDF pipeline (`..pipeline`):
the output only reaches its final path if the independent verifier finds no
leak; otherwise the temp file is removed (or quarantined on request) and the
caller gets the report. The review-gate object (`ReviewSession`) is shared, so
"every suggestion decided, every REVIEW finding acknowledged" means the same
thing for both formats.

Text containers come from the OOXML text model. Images are an additional set of
containers appended after the text ones: they are read with the configured OCR
engine (docs/OCR_BACKENDS.md) and their words carry pixel coordinates. Decision
files still store only container numbers and character offsets -- never document
text and never pixels -- so export re-reads the source (including image OCR)
and re-derives the needles. An image decision can therefore only be exported
with the same engine that produced it: the decision file pins the engine, and a
mismatch is refused rather than reinterpreted.

Engine pinning mirrors the PDF pipeline: suggest records which engine read the
images; export refuses when the current engine (or its declared locality)
differs, or when an image-redacting decision file has no engine record at all.
A non-local engine needs --allow-cloud-ocr on every invocation and its per-file
cloud finding must be acknowledged.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..detect.base import Detector
from ..models import Finding, Severity
from ..ocr_backends import (CloudOcrNotApproved, EngineConfig, EngineConfigError,
                            default_config)
from ..pipeline import ExportRefused          # the same user-facing refusal type
from ..policy import Decision, Policy, propose
from ..review import ReviewSession
from ..verify import VerifyReport
from . import images as image_tools
from .extract import extract_text, to_page_texts
from .package import DocxError, read_package
from .preflight import blocked, preflight_docx
from .verify import verify_docx
from .write import redact_docx

EXTRACTION_REFUSAL = "The document could not be read for detection; nothing was exported."
IMAGE_OCR_REFUSAL = ("The OCR engine could not re-read the document's images; text in pixels "
                     "cannot be redacted, so nothing was exported (fail closed).")


def _config(engine_config: EngineConfig | None) -> EngineConfig:
    return engine_config or default_config()


def _short_spec(spec: object, limit: int = 80) -> str:
    text = str(spec or "?")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pin(config: EngineConfig) -> dict:
    try:
        version = config.engine().version()
    except EngineConfigError:
        version = None
    return {"engine": config.spec, "version": version, "local": config.local}


def _engine_mismatch(recorded, config: EngineConfig, *, needed: bool) -> str | None:
    """Why this decision file cannot be exported with the current engine, if it cannot."""
    if not needed:
        return None
    if not isinstance(recorded, dict) or not str(recorded.get("engine") or "").strip():
        return ("this decision file redacts text inside images but has no OCR engine record; "
                "re-run `suggest` with the engine you want to export with "
                "(see docs/OCR_BACKENDS.md)")
    recorded_spec = str(recorded.get("engine")).strip()
    recorded_local = bool(recorded.get("local"))
    if recorded_spec != config.spec or recorded_local != config.local:
        return (f"this decision file was made with OCR engine {_short_spec(recorded_spec)} "
                f"({'local' if recorded_local else 'cloud/third-party'}), but this run uses "
                f"{_short_spec(config.spec)} ({'local' if config.local else 'cloud/third-party'}). "
                "Boxes from a different reader are not trusted; re-run `suggest` with the "
                "engine you want to export with (see docs/OCR_BACKENDS.md).")
    return None


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


def _coerce(decisions) -> list[tuple[int, int, int, Decision]]:
    out = []
    for decision in decisions:
        try:
            page, start, end = (int(decision.page), int(decision.start), int(decision.end))
        except (TypeError, ValueError):
            raise ExportRefused("a decision has malformed text offsets; output withheld") from None
        out.append((page, start, end, decision))
    return out


def suggest_docx(src: str, detector: Detector, policy: Policy,
                 ocr: bool = True, engine_config: EngineConfig | None = None) -> ReviewSession:
    """Pre-flight, then run the shared detectors on the text and image containers."""
    config = _config(engine_config)
    findings = preflight_docx(src, ocr=ocr, engine_config=config)
    decisions: list[Decision] = []
    ocr_info: dict | None = None
    if not blocked(findings):
        try:
            package = read_package(src)
            docx_text = extract_text(package)
            pages = to_page_texts(docx_text)
            if ocr and package.layout.media_parts:
                readings, errors = image_tools.read_images(package, config,
                                                           page_offset=len(pages))
                if errors:
                    findings = list(findings) + [Finding("PF128", Severity.REFUSE,
                                                         IMAGE_OCR_REFUSAL, None)]
                else:
                    pages = pages + [reading.page for reading in readings]
                    ocr_info = _pin(config)
            if not blocked(findings):
                decisions = propose(detector.detect(pages), policy)
        except DocxError as exc:
            findings = list(findings) + [Finding(exc.code, Severity.REFUSE,
                                                EXTRACTION_REFUSAL, None)]
    return ReviewSession(decisions, findings, ocr_info=ocr_info, policy=policy.name)


def load_decisions_docx(src: str, path: str, ocr: bool = True,
                        engine_config: EngineConfig | None = None) -> ReviewSession:
    """Load a decision file; findings and the engine pin are recomputed, never trusted."""
    config = _config(engine_config)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    decisions: list[Decision] = []
    for raw in data.get("decisions", []):
        raw = dict(raw)
        raw["rects"] = tuple(tuple(r) for r in raw.get("rects", ()))
        decisions.append(Decision(**raw))
    findings = preflight_docx(src, ocr=ocr, engine_config=config)
    recorded = data.get("ocr")
    try:
        package = read_package(src)
        text_count = len(extract_text(package).containers)
        images_present = bool(package.layout.media_parts)
    except DocxError:
        text_count, images_present = 0, False
    needs_images = bool(images_present and ocr and any(
        int(getattr(decision, "page", 0)) >= text_count for decision in decisions))
    mismatch = _engine_mismatch(recorded, config, needed=needs_images)
    if mismatch:
        findings = list(findings) + [Finding("PF022", Severity.REFUSE, mismatch, None)]
    session = ReviewSession(decisions, findings)
    session.ocr_info = recorded if isinstance(recorded, dict) else None
    session.policy = data.get("policy")
    session.acknowledged = set(data.get("acknowledged", []))
    return session


def export_docx(src: str, out: str, session: ReviewSession,
                keep_failed: bool = False, ocr: bool = True,
                engine_config: EngineConfig | None = None) -> VerifyReport:
    """Write, verify, and release only when the verifier passes."""
    src_path, out_path = Path(src).resolve(), Path(out).resolve()
    if src_path == out_path:
        raise ExportRefused("output must be a different file from the source")
    config = _config(engine_config)
    try:
        accepted = session.redactions()          # raises PermissionError while the gate is closed
    except PermissionError as exc:
        raise ExportRefused(str(exc)) from None
    if not accepted:
        raise ExportRefused("no redactions accepted; nothing to export")
    findings = preflight_docx(src, ocr=ocr, engine_config=config)   # recomputed
    if blocked(findings):
        raise ExportRefused("pre-flight refuses this file; nothing was written")
    try:
        package = read_package(src)
        docx_text = extract_text(package)
    except DocxError as exc:
        raise ExportRefused(EXTRACTION_REFUSAL) from exc
    containers = docx_text.containers
    text_count = len(containers)
    coerced = _coerce(accepted)
    images_present = bool(package.layout.media_parts)
    image_decisions = [item for item in coerced if item[0] >= text_count]
    if image_decisions and not ocr:
        raise ExportRefused("this decision file redacts text inside images, but image OCR "
                            "is disabled for this run (--no-ocr); nothing was written")
    readings: list = []
    if images_present and ocr:
        try:
            config.check_gate()
        except CloudOcrNotApproved as exc:
            raise ExportRefused(str(exc)) from None
        _name, _version, available, _error = image_tools.engine_summary(config)
        if image_decisions:
            mismatch = _engine_mismatch(session.ocr_info, config, needed=True)
            if mismatch:
                raise ExportRefused(mismatch)
            if not available:
                raise ExportRefused("this decision file redacts text inside images, but the "
                                    "configured OCR engine is unavailable; nothing was written")
        if available:
            # F3: re-read ALL source images at export, not only the ones with accepted
            # decisions; an accepted image passage must not stay readable in another image.
            readings, errors = image_tools.read_images(package, config,
                                                       page_offset=text_count)
            if errors:
                raise ExportRefused(IMAGE_OCR_REFUSAL)
            untrusted = image_tools.untrusted_word_boxes(readings)
            if untrusted:
                raise ExportRefused(untrusted)
    if image_decisions and not readings:
        raise ExportRefused("a decision points at an image container, but the source images "
                            "could not be re-read (was the file or the engine changed?)")
    # F3/R1 disclosure: tell the verifier which exported images were not fully
    # verified, so the report carries a WARN (V112) instead of an unqualified PASS.
    # Same tiny-text condition/constants as PF133 (images.tiny_text); zero-word images
    # are "unread", and when OCR was not used at all every image is "unused".
    source_names = sorted(package.layout.media_parts)
    index_of = {name: position + 1 for position, name in enumerate(source_names)}
    unverified_images: list[tuple[int, int, int, str]] = []
    if images_present:
        if readings:
            for reading in readings:
                reason = None
                if reading.words == 0:
                    reason = "unread"
                elif image_tools.tiny_text(reading):
                    reason = "tiny"
                if reason is not None:
                    unverified_images.append((index_of[reading.part.name],
                                              reading.part.width,
                                              reading.part.height, reason))
        else:
            decoded, decode_errors = image_tools.scan_images(package)
            if not decode_errors:
                unverified_images = [(index_of[part.name], part.width, part.height,
                                      "unused") for part in decoded]
    total_containers = text_count + len(readings)
    needles: list[tuple[str, str]] = []
    image_needles: list[tuple[int, str]] = []          # (container page, needle text)
    text_spans: list[tuple[int, int, int]] = []
    image_edits: dict[str, list[tuple[float, float, float, float]]] = {}
    for page, start, end, decision in coerced:
        if not (0 <= page < total_containers):
            raise ExportRefused("a decision points at a text container that no longer exists "
                                "(was the file changed after review?)")
        text = (containers[page].text if page < text_count
                else readings[page - text_count].page.text)
        if start < 0 or end > len(text) or end <= start:
            raise ExportRefused("a decision does not map to text in this document "
                                "(was the file changed after review?)")
        if page < text_count:
            text_spans.append((page, start, end))
        else:
            reading = readings[page - text_count]
            rects = reading.page.locate(start, end)
            if not rects:
                raise ExportRefused("a decision inside an image does not cover any recognised "
                                    "text (was the file or the OCR engine changed?)")
            target = image_edits.setdefault(reading.part.name, [])
            for rect in rects:
                target.append((rect.x0, rect.y0, rect.x1, rect.y1))
        needle = text[start:end]
        if needle.strip():
            needles.append((decision.category, needle))
            if page >= text_count:
                image_needles.append((page, needle))
    if not needles:
        raise ExportRefused("no accepted span maps to document text; nothing to export")
    # F3b: an accepted image passage must not remain readable in any OTHER source
    # image unless that other occurrence is itself covered by an accepted decision.
    for decision_page, needle in image_needles:
        others = [reading for reading in readings if reading.page.page != decision_page]
        for reading in others:
            if not image_tools.image_text_has_needle(reading.page.text, needle):
                continue
            covered = any(
                other_page == reading.page.page
                and image_tools.image_text_has_needle(other_needle, needle)
                for other_page, other_needle in image_needles)
            if not covered:
                raise ExportRefused(
                    "an accepted image redaction is still readable in another embedded "
                    "image; nothing was written (redact every image where that text "
                    "appears)")

    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=str(out_path.parent))
    os.close(fd)
    try:
        try:
            redact_docx(src, tmp, text_spans, image_edits=image_edits or None)
        except DocxError as exc:
            raise ExportRefused("the redacted file could not be written securely; "
                                "output withheld") from exc
        report = verify_docx(src, tmp, needles, image_edits=image_edits or None,
                             image_needles=[needle for _page, needle in image_needles] or None,
                             unverified_images=unverified_images or None,
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
