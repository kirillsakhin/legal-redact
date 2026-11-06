# Contributing

Thanks for looking. This is a small, pre-alpha project with strict honesty rules about claims.

## Ground rules

- **No real documents, ever.** Not in issues, pull requests, tests, benchmarks or screenshots.
  Use the synthetic generators in `src/legal_redact/bench/`.
- **No claims without a measurement.** A change that affects detection or export safety needs a
  test, and a claim in the README needs a benchmark or test behind it.
- **A guard you cannot make fail is not a guard.** New verifier checks need a seeded-leak injector
  in `bench/export_suite.py` that the check catches, and a clean export it must not reject.
- **Held-out suites are read-only.** `FROZEN_SHA256`, `HOLDOUT_SHA256`, `HOLDOUT4_SHA256` and
  `INDEP_SHA256` (the independent-data v5 suite) pin them; a test fails if the generator changes.
  Do not tune on them. When a change set has been scored on one, it is spent: add a new suite —
  for the synthetic ones, with new names, seeds and at least one new template; for the independent
  one, with new source excerpts and a new injection seed — pin its hash before scoring anything, and
  say so in `docs/BENCHMARKS.md`.
- Reports, logs and decision files must never contain document text. There are tests for this;
  keep them passing.

## Setup

```
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The `ml` extra (`pip install -e ".[ml]"` plus `python -m spacy download en_core_web_lg`) is only
needed to run the Presidio and GLiNER benchmark arms. The `dev` extra includes `python-docx`, which
only the DOCX tests use (as an independent third-party reader of exported files); the package itself
never imports it.

## Style

Small, plain Python. Prefer the minimum code that solves the problem; no speculative options. Keep
module docstrings honest about limits: they are the documentation of what the code does not do.

## Licence

By contributing you agree your contribution is licensed under AGPL-3.0-or-later, like the project.
