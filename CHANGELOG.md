# Changelog

## 0.0.1 (unreleased)

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
