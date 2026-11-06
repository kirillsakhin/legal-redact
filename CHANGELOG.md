# Changelog

## 0.6.0 (2026-11-06)

Compliance-support follow-up: a standalone DPIA/ROPA input extract and a DSAR/disclosure runbook.

- `redact --dpia PATH` (Markdown, or JSON for a `.json` path) writes a one-page DPIA/ROPA input
  extract from the same evidence record: files and SHA-256, matter/purpose/reviewer, data categories
  and volumes, policy, processing environment and OCR engine/locality, technical measures, verifier
  warnings and detector coverage gaps, reviewer attestations, and an explicit checklist for the
  controller to complete (legal basis, necessity/proportionality, retention, recipients/transfers,
  whether a DPIA is required, DPO advice, review date). It is clearly labelled as **not a DPIA** and
  contains no document text.
- New `docs/DSAR_WORKFLOW.md`: step-by-step disclosure/DSAR redaction workflow — inspect, choose the
  `dsar-disclosure` preset, review and attest, export with evidence/DPIA extracts, quality checks
  (`WARN V112` unread-image handling, second-person sampling), record-keeping and what not to do.
  Linked from README and COMPLIANCE.
- README/COMPLIANCE/TESTING updated; `--dpia` works for PDF and DOCX exports.
- Tests: 1 new (DPIA extract Markdown + JSON: headings/checklist/hashes/no document text). 323 tests
  green.

## 0.5.1 (2026-11-06)

Compliance-support quick wins: the evidence report becomes a per-matter workflow record, and the
review page makes the human steps explicit before export.

- Evidence report: optional per-matter context (`--controller`, `--matter`, `--purpose`,
  `--reviewer`), a reviewer-attestation section (with explicit NOT RECORDED when none was given),
  a residual-risk section (verifier WARN/FAIL checks, acknowledged pre-flight findings, and the
  categories the detectors do not cover: special-category data, indirect identifiers,
  handwriting/stamps), and a DPIA/ROPA inputs table. Still no document text, still not a
  compliance certificate.
- Reviewer attestations required before export from the review page (both PDF and DOCX): three
  statements (special-category data checked manually, indirect identifiers considered, authorised
  disclosure); the server enforces them and writes `<output>.evidence.md` automatically. The CLI
  records the same with `--attest` (unknown ids are refused).
- Positioning sharpened to "GDPR-aware, compliance-supporting — not a compliance certificate";
  README/COMPLIANCE/TESTING explain what the report does and does not prove.
- Tests: 4 new (review-page attestation gate + evidence auto-write for PDF and DOCX; CLI context/
  attestation recording; unknown-attestation refusal). 322 tests green.

## 0.5.0 (2026-11-06)

DOCX review page and DOCX image redaction. `legal-redact review file.docx` now opens the same local
review page as PDF: rendered through an installed Microsoft Word (Windows COM) or LibreOffice when
one is found, with detector suggestions drawn where the aligned text appears; a text review
otherwise. Text inside images embedded in a `.docx` is now read with the same OCR engine abstraction
as scanned PDFs, redacted in the pixels and re-verified. The `.docx` stays the source of truth — the
rendered PDF is a view, never the exported file.

- New `docx/render.py`: optional page renderer, detected and never installed. Microsoft Word via a
  PowerShell COM script (invisible, read-only open, macros force-disabled, link updates off,
  `ExportAsFixedFormat`, always closed/quit; a PID sweep kills only the Word instance the render
  started, so a user's open Word is never quit), preferred on Windows; LibreOffice
  (`soffice --headless --convert-to pdf`) with a unique throwaway user profile otherwise. 120 s
  timeout (`LEGAL_REDACT_RENDER_TIMEOUT`), temporary profile/output directories removed, output
  validated as a readable PDF. `inspect` prints `review_page=rendered (word|libreoffice)|text`.
- New `docx/align.py`: fail-closed token-level alignment between the .docx text model and the
  rendered PDF. Per-container monotone anchoring on tokens unique on both sides, local sequence
  matching between anchors, NFKC/casefold token equality and a neighbour-context requirement.
  Rendered mode is used only when ≥90% of the .docx text aligns and every suggestion maps cleanly;
  otherwise the page falls back to text review rather than draw a box that might be the wrong text.
  Manual boxes map back only when they fully contain whole words that are contiguous in one
  container and the span maps forward again; partial, mixed-part, non-contiguous, page-number-only
  and unmapped boxes are rejected with a clear message.
- New `docx/review.py`: `DocxReviewApp` implements the same HTTP surface as the PDF review app for
  the .docx pipeline (state, decide, acknowledge, bulk accept, manual boxes, page images, export)
  and `serve_docx` wires it to the existing token-authenticated local server. `review` on a `.docx`
  runs the normal pre-flight/suggest (or resumes a decision file), renders+aligns once, and
  explicitly falls back to text review when there is no renderer, rendering fails, or alignment is
  not trustworthy. Export from the page calls the existing `export_docx` writer + V101-V108 verifier
  and shows its report; the rendered PDF is never modified. Text review lists suggestions with short
  in-memory context excerpts (never written to decisions/reports/logs) plus accept/reject, bulk
  accept, acknowledgement and export controls; manual boxes require rendered mode.
- Review page HTML: mode banner, per-suggestion box list with page numbers (a .docx decision can
  appear on a different rendered page than its container index), text-mode context excerpts, hidden
  draw/repeats controls where unavailable. PDF review markup/behaviour unchanged (defaults keep the
  old path).
- CLI: `review` accepts `.docx` (dispatch to the docx backend); `inspect` reports the review-page
  mode and `images=`/`image_ocr=` for embedded images; help/docstrings updated. `--accept-repeats`
  remains PDF-only (noted/ignored for .docx); `--ocr-engine`/`--ocr-command`/`--ocr-local`/
  `--allow-cloud-ocr` now also select the reader for .docx images; `--accept-proposed`, `--ack`,
  `--port`, `--no-browser`, `--decisions` work for both.
- New `docx/images.py`: embedded images are decoded with PyMuPDF (no new dependency), bounded
  (pixels/bytes/frames), and read as extra text containers with the configured OCR engine at a
  300-dpi geometry convention (image pixels -> page points -> engine normalised boxes -> pixel word
  boxes). Without an engine, or with `--no-ocr`, no image text is read (small image REVIEW,
  page-sized REFUSE) but every image is still decoded and re-encoded. Unsupported/undecodable/
  oversized images are REFUSE PF127; an engine failure is REFUSE PF128; PF129 reports OCR counts
  and rotation reports without text; PF130 requires an acknowledgement when no independent output
  reader exists (Tesseract absent); a non-local engine keeps the double cloud gate (run approval +
  per-file PF019).
- Multi-frame refusal (audit F2/F5): animated GIF, APNG, animated WebP and multi-page TIFF are
  detected from the raw bytes **before** any decode (stdlib GIF image-descriptor parser, PNG `acTL`
  chunk, RIFF `ANIM`/`ANMF` + VP8X animation flag, TIFF page count) and REFUSED (PF127); a container
  whose frame count cannot be established is refused too. A single-frame GIF is accepted and
  normalised to PNG. This closes the leak where PyMuPDF decoded one frame, another frame kept the
  PII and the export passed.
- Unread images are fail-closed (audit F3): an image in which the engine finds no words is REFUSED
  when large/content-bearing (PF132) and needs an explicit acknowledgement when small (PF131); an
  image displayed below ~150 dpi is a REVIEW (PF133); engine word boxes that do not overlap actual
  ink in the source pixels refuse the file (PF128, pixel evidence independent of the engine).
  Export re-reads **all** source images and withholds the file if an accepted image passage is still
  readable in another image; V110 now re-reads every exported image, not only redacted ones.
- Canonical image pairing (audit F1): the writer renames image parts deterministically (sorted
  source order -> `word/media/redacted_N.png`) and the verifier rebuilds that exact source->output
  pairing from the source package instead of matching by pixel dimensions — two (or more) images of
  equal size now export PASS, while a count/name/tamper mismatch fails closed (V109).
- Metadata-free images everywhere (audit F4): **every** image in an export is re-encoded as
  lossless PNG with its pixels preserved and EXIF/XMP/ICC/DPI/text chunks stripped, not only images
  with accepted spans. An untouched image is therefore pixel-identical, not byte-identical; V111
  scans every output image for metadata and original bytes.
- Unread-image disclosure (audit round 2, R1): a small image the engine read no text in is REVIEW
  PF131 whose text now says its pixels were **NOT** checked by the export verifier and personal data
  may survive, and every export report carries a **WARN V112** naming each image that was not fully
  verified — text never read (or OCR not used at all) **or text too small to read reliably (the same
  PF133 tiny-text condition)** — with honest per-image wording; the same WARN row is in the
  `--evidence` report. The PASS stays possible but is visibly qualified.
  Very small text is a REVIEW PF133 even when the engine returned (misread) words: an image whose
  OCR word boxes are mostly under ~12 px at native resolution must be acknowledged before export,
  and its export report carries the V112 WARN.
  Ink evidence is now contrast/variance-based instead of absolute darkness: valid low-contrast glyphs
  count as ink (audit `I10` low-contrast text exports cleanly again), while uniform/blank boxes an
  engine hallucinated words over are still REFUSE PF128.
- Tests: 75 new. `tests/test_docx_review.py` (26) with a committed synthetic, Word-rendered
  `tests/fixtures/docx_review/contract.{docx,pdf}` so alignment runs on CI without Word or
  LibreOffice: renderer detection/labels, LibreOffice command/profile/timeout/failure handling,
  alignment mapping/word identity/reverse round-trip and fail-closed rejections, rendered HTTP
  round-trip with a mocked renderer (V101-V108 PASS, text-free API/decisions), manual-box mapping and
  export, text fallback (contexts in memory only; export works; unacknowledged findings block it),
  blocked pre-flight skipping the renderer, CLI dispatch/inspect, HTML mode markers.
  `tests/test_docx_images.py` (25) runs without Tesseract/Word/LibreOffice: the honest fake command
  engine drives suggesting/burning/re-verification, covering scan/decode and geometry, every refuse
  path, `--no-ocr`, pre-flight disclosure counts, image decisions and engine pinning, an end-to-end
  export whose pixels are black and whose original bytes are gone (V109-V111 PASS), untouched-image
  pixel identity, refusal without OCR / with another engine / without a pin, no-burn and
  leftover-EXIF mutations withheld, text and rendered review integration, cloud/same-engine
  acknowledgements, CLI plumb-through, and one real-Tesseract test skipped where Tesseract is absent.
  `tests/test_docx_image_hardening.py` (24) turns the independent-audit corpus findings F1-F5 and the
  round-2 residuals into regressions using the auditor's synthetic fixtures
  (`tests/fixtures/docx_audit/`): frame parsers and the four multi-frame `.docx` fixtures refused
  with no output, single-frame GIF accepted, zero-word large refused, zero-word small and low-res
  acknowledged, dishonest-engine ink evidence, cross-image leak refused at export and by V110, two
  same-size images paired canonically, count tamper failing V109, untouched metadata image re-encoded
  metadata-free with pixel equality, PNG tEXt/pHYs stripping, edited+untouched re-encode, the
  low-contrast `I10` fixture exporting PASS with contrast-based ink while uniform blanks still
  refuse, tiny-text `I22` going through PF133, the strengthened PF131 wording, V112 naming unread
  images (including with OCR not used), and a contrast-ink unit test. Full suite 322 passed with the
  existing PDF/OCR tests unchanged.
- GDPR positioning (with the compliance scope): purpose presets `contract-sharing`,
  `dsar-disclosure` and `publication` (schema-valid, every category stated, strictness ordering
  tested; each description says what it does not decide), a new `docs/COMPLIANCE.md` mapping
  GDPR-relevant redaction tasks to what the tool does and an explicit "not a compliance product"
  section (redaction is not anonymisation; no legal-basis, DPIA, records-of-processing or DSR
  workflow; special categories and indirect identifiers remain human calls), and a README
  positioning box with the correct way to describe the tool. `redact --evidence PATH` (Markdown, or
  JSON with a `.json` path) now writes a compliance evidence report after a successful export:
  source/redacted sizes and sha256, policy, decision counts by status and category, acknowledged
  pre-flight findings, OCR engine/version/locality, every verifier check and limit, and the explicit
  "not a compliance certificate" disclaimer — with no document text in the report. 4 new tests.
- Docs: `docs/DOCX.md` review-page section (rendered vs text, requirements, fail-closed alignment,
  manual boxes, renderer privacy/temp files, measured timings) and image section (OCR/geometry,
  refuse codes, writer burn, V109-V111, engine pinning, image review labels, limits/timings); README
  supported-formats row and pipeline table, DOCX usage/limitations/security, OCR install note;
  `docs/TESTING.md` Track C2 (review) and C3 (images) with forced-text/`--no-ocr`/engine-mismatch
  procedures; `docs/THREAT_MODEL.md` T37-T44 and the v0.5 scope. No version bump.

## 0.4.1 (2026-11-06)

Test-only fix for CI. One new test (	est_inspect_reports_default_engine) assumed a local
Tesseract, so the hosted runners -- which have none -- failed instead of exercising the
unavailable-engine path. The test now asserts the unavailable-engine refusal when Tesseract is
absent and the normal path when it is present; product behaviour is unchanged. 234 tests green on
all six CI legs.
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
