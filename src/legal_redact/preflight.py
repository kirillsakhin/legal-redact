"""Fail-safe input inspection.

Principle: if we cannot be sure we can remove everything, say so BEFORE the
lawyer spends time reviewing. Each check emits a Finding with a stable code.
Messages never include document text.

REFUSE  -> export blocked (we cannot guarantee anything useful).
REVIEW  -> export possible only after the lawyer acknowledges the risk.

OCR changes the boundary, not the principle: a page with no usable text layer is
no longer refused when an OCR engine is available, but it becomes a REVIEW
finding that says OCR will be used, plus one that says what OCR cannot see
(handwriting, signatures, photos) and that OCR misreads mean a name it cannot
read cannot be redacted. `ocr=False` (CLI --no-ocr) restores the old refusal.

Engine choice (docs/OCR_BACKENDS.md) adds its own gates:
- a non-local (cloud/third-party) engine is REFUSED in pre-flight unless the run
  was approved, and its verbatim disclaimer is a REVIEW finding (PF019) that must
  be acknowledged before export;
- an unavailable/invalid configured engine is REFUSED (PF020);
- without Tesseract there is no independent cross-check of a custom engine
  (PF021 REVIEW) and no OSD orientation check (PF023 REVIEW).
"""
from __future__ import annotations

import pymupdf

from .models import Finding, Severity
from .ocr import OSD_MIN_CONFIDENCE, needs_ocr, ocr_available, ocr_orientation, ocr_version
from .ocr_backends import (CLOUD_OCR_DISCLAIMER, DEFAULT_ENGINE, EngineConfig,
                           EngineConfigError, default_config)

# An image covering more than this share of a page is treated as "page content
# may live in pixels" (scan, screenshot, signature block photo).
LARGE_IMAGE_SHARE = 0.25


def _engine_summary(config: EngineConfig, tesseract_ok: bool) -> tuple[str, str | None, bool, str | None]:
    """(name, version, local, unavailability reason) for the configured engine."""
    if config.spec == DEFAULT_ENGINE:
        return "tesseract", (ocr_version() if tesseract_ok else None), True, (
            None if tesseract_ok else "Tesseract with the 'eng' language data was not found")
    try:
        engine = config.engine()
        name = engine.name
        version = engine.version()
        available = engine.available()
    except EngineConfigError as exc:
        return "command", None, config.local, str(exc)
    reason = None if available else "the configured engine command cannot be found"
    return name, version, config.local, reason


def preflight(doc: pymupdf.Document, ocr: bool = True,
              engine_config: EngineConfig | None = None) -> list[Finding]:
    """Inspect the document. `ocr=False` forces the old fail-closed scan refusal."""
    out: list[Finding] = []
    add = lambda c, s, m, p=None: out.append(Finding(c, s, m, p))  # noqa: E731

    if doc.needs_pass or doc.is_encrypted:
        add("PF001", Severity.REFUSE, "Document is encrypted or password-protected.")
        return out  # nothing else can be inspected reliably

    if doc.page_count == 0:
        add("PF002", Severity.REFUSE, "Document has no pages.")
        return out

    config = engine_config or default_config()
    tesseract_ok = ocr_available()
    engine_name, engine_version, engine_local, engine_reason = _engine_summary(config, tesseract_ok)
    engine_ok = engine_reason is None
    cloud_blocked = (not engine_local) and (not config.allow_cloud)

    # Pages with no usable text layer: likely scans. The OCR engine decides
    # whether they are a REVIEW (we can read them) or a REFUSE.
    no_text = [i for i in range(doc.page_count) if needs_ocr(doc[i])]
    if no_text:
        if not ocr:
            why = "disabled for this run (--no-ocr)"
            if len(no_text) == doc.page_count:
                add("PF003", Severity.REFUSE,
                    "No extractable text on any page (scanned or image-only). "
                    f"OCR is not supported: it is {why}.")
            else:
                for i in no_text:
                    add("PF004", Severity.REVIEW,
                        "Page has no usable text layer; OCR is "
                        f"{why}, so text inside images on this page cannot be redacted.", i)
        elif cloud_blocked:
            add("PF020", Severity.REFUSE,
                "A non-local (cloud/third-party) OCR engine is selected but not approved for "
                "this run. Passing page images to an external OCR service can compromise the "
                "document. Re-run with --allow-cloud-ocr (or LEGAL_REDACT_ALLOW_CLOUD_OCR=1) "
                "if your firm has approved the service and the transfer is lawful; see "
                "docs/OCR_BACKENDS.md. No page was rendered or sent.")
        elif not engine_ok:
            if config.spec == DEFAULT_ENGINE:
                if len(no_text) == doc.page_count:
                    add("PF003", Severity.REFUSE,
                        "No extractable text on any page (scanned or image-only). "
                        "OCR is not supported: it is not available.")
                else:
                    for i in no_text:
                        add("PF004", Severity.REVIEW,
                            "Page has no usable text layer; OCR is not available, so text "
                            "inside images on this page cannot be redacted.", i)
            else:
                add("PF020", Severity.REFUSE,
                    f"The configured OCR engine is unavailable or invalid ({engine_reason}). "
                    "Fix --ocr-engine/--ocr-command (see docs/OCR_BACKENDS.md); nothing was "
                    "rendered or sent.")
        else:
            if len(no_text) == doc.page_count:
                add("PF003", Severity.REVIEW,
                    "No text layer on any page (scanned or image-only). OCR will be used.")
            else:
                for i in no_text:
                    add("PF004", Severity.REVIEW,
                        "Page has no usable text layer; OCR will be used on this page.", i)
            add("PF015", Severity.INFO,
                f"OCR will be used on {len(no_text)} page(s) with the "
                f"{'local' if engine_local else 'NON-LOCAL (third-party)'} engine "
                f"{engine_name} ({engine_version or 'version unknown'}). OCR can misread "
                "text, and a name it cannot read cannot be redacted: check each OCR page "
                "against its image. Rasterised pages are written as images and are not "
                "searchable.")
            add("PF016", Severity.REVIEW,
                f"{len(no_text)} page(s) will be rebuilt as images. Handwriting, "
                "signatures, stamps and photographs on them are not detected or "
                "redacted by OCR; check those yourself.")
            if not engine_local:
                # The per-file half of the cloud gate: export stays blocked until the
                # lawyer acknowledges the verbatim disclaimer. The engine-level gate
                # (--allow-cloud-ocr) has already been enforced before any OCR call.
                add("PF019", Severity.REVIEW, CLOUD_OCR_DISCLAIMER)
            if config.spec != DEFAULT_ENGINE and not tesseract_ok:
                add("PF021", Severity.REVIEW,
                    "Tesseract is not installed, so the export verification cannot use an "
                    "independent OCR reader: the same custom engine will check its own "
                    "output. That cross-check is weaker than the default. Check the "
                    "verification report yourself.")
            # Orientation: Tesseract OSD is the only local orientation check and it is
            # used even when a custom engine does the OCR. Without Tesseract an
            # orientation can only be checked by the engine's own boxes, so the pages
            # become an explicit REVIEW instead of a silent guess.
            if tesseract_ok:
                rotated, unknown = [], []
                for i in no_text:
                    angle, confidence = ocr_orientation(doc[i])
                    if angle and confidence >= OSD_MIN_CONFIDENCE:
                        rotated.append(i + 1)
                    elif confidence < OSD_MIN_CONFIDENCE:
                        unknown.append(i + 1)
                if rotated:
                    add("PF017", Severity.REVIEW,
                        f"{len(rotated)} page(s) appear rotated; OCR will correct the "
                        "orientation and map the boxes back to the page. Check the "
                        "corrected pages.")
                if unknown:
                    add("PF018", Severity.REVIEW,
                        f"Orientation of {len(unknown)} scanned page(s) could not be "
                        "determined reliably; OCR may misread them. Check those page "
                        "images yourself.")
            else:
                add("PF023", Severity.REVIEW,
                    f"Orientation of {len(no_text)} scanned page(s) could not be "
                    "machine-checked because Tesseract OSD is not installed. The configured "
                    "engine must return word boxes already in the upright page frame; if a "
                    "page is rotated, OCR may misread it. Check those page images yourself.")
    ocr_pages = set(no_text) if (ocr and engine_ok and not cloud_blocked) else set()

    for i in range(doc.page_count):
        page = doc[i]
        if i not in ocr_pages:
            # Pages that are NOT OCR'd keep the generic image warnings: their
            # pixels are not read. OCR'd pages are covered by PF015/PF016.
            area = page.rect.width * page.rect.height
            for img in page.get_images(full=True):
                for r in page.get_image_rects(img[0]):
                    if area and (r.width * r.height) / area >= LARGE_IMAGE_SHARE:
                        add("PF005", Severity.REVIEW,
                            "Large image on page; text or signatures inside images are not detected.", i)
                        break
                else:
                    add("PF006", Severity.REVIEW,
                        "Embedded image present; contents are not inspected.", i)
        annots = [a for a in page.annots() or []]
        if annots:
            add("PF007", Severity.REVIEW,
                f"{len(annots)} annotation(s) present; comments and form values can hold personal data.", i)
        if any(w for w in page.widgets() or []):
            add("PF008", Severity.REVIEW, "Form fields present.", i)

    if doc.embfile_count():
        add("PF009", Severity.REFUSE, "Embedded file attachments present.")

    cat = doc.pdf_catalog()
    cat_str = doc.xref_object(cat, compressed=False) if cat else ""
    if "/XFA" in cat_str:
        add("PF010", Severity.REFUSE, "XFA form detected; not supported.")
    if "/JavaScript" in cat_str or "/JS" in cat_str:
        add("PF011", Severity.REVIEW, "Document-level JavaScript present.")
    if "/OCProperties" in cat_str:
        add("PF012", Severity.REVIEW, "Optional content layers present; hidden layers may hold text.")

    meta = {k: v for k, v in (doc.metadata or {}).items()
            if v and k not in ("format", "encryption")}
    if meta:
        add("PF013", Severity.INFO,
            f"Metadata fields present: {', '.join(sorted(meta))}. They will be stripped on export.")
    if doc.get_toc():
        add("PF014", Severity.REVIEW, "Bookmarks/outline present; titles may hold personal data.")
    return out


def blocked(findings: list[Finding]) -> bool:
    return any(f.severity is Severity.REFUSE for f in findings)
