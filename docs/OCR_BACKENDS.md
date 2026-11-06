# Pluggable OCR engines

Status: draft for v0.4.0. The default OCR engine is the local Tesseract
pipeline; this document describes the optional `command` engine that lets you
plug in your own reader, including a paid third-party service through a wrapper
you write. Engine choice is a compatibility/choice feature: it does not change
the numbers measured in [OCR_EVALUATION.md](OCR_EVALUATION.md), and this project
makes no comparative accuracy claim about any engine.

## What the two engines are

| engine | what runs | where pages go | requirements |
|---|---|---|---|
| `tesseract` (default) | the measured local pipeline in `legal_redact/ocr.py`: grayscale render, Tesseract OSD orientation correction, TSV word boxes | nowhere; pages stay in memory on this machine | Tesseract 5.x with `eng` + `osd` training data |
| `command:<cmdline>` | an executable you configure, run once per page | wherever your wrapper sends them | your wrapper and any SDK/credentials it needs |

The rest of the pipeline (detectors, review gate, destructive writer, fail-closed
verifier) does not change: both engines return the same word/offset/box model,
and the PDF text-layer path and the DOCX path are untouched.

## Command line

```text
# default: local Tesseract
legal-redact inspect contract.pdf

# your own local wrapper (it never leaves the machine)
legal-redact inspect contract.pdf --ocr-engine "command:python my_ocr.py" --ocr-local

# a third-party/cloud service through your wrapper: two gates, see below
legal-redact inspect contract.pdf \
  --ocr-engine "command:python cloud_ocr.py" --allow-cloud-ocr

# equivalent environment forms
$env:LEGAL_REDACT_OCR_ENGINE = "command:python my_ocr.py"
$env:LEGAL_REDACT_OCR_LOCAL = "1"
```

`--ocr-engine` and `--ocr-command` exist on `inspect`, `suggest`, `redact` and
`review`. `--ocr-command "python my_ocr.py"` is shorthand for
`--ocr-engine "command:python my_ocr.py"`. `--no-ocr` still means no OCR at all.

A command engine is treated as **non-local (third-party/cloud) unless you declare
it local** with `--ocr-local` (or `LEGAL_REDACT_OCR_LOCAL=1`), because the safe
assumption is that page images could leave the machine. The command is split into
argv without a shell: double quotes group one argument (Windows paths with
spaces work), and no variable/glob/pipe expansion ever happens.

## The JSON contract

The pipeline renders one page to PNG (RGB, display orientation, at 300 dpi by
default) and runs:

```text
<your argv...>  <page.png>  <page_width_pt>  <page_height_pt>  <dpi>
```

The same three numbers are also in the environment:
`LEGAL_REDACT_OCR_PAGE_WIDTH_PT`, `LEGAL_REDACT_OCR_PAGE_HEIGHT_PT`,
`LEGAL_REDACT_OCR_DPI`. The wrapper must write **exactly one JSON object** to
stdout and exit 0. stderr is ignored (and never copied into reports).

| field | required | meaning |
|---|---|---|
| `words` | yes | array of word objects, in reading order; `[]` means a blank page |
| `words[].text` | yes | non-empty string (the recognised word) |
| `words[].box` | yes | `[x0, y0, x1, y1]` in a **0..1 top-left page frame**: `0 <= x0 < x1 <= 1`, `0 <= y0 < y1 <= 1` |
| `words[].confidence` | yes | number in 0..1 |
| `local` | yes | boolean: true if nothing left the machine, false if the wrapper used a remote service. Must agree with the declared locality (`--ocr-local`), or the page is rejected |
| `engine` | no | your engine name (default `"command"`), shown in reports |
| `version` | no | your engine version, shown in reports |
| `rotation` | no | degrees of orientation correction your wrapper applied (0/90/180/270 or null) |
| `meta` | no | small diagnostic object (scalars only); never put in a report |

Boxes are already in the page frame the pipeline will redact in: your wrapper is
responsible for orientation (if the page is rotated, rotate the image and map the
boxes back before answering). A word box may be generous (a whole line is safe
but redacts extra); it must never be smaller than the word it claims.

### Complete example

A wrapper that reads the PNG with your own local engine:

```python
#!/usr/bin/env python
"""Minimal OCR-engine wrapper. Usage: python my_ocr.py <png> <w> <h> <dpi>."""
import json
import sys

if len(sys.argv) < 5:
    sys.exit("usage: my_ocr.py <png> <width_pt> <height_pt> <dpi>")
png, width, height, dpi = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])

# ... call your OCR here. The page image is png; width/height are PDF points.
# It must return, in reading order, dicts with text, box (0..1 L/T/R/B) and score.
raw_words = my_engine.read(png)      # your code

words = [{"text": w.text,
          "box": [w.x0 / width, w.y0 / height, w.x1 / width, w.y1 / height],
          "confidence": w.score}
         for w in raw_words]
print(json.dumps({"engine": "my-engine", "version": my_engine.version(),
                  "local": True, "rotation": 0, "words": words}))
```

Run it with `--ocr-engine "command:python my_ocr.py" --ocr-local`.

### Paid third-party engine wrapper (Azure DI / AWS Textract / Google Vision style)

A cloud wrapper is the same contract, with `local: false`, and you provide the
SDK and credentials (this project ships none and makes no network call itself):

```python
#!/usr/bin/env python
"""Cloud OCR wrapper. You supply the SDK, endpoint and credentials."""
import json
import sys
import os
from pathlib import Path

png, width, height, dpi = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])

# Example shape only: every provider's call differs.
from azure.ai.documentintelligence import DocumentIntelligenceClient   # user-installed
from azure.core.credentials import AzureKeyCredential

client = DocumentIntelligenceClient(os.environ["AZURE_DI_ENDPOINT"],
                                    AzureKeyCredential(os.environ["AZURE_DI_KEY"]))
with open(png, "rb") as fh:
    poller = client.begin_analyze_document("prebuilt-read", body=fh)
result = poller.result()

words = []
for page in result.pages:
    for word in page.words:
        x0, y0, x1, y1 = word.polygon[0], word.polygon[1], word.polygon[4], word.polygon[5]
        words.append({"text": word.content,
                      "box": [x0 / width, y0 / height, x1 / width, y1 / height],
                      "confidence": word.confidence})
print(json.dumps({"engine": "azure-document-intelligence",
                  "version": "2024-11-30", "local": False, "words": words}))
```

Run it with `--ocr-engine "command:python cloud_ocr.py" --allow-cloud-ocr`, then
acknowledge the cloud finding per file before export (see the next section).

### Local RapidOCR wrapper (no new mandatory dependency)

RapidOCR is not installed or supported by this project (it was evaluated and
rejected as the default in [OCR_EVALUATION.md](OCR_EVALUATION.md): ~5-6x slower,
+~160 MB of dependencies, no orientation handling). If you want it anyway,
install it yourself in the same environment and wrap it:

```python
# pip install rapidocr onnxruntime   (your choice; not a dependency of legal-redact)
import json, sys
from rapidocr import RapidOCR

png, width, height, dpi = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
res = RapidOCR()(png, return_word_box=True)
words = []
for line in res.word_results or []:
    for text, score, box in line:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        words.append({"text": text,
                      "box": [min(xs) / width, min(ys) / height,
                              max(xs) / width, max(ys) / height],
                      "confidence": float(score)})
print(json.dumps({"engine": "rapidocr", "version": "3.10", "local": True,
                  "rotation": 0, "words": words}))
```

Use `--ocr-local`: its ONNX models run on this machine. It returns an empty list
when it finds nothing, including on rotated pages it cannot read -- the pipeline
cannot tell its blindness from a blank page, so treat a custom engine's quality
as a review item (PF015/PF016) and keep using the default Tesseract unless you
have a reason.

## Cloud/third-party OCR: what you are agreeing to

> Third-party (cloud) OCR is selected. Page images of this document will be sent to an external OCR
> service configured by you, outside this machine. That processing can result in the document being
> compromised: third-party access, retention and logging, data location and onward transfers,
> interception, or a provider breach. Do not use cloud OCR for privileged, confidential or client
> documents unless your firm has approved the service, the transfer is lawful (for example a
> suitable data processing agreement is in place) and you are authorised to share the document. The
> local Tesseract default does not carry this risk.

This exact text is printed as a prominent block on stderr by every command that
may OCR with a non-local engine (including `inspect`), is the message of the
pre-flight REVIEW finding `PF019`, and appears as a warning banner on the review
page (together with an explicit approval checkbox for that finding).

It is **doubly gated**:

1. **Engine gate (per run).** A non-local engine is refused before any page is
   rendered or sent unless the invocation passes `--allow-cloud-ocr` (or
   `LEGAL_REDACT_ALLOW_CLOUD_OCR=1`). The refusal names this document and the
   flag; a wrapper cannot be invoked, and no page image is produced. The
   approval is not remembered between runs: every invocation that may OCR with
   the cloud engine must carry it.
2. **Per-file gate (per document).** With the run approved, pre-flight emits
   REVIEW `PF019` with the verbatim disclaimer. Export stays blocked until the
   lawyer acknowledges that code (CLI `--ack PF019`, or the explicit checkbox on
   the review page: *"I approve sending this document's page images to the
   third-party OCR service (cloud processing may compromise the document)"*).

The decision file records the engine, its version, and whether it was local
(`ocr: {"engine", "version", "local", "cloud"}`). Export refuses when the engine
or its declared locality changed, and a cloud-pinned decision file still needs
`--allow-cloud-ocr` on the exporting invocation.

The local Tesseract default sends nothing anywhere; this whole section applies
only if you choose a cloud wrapper.

## Safety: what the engine layer guarantees, and what it cannot

- **Strict validation, fail closed.** Invalid JSON, a missing/non-boolean
  `local`, non-finite numbers, a box outside 0..1 or with `x0 >= x1`, confidence
  outside 0..1, empty text, an oversized payload, a nonzero exit, a timeout
  (180 s default in the CLI path), or a `local` answer that contradicts the
  declared locality: all of these are errors. A failed read is never treated as
  "no text" and never becomes a pass.
- **No shell.** The command is split into argv and executed directly, so page
  paths and flags cannot inject shell syntax. stdout is capped and parsed; the
  wrapper's stderr is never copied into a report (it could contain document text).
- **Engine choice does not bypass verification.** Detection is only as good as
  the reader: a word the engine does not return cannot be suggested for
  redaction (pre-flight says so: PF015/PF016). The verifier still checks every
  redaction; for image pages it re-OCRs the OUTPUT.
- **Cross-check preference.** The export verifier prefers the local Tesseract
  reader whenever Tesseract exists, even if a custom engine produced the
  suggestions -- that is an independent check. If Tesseract is absent, the
  verifier can only ask the same custom engine to check its own output; that is
  labelled `SAME-ENGINE (weaker)` in the report, requires a pre-flight REVIEW
  acknowledgement (`PF021`), and it never turns a FAIL into a PASS.
- **Orientation.** With Tesseract installed, its OSD orientation check runs in
  pre-flight even when a custom engine does the OCR (PF017/PF018 as usual).
  Without Tesseract the pipeline cannot machine-check orientation, so it emits
  REVIEW `PF023` and the wrapper must return boxes already in the upright page
  frame.
- **Pinning.** `suggest` records the engine in the decision file. `redact`
  refuses (no output) if the current engine spec or declared locality differs, or
  if the document has OCR pages and the decision file predates the pin: re-run
  `suggest` with the engine you want. Boxes made by one reader are not silently
  reinterpreted by another. The spec is compared literally, so use the same
  command string at suggest and export (a relative path that resolves differently
  after a `cd` counts as a different engine).
- **Not covered.** This feature makes no accuracy claim about any engine and
  does not audit what a cloud provider does with the images; that is your
  contract with the provider. A malicious wrapper is code you chose to run: it
  receives the document's page image by design.

See [THREAT_MODEL.md](THREAT_MODEL.md) (T31-T36) for the failure-mode table and
[OCR_EVALUATION.md](OCR_EVALUATION.md) for the measured Tesseract numbers.
