"""Stage 4: actually remove content, writing a NEW file.

A black rectangle is not redaction. What this module does:
1. add a redaction annotation over every accepted box and APPLY it: PyMuPDF
   deletes the text (and image pixels) under the box from the page content;
2. scrub everything else that can carry personal data and is not page text:
   metadata, XMP, annotations, links, form values, attachments, JavaScript,
   bookmarks, thumbnails, hidden text;
3. save as a fresh, garbage-collected file (no incremental update, so no old
   revision of the page is left behind).

It never writes over the source. Whether it WORKED is not decided here: that is
verify.py's job, using different libraries.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

from .models import PageText


LEAD_PAD = 0.4   # points; reaches into the space before a word, not into the letter before it


def rects_for_spans(pages: list[PageText], spans) -> list[tuple[int, tuple]]:
    """spans: iterable of (page, start, end) -> [(page, (x0,y0,x1,y1)), ...]"""
    out = []
    for page, start, end in spans:
        for r in pages[page].locate(start, end):
            out.append((page, (r.x0, r.y0, r.x1, r.y1)))
    return out


def write_redacted(src: str | Path, out: str | Path, boxes: list[tuple[int, tuple]]) -> None:
    src, out = Path(src).resolve(), Path(out).resolve()
    if src == out:
        raise ValueError("refusing to overwrite the source file")
    doc = pymupdf.open(src)
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise ValueError("encrypted input")
        by_page: dict[int, list[tuple]] = {}
        for page, rect in boxes:
            by_page.setdefault(page, []).append(rect)
        for pno, rects in by_page.items():
            page = doc[pno]
            for r in rects:
                # MuPDF loses the advance of the FIRST glyph it removes in a run, so text
                # after a redaction slides left by that much (measured: exactly one glyph
                # width). Starting the removal on the preceding SPACE makes the lost advance
                # a space, and the kept text then lands flush against the box instead of
                # having its first letter swallowed by it. See LEAD_PAD and V12.
                page.add_redact_annot(pymupdf.Rect(r[0] - LEAD_PAD, r[1], r[2], r[3]),
                                      fill=(0, 0, 0))
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        doc.scrub(attached_files=True, clean_pages=True, embedded_files=True, hidden_text=True,
                  javascript=True, metadata=True, redactions=True, redact_images=2,
                  remove_links=True, reset_fields=True, reset_responses=True,
                  thumbnails=True, xml_metadata=True)
        doc.set_toc([])
        doc.save(out, garbage=4, clean=True, deflate=True, incremental=False, linear=False)
    finally:
        doc.close()
