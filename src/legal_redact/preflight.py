"""Fail-safe input inspection.

Principle: if we cannot be sure we can remove everything, say so BEFORE the
lawyer spends time reviewing. Each check emits a Finding with a stable code.
Messages never include document text.

REFUSE  -> export blocked (we cannot guarantee anything useful).
REVIEW  -> export possible only after the lawyer acknowledges the risk.
"""
from __future__ import annotations

import pymupdf

from .models import Finding, Severity

# An image covering more than this share of a page is treated as "page content
# may live in pixels" (scan, screenshot, signature block photo).
LARGE_IMAGE_SHARE = 0.25


def preflight(doc: pymupdf.Document) -> list[Finding]:
    out: list[Finding] = []
    add = lambda c, s, m, p=None: out.append(Finding(c, s, m, p))  # noqa: E731

    if doc.needs_pass or doc.is_encrypted:
        add("PF001", Severity.REFUSE, "Document is encrypted or password-protected.")
        return out  # nothing else can be inspected reliably

    if doc.page_count == 0:
        add("PF002", Severity.REFUSE, "Document has no pages.")
        return out

    # Pages with no extractable text: likely scans. OCR is out of scope for v1.
    no_text = [i for i in range(doc.page_count)
               if not doc[i].get_text("text").strip()]
    if len(no_text) == doc.page_count:
        add("PF003", Severity.REFUSE,
            "No extractable text on any page (scanned or image-only). OCR is not supported.")
    else:
        for i in no_text:
            add("PF004", Severity.REVIEW,
                "Page has no extractable text; it may be an image of text.", i)

    for i in range(doc.page_count):
        page = doc[i]
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
