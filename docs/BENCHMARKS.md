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
- doc_redaction, commercial tools, LLM-based redaction, TypeSafe Jev, AutoTrust JEV-9B.
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

## Head-to-head with doc_redaction

[doc_redaction](https://github.com/seanpedrick-case/doc_redaction) (AGPL-3.0) is the closest
open-source tool. It was run as a separate program in its own environment (never bundled or
copied), and every output PDF was scored the same way as ours, from the output file only
(`bench/blackbox.py`): a gold personal-data string still extractable by pdfminer.six or pypdfium2
is a leak; a protected string no longer extractable is lost. Controls in the same table: the
unredacted source (must leak everything, and does: 100%) and an oracle export built from the gold
labels (must leak nothing and lose nothing, and does: 0%).

**This is a home-field comparison.** The categories, formats and layouts are ours, and our
detectors were developed on documents of the same family. doc_redaction got no tuning at all.
Treat the gap as an upper bound on any real advantage.

Setup, exactly: PyPI release 2.8.1 (GitHub `main` was 2.9.0), Python 3.14, PyMuPDF 1.28.2, `Local text`
extraction (the PDFs have selectable text) and local Presidio with `en_core_web_lg`, no OCR, no
human review, 175 holdout-v4 documents. Two configurations: its shipped default entity list
(`TITLES, PERSON, PHONE_NUMBER, EMAIL_ADDRESS, STREETNAME, UKPOSTCODE, CUSTOM`) and that list plus
`IBAN_CODE, CREDIT_CARD, LOCATION`. Its command line exposes no entity for IDs or dates of birth.

**A workaround was needed.** On this machine the documented command fails, including on the tool's
own example PDF: it opens `placeholder_image_<n>.png` in the working directory and crashes when
the file is missing (Poppler, which it expects, is not installed, and I did not download
third-party binaries). We supplied a real 300 dpi render of each page under that name
(`scripts/run_doc_redaction.py`). Output boxes were checked by eye and sit correctly over the text.
A proper install of its prerequisites might behave differently.

### Results (175 documents, holdout v4)

| Tool and mode | Docs with a readable personal string (95% CI) | Protected text lost |
|---|---|---|
| control: unredacted source | 100% | 0.0% |
| control: oracle export | 0% (0-2%) | 0.0% |
| legal-redact, accept every suggestion | 1% (0-4%), all 175 released | 4.5% |
| legal-redact, auto-proposed only (review queue skipped) | 48% (41-56%), 166 released, 9 withheld | 0.7% |
| doc_redaction, shipped defaults | 100% (98-100%) | 1.6% |
| doc_redaction, + IBAN, card, location | 86% (80-90%) | 10.1% |

Restricting to the categories each tool exposes (a document leaks only if one of those leaked):

| Categories | legal-redact (accept all) | doc_redaction |
|---|---|---|
| PERSON, EMAIL, PHONE, ADDRESS | 1% | 82% (75-87%), shipped defaults |
| + IBAN, CARD | 1% | 64% (57-71%), extended |
| PERSON, PHONE, ADDRESS, IBAN, CARD (no EMAIL) | 1% | 56% (49-63%), extended |

**About the email column.** doc_redaction leaked 33% of email strings. All 58 were on the reserved
`.test` domain; every `.org` and `.eu` address was redacted. Presidio's email recognizer does not
treat `.test` as an address, and a third of this suite's emails use it, so the number is an
artifact of the synthetic data that penalises Presidio-based tools. The tables above therefore
also show the comparison without EMAIL.

### What this does and does not show

- doc_redaction here behaves much like Presidio (large model) alone, which we had already measured:
  names recall about 0.8 to 0.9, no address/DOB/ID recognizers in the default setup. What we add is
  GLiNER, structured-identifier rules with checksums, conflict resolution, consensus review and
  the verifier. So the gap says "layering these detectors recalls more on this data", which we
  expected, not "a better tool in general".
- Our figure is for accept-every-suggestion with no human decisions, the closest match to a batch
  tool. It costs more suggestions to read. Skipping our review queue leaves 48% of documents leaking,
  so the queue is not optional. The 9 withheld exports in that mode are documents where the same
  text was redacted in one place and deliberately left readable in another (a queue item that was
  skipped), which the verifier correctly refuses.
- doc_redaction is designed around a human review interface, so its batch output is not its intended
  end state. Its strengths are outside this suite: scans and OCR, DOCX, spreadsheets, other
  languages, redaction review workflows.
- Our protected-text loss (4.5%) is between its two configurations (1.6% and 10.1%).
- One suite, one run, synthetic data, one tool, version 2.8.1, a workaround on one machine.
  Commercial tools and LLM-based redaction remain unmeasured.

Reproduce: run `scripts/run_doc_redaction.py` with doc_redaction's own Python environment over the
pinned PDFs, then `legal-redact bench-h2h --work DIR --external "label=results.json"`.

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
