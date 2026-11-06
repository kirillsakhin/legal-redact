# Changelog

## 0.2.0 (2026-11-06)

Quality, independent evidence and scanned-PDF support.

- Collateral damage: three guards in the ensemble — a date of birth is only proposed with birth
  evidence (keyword on the date's line or nearby), a person name adjacent to a company designator is
  the firm, and a bare number next to a currency token is an amount rather than an ID. On the spent
  v4 suite (diagnostic only) protected text lost went from 4.5% to 0.0%; on the dev corpus from 4.5%
  to 0.0%; PERSON recall unchanged.
- Review workload: `strong_confidence` for a lone PERSON detector lowered to 0.85 (Presidio's usual
  clean-name score), and a PERSON box fully inside an ADDRESS box is dropped as redundant. Queue per
  document on the spent v4 suite: 0.90 -> 0.59 mean (median 1 -> 0).
- Speed: one batched GLiNER inference per document instead of per view, plus a within-run detection
  cache. Ensemble detection 60 -> 40 ms/doc (1.5x) with 20/20 documents producing identical
  suggestion spans, categories and support (max confidence delta 6e-6).
- New independent-data benchmark "holdout v5": 100 one-page documents built on third-party CUAD v1
  contract text (CC BY 4.0, provenance in `src/legal_redact/bench/INDEP_NOTICE.md`) with PII injected
  by a separate faker-seeded generator, hash-pinned by `INDEP_SHA256`, and a new
  `bench-h2h --suite indep` path. On it, where the tool released a file it left no readable personal
  string and damaged none of the protected decoys (0 of 60); the blind accept-all flow withheld 40 of
  100 files rather than release an unverified export. The best other tool leaked in 8% of documents
  and destroyed 31.6% of protected decoys; doc_redaction's batch output leaked in 100%.
- OCR for scanned pages (English, local, offline; Tesseract is an external program, no new Python
  dependency): pages with no or almost no text layer are rendered in greyscale at 300 dpi and read
  into the same word/offset/box model; pre-flight changes PF003/PF004 from REFUSE to REVIEW when OCR
  is available and adds PF015/PF016 about what OCR cannot read, keeping the old refusal with
  `--no-ocr` or without Tesseract. Tesseract's orientation detector runs on every page to be OCR'd:
  a sideways or upside-down page is re-read upright with every box mapped back to the original page
  (PF017 REVIEW), and an undeterminable orientation is PF018 REVIEW — never a silent mis-read.
  Scanned pages are re-rasterised at 300 dpi with the boxes burnt into the pixels and rebuilt as
  image-only pages (mixed documents keep text pages as text, in order, at the same page size). The
  verifier gains raster-page checks: no text layer (V13), boxes black in the extracted page image
  (V14), OCR of the output cannot recover a redacted string (V15), kept content still readable
  (V16). Seeded tests: an unredacted scan and under-covering boxes are refused, a clean scan passes.
- OCR backend evaluated against 16 synthetic fixtures and one alternative engine; the measured
  comparison and the decision are in `docs/OCR_EVALUATION.md`. Adopted: greyscale rendering (same
  accuracy, 0.60 -> 0.47 s/page, 26 -> 8.7 MB/page) and Tesseract OSD orientation handling (rotated
  pages 0/9 -> 9/9 planted values). Rejected with numbers: contrast stretching, adaptive second
  pass, RapidOCR (5-6x slower, +160 MB, unusable boxes on rotated pages), cloud/VLM OCR by design.
  Tests: 21 new (13 OCR + 8 orientation; 151 total).
- Docs: README, `docs/BENCHMARKS.md` (independent-data section, OCR section, v4 marked historical),
  `docs/OCR_EVALUATION.md` (new), `docs/THREAT_MODEL.md` (T14-T18 for OCR) and CONTRIBUTING updated.

## 0.0.1 (initial)

First version; developed on synthetic documents only.

- PDF text and word-coordinate extraction; pre-flight that refuses encrypted files, scans,
  attachments and XFA forms and flags images, annotations, forms, scripts and layers.
- Detectors: rules (with IBAN and Luhn checksums), Presidio, GLiNER, and an ensemble with checksum
  checks, conflict resolution and name bridging.
- Policy files and a review gate that blocks export until every suggestion is decided.
- Local review page (accept, reject, draw boxes, acknowledge findings, export).
- Redaction writer (PyMuPDF) that removes content and scrubs metadata, annotations, attachments,
  scripts, bookmarks and hidden text.
- Independent export verifier (pdfminer.six, pypdfium2, pikepdf) and a seeded-leak suite.
- Synthetic benchmark with hash-pinned held-out suites; results in `docs/BENCHMARKS.md`.
- Head-to-head harness with a black-box output scorer, compared against doc_redaction 2.8.1.
- Head-to-head expanded to ltillmann/pdf-redactor (MIT), JoshData/pdf-redactor (CC0) and a Presidio
  reference pipeline, all scored from their output files the same way; harnesses in `scripts/`. On
  the pinned suite the accept-all mode left 1% of documents leaking, against 49-100% for the others.
- Attempted the independent `pdfredeval` rendering-condition benchmark: the tool fails closed (no
  output) because those pages are outside its declared scope. Documented in `docs/BENCHMARKS.md`.
- "Find repeats" in the review page and `--accept-repeats`: propose other occurrences of accepted text.
- Verifier fix: a name wrapped over two lines is one needle, not one per line (it refused correct exports).
