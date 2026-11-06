"""Stage 1: text + coordinate extraction.

Uses PyMuPDF word boxes. We rebuild the page text ourselves (words joined by a
space inside a line, a newline between lines) so that every character offset
maps back to a word and therefore to a rectangle. We deliberately do NOT use
page.get_text("text"): its offsets cannot be mapped back to geometry.

Pages with no usable text layer (scans, image-only pages) are read with OCR
instead when `ocr=True` and a local Tesseract is available; `ocr_page` builds
the same PageText model, so every consumer is unchanged. A page that already
has a text layer is never OCR'd: its real text stays authoritative. When OCR is
requested but unavailable, such pages keep their empty text (preflight refuses
them before this matters) -- extraction never invents text.

Known limits (documented, not hidden):
- A name split by a line break or hyphenation is two words on two lines; the
  detector sees "Smi-\\nth". Phase 3 adds a de-hyphenated detection view.
- Text drawn as images or vector outlines is invisible here unless it is on a
  page with no text layer and OCR is enabled (see preflight PF003/PF004/PF015).
"""
from __future__ import annotations

import pymupdf

from .models import PageText, Rect, Word
from .ocr import needs_ocr, ocr_available, ocr_page


def extract_pages(doc: pymupdf.Document, ocr: bool = False) -> list[PageText]:
    """Extract every page; OCR pages with no usable text layer when `ocr` is on."""
    ocr_usable = bool(ocr) and ocr_available()
    pages: list[PageText] = []
    for pno in range(doc.page_count):
        page = doc[pno]
        if ocr_usable and needs_ocr(page):
            pages.append(ocr_page(page))
            continue
        raw = page.get_text("words", sort=True)
        # raw item: x0, y0, x1, y1, word, block_no, line_no, word_no
        parts: list[str] = []
        words: list[Word] = []
        offset = 0
        line_ids: dict[tuple[int, int], int] = {}
        prev_key: tuple[int, int] | None = None
        for x0, y0, x1, y1, txt, b, ln, _ in raw:
            key = (b, ln)
            lid = line_ids.setdefault(key, len(line_ids))
            if prev_key is not None:
                sep = " " if key == prev_key else "\n"
                parts.append(sep)
                offset += 1
            start = offset
            parts.append(txt)
            offset += len(txt)
            words.append(Word(txt, Rect(x0, y0, x1, y1), lid, start, offset))
            prev_key = key
        pages.append(PageText(pno, page.rect.width, page.rect.height,
                              "".join(parts), words))
    return pages
