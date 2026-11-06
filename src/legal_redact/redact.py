"""Stage 4: actually remove content, writing a NEW file.

Two kinds of page need two kinds of removal, and both are destructive:

1. TEXT pages (as before): add a redaction annotation over every accepted box
   and APPLY it, so PyMuPDF deletes the text (and image pixels) under the box
   from the page content.
2. RASTER pages (a scanned page that stage 1b read with OCR): there is no text
   layer to delete, so the page is re-rendered at high dpi and the boxes are
   burned into the PIXEL DATA itself (Pixmap.set_rect), then the page is rebuilt
   from that single burnt image. Drawing black rectangles over the original
   scan would redact nothing: the original pixels would still be in the file
   and could be recovered by removing the rectangles. The original page, its
   image, its text layer and its metadata are never copied to the output.

Mixed documents keep TEXT pages as text and rebuild only RASTER pages, in the
same order and at the same page size.

On top of both paths the same scrub removes everything else that can carry
personal data: metadata, XMP, annotations, links, form values, attachments,
JavaScript, bookmarks, thumbnails, hidden text. Saved as a fresh,
garbage-collected file (no incremental update, so no old revision survives).

It never writes over the source. Whether it WORKED is not decided here: that is
verify.py's job, using different libraries.
"""
from __future__ import annotations

import math
from pathlib import Path

import pymupdf

from .models import PageText

LEAD_PAD = 0.4   # points; reaches into the space before a word, not into the letter before it

# Padding around an OCR word box before it is burned into the pixels. The OCR
# box is the glyph bounding box; a small margin keeps antialiased edges and
# descenders inside the black area. Too much padding would eat neighbouring kept
# words, so it stays well under one space width (a space at 8-11 pt is ~2-3 pt).
RASTER_BOX_PAD = 1.2   # points per side

# 300 dpi is the same scale OCR read the page at, and is the usual scan quality:
# re-rendering at the source dpi would upscale an already lossy scan for nothing.
RASTER_DPI = 300


def rects_for_spans(pages: list[PageText], spans) -> list[tuple[int, tuple]]:
    """spans: iterable of (page, start, end) -> [(page, (x0,y0,x1,y1)), ...]"""
    out = []
    for page, start, end in spans:
        for r in pages[page].locate(start, end):
            out.append((page, (r.x0, r.y0, r.x1, r.y1)))
    return out


def _apply_text_redactions(page: pymupdf.Page, rects) -> None:
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


def _burn_boxes(page: pymupdf.Page, pix: pymupdf.Pixmap, rects) -> None:
    """Paint every box black INTO the pixmap samples.

    Pixel coordinates are derived from the rendered image's real size, not from
    the requested dpi, so rounded page dimensions cannot shift the box. Bounds
    are rounded outwards (floor the top-left, ceil the bottom-right) so no
    partially covered pixel of the original scan is left behind.
    """
    if not rects:
        return
    sx = pix.width / page.rect.width if page.rect.width else 1.0
    sy = pix.height / page.rect.height if page.rect.height else 1.0
    for r in rects:
        ix0 = math.floor((r[0] - RASTER_BOX_PAD - page.rect.x0) * sx)
        iy0 = math.floor((r[1] - RASTER_BOX_PAD - page.rect.y0) * sy)
        ix1 = math.ceil((r[2] + RASTER_BOX_PAD - page.rect.x0) * sx)
        iy1 = math.ceil((r[3] + RASTER_BOX_PAD - page.rect.y0) * sy)
        ix0, iy0 = max(0, ix0), max(0, iy0)
        ix1, iy1 = min(pix.width, ix1), min(pix.height, iy1)
        if ix1 <= ix0 or iy1 <= iy0:
            continue
        pix.set_rect(pymupdf.IRect(ix0, iy0, ix1, iy1), (0, 0, 0))


def _add_raster_page(out_doc: pymupdf.Document, page: pymupdf.Page, rects,
                     dpi: int) -> None:
    """Rebuild one page as a single image with the boxes burnt into its pixels."""
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    _burn_boxes(page, pix, rects)
    new = out_doc.new_page(width=page.rect.width, height=page.rect.height)
    # Lossless PNG: the verification reads back exactly the pixels the lawyer
    # will see, with no compression artefacts around blacked-out text.
    new.insert_image(new.rect, stream=pix.tobytes("png"))


def _scrub(doc: pymupdf.Document) -> None:
    doc.scrub(attached_files=True, clean_pages=True, embedded_files=True, hidden_text=True,
              javascript=True, metadata=True, redactions=True, redact_images=2,
              remove_links=True, reset_fields=True, reset_responses=True,
              thumbnails=True, xml_metadata=True)
    doc.set_toc([])


def _save(doc: pymupdf.Document, out: Path) -> None:
    doc.save(out, garbage=4, clean=True, deflate=True, incremental=False, linear=False)


def write_redacted(src: str | Path, out: str | Path, boxes: list[tuple[int, tuple]],
                   raster_pages=(), raster_dpi: int = RASTER_DPI) -> None:
    """Write `out`: boxes removed from text pages, burnt into raster pages.

    `raster_pages` is the set of page numbers stage 1b had to OCR; those pages
    are rebuilt as images. Passing no raster pages is the old text-only writer,
    byte-for-byte behaviour on text documents.
    """
    src, out = Path(src).resolve(), Path(out).resolve()
    if src == out:
        raise ValueError("refusing to overwrite the source file")
    raster = {int(p) for p in raster_pages}
    doc = pymupdf.open(src)
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise ValueError("encrypted input")
        by_page: dict[int, list[tuple]] = {}
        for page, rect in boxes:
            by_page.setdefault(page, []).append(rect)
        if not raster:
            for pno, rects in by_page.items():
                _apply_text_redactions(doc[pno], rects)
            _scrub(doc)
            _save(doc, out)
            return
        out_doc = pymupdf.open()
        try:
            for pno in range(doc.page_count):
                rects = by_page.get(pno, [])
                if pno in raster:
                    _add_raster_page(out_doc, doc[pno], rects, raster_dpi)
                else:
                    out_doc.insert_pdf(doc, from_page=pno, to_page=pno)
                    _apply_text_redactions(out_doc[-1], rects)
            _scrub(out_doc)
            _save(out_doc, out)
        finally:
            out_doc.close()
    finally:
        doc.close()
