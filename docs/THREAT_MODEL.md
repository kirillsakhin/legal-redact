# Threat model (v0.1, Phase 1)

What can go wrong when a lawyer shares a redacted PDF, and what this tool does
about it. Status column: **designed** = planned for a later phase, **tested** =
covered by an automated test today.

## Assets
Personal data of natural persons inside the PDF (names, contact details, IDs,
bank details, dates of birth), in any place the PDF can hold it.

## Failure modes

| # | Failure | Example | Mitigation | Status |
|---|---|---|---|---|
| T1 | Cosmetic redaction | Black box drawn over live text | Content is removed from the page stream by the writer, then re-checked from the output file | designed (Phase 5) |
| T2 | Detector false negative | Unusual name not suggested | Human review of every page; benchmark reports misses by category; no page is ever skipped automatically | designed |
| T3 | Text lives outside the visible layer | Hidden/white text, off-page text | Verifier scans every text run, not just visible text | designed |
| T4 | Text lives outside page content | Metadata, XMP, bookmarks, annotations, form values, attachments, JavaScript, layers | Preflight flags or refuses; export strips metadata | preflight **tested**; stripping designed |
| T5 | Text lives in pixels | Scans, signatures, screenshots | Refuse image-only files; flag large or any embedded images for manual review. No OCR in v1 | **tested** |
| T6 | Leftover bytes | Old revisions, unused objects | Output is written as a fresh, garbage-collected file; raw bytes scanned | designed |
| T7 | Verifier blind to its own writer's bug | Same library writes and checks | Verifier uses different libraries (pdfminer.six, pypdfium2, pikepdf) | designed |
| T8 | Leakage through our own logs | Report quotes the name | Reports and logs carry codes, categories, page numbers and hashes only; test scans output for canary strings | CLI inspect **tested** |
| T9 | Network exfiltration | Cloud model call | Offline by default; any hosted model is opt-in with a warning | designed |
| T10 | False sense of anonymity | Name removed, role + company still identifies | Reports state plainly that redaction is not anonymisation; indirect identification is a human judgment | designed (README/report) |

## Out of scope for v1
OCR and scans, DOCX, non-English text, digital signatures (redaction invalidates
them), a legal conclusion that a document is anonymous or GDPR-compliant.

## Refuse vs review
- **Refuse** (export blocked): encrypted, no extractable text, embedded files, XFA forms.
- **Review** (lawyer must acknowledge): images, annotations, form fields, JavaScript, layers, bookmarks, pages without text.
