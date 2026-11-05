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
