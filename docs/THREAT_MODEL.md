# Threat model (v0.2)

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
| T5 | Text in pixels | Scans, signatures, screenshots | Pages with no usable text layer are OCR'd locally (English) and rebuilt as image-only pages with the boxes burnt into the pixel data; images on pages that DO have a text layer are still flagged for manual review. OCR cannot read handwriting, stamps, signatures or photos | tested for printed scans (clean scan passes, unredacted scan and under-covering boxes refused); **handwriting/photos unchecked by nature** |
| T6 | Leftover bytes | Old revisions, unused objects | Fresh garbage-collected file; verifier rejects extra revisions and scans raw objects | tested (incremental save caught); unused-object coverage limited to the scan |
| T7 | Verifier blind to the writer's bug | Same library writes and checks | Verifier uses pdfminer.six, pypdfium2, pikepdf; writer uses PyMuPDF | by design; shared PDF parsing bugs are possible |
| T8 | Leakage through our logs and reports | Report quotes a name | Reports, logs and decision files carry categories, page numbers, boxes and counts only | tested (canary checks on report, decisions, API state) |
| T9 | Network exfiltration | Cloud model call | No hosted model is implemented; detectors run locally. Model weights download from Hugging Face on first use | by design; first-run download is a network call by the model libraries |
| T10 | False sense of anonymity | Name removed, role plus company still identifies | Reports and README state that redaction is not anonymisation; indirect identification is a human judgment | documented only |
| T11 | Review page exposed to other software | Another web page or process calls the local API | Binds to 127.0.0.1, per-run token, Host-header check (DNS rebinding), strict CSP, no document text in API | tested (token, host, headers, no text); does not defend against other software running as the same user |
| T12 | Writer damages kept text | MuPDF shifts text after a removed word under the box | Removal starts on the preceding space; verifier warns on layout shift and on kept characters inside a box | partially fixed; line-start redactions can still shift text (warns) |
| T13 | Output path abuse | Export overwrites the source | Refuses output equal to the source; temp file then rename; file withheld when verification fails | tested |
| T14 | OCR misread | Tesseract reads "Kowalczyk" as "Kowalcyk", or drops a low-contrast name | Low-confidence words are KEPT (dropping them would hide the text that most needs review); the review page shows the original page image beside each suggestion; pre-flight states that a name OCR cannot read cannot be redacted (PF015) and every OCR page is a REVIEW finding (PF016); the decision file stores boxes, so export re-OCRs the source and searches for exactly the text the user saw | **open by nature**: a character no OCR pass reads is invisible to both detection and the export check. The control is the human comparing the page image |
| T15 | Rasterised page recoverable | A black rectangle drawn over the untouched scan, or a box too small to cover the word, leaves the original pixels in the file and in the image | The writer burns the boxes into the pixel samples before encoding and rebuilds the page from that single image; the verifier checks there is no text layer (V13), that the rebuilt page has exactly one full-page image whose box areas are dark in the extracted pixel data (V14), and that OCR of the output cannot recover a redacted string, refusing on a name fragment too (V15) | tested: vector-overlay writer, copy-without-redaction writer and under-covering boxes are all refused; clean scan passes |
| T16 | Searchability and interoperability lost | A rasterised page has no text: not searchable, not selectable, invisible to text-based downstream tools, and the file is larger than a text PDF | Pre-flight says so (PF015); README states the measured size trade-off; the verifier checks the page really has no text layer (V13) and warns if the content that should have survived is no longer readable (V16) | documented and tested; by design, a scan has to be an image if its original was one |
| T17 | OCR text leaking through our own artefacts | The recognised words end up in a report, decision file or the review API | OCR words stay in memory as needles; decision files store offsets and boxes only; reports carry categories, counts and page numbers | tested (scan decision round-trip and report checked for document text) |
| T18 | Mis-oriented scan | A sideways or upside-down page: OCR reads garbage (measured mean confidence ~30 vs ~95 upright), so nothing is redacted while the page looks reviewed | When a read looks unreliable, Tesseract OSD decides the rotation, the page is re-read upright and every box is mapped back to the original page coordinates; pre-flight runs the same OSD check on every page to be OCR'd and emits a REVIEW finding: PF017 (rotated; auto-corrected) or PF018 (orientation undeterminable; check the page yourself). No page is silently mis-read | tested: 90/180-degree scans went 0/9 -> 9/9 planted values; mutation test disables OSD and shows recall collapses; PF017/PF018 covered by tests |

## Out of scope for v0.2
DOCX, non-English OCR, handwriting and signature detection, detecting text inside pictures on a
page that has its own text layer, digital signatures (redaction invalidates them), a legal
conclusion that a document is anonymous or GDPR-compliant.

## Refuse vs review
- **Refuse** (export blocked): encrypted, no extractable text when OCR is unavailable or disabled
  (`--no-ocr`), embedded files, XFA forms.
- **Review** (lawyer must acknowledge): OCR use (PF003/PF004), what OCR cannot see (PF016),
  rotated scans it will correct (PF017), scans whose orientation could not be determined (PF018),
  images on text pages, annotations, form fields, JavaScript, layers, bookmarks.
