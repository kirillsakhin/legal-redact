"""Fail-safe input inspection.

Principle: if we cannot be sure we can remove everything, say so BEFORE the
lawyer spends time reviewing. Each check emits a Finding with a stable code.
Messages never include document text.

REFUSE  -> export blocked (we cannot guarantee anything useful).
REVIEW  -> export possible only after the lawyer acknowledges the risk.

OCR changes the boundary, not the principle: a page with no usable text layer is
no longer refused when a local Tesseract is available, but it becomes a REVIEW
finding that says OCR will be used, plus one that says what OCR cannot see
(handwriting, signatures, photos) and that OCR misreads mean a name it cannot
read cannot be redacted. `ocr=False` (CLI --no-ocr) restores the old refusal.
"""
from __future__ import annotations

import pymupdf

from .models import Finding, Severity
from .ocr import (OSD_MIN_CONFIDENCE, needs_ocr, ocr_available, ocr_orientation,
                  ocr_version)

# An image covering more than this share of a page is treated as "page content
# may live in pixels" (scan, screenshot, signature block photo).
LARGE_IMAGE_SHARE = 0.25


def preflight(doc: pymupdf.Document, ocr: bool = True) -> list[Finding]:
    """Inspect the document. `ocr=False` forces the old fail-closed scan refusal."""
    out: list[Finding] = []
    add = lambda c, s, m, p=None: out.append(Finding(c, s, m, p))  # noqa: E731

    if doc.needs_pass or doc.is_encrypted:
        add("PF001", Severity.REFUSE, "Document is encrypted or password-protected.")
        return out  # nothing else can be inspected reliably

    if doc.page_count == 0:
        add("PF002", Severity.REFUSE, "Document has no pages.")
        return out

    # Pages with no usable text layer: likely scans. The OCR capability decides
    # whether they are a REVIEW (we can read them) or the old REFUSE.
    ocr_usable = bool(ocr) and ocr_available()
    no_text = [i for i in range(doc.page_count) if needs_ocr(doc[i])]
    if no_text:
        engine = ocr_version() if ocr_usable else None
        if ocr_usable:
            if len(no_text) == doc.page_count:
                add("PF003", Severity.REVIEW,
                    "No text layer on any page (scanned or image-only). OCR will be used.")
            else:
                for i in no_text:
                    add("PF004", Severity.REVIEW,
                        "Page has no usable text layer; OCR will be used on this page.", i)
            add("PF015", Severity.INFO,
                f"OCR will be used on {len(no_text)} page(s) with the local "
                f"{engine or 'Tesseract'} engine (English). OCR can misread text, and a "
                "name it cannot read cannot be redacted: check each OCR page against its "
                "image. Rasterised pages are written as images and are not searchable.")
            add("PF016", Severity.REVIEW,
                f"{len(no_text)} page(s) will be rebuilt as images. Handwriting, "
                "signatures, stamps and photographs on them are not detected or "
                "redacted by OCR; check those yourself.")
            # Orientation: every page that will be OCR'd is checked with Tesseract
            # OSD. A rotated page is corrected (boxes mapped back) and surfaced as
            # a REVIEW; an undeterminable orientation is a REVIEW too, never a
            # silent guess. This is the only OSD pass in pre-flight and it costs
            # about 0.4 s per scanned page.
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
            why = "disabled for this run (--no-ocr)" if not ocr else "not available"
            if len(no_text) == doc.page_count:
                add("PF003", Severity.REFUSE,
                    "No extractable text on any page (scanned or image-only). "
                    f"OCR is not supported: it is {why}.")
            else:
                for i in no_text:
                    add("PF004", Severity.REVIEW,
                        "Page has no usable text layer; OCR is "
                        f"{why}, so text inside images on this page cannot be redacted.", i)
    ocr_pages = set(no_text) if ocr_usable else set()

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
