# legal-redact

[![CI](https://github.com/kirillsakhin/legal-redact/actions/workflows/ci.yml/badge.svg)](https://github.com/kirillsakhin/legal-redact/actions/workflows/ci.yml)

Review-first redaction of personal data in PDF and Word (`.docx`) contracts, with independent
checks of the exported file. Local and offline by default.

> **Status: pre-alpha (0.4.1).** Developed and tested on synthetic documents and public contract
> text; not tested on real client documents. It does not make legal determinations, and removing
> personal data is not anonymisation or GDPR compliance.

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
| 1. Extract | Text and word coordinates, so a text span maps back to boxes on the page. Pages with no usable text layer are OCR'd instead (same word/offset/box model). | `extract.py` |
| 1b. OCR | Only for pages with no (or almost no) text layer: the local Tesseract engine reads the page image (grayscale) into the same model. A page that already has a text layer is never OCR'd. Pages rotated 90/180 degrees are detected with Tesseract OSD and re-read upright, with every box mapped back to the original page. | `ocr.py` |
| 0. Pre-flight | Refuses encrypted files, attachments and XFA forms, and refuses scans when OCR is unavailable or disabled; otherwise flags OCR use and what OCR cannot see, plus images, annotations, forms, scripts, layers. | `preflight.py` |
| 2. Detect | Rules for structured identifiers (IBAN and card checksums), Presidio, and GLiNER, merged by an ensemble with conflict resolution. | `detect/` |
| 3. Decide | A policy (TOML) says redact, keep or review per category; the review gate blocks export until every suggestion has a decision. | `policy.py`, `review.py`, `review_server.py` |
| 4. Remove | Applies redactions (text under each box is deleted), scrubs metadata, annotations, attachments, scripts, bookmarks and hidden text, writes a fresh file. Scanned pages are re-rasterised at 300 dpi and the boxes are burnt into the pixel data, then rebuilt as image-only pages. | `redact.py` |
| 5. Verify | Re-reads the output with pdfminer.six, pypdfium2 and pikepdf; for rebuilt image pages it also OCRs the output, checks there is no text layer and that the boxes are black in the page image itself; withholds the file if a leak is found. | `verify.py` |

Reports and decision files contain categories, page numbers, boxes and counts, never document text.

For `.docx`, stages 0/1/4/5 run through the `docx/` package: extraction builds the same synthetic
page/offset model from `w:t` runs in the body, headers, footers, notes and text boxes, so the
detectors and the review gate are reused unchanged; the writer edits the OOXML with lxml and the
verifier re-reads it with other stdlib readers. The PDF pipeline's behaviour is untouched.

## Supported formats

| Format | Read | Export | Review page | Notes |
|---|---|---|---|---|
| PDF, text layer | yes | yes (`redact.py`) | yes | the original pipeline; unchanged in 0.3.0 |
| PDF, scanned | OCR (English) with local Tesseract, or a configured custom engine (docs/OCR_BACKENDS.md) | yes, re-rasterised image pages | yes | see `docs/OCR_EVALUATION.md` |
| `.docx` (OOXML Word) | yes (`docx/`), including headers/footers, notes, text boxes and AutoText | yes (`docx/write.py`), verified fail-closed | **no** — CLI only (`inspect`, `suggest`, `redact`) | `.doc`, `.docm`, encrypted, tracked-changes and macro files are refused; images ≥25% of a page are refused; part names are neutralised. See [docs/DOCX.md](docs/DOCX.md) |

For `.docx` the transport is the same: pre-flight → shared detectors on the extracted text →
decision file → export → independent verification. The browser review page renders PDF pages, so
`legal-redact review` says so and stops; DOCX review is through the decision file (edit each
`"status"` to `"accepted"`/`"rejected"`).

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
it was found. Without it, pages with no text layer are refused exactly as before. OCR runs locally
and offline; its output is cached nowhere. The measured comparison of OCR configurations and an
alternative engine (RapidOCR) is in [docs/OCR_EVALUATION.md](docs/OCR_EVALUATION.md).

A different reader (a local model, or a paid third-party service through a wrapper you write) can be
plugged in with `--ocr-engine`/`--ocr-command`; the contract, examples and the two-gate cloud
procedure are in [docs/OCR_BACKENDS.md](docs/OCR_BACKENDS.md). Engine choice is a compatibility
option, not an accuracy claim.

> **Cloud/third-party OCR is opt-in and doubly gated.** Third-party (cloud) OCR is selected. Page
> images of this document will be sent to an external OCR service configured by you, outside this
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

`.docx` uses the same three commands; `inspect`, `suggest` and `redact` detect the format from the
file. The review page cannot show Word pages, so `review` is PDF-only:

```
legal-redact inspect contract.docx
legal-redact suggest contract.docx -o decisions.json --policy counterparty
# edit statuses; acknowledge pre-flight REVIEW codes you accept, e.g. --ack PF114
legal-redact redact contract.docx --decisions decisions.json -o contract.redacted.docx --ack PF114
```

A `.docx` pre-flight that reports a REVIEW code (comments being stripped, hidden-text carriers, small
images, custom XML) blocks export until that code is acknowledged with `--ack`. REFUSE findings
(encrypted, `.doc`/`.docm`, macros, tracked changes, embedded OLE objects, unknown parts, malformed
packages, large images, a part/presentation name that reads like an identifier) always block export.
The `.docx` review step is batch-style — the decision file carries no document text, so open the
source in Word to see what you are approving; the PDF review page remains the stronger workflow.

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

Policies live in `src/legal_redact/policies/*.toml` (`counterparty` and `auditor` are illustrative,
not legal advice). Pass your own with `--policy path/to/policy.toml`.

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
- `.docx` known gaps: text inside images is not read — an image covering at least 25% of a page is
  REFUSED (it may be a pasted scan; convert that page to PDF and redact it as a scan), smaller ones
  are flagged for review and kept unchanged; presentation-only text in copied parts (style names,
  list-numbering format text, embedded font family names, theme/settings values) is not inspected
  or redacted, though structured identifiers in it refuse the file — PF126 reports how much exists
  and is informational, so a person-shaped name in a style or font name is disclosed but does not
  block a batch export; numeric values are only treated
  as geometry in the known `wp:posOffset`/`wp14:pctSize` layout elements and every other numeric
  text node is refused; a decomposed-Unicode (NFD) name can evade detection (same gap as PDF); the
  browser review page is PDF-only, so **`.docx` review is batch-style and weaker** — the decision
  file contains no document text, so the lawyer must open the source in Word to review it.
  Over-redaction is possible where Word stores the same content twice (AlternateContent choice and
  fallback are both redacted).
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
requests, requires a random token per run, checks the Host header, sends a strict
content-security policy, and its API never returns document text. Decisions autosave to
`<pdf>.decisions.json` without document text. Anyone with access to your user session can still
read your files; this is a local tool, not a multi-user service. See [SECURITY.md](SECURITY.md) and
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

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
pytest                                   # 234 tests, core dependencies only; OCR tests skip without Tesseract
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
