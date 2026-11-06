# Benchmarks

Everything here is measured on **synthetic** documents. None of it is evidence about real
client documents. It shows how the detectors and the export checks behave under a
controlled, reproducible protocol, and where they fail.

## Protocol

| Suite | What it is | Status |
|---|---|---|
| dev | 4 templates (nda, services, loan, lease), written together with the rules. Leave-template-out evaluation. | Used for building. Scores flatter the rules (circular: same author for generator and regexes). |
| v2 "frozen" | 4 new templates, wrapped paragraphs, names from many origins, foreign address/IBAN/ID formats. Hash-pinned. | Scored once, failures read, fixes made. Post-fix numbers are **contaminated**. |
| v3 "holdout" | 150 docs: new names, new seeds, 2 new templates. Hash-pinned before scoring. | Scored once for change set 1. Now read. |
| v4 "holdout4" | 175 docs: new names, new seeds, 1 new template. Hash-pinned before scoring. | Scored once for change set 2. Now read: the next change set needs a v5. |

Rules that keep the numbers honest:

1. A suite is pinned by a SHA-256 of its generated text and labels (`FROZEN_SHA256`,
   `HOLDOUT_SHA256`, `HOLDOUT4_SHA256`). A test fails if the generator changes.
2. Nothing is tuned on a held-out suite. Model thresholds are fitted on the dev corpus only.
3. Once a suite's failures have been read, it can no longer judge changes made in response.
4. Documents go through the real pipeline: render to PDF, reopen, extract. If extraction does
   not reproduce the generator's text, the run stops.

Metric definitions: a gold span is a **leak** if any of its characters is not covered by the
union of all predicted spans. A **document leak** is one with at least one leaked span.
**Protected damage** is the share of legally important spans (parties, dates, amounts, governing
law) overlapped by any prediction. Per-category precision and recall use overlap matching of the
same category; "strict" means identical boundaries.

## Detector results

### Change set 2 on holdout v4 (175 documents)

| Arm | Docs with a leak (95% CI) | PERSON P / R | Protected text damaged | Review queue per doc |
|---|---|---|---|---|
| rules only | 100% | 0.39 / 0.42 | 27.6% | 4.2 |
| Presidio (spaCy large) alone | 100% | 0.95 / 0.89 | 1.6% | 5.3 |
| GLiNER alone | 49% (42-56) | 0.99 / 0.98 | 5.3% | 4.7 |
| previous ensemble | 10% (7-16) | 0.83 / 1.00 | 6.8% | 2.9 |
| + conflict resolution | 10% (7-16) | 0.85 / 1.00 | 4.9% | 2.5 |
| + Presidio large instead of small | 10% (6-15) | 0.95 / 1.00 | 4.5% | 1.0 |
| + organisation/place veto (tried) | 18% (13-25) | 0.96 / 0.99 | 3.9% | 1.3 |
| **+ name bridging (current)** | **2% (1-5)** | **0.96 / 1.00** | **4.5%** | **0.9** |

What each change did:

- **Conflict resolution**: amounts are not IDs; a suggestion fully inside a more confident one
  of another category is dropped (coverage cannot shrink).
- **Presidio large**: most of the name-precision gain and the drop in review workload.
- **Veto** (organisation/place names demote a PERSON): made things worse; removed from the
  ensemble, kept here as a negative result.
- **Name bridging**: two names separated only by ", " become one box, so "Surname, Firstname"
  no longer leaves the comma showing between two boxes. This generator writes about 10% of
  names that way, so part of the gain is by construction. Frequency in real contracts is unknown.

Presidio alone has no address or date-of-birth recognizer, so its 100% is a coverage gap, not a
verdict on its names.

### Earlier results (kept for the record)

Holdout v3, previous configurations:

| Detector | Docs with a leak | PERSON P / R | Protected text damaged |
|---|---|---|---|
| rules | 100% | 0.44 / 0.53 | 30.5% |
| Presidio small | 100% | 0.80 / 0.82 | 2.9% |
| Presidio large | 100% | 0.95 / 0.94 | 2.5% |
| GLiNER knowledgator | 45% (37-53) | 0.99 / 0.98 | 3.8% |
| GLiNER urchade | 99% | 0.98 / 0.37 | 0.6% |
| ensemble, change set 1 | 13% (9-20) | 0.83 / 0.99 | 6.2% |

Dev corpus: the rules baseline scores 1.00 on structured categories because the generator and
the regexes share an author. Only PERSON (precision 0.72, recall 0.92) and the 28% of documents
with a leak were informative. Model choice mattered a lot: the two GLiNER weights differ by 0.64
in PERSON recall.

### Review workload

With a simulated reviewer (a model, not a measurement of people), the final ensemble on v4
leaves 2% of documents with a leak if the reviewer decides the queue perfectly, 3% at a 5% error
rate and 8% at 10%. If the review queue is ignored, 51% leak. The queue averages 0.9 items per
document, on top of checking the automatically proposed ones.

## Export verifier results

`legal-redact bench-export` makes a clean export and 13 deliberately leaky variants of each
synthetic document: cosmetic box only, misaligned box, metadata, XMP, annotation, embedded
file, white text, invisible text, bookmark, link URI, JavaScript, form value, incremental save.
The injectors use PyMuPDF; the verifier reads with pdfminer.six, pypdfium2 and pikepdf.

Last run (15 documents): 0 of 15 clean exports wrongly failed; 195 of 195 leaky variants
caught, each by the check designed for it. Layout-shift warnings on clean exports: 3 of 15.
This covers the routes we thought of; it cannot show that no other route exists.

Findings from building it: the first geometry check flagged every clean export (it counted
neighbouring glyph boxes), and fixing that exposed a real writer defect (MuPDF loses the width of
the first glyph it removes, so following text slides left under the box). Both are fixed; see
`verify.py` and `redact.py`.

## Not measured

- Real documents of any kind. Human reviewers (the reviewer error rates are simulated).
- Commercial tools, LLM-based redaction, TypeSafe Jev and AutoTrust JEV-9B. Other open-source
  tools are compared separately below.
- Hyphenated line ends, multi-page and tabular layouts, columns, non-Latin scripts, scans.
- Open on v4: contract dates are sometimes labelled dates of birth (DOB precision 0.60), the
  likely cause of the remaining protected-text damage (not confirmed); ID span boundaries differ
  from the labels (strict recall 0.79).

## Reproduce

```
legal-redact bench --n 30 --detector rules presidio-lg gliner-kn ensemble
legal-redact bench --suite holdout4 --detector rules presidio-lg gliner-kn ens+resolve+lg ensemble
legal-redact bench-export --n 15
```

Detector arms other than `rules` need the `ml` extra and the models (see the README). GPU is
optional. Scores can differ slightly between machines and library versions.

## Comparison with other open-source tools

We ran every tool that could be driven as a batch program over the same pinned 175-document
suite (holdout v4). Each tool was given the source PDFs and nothing else; every output PDF was
then scored from the file alone with `bench/blackbox.py`: a gold personal-data string still
extractable by pdfminer.six or pypdfium2 is a leak, and a protected string no longer extractable
is lost. The tools were used as shipped (harnesses in `scripts/`, commands below), not tuned.

Controls in the full table: the unredacted source (must leak everything, and does: 100%) and an
oracle export built from the gold labels (must leak nothing and lose nothing, and does: 0%).

**This is a home-field comparison.** The documents, their layouts and their formats are ours,
and our detectors were developed on documents of the same family; the other tools got no tuning
at all. Treat the gap as an upper bound on any real advantage. An attempt to validate on an
independent third-party benchmark, and why it is not a like-for-like comparison, is at the end of
this section.

### The tools, as run

- **doc_redaction 2.8.1** (AGPL-3.0, the closest full tool). PyPI release 2.8.1 (GitHub `main`
  was 2.9.0), Python 3.14, PyMuPDF 1.28.2, `Local text` extraction and local Presidio with
  `en_core_web_lg`, no OCR, no human review. Two configurations: its shipped default entity list
  (`TITLES, PERSON, PHONE_NUMBER, EMAIL_ADDRESS, STREETNAME, UKPOSTCODE, CUSTOM`) and that list
  plus `IBAN_CODE, CREDIT_CARD, LOCATION`. Its command line exposes no entity for IDs or dates of
  birth. **A workaround was needed:** on this machine the documented command fails, including on
  its own example PDF, because it opens `placeholder_image_<n>.png` in the working directory and
  Poppler is not installed; we supplied a real 300 dpi render of each page under that name
  (`scripts/run_doc_redaction.py`). Output boxes were checked by eye and sit correctly over the
  text. A proper install of its prerequisites might behave differently.
- **ltillmann/pdf-redactor** (MIT). Its shipped CLI with every built-in detector enabled
  (`-e -l -p -s -b -d -f -q -r`): emails, links, phone numbers, IBAN, BIC, dates, timestamps, QR
  codes and barcodes. By design it does **not** detect names, addresses or IDs. Run from its own
  environment, one subprocess per document (`scripts/run_ltillmann_pdf_redactor.py`).
- **JoshData/pdf-redactor** (CC0). Its repository's command-line script is a documented no-op;
  its README says to use the library with text-filtering functions and points at `example.py`.
  We used that bundled filter set: an SSN-shaped regex, dash normalisation, a link rewrite and
  metadata/XMP rewrites. It does not aim at names or addresses at all, so it is a leak floor by
  design (`scripts/run_joshdata_pdf_redactor.py`). One documented deviation: `example.py`'s
  `Title` filter raises on PDFs with no `/Title` metadata, so it is made None-safe; metadata is
  not read by the scorer and every page-text filter is verbatim.
- **Presidio reference pipeline** (documented glue, not a shipped tool). The "why not just
  Presidio?" baseline: presidio-analyzer 2.2.366 with `en_core_web_lg`, every default entity,
  character spans mapped to PyMuPDF word boxes, real redactions (the text is removed, not
  covered) — `scripts/run_presidio_pipeline.py`.

### Results (175 documents, holdout v4)

| Tool and configuration | Docs with a readable personal string (95% CI) | Protected text lost |
|---|---|---|
| control: unredacted source | 100% | 0.0% |
| control: oracle export | 0% (0-2%) | 0.0% |
| **legal-redact, every suggestion accepted** | **1% (0-4%)**, all 175 released | **4.5%** |
| legal-redact, auto-proposed only (review queue skipped) | 48% (41-56%), 166 released, 9 withheld | 0.7% |
| doc_redaction 2.8.1, shipped defaults | 100% (98-100%) | 1.6% |
| doc_redaction 2.8.1, + IBAN, card, location | 86% (80-90%) | 10.1% |
| ltillmann/pdf-redactor, all built-in detectors | 100% (98-100%) | 27.2% |
| JoshData/pdf-redactor, bundled example filters | 100% (98-100%) | 0.0% |
| Presidio reference pipeline (our glue) | 49% (41-56%) | 30.1% |

Restricting to the categories each tool exposes (a document leaks only if a string of one of
those categories leaked), which removes the penalty for a tool that never claimed a category:

| Categories | legal-redact (accept all) | the other tool |
|---|---|---|
| PERSON, EMAIL, PHONE, ADDRESS | 1% (0-4%) | doc_redaction, shipped defaults: 82% (75-87%) |
| + IBAN, CARD | 1% (0-4%) | doc_redaction, extended: 64% (57-71%) |
| PERSON, PHONE, ADDRESS, IBAN, CARD (no EMAIL) | 1% (0-4%) | doc_redaction, extended: 56% (49-63%) |
| EMAIL, PHONE, IBAN | 0% (0-2%) | ltillmann/pdf-redactor: 96% (92-98%) |
| ID only (SSN-shaped) | 0% (0-2%) | JoshData/pdf-redactor: 49% (42-56%) |
| PERSON, EMAIL, PHONE, ADDRESS, CARD, IBAN, ID | 1% (0-4%) | Presidio reference: 49% (41-56%) |

**About the email column.** doc_redaction leaked 33% of email strings. All 58 were on the reserved
`.test` domain; every `.org` and `.eu` address was redacted. Presidio's email recognizer does not
treat `.test` as an address, and a third of this suite's emails use it, so the number is an
artifact of the synthetic data that penalises Presidio-based tools. ltillmann's regex catches
these addresses where Presidio does not, which is part of why the tools' per-category profiles
differ. The tables above therefore also show the comparison without EMAIL.

### What this does and does not show

- **Recall.** On this suite our accept-every-suggestion mode leaves 1% of documents with a readable
  personal string. The best other configuration is 49% (a Presidio reference pipeline); the two
  regex-only tools and doc_redaction's shipped defaults are at 100%. The restricted table shows the
  same picture when each tool is judged only on categories it claims.
- **Collateral damage is real, and we do not win every column.** Our suggestions touch 4.5% of
  protected text: more than doc_redaction's shipped defaults (1.6%) and JoshData's example filters
  (0%), less than doc_redaction extended (10.1%), ltillmann (27.2%) and the Presidio pipeline
  (30.1%). Most of ours is contract dates read as dates of birth. That is the price of recall, and
  the review queue is how a person avoids paying it.
- **The review queue is not optional.** Accepting only what the policy proposes (skipping the human
  queue) leaves 48% of documents leaking, so the 1% figure assumes every suggestion is accepted. The
  9 withheld exports in that mode are documents where the same text was redacted in one place and
  deliberately left readable in another, which the verifier correctly refuses.
- **Two tools never claimed names at all.** ltillmann and JoshData's example filters are
  structured-identifier tools; their 100% in the full table is a scope statement, not a failure. The
  restricted table is the fair view of them, and even there they leave 96% and 49%.
- **The task differs.** doc_redaction is designed around a human review interface, so its batch
  output is not its intended end state, and its strengths are outside this suite: scans and OCR,
  DOCX, spreadsheets, other languages, review workflows. The Presidio pipeline is glue we wrote, not
  a product, and its 30.1% protected loss is what untuned default entities do to contracts.
- One suite, one run, synthetic data, four other tools, a workaround on one machine. Commercial
  tools and LLM-based redaction remain unmeasured.

Reproduce, each script in an environment that has its tool's dependencies:
`scripts/run_doc_redaction.py IN OUT default|extended`, `scripts/run_ltillmann_pdf_redactor.py IN OUT
TOOL_PDF_REDACTOR_PY TOOL_PYTHON`, `scripts/run_joshdata_pdf_redactor.py IN OUT TOOL_DIR`,
`scripts/run_presidio_pipeline.py IN OUT`; then
`legal-redact bench-h2h --work DIR --external "label=results.json"` for each results file.

### Independent benchmark attempt: `pdfredeval`

The only independent benchmark of PDF redaction tools we found is
[pdfredeval](https://github.com/RedactionTools/pdf-redaction-benchmarks) (AGPL-3.0), which
publishes black-box results for web redaction services. We ran our tool through it at framework and
dataset revision `v0.1.1`, on its generated case `extraction-conditions-1` (one page with the same
invented name planted under 54 rendering conditions: 27 in the text layer, 27 rendered as images)
and on `pii-detection-1` (a form-like page with 86 probes). The adapter is out of tree
(`scripts/pdfredeval_adapter/`, pinned to `pdfredeval==0.1.1`); the tool was neither modified nor
tuned for it.

**The shipped tool cannot be ranked on the published case: it failed closed.** It suggested 71
spans (all policy-routed to review), the batch flow accepted all of them, and the tool's own
verifier then found residual copies of the planted name that no accepted box covered
(pdfminer.six `PERSONx7`, pypdfium2 `PERSONx9`). It refused to release the file. `pdfredeval` has
no "refused" outcome, so there is no leak rate for the shipped tool there: not 0, and not 1.

Two things can be said, both narrowly:

- **Engine only, clearly separated from the product.** Scoring the artefact the verifier withheld
  (`keep_failed`, recorded in the run manifest and never presented as shipped behaviour) on the
  27-probe text-layer scope, the engine leaked 4 of 27 (14.8%, 95% CI 5.9-32.5%). On that same
  27-probe scope the published tools' own channel reach implies pdf-redaction:web 14.8%,
  doc_redaction 2.8.1 44.4%, ai-redact:web 51.9% and saferedact-app:web 51.9%. All four leaks are
  the vertical/stacked text cells. Text retention was 0.672 against the benchmark's 0.9 floor; that
  failure is inflated by the blind accept-all flow described below. These numbers are not
  comparable to the published 54-probe headlines (0.222 / 0.444 / 0.741), which include the 27
  raster probes this tool declares unsupported, and we do not put them side by side.
- **The second case, shipped.** On `pii-detection-1` the verifier passed and the tool released a
  file: 0 of 34 in-scope probes leaked, one of five in-scope distractors was over-redacted (a VAT
  id), 47 probes were out-of-scope categories, and text retention was 0.518. The page is a
  form-like remittance advice with no published counterpart; retention and over-redaction come
  from the blind accept-all flow, which also accepts 105 low-confidence `ID` false positives that a
  reviewer would reject.

The benchmark's published case is deliberately adversarial: one value under 54 renderings,
including text rendered as images and stacked one glyph per line. This tool declares text-layer
input only and fails closed rather than release an export it cannot prove. That is the honest
result: on the part of the independent benchmark the tool actually covers, its engine is level with
the best published tool; as a product it produces no comparable number, and closing that gap needs
OCR/vertical-text support and a batch-operable way to resolve review refusals.

Reproduce: `pip install "pdfredeval[score,report,generate]==0.1.1"`, then
`pip install -e scripts/pdfredeval_adapter`, then `pdfredeval generate all --seed 1` and
`pdfredeval submit legal-redact:desktop extraction-conditions-1` with `LEGAL_REDACT_HOME` set.

### Correction, and a verifier bug the comparison exposed

The first version of this section reported 173 released and 2 withheld (accept all), and 157
released and 18 withheld (auto only), and explained the withheld files as repeated names caught only
once. That explanation was wrong. The verifier built one "redacted text" needle per line box, so a name
wrapped over two lines became two needles; a lone surname then matched the same word in kept text (a
firm name) and a correct redaction was refused. It now builds one needle per redacted decision,
however many lines it spans; a surname surviving elsewhere is a warning, not a failure. A test
(`test_wrapped_name_is_one_needle_...`) failed before the fix and passes after it. The seeded-leak
suite still catches 195 of 195 leaky variants with 0 of 15 clean exports wrongly failed, and it now
builds needles the same way the real export does. The corrected numbers are in the table above.

### Repeat occurrences ("Find repeats")

The review page and `legal-redact redact --accept-repeats` can propose every other occurrence of text
you accepted (case-insensitive, whitespace-flexible, word boundaries; skips text you also rejected
and anything already covered). It is unit-tested, including line-wrapped occurrences and a guard
test that fails when the word-boundary check is removed. **It added nothing on this benchmark**
(0 proposals in every arm): the ensemble already redacts every occurrence in these documents, and
in the auto-only arm the remaining occurrences were rejected on purpose. So there is no
benchmark evidence yet that it helps; it targets a case (inconsistent detection of repeats) these
synthetic documents do not produce.
