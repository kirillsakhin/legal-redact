# legal-redact

[![CI](https://github.com/kirillsakhin/legal-redact/actions/workflows/ci.yml/badge.svg)](https://github.com/kirillsakhin/legal-redact/actions/workflows/ci.yml)

Review-first redaction of personal data in PDF and Word (`.docx`) contracts, with independent
checks of the exported file. Local and offline by default.

> **Status: pre-alpha (0.6.1).** Developed and tested on synthetic documents and public contract
> text; not tested on real client documents. It does not make legal determinations, and removing
> personal data is not anonymisation or GDPR compliance.

> **GDPR-aware, compliance-supporting — not a compliance certificate.** The tool supports
> redaction tasks that arise under GDPR and similar regimes (data minimisation, disclosure/DSAR-style
> redaction) under human review, with independent verification and a workflow record. It does not
> determine legal bases, does not anonymise, does not comprehensively detect special-category or
> indirectly identifying data, and is not a DPIA, records-of-processing or DSR workflow. Read
> [docs/COMPLIANCE.md](docs/COMPLIANCE.md) before using it in a compliance process.

A lawyer opens a short English, text-based PDF or `.docx` contract, picks a disclosure purpose (a
policy), reviews what the tool suggests and why, and exports a separate redacted copy. The model
recommends; the lawyer decides. Drawing a black box over text is not redaction, so the export
removes the content and then checks the result with different libraries than the ones that wrote
it. Scanned PDF pages with no text layer are read with local English OCR into the same word model,
so the same review flow works; they are exported as re-rasterised images with the redaction burnt
into the pixels. `.docx` files are exported as `.docx`: the accepted characters are replaced in
the runs that held them, and comments, hidden text carriers and metadata are removed (see
[docs/DOCX.md](docs/DOCX.md)).

## How it works

| Stage | What happens | Module |
|---|---|---|
| 1. Extract | Text and word coordinates, so a text span maps back to boxes on the page. Pages with no usable text layer are OCR'd instead (same word/offset/box model). For `.docx`, embedded images are decoded and OCR'd into extra containers whose word boxes are in image pixels. | `extract.py`, `docx/images.py` |
| 1b. OCR | For pages with no (or almost no) text layer, the local Tesseract engine reads the page image (grayscale) into the same model; for `.docx`, embedded images are read with the same engine abstraction and boxes map back to image pixels. A PDF page that already has a text layer is never OCR'd. Pages rotated 90/180 degrees are detected with Tesseract OSD and re-read upright, with every box mapped back to the original page. | `ocr.py`, `docx/images.py` |
| 0. Pre-flight | Refuses encrypted files, attachments and XFA forms, and refuses scans when OCR is unavailable or disabled; otherwise flags OCR use and what OCR cannot see, plus images, annotations, forms, scripts, layers. | `preflight.py` |
| 2. Detect | Rules for structured identifiers (IBAN and card checksums), Presidio, and GLiNER, merged by an ensemble with conflict resolution. | `detect/` |
| 3. Decide | A policy (TOML) says redact, keep or review per category; the review gate blocks export until every suggestion has a decision. | `policy.py`, `review.py`, `review_server.py` |
| 4. Remove | Applies redactions (text under each box is deleted), scrubs metadata, annotations, attachments, scripts, bookmarks and hidden text, writes a fresh file. Scanned pages are re-rasterised at 300 dpi and the boxes are burnt into the pixel data, then rebuilt as image-only pages. Every `.docx` image is re-encoded as metadata-free PNG (pixels preserved; accepted image spans are painted black first) and renamed canonically. | `redact.py`, `docx/write.py` |
| 5. Verify | Re-reads the output with pdfminer.six, pypdfium2 and pikepdf; for rebuilt image pages it also OCRs the output, checks there is no text layer and that the boxes are black in the page image itself; for `.docx` it rebuilds the canonical image pairing, checks pixel dimensions/black areas/pixel equality, re-OCRs every exported image with the preferred independent reader, checks metadata and original bytes are gone, and reports a WARN (V112) naming any image whose text was never read **or was too small to read reliably**; withholds the file if a leak is found. | `verify.py`, `docx/verify.py` |

Reports and decision files contain categories, page numbers, boxes and counts, never document text.

For `.docx`, stages 0/1/4/5 run through the `docx/` package: extraction builds the same synthetic
page/offset model from `w:t` runs in the body, headers, footers, notes and text boxes, so the
detectors and the review gate are reused unchanged; text inside embedded images is read with the
same OCR engine abstraction (each image becomes an extra container whose words carry pixel
coordinates), and every image is re-encoded as metadata-free PNG with accepted spans painted black
and re-verified. The writer edits the OOXML with lxml and the verifier re-reads it with other stdlib
readers. The PDF pipeline's behaviour is untouched.

## Supported formats

| Format | Read | Export | Review page | Notes |
|---|---|---|---|---|
| PDF, text layer | yes | yes (`redact.py`) | yes | the original pipeline; unchanged in 0.3.0 |
| PDF, scanned | OCR (English) with local Tesseract, or a configured custom engine (docs/OCR_BACKENDS.md) | yes, re-rasterised image pages | yes | see `docs/OCR_EVALUATION.md` |
| `.docx` (OOXML Word) | yes (`docx/`), including headers/footers, notes, text boxes, AutoText and (with OCR) text inside embedded images | yes (`docx/write.py`), verified fail-closed | **yes** — rendered via Microsoft Word or LibreOffice; text review without a renderer | `.doc`, `.docm`, encrypted, tracked-changes and macro files are refused; multi-frame (animated GIF/APNG/WebP, multi-page TIFF) and unreadable/unsupported images are refused; unread large images are refused and unread small ones acknowledged; part names are neutralised. See [docs/DOCX.md](docs/DOCX.md) |

For `.docx` the transport is the same: pre-flight → shared detectors on the extracted text →
review page → export → independent verification. The review page renders the .docx through an
installed Word (Windows COM) or LibreOffice when one is found and draws each suggestion where the
aligned text appears; the rendered page is a view only. Without a renderer — or when a suggestion
cannot be placed on the rendered page with confidence — it switches to a text review listing each
suggestion with a short in-memory context excerpt. The `.docx` itself is always the source of
truth: export always runs the same writer and verifier, never the rendered PDF.

## Install

Requires Python 3.11 or later. Developed on 3.14; the same suite passes on 3.11, 3.12 and 3.13 on
both Windows and Linux, and CI runs and passes that matrix on every push.

```
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"    # core: rules detector, writer, verifier, review page
pytest
```

`lxml` is a core dependency (the DOCX writer/editor); `python-docx` is in the `dev` extra for
tests only — one test uses it as an independent third-party reader of the exported `.docx`. The
package itself never imports `python-docx`.

For the full ensemble (Presidio + GLiNER):

```
pip install -e ".[ml]"
python -m spacy download en_core_web_lg
```

The first run downloads model weights from Hugging Face; after that the models are cached locally.
The benchmarks ran on an RTX 5090 laptop GPU. CPU speed and speed on large files are untested.
The tool's own code makes no network calls; with the weights cached, the ensemble ran with `HF_HUB_OFFLINE=1` in the author's environment (one machine, one run).

OCR for scanned pages is an external program, not a Python dependency: install Tesseract 5.x with
the `eng` and `osd` language data (the `osd` data is used to detect and correct rotated pages) and
put it on PATH, or point `TESSERACT_CMD` at the executable. `legal-redact inspect` reports whether
it was found. Without it, pages with no text layer are refused exactly as before; text inside
images embedded in a `.docx` is also read with the same engine (Tesseract by default), and without
an engine those images are not read (the old fail-closed image rule applies; large unread images are
still refused because every image must be decodable for the metadata-free re-encode). OCR runs
locally and offline; its output is cached nowhere. The measured comparison of OCR configurations and
an alternative engine (RapidOCR) is in [docs/OCR_EVALUATION.md](docs/OCR_EVALUATION.md).

A different reader (a local model, or a paid third-party service through a wrapper you write) can be
plugged in with `--ocr-engine`/`--ocr-command`; the contract, examples and the two-gate cloud
procedure are in [docs/OCR_BACKENDS.md](docs/OCR_BACKENDS.md). Engine choice is a compatibility
option, not an accuracy claim.

> **Cloud/third-party OCR is opt-in and doubly gated.** Third-party (cloud) OCR is selected. Page
> images of this document (and, for a `.docx`, its embedded images) will be sent to an external OCR
> service configured by you, outside this
> machine. That processing can result in the document being compromised: third-party access,
> retention and logging, data location and onward transfers, interception, or a provider breach. Do
> not use cloud OCR for privileged, confidential or client documents unless your firm has approved
> the service, the transfer is lawful (for example a suitable data processing agreement is in place)
> and you are authorised to share the document. The local Tesseract default does not carry this
> risk. A non-local engine is refused unless `--allow-cloud-ocr` is passed for the run, and each
> document's `PF019` finding must be acknowledged before export.

## Use

Review page (recommended):

```
legal-redact review contract.pdf
```

Opens a local page showing each suggestion over the document with its category, confidence,
detector and reason. Accept, reject or draw your own box (`j`/`k` move, `a` accept, `r` reject,
`d` draw). "Find repeats" proposes every other occurrence of text you accepted; you still decide each. Export stays disabled until nothing is undecided and every pre-flight finding is
acknowledged, and the verification report is shown on the page.

Command line:

```
legal-redact inspect contract.pdf
legal-redact suggest contract.pdf -o decisions.json --policy counterparty
# set each decision's "status" to "accepted" or "rejected" in decisions.json
legal-redact redact contract.pdf --decisions decisions.json -o contract.redacted.pdf
```

Add `--evidence report.md` (or a `.json` path) to any `redact` command to write a compliance
evidence report after a successful export: sizes and SHA-256 of both files, policy, decision counts,
acknowledged findings, OCR engine/locality, every verifier check and its limits, a residual-risk
section (warnings, acknowledged findings, detector coverage gaps), a DPIA/ROPA inputs table, and the
reviewer attestations — no document text. Optional per-matter fields `--controller`, `--matter`,
`--purpose`, `--reviewer` and `--attest special_categories,indirect_identifiers,authorised` are
recorded in the report. The review page collects the attestations itself before export and writes
`<output>.evidence.md` automatically. A `--dpia report.md` flag writes a one-page DPIA/ROPA input
extract (with the controller's legal-basis, retention and sign-off fields left blank). See
[docs/COMPLIANCE.md](docs/COMPLIANCE.md) for what the reports do and do not prove, and
[docs/DSAR_WORKFLOW.md](docs/DSAR_WORKFLOW.md) for a step-by-step disclosure/DSAR workflow.

`.docx` uses the same commands; `inspect`, `suggest`, `redact` and `review` detect the format from
the file. The review page needs Microsoft Word (Windows) or LibreOffice to show the rendered layout;
without one it is a text review with the same accept/reject/ack/export controls:

```
legal-redact review contract.docx
# rendered mode: boxes over Word's layout, manual boxes accepted only when they map
# back to a contiguous passage; text mode: suggestions with short context excerpts
legal-redact inspect contract.docx
legal-redact suggest contract.docx -o decisions.json --policy counterparty
# edit statuses; acknowledge pre-flight REVIEW codes you accept, e.g. --ack PF114
legal-redact redact contract.docx --decisions decisions.json -o contract.redacted.docx --ack PF114
```

A `.docx` pre-flight that reports a REVIEW code (comments being stripped, hidden-text carriers, image
OCR use and what it may miss, an unread small image, **very small text** or an image displayed below
~150 dpi, a same-engine output check, cloud OCR, custom XML) blocks export until that code is
acknowledged with `--ack`. REFUSE findings
(encrypted, `.doc`/`.docm`, macros, tracked changes, embedded OLE objects, unknown parts, malformed
packages, multi-frame/unreadable/unsupported images, an unread large image, engine output that does
not match the pixels, a part/presentation name that reads like an identifier)
always block export. Images are read with the selected OCR engine (local Tesseract by default);
export re-reads every source image and withholds the file if an accepted image passage is still
readable in another image, every image is re-encoded as metadata-free PNG (pixels preserved; an
untouched image is pixel-identical, not byte-identical), and the verifier re-reads every exported
image. An unread or too-small-to-read image's pixels are never certified: the export report carries
a **WARN V112** naming each affected image ("text not read" or "text was very small ... pixels were
NOT verified for hidden text"), on top of the PF131/PF133 acknowledgement, and the same WARN row is
in the evidence report. With `--no-ocr` the
old rule applies: image text is not read, small images are acknowledged, page-sized images are
refused; every image is still re-encoded metadata-free and every image is named in V112. A decision
file that redacts image text records the engine that produced it, and export refuses a different
engine rather than reinterpret old offsets.
The rendered page never changes what is redacted — export writes and verifies the `.docx`, and a
mapping that cannot be trusted produces a text review instead of a possibly-misleading box. On this
machine a Word-rendered review session pays a one-off Word start-up (about 6 s for the test fixture);
the text review starts immediately.

Scanned pages are OCR'd automatically where needed. `--no-ocr` (on `inspect`, `suggest`, `redact`
and `review`) turns OCR off and restores the old fail-closed refusal of pages without a text layer.

A custom OCR engine is selected with `--ocr-engine`/`--ocr-command` on the same four commands
(`LEGAL_REDACT_OCR_ENGINE` is the environment fallback). A command engine is treated as a
third-party/cloud service unless declared local with `--ocr-local`; `--allow-cloud-ocr` is required
for a run that may send page images off the machine, and the per-file cloud finding must be
acknowledged. See [docs/OCR_BACKENDS.md](docs/OCR_BACKENDS.md) for the JSON contract and examples.

```
legal-redact inspect scan.pdf --ocr-engine "command:python my_ocr.py" --ocr-local
legal-redact suggest scan.pdf -o decisions.json --ocr-engine "command:python my_ocr.py" --ocr-local
```

Policies live in `src/legal_redact/policies/*.toml`. Purpose presets: `contract-sharing`,
`dsar-disclosure`, `publication` (plus the earlier illustrative `counterparty` and `auditor`). They
are examples, not legal advice, and each states what it does not decide; the compliance scope and
the exact wording to describe the tool are in [docs/COMPLIANCE.md](docs/COMPLIANCE.md). Pass your
own with `--policy path/to/policy.toml`.

## What the verifier checks, and what it cannot

Two independent text extractions; characters inside each redaction box; metadata and XMP; hidden
containers (attachments, scripts, forms, bookmarks, annotations, thumbnails); a raw scan of every
decoded PDF object; old revisions; images; collateral word loss; whether boxes render dark; page
integrity; layout shift. For a page rebuilt as an image (a scan) it additionally checks that no text
layer survived, that there is exactly one full-page image and the boxes are black in that image's
pixel data (not just painted on top), that OCR of the output cannot read the strings you chose to
redact, and that the text that should have survived is still readable.

A pass means "no leak found by these checks". It does not mean the document is safe. Not covered:
text turned into images or outlines on a page that has a text layer, text under images on text pages,
unusual font encodings, digital signatures (redaction invalidates them), and indirect identification
(a job title plus a company can identify someone). For OCR'd pages, a character Tesseract misreads is
invisible to both the detectors and the export check, and handwriting, stamps, signatures and photos
are not read at all. Every report lists these.

For `.docx` the verifier reads the output with stdlib `zipfile`, `xml.etree.ElementTree` and raw-byte
scanning — not with the lxml writer — and withholds the file when: a redacted passage is still found
in the extracted text, the attributes or the raw bytes of any part (V102/V103); a comment, tracked
change, macro, embedded object, external relationship, alternate-text/field-code/hyperlink carrier or
text-carrying XML construct survives (V104); document properties are not empty (V105); paragraphs,
runs, image/font content or any part the writer may not edit changed (V106); content types or
relationship targets no longer resolve (V107); or a source-chosen part name survives instead of a
generated neutral one (V108). Comments and links being *removed*, images being copied, small-image
review and presentation-only text are intentional and surfaced at pre-flight (PF111/PF112/PF113/
PF123/PF124/PF126); see [docs/DOCX.md](docs/DOCX.md) for the full list and the known gaps.

## Results, briefly

**Independent-data suite (100 documents, third-party CUAD contract text, PII injected
independently, hash-pinned).** Where the tool released a file it left no readable personal string:
0 of 60 documents (95% CI 0-6%), and it damaged none of the planted protected decoys. The blind
"accept every suggestion" flow withheld 40 of 100 rather than release a file whose accepted text
was still readable elsewhere — the fail-closed behaviour working, caused on this data by company
names labelled as persons. The best other tool on the same data, a Presidio reference pipeline,
leaked in 8% of documents and destroyed 31.6% of the protected decoys; doc_redaction 2.8.1's batch
output leaked in 100% (26% judged only on the categories it exposes) and over-redacted 11% of
protected text in its extended configuration; two regex tools leaked in 100%. Full table,
scope-restricted rows and caveats: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

**Home-field suite (175 synthetic documents, kept as the protocol record).** The ensemble left 2% of
documents with at least one undetected span, against 49% for the best single model. The 0.2.0
quality pass cut protected-text damage from 4.5% to 0.0% on that suite and the review queue from
0.90 to 0.59 items per document; those are diagnostics, not validation, because the pass was
designed from that suite's failures. The export verifier caught 195 of 195 deliberately leaky
exports across 13 leak routes and wrongly failed 0 of 15 clean ones.

These numbers say how the pieces behave under a controlled protocol. They are not evidence about
real contracts. The protocol, the failed attempts (an organisation-name veto made things worse),
the caveats and the open problems are in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

## Limitations

- English text-based PDF contracts, English printed scans (OCR) and English `.docx` documents. No
  other languages. Handwriting, signatures, stamps and photographs are not detected; pre-flight flags
  every OCR page for a human look and says a name OCR cannot read cannot be redacted.
- `.docx` support is deliberately narrow and fail-closed. Refused: encrypted files, legacy `.doc`,
  `.docm`/`.dotx`, macros/ActiveX, tracked changes (accept or reject them in Word first), embedded
  OLE objects, legacy form fields, `.docx`-external content (`altChunk`/subdocuments), unknown parts
  (charts, SmartArt, glossary), text outside the supported containers, and packages whose structure
  this tool does not recognise. Read and redacted: body text, headers, footers, footnotes, endnotes
  and text boxes (including nested and alternate-content copies). Removed on export: comments,
  custom XML, document-property bags, page thumbnails, alternate text/tooltips/table captions, field
  instructions, hyperlink targets (links become plain text), content-control metadata and document
  variables. See [docs/DOCX.md](docs/DOCX.md).
- `.docx` known gaps: text inside images is read with OCR and can be redacted, but OCR misreads are
  invisible to detection and to the output check, and handwriting, signatures, stamps, photographs
  and very-low-resolution text are not understood. An image in which the engine finds no words is
  REFUSED when it is large/content-bearing (it cannot be certified) and needs an explicit
  acknowledgement when it is small ("pixels were NOT checked ... personal data may survive"); an
  image displayed below about 150 dpi or containing very small text (a large share of OCR word boxes
  under ~12 px at native resolution) is a REVIEW, and engine word boxes that sit on uniform/blank
  pixels refuse the file (contrast-based ink evidence, so valid low-contrast text is accepted).
  Every export report carries a **WARN V112** naming each image whose text was never read **or was
  too small to read reliably** (and the same WARN row is in the evidence report), so a PASS never
  silently claims those pixels were verified. Animated GIF, APNG, animated WebP,
  multi-page TIFF and undecodable/unsupported/oversized images are REFUSED rather than silently
  copied; a single-frame GIF is accepted and normalised to PNG. **Every** image in an export is
  re-encoded as lossless PNG at the same pixel size with metadata stripped (JPEG/GIF/etc. becomes
  PNG; file size may grow), so an untouched image is pixel-identical but not byte-identical and its
  GPS/author/ICC metadata is gone. Image suggestions are listed in the review page but drawn without
  a box (their position on the rendered page is not resolvable without guessing). The image OCR
  output check can raise a false alarm (and withhold the file) when a redacted phrase is a compact
  prefix of unredacted text in another image. Presentation-only text in copied parts (style names,
  list-numbering format text, embedded font family names, theme/settings values) is not inspected
  or redacted, though structured identifiers in it refuse the file — PF126 reports how much exists
  and is informational, so a person-shaped name in a style or font name is disclosed but does not
  block a batch export; numeric values are only treated
  as geometry in the known `wp:posOffset`/`wp14:pctSize` layout elements and every other numeric
  text node is refused; a decomposed-Unicode (NFD) name can evade detection (same gap as PDF);
  over-redaction is possible where Word stores the same content twice (AlternateContent choice and
  fallback are both redacted).
- `.docx` review page: rendered mode needs Microsoft Word (Windows) or LibreOffice installed
  (detected, never installed) and costs a one-off Word start-up per review session (about 6 s on
  this machine). It is fail-closed by design: a suggestion is only drawn when its text can be
  aligned to the rendered pages with confidence, and otherwise the whole page falls back to text
  review — a box that might be the wrong text is never shown. Manual boxes must fully contain whole
  words and map back to one contiguous .docx passage, or they are rejected. The rendered PDF is
  temporary and contains the unredacted document while the review page is open; it is deleted at
  session end. LibreOffice was implemented and unit-tested but not exercised on a machine with
  LibreOffice installed; the Word path is exercised end-to-end. See [docs/DOCX.md](docs/DOCX.md).
- Scans that are sideways or upside down cost one extra OCR pass: pre-flight runs Tesseract's
  orientation detector on every page it will OCR (about 0.4 s per scanned page), and a page whose
  orientation cannot be determined is a REVIEW finding (PF018), never a silent guess. A detected
  rotation is corrected and also surfaced for review (PF017).
- Rasterised scan pages are rebuilt as images: they are not searchable or text-selectable, and their
  output size follows the source scan, not the text. On a synthetic one-page contract, a 198 KB
  300 dpi JPEG scan became a 193 KB lossless output page, while the text PDF of the same content is
  2.9 KB. Rasterisation also costs one OCR pass per page on review and one on export (about
  0.5-0.7 s per page on this machine, measured, plus about 8.7 MB per page while the 300 dpi
  grayscale image is in memory; a rotated page needs a second pass and takes about 1.6 s).
- OCR is a guess: an error cuts both ways. A name it misreads cannot be redacted (surfaced at
  pre-flight), and an error in the output check could hide a leak or, more often, raise a false
  alarm (a fragment of a redacted name is refused on image pages). See `docs/THREAT_MODEL.md` and
  the measured OCR comparison in `docs/OCR_EVALUATION.md`.
- A custom OCR engine is code you chose to run: its accuracy drives what can be suggested for
  redaction, and a cloud engine sends page images off this machine (see the warning above and
  `docs/OCR_BACKENDS.md`). Without Tesseract installed the export verifier must ask the same custom
  engine to check its own output — the report labels that check `SAME-ENGINE (weaker)` and a
  pre-flight acknowledgement is required. The engine is pinned in the decision file: re-run
  `suggest` if you change it between review and export.
- Not tested: real documents, multi-column, tabular or form-like layouts, hyphenated line ends,
  non-Latin scripts, heavy skew and handwriting.
- Text laid out vertically or stacked one glyph per line is not reconstructed as a name. On an
  independent benchmark this left 4 of 27 planted copies behind, and the export was withheld
  rather than released.
- Detectors can miss personal data; the verifier checks that what you decided to redact is gone, not
  that you decided to redact everything. Read every page.
- Redactions at the very start of a line can shift the following text; the verifier warns.
- A date of birth is only proposed when a birth keyword ("born", "date of birth", "DOB") sits on
  the date's own line or within a short window; a DOB value printed far from its keyword is missed.
- A person name adjacent to a company designator (Ltd, LLP, Studio, Partners, GmbH, ...) is treated
  as part of the firm and not proposed. Conversely, a company name with no designator can still be
  labelled a person: on the independent-data suite that made the verifier withhold 40 of 100 blind
  batch exports rather than release a file whose accepted text recurred elsewhere, and accepting
  those suggestions over-redacts the company name. The review queue is where this is caught.
- Compared against the open-source tools that run as batch programs, on two suites: a home-field
  synthetic suite (doc_redaction 2.8.1, ltillmann/pdf-redactor, JoshData/pdf-redactor, a Presidio
  reference pipeline) and an independent-data suite built on public CUAD contract text with
  independently injected PII (same tools). A third-party rendering-condition benchmark (pdfredeval)
  was attempted; its pages are outside this tool's declared scope and it refused to release rather
  than produce a score. Commercial tools and LLM-based redaction are unmeasured.

## Security and privacy

The review page serves a confidential document, so it binds to 127.0.0.1 only, makes no external
requests, requires a random token per run, checks the Host header, sends a strict content-security
policy, and its API carries no document text in rendered mode. The `.docx` text review does return
short context excerpts to the local page so the lawyer can see what they are deciding; those
excerpts exist only in memory and are never written to the decision file, reports or logs. Decisions
autosave to `<file>.decisions.json` without document text. A `.docx` rendered review asks an installed
Word or LibreOffice to render the source into a temporary PDF (deleted when the session ends); that
is the user's own office program, not a network service. Anyone with access to your user session can
still read your files; this is a local tool, not a multi-user service. See [SECURITY.md](SECURITY.md)
and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

Do not put real client documents in issues, tests or benchmarks. All test data is synthetic.

## Licence and dependencies

AGPL-3.0-or-later (see `LICENSE`). PDF writing uses PyMuPDF, which is AGPL-3.0 or commercially
licensed by Artifex; the project is AGPL for that reason, and the writer sits in one module
(`redact.py`) so it could be replaced. PyMuPDF 2.x bundles the code that used to be published as
the separate PyMuPDF4LLM and PyMuPDF Layout packages; `pymupdf4llm` and `pymupdf_layout` 2.x are
empty shims kept only so the 1.x to 2.x upgrade is safe, and carry no licence of their own.

| Component | Licence |
|---|---|
| PyMuPDF | AGPL-3.0 or Artifex commercial |
| lxml | BSD-3-Clause |
| pdfminer.six | MIT |
| pypdfium2 | BSD-3-Clause / Apache-2.0 |
| pikepdf | MPL-2.0 |
| python-docx (tests only) | MIT |
| Presidio | MIT |
| spaCy and `en_core_web_*` models | MIT |
| GLiNER (code) | Apache-2.0 |
| GLiNER weights used: `knowledgator/gliner-pii-base-v1.0`, `urchade/gliner_multi_pii-v1` | Apache-2.0 (per the Hugging Face model cards; check before redistributing) |
| PyTorch | BSD-style |
| Tesseract OCR (optional external program, not linked or redistributed) | Apache-2.0 |
| A custom OCR engine wrapper you configure (not bundled, not required) | your choice; check the engine's licence and data terms before use |
| `pymupdf4llm`, `pymupdf_layout` (empty shims installed automatically with PyMuPDF 2.1) | No licence declared: they contain metadata only, no code. The 4LLM and Layout code they replace ships inside PyMuPDF itself: AGPL-3.0 or Artifex commercial |

## Development

```
pytest                                   # 323 tests, core dependencies only; OCR tests skip without Tesseract
legal-redact bench-export --n 15         # can the verifier actually fail?
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Held-out benchmark suites are hash-pinned: do not tune on
them, and create a new one when a change set has been scored. The OCR configuration comparison
(what was adopted, what was rejected, and why) is in
[docs/OCR_EVALUATION.md](docs/OCR_EVALUATION.md); the pluggable-engine contract and the cloud
procedure are in [docs/OCR_BACKENDS.md](docs/OCR_BACKENDS.md); the `.docx` design note, refusals and
known gaps are in [docs/DOCX.md](docs/DOCX.md).

To try the tool without any real documents, follow [docs/TESTING.md](docs/TESTING.md):
`scripts/make_test_documents.py` writes synthetic PDF and `.docx` fixtures with planted personal
data and protected lookalikes, and `scripts/check_planted_values.py` checks a redacted output for
them (text layer, OCR for rasterised pages, and every `.docx` package part).
