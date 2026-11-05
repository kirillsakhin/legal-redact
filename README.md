# legal-redact

Review-first redaction of personal data in PDF contracts, with independent
checks of the exported file. Local and offline by default.

**Status: Phase 1 of 8 (pre-alpha).** Today it can inspect a PDF and extract
text with coordinates. It cannot redact anything yet.

```
legal-redact inspect contract.pdf
```

Prints page and word counts plus pre-flight findings (codes and page numbers,
never document text) and says whether export would be refused.

## What it will do

1. Extract text and coordinates.
2. Suggest sensitive passages (rules + Presidio + GLiNER), each with a category and a reason.
3. Apply a disclosure-purpose policy; the lawyer accepts, rejects or adds redactions.
4. Write a new file with the content actually removed.
5. Re-open the output and check it with different libraries than the writer used.

The tool recommends; the lawyer decides. Redaction is not anonymisation: indirect
identification, context and the purpose of disclosure still need human judgment.

See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for what is and is not covered.

## Development

```
python -m venv .venv && .venv/Scripts/activate
pip install -e ".[dev,verify]"
pytest
```

All test documents are synthetic.

## Licence

AGPL-3.0-or-later (see `LICENSE`). PDF writing uses PyMuPDF, which is AGPL-3.0 or
commercially licensed by Artifex.

## Benchmark (Phase 2)

```
legal-redact bench --n 30 --show-misses
```

Generates synthetic contracts from 4 templates, renders them to PDF, re-extracts
the text, and scores a detector with leave-template-out evaluation. Reports
per-category precision/recall, documents with at least one residual leak,
protected-text damage (over-redaction) and the leaked spans.

**Read the numbers with care.** The rules baseline scores 1.00 on structured
categories (email, phone, IBAN, card, ID, DOB, address) because the synthetic
generator and the regexes were written by the same hands: the formats match by
construction. That shows the harness works, not that rules are this good on real
contracts. The informative result is PERSON (precision 0.72, recall 0.92 on
the first run; every miss is an ALL-CAPS signature name) and the 28% of documents
with a residual leak. Real-document accuracy is unknown and is not claimed.

### Detector comparison (Phase 3, one run, 120 synthetic documents, 4 held-out templates)

| Detector | PERSON precision | PERSON recall | Docs with a leak | Protected text damaged |
|---|---|---|---|---|
| rules | 0.72 | 0.92 | 28% | 0.0% |
| Presidio (spaCy small) | 0.70 | 0.84 | 97% | 2.2% |
| GLiNER `knowledgator/gliner-pii-base-v1.0` | 1.00 | 0.99 | 30% | 0.0% |
| GLiNER `urchade/gliner_multi_pii-v1` | 1.00 | 0.35 | 97% | 0.0% |
| **ensemble** (rules + Presidio + GLiNER-kn, union merge) | 0.59 | 1.00 | **2%** | 2.2% |

What the numbers do and do not say:

- Presidio alone has no address or date-of-birth recognizer and missed most `.test`
  e-mail addresses in this setup, so its leak rate says little about its design.
- GLiNER-kn found names well but missed every lease "ID number" (ID recall 0.67);
  the rules caught those. The detectors fail in different places, which is why the
  ensemble leaks least. It buys that with many extra suggestions (PERSON precision 0.59),
  i.e. more review work, and 2.2% of protected spans touched.
- GLiNER's threshold is tuned on the training templates only. Model choice mattered
  a lot: the two GLiNER weights differ by 0.64 PERSON recall.
- Caveats: one seed, 4 templates, invented names from a short list, no wrapped
  or hyphenated lines. GLiNER's training data is not known to exclude text like this.
  These are results on synthetic documents, not evidence about real client files.

### Held-out results (Phase 3b)

Protocol. The dev corpus is for building. Suite v2 ("frozen") was scored once, its
failures were read, and fixes were made (wrap-tolerant patterns, line-break and
ALL-CAPS model views, checksum checks, no naive name rule in the ensemble). Because
of that, v2 numbers after the fixes are **contaminated** and not reported as
evidence. Suite v3 ("holdout": new names, two new templates, new seeds, 150 docs) was
pinned by hash before it was scored and nothing was tuned on it. Same generator
family, so it tests whether the fixes generalise to new names and layouts, not
real contracts.

| Detector (holdout v3) | Docs with a leak (95% CI) | PERSON P / R | Protected text damaged |
|---|---|---|---|
| rules | 100% | 0.44 / 0.53 | 30.5% |
| Presidio (spaCy small) | 100% | 0.80 / 0.82 | 2.9% |
| Presidio (spaCy large) | 100% | 0.95 / 0.94 | 2.5% |
| GLiNER knowledgator | 45% (37-53) | 0.99 / 0.98 | 3.8% |
| GLiNER urchade | 99% | 0.98 / 0.37 | 0.6% |
| ensemble, Phase 3 config | 13% (9-20) | 0.55 / 0.99 | 34.2% |
| **ensemble, current** | **13% (9-20)** | **0.83 / 0.99** | **6.2%** |

Read this carefully:

- Presidio alone scores 100% leaky because it has no address or date-of-birth
  recognizer; that is a coverage gap, not a quality verdict on its names. On names
  alone, Presidio with the large model (0.95/0.94) has better precision than our
  ensemble (0.83/0.99). The ensemble trades precision for recall by design (union),
  which means more review work.
- The ensemble's lead is mostly complementarity: GLiNER misses IBANs (recall 0.15),
  rules and Presidio cover them; rules cannot read names, GLiNER can.
- With a simulated reviewer who decides the review queue perfectly, 13% of documents
  still leak: a reviewer cannot remove what was never suggested. With the queue
  ignored, 67% leak. The queue (about 2 items per document) must be read.
- Open problems: ID precision 0.69 and card precision 0.77 (extra review clicks),
  6.2% of protected text still touched, 23 leaked spans not yet analysed (the holdout
  stays unread until the next change set is ready).
- Not compared: doc_redaction, commercial tools, LLM-based redaction. Not tested:
  hyphenated line ends, multi-page and tabular layouts, non-Latin scripts, real documents.
