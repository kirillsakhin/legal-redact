# Changelog

## 0.4.0 (2026-11-06)

Pluggable OCR engines. The default stays the measured local Tesseract pipeline;
a validated external adapter lets a user plug in their own reader, including a
paid third-party service through a wrapper they write. Engine choice is a
compatibility feature, not an accuracy claim, and the fail-closed guarantees are
kept.

- New `ocr_backends/` package: an `OcrEngine` contract (`analyze(png, width_pt,
  height_pt, dpi) -> OcrResult` with normalized word boxes, confidence, engine
  name/version/local and rotation disclosure), the default `TesseractEngine`
  (wraps the measured 300 dpi grayscale + OSD pipeline unchanged), and a
  `CommandEngine` that runs a configured external wrapper once per page with no
  shell. Its JSON output is strictly validated (shape, finite numbers, boxes
  inside 0..1, confidence 0..1, non-empty text, required boolean `local`, word
  and payload caps); any invalid answer, crash or timeout is a refused page, not
  "no text".
- CLI/environment: `--ocr-engine`/`--ocr-command` on `inspect`, `suggest`,
  `redact` and `review`; `LEGAL_REDACT_OCR_ENGINE` fallback. `inspect`, the
  review page state and the export report show the engine name, version and
  locality. `--no-ocr` still disables OCR.
- Cloud/third-party OCR is doubly gated: a non-local engine is refused before
  any page is rendered unless `--allow-cloud-ocr` is passed for that invocation,
  and pre-flight emits a REVIEW finding (PF019) carrying the verbatim
  confidentiality disclaimer that must be acknowledged before export. The review
  page shows a warning banner and an explicit approval checkbox; the CLI prints
  the disclaimer on stderr. The local Tesseract default is unaffected.
- Decision files record the engine, version and locality. Export refuses, with
  no output, when the engine or declared locality changed since `suggest`, or
  when a document with OCR pages has a decision file without the pin: re-run
  `suggest`. A cloud-pinned file needs cloud approval again on the export
  invocation.
- Pre-flight adds: PF019 (cloud disclaimer, REVIEW), PF020 (engine
  unavailable/invalid or cloud not approved, REFUSE), PF021 (Tesseract missing:
  custom engine would check its own output, REVIEW), PF023 (Tesseract missing:
  orientation cannot be machine-checked, REVIEW). PF015 now names the engine and
  locality. PF017/PF018 orientation checks still run when Tesseract exists,
  whatever engine does the OCR.
- Verification: V15 prefers Tesseract as the independent output reader whenever
  it exists, even for custom-engine suggestions; without Tesseract it uses the
  configured engine and labels the check `SAME-ENGINE (weaker)` -- a leak still
  fails. V13/V14/V16 unchanged. The export report includes the engine and reader.
- Tests: 35 new (`tests/test_ocr_backends.py` + fake command-engine fixture),
  including the cloud gates (a marker proves no wrapper call happens before
  approval), invalid-output refusals, engine-mismatch refusal, the V15
  cross-check label, review-page cloud state, and a regression pin for the
  Tesseract backend's word boxes (234 tests total). Docs: `docs/OCR_BACKENDS.md`
  (contract, paid-engine and RapidOCR wrapper examples, cloud section),
  README warning, THREAT_MODEL T31-T36, TESTING.md custom-engine path.
- Measured on a new suite created for this change set (`ocr-v4-lease7`: 16 scan variants, 9 planted
  values, suite fingerprint `af724be5f27e5508...`; details in `docs/OCR_EVALUATION.md`): the
  refactored default Tesseract backend reproduced v0.3.2's reads on **16/16 variants** with identical
  words and boxes and the same planted-value recall (127/144 both), total OCR time 13.87 s -> 13.96 s
  (noise). The external-adapter path costs ~55 ms/page on top of the wrapper's own work (trivial
  wrapper 0.055 s/page; default fast path 0.40 s/page). No comparative accuracy claim is made for
  third-party engines.

## 0.3.2 (2026-11-06)


Review-page fix found during the first hands-on test of the scanned-PDF flow. No change to
detection, redaction or verification.

- Ticking two pre-flight acknowledgements (e.g. PF003 and PF016 on a scan) in quick succession
  could leave the page showing "not acknowledged" even though the server had saved both: the two
  state responses could arrive out of order and the older one overwrote the newer one. Actions now
  apply only the newest response, so the gate opens as soon as the last acknowledgement lands.
- A failed export attempt from before the acknowledgements ("Not exported: export blocked: ...")
  stayed on screen after the gate opened, reading as if export were still blocked. The stale
  failure message is now cleared when the gate becomes ready.

## 0.3.1 (2026-11-06)

Hands-on testing kit, no functional change to redaction or verification.

- `scripts/make_test_documents.py` writes synthetic fixtures with planted personal data and
  protected lookalikes: a text PDF, an image-only scan, a 90-degree-rotated scan, a `.docx` with a
  name split across runs plus a comment and metadata, a `.docx` with a tracked change (expected
  refusal) and a `.docx` with a full-page pasted image (expected refusal).
- `scripts/check_planted_values.py` checks a redacted output for those values (PDF text layer, OCR
  for rasterised pages, extracted text and raw bytes of every `.docx` package part) and reports
  protected lookalikes that disappeared.
- `docs/TESTING.md`: step-by-step hands-on test plan covering PDF, scans and orientation, `.docx`,
  the fail-closed refusal cases, regression checks, what to report and the known gaps. README links
  it; `testdocs/` is git-ignored.

## 0.3.0 (2026-11-06)

Word (`.docx`) support as the second input format, with the same review gate and fail-closed
independent verification as PDF. The PDF pipeline's behaviour is unchanged.

- New `docx/` package. Extraction: file-type detection; ZIP/OOXML package validation (duplicate
  names, encrypted entries, size limits, CRC, part-name vocabulary); text from `word/document.xml`,
  `header*.xml`, `footer*.xml`, `footnotes.xml`, `endnotes.xml`, `word/glossary/document.xml` and
  `w:txbxContent` text boxes inside any of them, with a stable per-run offset index (paragraph
  boundaries are newlines). Floating/anchored drawings are supported: only numeric text in the
  explicitly allowlisted drawing-layout elements (`wp:posOffset` under a position element,
  `wp14:pctWidth`/`pctHeight` under a size-relative element, plausible range) is treated as
  geometry — any other text outside `w:t` is refused. The shared detectors run on synthetic
  `PageText` objects; suggestion offsets map back to the `w:t` elements that held the characters.
  Decision files are the same JSON as for PDF (container numbers and offsets, no text).
- Pre-flight (`docx/preflight.py`, codes PF101-PF126): REFUSE on encrypted containers, legacy
  `.doc`, `.docm`/`.dotx`, macros/ActiveX, tracked changes, embedded OLE/package objects,
  `altChunk`/subdocuments, legacy form fields, parts outside the recognised set (including image/font
  extensions outside the allowlist), text outside the supported containers (including numeric text
  in unknown elements), malformed packages, text-carrying XML constructs (comments, processing
  instructions, DOCTYPE, CDATA outside text parts, hidden text in content-type/relationship
  attributes), images covering at least 25% of a page, and part/presentation names that read like a
  structured identifier. REVIEW on comments, hidden-text carriers, small images and custom XML.
  INFO on metadata scrubbing, what was read, and presentation-only text copied verbatim
  (style names, list-numbering format values, font family names). Findings never contain document
  text.
- Writer (`docx/write.py`, lxml): every character of an accepted span becomes U+2588 in its own run
  (with `xml:space="preserve"`), so a span crossing runs leaves no original character anywhere.
  Removes comments/comment marks/comment-author names, tracked-change residue, alternate
  text/tooltips/table captions, field instructions, hyperlinks (unwrapped to plain text, external
  relationships dropped), content-control metadata, custom XML, document variables, mail-merge and
  attached-template settings; rebuilds core/app property parts empty; drops custom properties,
  people.xml and page thumbnails; neutralises bookmark names; renames image and embedded-font parts
  to generated neutral names and rewrites relationships/content types; copies every other part
  byte-for-byte and preserves ZIP entry order/compression.
- Verifier (`docx/verify.py`) reads the output with stdlib `zipfile` + `xml.etree.ElementTree` +
  raw-byte scanning, never lxml, and withholds the file on any failure: redacted needles in text,
  attributes or raw part bytes (V102/V103); surviving comments/revisions/macros/embedded
  objects/external links/hidden carriers (V104); non-empty document properties (V105); changed
  paragraph/run counts, modified image/font content, source-chosen part names or edited/added/
  dropped parts that should be byte-identical (V106/V108); unresolved content types or relationship
  targets (V107). Every report lists the DOCX limits (images, presentation-only text, numeric layout
  values, decomposed Unicode, indirect identification, ...).
- CLI: `inspect`, `suggest` and `redact` accept `.docx` by extension/magic; `review` (the browser
  page) stays PDF-only with a clear message, and the docs say DOCX review is batch-style and weaker
  because the decision file carries no document text. Same gate: export blocked until every
  suggestion is decided and every REVIEW finding acknowledged (`--ack PF114` etc.);
  `--accept-proposed` works.
- Dependencies: `lxml` added to core; `python-docx` added to the `dev` extra for tests only (one
  test opens the output with it as a third-party reader; the package never imports it).
- Tests: 48 new (`tests/test_docx.py`, 199 total): plain document, name split across runs,
  header/footer/footnote, text boxes (inline, floating/anchored, nested, alternate-content copies),
  AutoText/glossary, comments stripped, metadata scrubbed, hidden carriers removed, small images
  flagged and copied unchanged with neutral names, large images refused, embedded fonts renamed,
  numeric hidden text refused except known layout values, PII in part names and font-family names
  refused, refusal cases for every REFUSE code, and mutation checks that disable the run replacement
  / leave a header copy / keep a comment part / keep metadata / keep source part names and show the
  verifier withholds the file. Plus CLI inspect/suggest/redact end-to-end and a
  python-docx-generated document accepted and redacted.
- Real Microsoft Word smoke test: a redacted fixture opened read-only in Word 16 without a repair
  prompt and, exported to PDF, rendered the U+2588 blocks with no recoverable planted PII.
- PF126 (presentation-only text: style/font/numbering names) is informational and does not block
  export; docs/DOCX.md states this and the README limitation does too.
- Docs: README supported formats, DOCX usage and limits (including batch-style review, image-pixel
  residual risk, presentation-only text and the Unicode-normalisation gap);
  `docs/THREAT_MODEL.md` T19-T30 for DOCX; new `docs/DOCX.md` design note (what is read, what is
  refused, how verification works, known gaps); CONTRIBUTING notes `python-docx` is test-only.

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
