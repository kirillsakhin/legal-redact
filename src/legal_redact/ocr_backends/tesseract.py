"""The default engine: the measured local Tesseract pipeline.

This is a thin adapter around `legal_redact.ocr` so the default path keeps
exactly the behaviour that was measured and documented in
`docs/OCR_EVALUATION.md` (grayscale render, OSD orientation correction, TSV
word boxes mapped back to the page). Two entry points:

- `analyze()` implements the `OcrEngine` contract by rebuilding a one-page
  document from the supplied PNG and reusing `ocr.ocr_page_meta`, so a protocol
  caller sees the same words as the pipeline.
- `analyze_page()` is the fast path used by the pipeline itself: it works on the
  pymupdf page directly, avoiding a render/re-encode round trip.

Tesseract always reports `local: true`: it is an executable on this machine and
the package makes no network call of its own.
"""
from __future__ import annotations

import pymupdf

from .. import ocr as _ocr
from ..ocr import OcrError, OcrUnavailable
from .base import OcrResult, OcrWord


class TesseractEngine:
    name = "tesseract"

    def available(self) -> bool:
        return _ocr.ocr_available()

    def version(self) -> str | None:
        return _ocr.ocr_version()

    def local(self) -> bool:
        return True

    def analyze_page(self, page: pymupdf.Page,
                     dpi: int = _ocr.DEFAULT_DPI) -> OcrResult:
        """Run the measured pipeline on a pymupdf page."""
        page_text, meta = _ocr.ocr_page_meta(page, dpi=dpi)
        width, height = page.rect.width, page.rect.height
        words = tuple(
            OcrWord(w.text,
                    (w.rect.x0 / width, w.rect.y0 / height,
                     w.rect.x1 / width, w.rect.y1 / height),
                    w.confidence if w.confidence is not None else 1.0)
            for w in page_text.words
        )
        return OcrResult(words, self.name, self.version(), True,
                         int(meta.get("rotation", 0)),
                         {"mean_confidence": meta.get("mean_confidence"),
                          "words": meta.get("words")})

    def analyze(self, image_png: bytes, page_width_pt: float, page_height_pt: float,
                dpi: int) -> OcrResult:
        if not image_png:
            raise OcrError("OCR engine received an empty page image")
        if not self.available():
            raise OcrUnavailable("Tesseract with the 'eng' language data was not found")
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=page_width_pt, height=page_height_pt)
            page.insert_image(page.rect, stream=image_png)
            return self.analyze_page(page, dpi=dpi)
        finally:
            doc.close()
