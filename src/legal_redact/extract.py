"""Stage 1: text + coordinate extraction.

Uses PyMuPDF word boxes. We rebuild the page text ourselves (words joined by a
space inside a line, a newline between lines) so that every character offset
maps back to a word and therefore to a rectangle. We deliberately do NOT use
page.get_text("text"): its offsets cannot be mapped back to geometry.

Known limits (documented, not hidden):
- A name split by a line break or hyphenation is two words on two lines; the
  detector sees "Smi-\\nth". Phase 3 adds a de-hyphenated detection view.
- Text drawn as images or vector outlines is invisible here (see preflight).
"""
from __future__ import annotations

import pymupdf

from .models import PageText, Rect, Word


def extract_pages(doc: pymupdf.Document) -> list[PageText]:
    pages: list[PageText] = []
    for pno in range(doc.page_count):
        page = doc[pno]
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
