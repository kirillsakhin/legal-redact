# Threat model (v0.1)

What can go wrong when a lawyer shares a redacted PDF, and what this tool does about it.
"Tested" means an automated test or the seeded-leak suite (`legal-redact bench-export`) covers
it. It does not mean the route is proven closed: the suite only covers routes we thought of.

## Assets
Personal data of natural persons inside the PDF (names, contact details, IDs, bank details,
dates of birth), in any place the PDF can hold it, and the confidential document itself while the
review page is open.

## Failure modes

| # | Failure | Example | Mitigation | Status |
|---|---|---|---|---|
| T1 | Cosmetic redaction | Black box drawn over live text | The writer deletes the content under each box; the verifier re-reads the output file with other libraries | tested (cosmetic and misaligned-box leaks caught) |
| T2 | Detector false negative | Unusual name not suggested | Human review of every page; benchmark reports misses by category; no page is ever skipped automatically | **open by nature**: 2% of synthetic documents still had a missed span; real-document rate unknown |
| T3 | Text outside the visible layer | White text, invisible text (render mode 3) | Verifier searches all extractable text with two readers; writer removes hidden text | tested |
| T4 | Text outside page content | Metadata, XMP, bookmarks, annotations, form values, attachments, JavaScript, links, thumbnails | Pre-flight refuses or flags; writer scrubs; verifier checks containers and scans every decoded object | tested |
| T5 | Text in pixels | Scans, signatures, screenshots | Refuse image-only files; flag images for manual review. No OCR. Images are never inspected | pre-flight tested; **content of images unchecked** |
| T6 | Leftover bytes | Old revisions, unused objects | Fresh garbage-collected file; verifier rejects extra revisions and scans raw objects | tested (incremental save caught); unused-object coverage limited to the scan |
| T7 | Verifier blind to the writer's bug | Same library writes and checks | Verifier uses pdfminer.six, pypdfium2, pikepdf; writer uses PyMuPDF | by design; shared PDF parsing bugs are possible |
| T8 | Leakage through our logs and reports | Report quotes a name | Reports, logs and decision files carry categories, page numbers, boxes and counts only | tested (canary checks on report, decisions, API state) |
| T9 | Network exfiltration | Cloud model call | No hosted model is implemented; detectors run locally. Model weights download from Hugging Face on first use | by design; first-run download is a network call by the model libraries |
| T10 | False sense of anonymity | Name removed, role plus company still identifies | Reports and README state that redaction is not anonymisation; indirect identification is a human judgment | documented only |
| T11 | Review page exposed to other software | Another web page or process calls the local API | Binds to 127.0.0.1, per-run token, Host-header check (DNS rebinding), strict CSP, no document text in API | tested (token, host, headers, no text); does not defend against other software running as the same user |
| T12 | Writer damages kept text | MuPDF shifts text after a removed word under the box | Removal starts on the preceding space; verifier warns on layout shift and on kept characters inside a box | partially fixed; line-start redactions can still shift text (warns) |
| T13 | Output path abuse | Export overwrites the source | Refuses output equal to the source; temp file then rename; file withheld when verification fails | tested |

## Out of scope for v0.1
OCR and scans, DOCX, non-English text, digital signatures (redaction invalidates them), a legal
conclusion that a document is anonymous or GDPR-compliant.

## Refuse vs review
- **Refuse** (export blocked): encrypted, no extractable text, embedded files, XFA forms.
- **Review** (lawyer must acknowledge): images, annotations, form fields, JavaScript, layers,
  bookmarks, pages without text.
