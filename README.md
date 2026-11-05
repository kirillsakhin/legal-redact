# legal-redact

Review-first redaction of personal data in PDF contracts, with independent checks of the
exported file. Local and offline by default.

> **Status: pre-alpha (0.0.1).** Developed and tested on synthetic documents only. Not tested on
> real documents. It does not make legal determinations, and removing personal data is not
> anonymisation or GDPR compliance.

A lawyer opens a short English, text-based PDF contract, picks a disclosure purpose (a policy),
reviews what the tool suggests and why, and exports a separate redacted copy. The model
recommends; the lawyer decides. Drawing a black box over text is not redaction, so the export
removes the content and then checks the result with different libraries than the ones that
wrote it.

## How it works

| Stage | What happens | Module |
|---|---|---|
| 1. Extract | Text and word coordinates, so a text span maps back to boxes on the page. | `extract.py` |
| 0. Pre-flight | Refuses encrypted files, scans, attachments and XFA forms; flags images, annotations, forms, scripts, layers. | `preflight.py` |
| 2. Detect | Rules for structured identifiers (IBAN and card checksums), Presidio, and GLiNER, merged by an ensemble with conflict resolution. | `detect/` |
| 3. Decide | A policy (TOML) says redact, keep or review per category; the review gate blocks export until every suggestion has a decision. | `policy.py`, `review.py`, `review_server.py` |
| 4. Remove | Applies redactions (text under each box is deleted), scrubs metadata, annotations, attachments, scripts, bookmarks and hidden text, writes a fresh file. | `redact.py` |
| 5. Verify | Re-reads the output with pdfminer.six, pypdfium2 and pikepdf; withholds the file if a leak is found. | `verify.py` |

Reports and decision files contain categories, page numbers, boxes and counts, never document text.

## Install

Requires Python 3.11 or later. Developed on 3.14; a CI matrix for 3.11 to 3.13 is configured but has
not yet run.

```
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"    # core: rules detector, writer, verifier, review page
pytest
```

For the full ensemble (Presidio + GLiNER):

```
pip install -e ".[ml]"
python -m spacy download en_core_web_lg
```

The first run downloads model weights from Hugging Face; after that the models are cached locally.
The benchmarks ran on an RTX 5090 laptop GPU. CPU speed and speed on large files are untested.
The tool's own code makes no network calls; with the weights cached, the ensemble ran with `HF_HUB_OFFLINE=1` in the author's environment (one machine, one run).

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

Policies live in `src/legal_redact/policies/*.toml` (`counterparty` and `auditor` are illustrative,
not legal advice). Pass your own with `--policy path/to/policy.toml`.

## What the verifier checks, and what it cannot

Two independent text extractions; characters inside each redaction box; metadata and XMP; hidden
containers (attachments, scripts, forms, bookmarks, annotations, thumbnails); a raw scan of every
decoded PDF object; old revisions; images; collateral word loss; whether boxes render dark; page
integrity; layout shift.

A pass means "no leak found by these checks". It does not mean the document is safe. Not covered:
text turned into images or outlines, text under images (no OCR), unusual font encodings, digital
signatures (redaction invalidates them), and indirect identification (a job title plus a company
can identify someone). Every report lists these.

## Results, briefly

On synthetic documents, with a hash-pinned held-out suite (175 documents) that nothing was tuned on:

- The ensemble left 2% of documents with at least one undetected span (95% interval 1 to 5%),
  against 49% for the best single model and 100% for Presidio alone (which has no address or
  date-of-birth recognizer). Names: precision 0.96, recall 1.00.
- A review queue of about one item per document remains, and 4.5% of legally important text is
  still touched by suggestions. With the queue ignored, 51% of documents would leak.
- The export verifier caught 195 of 195 deliberately leaky exports across 13 leak routes and wrongly
  failed 0 of 15 clean ones.

Against doc_redaction 2.8.1 run in batch mode on the same suite, it left between 56% (extended configuration, categories it supports, email excluded) and 100% (shipped defaults, all categories) of documents with a readable personal string, versus 1% for ours when every suggestion is accepted. That is a home-field result: the categories and formats are ours, and the gap mostly reflects layering several detectors on top of a Presidio-only baseline.

These numbers say how the pieces behave under a controlled protocol. They are not evidence about
real contracts. The protocol, the failed attempts (an organisation-name veto made things worse),
the caveats and the open problems are in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

## Limitations

- English, text-based, short PDF contracts. No scans or OCR, no DOCX, no other languages.
- Not tested: real documents, multi-page or tabular layouts, hyphenated line ends, non-Latin scripts.
- Detectors can miss personal data; the verifier checks that what you decided to redact is gone, not
  that you decided to redact everything. Read every page.
- Redactions at the very start of a line can shift the following text; the verifier warns.
- Contract dates are sometimes mistaken for dates of birth (suggestions you can reject).
- Compared against one other tool only (doc_redaction 2.8.1, on our own synthetic suite, with a workaround; see docs/BENCHMARKS.md). Not compared against commercial tools or LLM-based redaction.

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
(`redact.py`) so it could be replaced.

| Component | Licence |
|---|---|
| PyMuPDF | AGPL-3.0 or Artifex commercial |
| pdfminer.six | MIT |
| pypdfium2 | BSD-3-Clause / Apache-2.0 |
| pikepdf | MPL-2.0 |
| Presidio | MIT |
| spaCy and `en_core_web_*` models | MIT |
| GLiNER (code) | Apache-2.0 |
| GLiNER weights used: `knowledgator/gliner-pii-base-v1.0`, `urchade/gliner_multi_pii-v1` | Apache-2.0 (per the Hugging Face model cards; check before redistributing) |
| PyTorch | BSD-style |
| `pymupdf_layout`, `pymupdf4llm` (installed automatically with PyMuPDF 2.1) | [TO CONFIRM]: package metadata states no licence; check before any release |

## Development

```
pytest                                   # 67 tests, core dependencies only
legal-redact bench-export --n 15         # can the verifier actually fail?
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Held-out benchmark suites are hash-pinned: do not tune on
them, and create a new one when a change set has been scored.
