"""Turn an engine's OcrResult into the pipeline's PageText model."""
from __future__ import annotations

import pymupdf

from ..models import PageText, Rect, Word
from .base import OcrEngine, OcrResult
from .tesseract import TesseractEngine


def page_text_from_result(page: pymupdf.Page, result: OcrResult) -> PageText:
    """OcrResult (normalized boxes) -> PageText (PDF-point boxes, char offsets).

    The contract has no line grouping: each word gets its own line id so a
    decision's rectangle is the union of exactly the words it covers (never a
    whole-page block because two distant words shared a line id).
    """
    width, height = page.rect.width, page.rect.height
    parts: list[str] = []
    words: list[Word] = []
    offset = 0
    for i, word in enumerate(result.words):
        if i:
            parts.append(" ")
            offset += 1
        start = offset
        parts.append(word.text)
        offset += len(word.text)
        x0, y0, x1, y1 = word.box
        rect = Rect(x0 * width, y0 * height, x1 * width, y1 * height)
        words.append(Word(word.text, rect, i, start, offset, confidence=word.confidence))
    return PageText(page.number, width, height, "".join(parts), words, ocr=True)


def page_text_with_engine(page: pymupdf.Page, engine: OcrEngine,
                          dpi: int = 300) -> PageText:
    """Read one page with `engine` into the pipeline's PageText model.

    Tesseract gets its measured fast path (render once inside the pipeline);
    every other engine receives the page as a PNG at `dpi` plus its size in PDF
    points, and its validated word boxes are mapped into the page frame.
    """
    if isinstance(engine, TesseractEngine):
        from ..ocr import ocr_page
        return ocr_page(page, dpi=dpi)
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    png = pix.tobytes("png")
    result = engine.analyze(png, page.rect.width, page.rect.height, dpi)
    return page_text_from_result(page, result)
