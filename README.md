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
