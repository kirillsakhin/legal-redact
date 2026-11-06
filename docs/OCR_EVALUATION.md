# OCR backend evaluation (2026-11)

The user asked whether a different OCR backend would serve the tool better,
pointing at SBP Fleet's `lexie/ocr_bot.py`. This note records what that
implementation does, what could transfer to a local, box-producing,
fail-closed redaction tool, what was measured, and what was adopted.

## What the Fleet implementation is, and what transfers

Fleet's pipeline (per its docstring) renders pages at 300 DPI with Tesseract OSD
auto-rotation, runs five OCR agents in parallel (Claude Vision, Tesseract→LLM,
a local Qwen3-VL VLM, EasyOCR, Yandex Vision) and a three-pass LLM reviewer, and
produces structured spreadsheet/document output. It is an extraction-accuracy
ensemble for Russian legal scans.

It cannot be our backend: it is cloud/LLM-dependent (documents would leave the
host), its VLM/LLM layers can hallucinate text (a hallucinated string makes
"did we remove it?" verification meaningless), and it produces no word boxes in
PDF points, which real redaction needs.

Three cheap ideas were worth measuring (task-10):

1. grayscale + mild contrast preprocessing;
2. Tesseract OSD orientation handling (correct with boxes mapped back, or a
   REVIEW finding -- never a silent mis-read);
3. an adaptive second OCR pass (higher dpi / alternate `--psm`) triggered by
   low mean word confidence or too few words.

Plus one alternative engine: RapidOCR (pip-only, ONNX runtime already ships as
a PyMuPDF 2.1 dependency; checked to install and run on Windows + Python 3.14).

## Method

- Fixtures: the synthetic `loan` contract used by the OCR tests (all text
  invented), rendered at 300 DPI and wrapped in image-only PDFs. 9 planted
  personal values with known truth boxes (`Oskar Kowalczyk`, `4 January 1980`,
  `D88065013`, `82 Station Lane, Northvale, GF8 7TR`,
  `GB53 PSCR 5001 9062 0132 78`, `4513 7823 7732 4327`, `Dmitri B. Sample`,
  `F43343274`, `+49 30 648823`).
- 16 variants: clean; 12 degraded (JPEG q10/q30, blur, hard blur, noise, faded
  contrast, low-dpi upscales, salt-and-pepper, combined); 3 rotated
  (90/180/270 degrees, aspect-preserving landscape/portrait pages as a scanner
  would produce).
- Metrics: **planted recall** (value read, normalized); **box coverage** (share
  of the truth glyph band covered by OCR boxes after the writer's 1.2 pt
  padding); **word IoU** against truth word boxes; **area×** (returned box area
  / truth glyph-band area -- over-redaction); **seconds/page**; install size;
  licence.
- Environment: Windows 11, Python 3.14.7, Tesseract 5.5.3, CPU only. Timings are
  wall-clock on this machine and vary with load; they are comparable within the
  table, not across machines.
- Scripts and the measured result files live in `_ocr_eval/` in the `ocr`
  worktree (evaluation only, not shipped, not needed at runtime): `make_fixtures.py`,
  `eval_tesseract.py`, `eval_adopted.py`, `rapid_raw.py`, `eval_rapidocr.py`,
  `compare.py`, with raw results in `results_*.json` and `table.md`. Fixtures are
  regenerated with `python _ocr_eval/make_fixtures.py` (needs PyMuPDF + Pillow,
  both present in the dev environment). The isolated RapidOCR environment is not
  kept; recreating its rows is `python -m venv`, `pip install rapidocr onnxruntime`,
  then `rapid_raw.py` (models are bundled, ~32 MB; no download at runtime).

## Results

| Engine / config | clean recall | clean cover | word IoU | area× | degraded recall (mean/min) | rot90/180/270 recall | s/page clean | s/page degraded | deps | licence |
|---|---|---|---|---|---|---|---|---|---|---|
| Tesseract 5.5, baseline (RGB, psm3, 300) | 9/9 | 0.95 | 0.62 | 1.33 | 8.58 / 6/9 | 0/0/9 | 0.60 | 1.27 | none (external tesseract) | Apache-2.0 |
| Tesseract 5.5, grayscale | 9/9 | 0.95 | 0.62 | 1.33 | 8.58 / 6/9 | 0/0/9 | 0.43 | 0.67 | none (external tesseract) | Apache-2.0 |
| Tesseract 5.5, grayscale + contrast 1.3 | 9/9 | 0.95 | 0.62 | 1.33 | 8.42 / **5/9** | 0/0/9 | 0.43 | 0.52 | none (external tesseract) | Apache-2.0 |
| Tesseract 5.5, psm6 (single pass) | 9/9 | 0.95 | 0.62 | 1.33 | 8.67 / 6/9 | 0/0/**0** | 0.52 | 1.13 | none (external tesseract) | Apache-2.0 |
| Tesseract 5.5, adaptive second pass (psm6 and 400 dpi when unsure) | 9/9 | 0.95 | 0.62 | 1.33 | 8.58 / 6/9 | 9/9/9 | 0.61 | 1.18 | none (external tesseract) | Apache-2.0 |
| Tesseract 5.5, candidate (grayscale + OSD, no retry) | 9/9 | 0.95 | 0.62 | 1.33 | 8.58 / 6/9 | **9/9/9** | 0.42 | 0.66 | none (external tesseract) | Apache-2.0 |
| **Tesseract 5.5, shipped `ocr.py`** | **9/9** | 0.95 | 0.62 | 1.33 | 8.58 / 6/9 | **9/9/9** | **0.47** | **0.70** | none (external tesseract) | Apache-2.0 |
| RapidOCR 3.10 (PP-OCRv6, word boxes) | 9/9 | 0.96 | 0.87 | 1.94 | **8.83 / 8/9** | 6/6/9* | 3.82 | 2.78 | ≈160 MB (opencv 117 MB, rapidocr+models 33 MB, shapely 7 MB; onnxruntime and numpy are already PyMuPDF deps) | Apache-2.0 |

\* RapidOCR reads rotated pages only partially and (without an OSD wrapper) gives
no valid page coordinates for them: box coverage on rot90/180 was 0.14 with word
IoU 0.04, so its output is unusable for redaction on those pages.

## Decision

**Adopted: grayscale rendering.** Identical accuracy to RGB at 300 DPI, ~30%
faster per page (0.60 → 0.43 s clean) and one third of the image memory
(~26 MB → ~8.7 MB per A4 page at 300 DPI). No dependency, no behaviour change.

**Adopted: OSD orientation handling.** A rotated page used to read as garbage
(mean confidence 29-33 vs 95 upright; 0/9 planted values). When the first read
is unreliable, Tesseract OSD decides the rotation, the page is re-rendered
upright and every word box is mapped back into the original page coordinates;
the correction replaces the first read only when demonstrably better. Result:
0/9 → 9/9 on both 90° and 180° pages, +1.1 s only on those pages. Pre-flight
runs the same OSD check on every page that will be OCR'd and emits a REVIEW
finding either way: PF017 "appears rotated; OCR will correct it" or PF018
"orientation could not be determined reliably; check the page". A page is never
silently mis-read. The 270° case is read correctly by Tesseract without help and
is still flagged PF017 (it is rotated; the reviewer should know).

**Rejected: contrast stretching 1.3x.** It made the hard-blur page worse
(8/9 → 7/9, degraded minimum 6/9 → 5/9) and never improved recall elsewhere.

**Rejected: adaptive second pass.** Across 12 degraded variants it changed no
planted-value recall, 400 dpi was slightly worse (upscaling adds nothing), and
it costs 1-2 extra OCR passes on every suspicious page. Mean word confidence is
also not a sound failure trigger: a `severe_blur` page read at mean confidence
82 still missed 3 of 9 planted values, while rotated garbage sits at ~30. The
review gate, not a retry heuristic, is the control.

**Rejected: RapidOCR as the backend (or an optional extra).** It is accurate
(9/9 clean; the best degraded mean/min, 8.83/8; word boxes available via
`return_word_box=True`) and Apache-2.0, but:

- ~5-6x slower per page (3.8 s clean, 2.8 s degraded vs 0.47/0.70 s) on CPU;
- +≈160 MB of dependencies, including OpenCV 117 MB; `onnxruntime` and `numpy`
  are already PyMuPDF dependencies, but `cv2`, `shapely`, `pyclipper` and the
  RapidOCR package/models are not;
- no orientation handling: 6/9 on rotated pages with unusable coordinates, so it
  would need the same OSD pre/post-processing we already wrote for Tesseract;
- its accuracy edge is concentrated on heavily degraded pages, which pre-flight
  already routes to human review (PF018 review is triggered precisely when the
  orientation/quality signal is bad), and Tesseract actually beat it on the
  noise variants (9/9 vs 8/9).

A second backend remains possible later behind an optional extra if real
client scans show a systematic Tesseract failure mode; nothing in this
evaluation justifies making it the default or a maintained extra now.

**Rejected by design: cloud/LLM OCR.** VLM/LLM layers can hallucinate text, so
"OCR of the output cannot recover the redacted string" would stop meaning
"the string is not in the pixels". The tool's fail-closed guarantee requires a
deterministic local engine.


## A note on engine choice (v0.4.0)

The pluggable-engine feature (`--ocr-engine`, see
[docs/OCR_BACKENDS.md](OCR_BACKENDS.md)) lets a user replace the reader, but it
does not change anything measured above: the default remains the local Tesseract
pipeline evaluated in this note, and the numbers in the table describe that
pipeline only. No comparative accuracy claim is made for any custom or
third-party engine -- this project did not benchmark them, and a cloud engine's
quality is your provider's claim, not this tool's. A custom engine changes what
can be suggested for redaction (a word it does not return cannot be redacted),
and when Tesseract is absent the export verifier can only ask the same engine to
check its own output (`SAME-ENGINE (weaker)`, requires an acknowledgement).

## Measured: refactor parity and adapter overhead (v0.4.0)

The pluggable-engine change set touched the OCR code path, so it was scored on a **new** suite
(ocr-v4-lease7: template lease, seed 7 -- deliberately different from the 0.2.0 suite, which was
spent; 16 scan variants, clean + degraded + rotated, 9 planted personal values; suite fingerprint
sha256 `AF724BE5F27E5508CCBB654AD0E519F67AB1E1EC6A6CAE9367820F96D6E18D54`). The harness and fixtures live in the development workspace
(`_legalredact_publish\ocr_v4_eval\`) and do not ship. Method: run the public
`extract_pages(ocr=True)` on the same 16 image-only PDFs with the v0.3.2 tree and with the v0.4.0
tree, then compare planted-value recall, the returned word list (text + rectangle, rounded to 0.01 pt)
and wall-clock seconds per page.

| | v0.3.2 | v0.4.0 |
|---|---|---|
| Planted-value recall (16 variants x 9) | 127/144 | 127/144 |
| Variants with identical word reads | -- | **16/16** |
| Total OCR time, 16 pages | 13.87 s | 13.96 s |

The default Tesseract backend is byte-for-byte behaviour-preserving: every variant returned the same
words with the same boxes, recall was identical (including the hard cases: `hardblur` 4/9,
`lowdpi80` 8/9, `saltpepper` 7/9, `combined` 0/9 -- the review gate, not a retry, is the control
there), and the total time difference is inside run-to-run noise.

**Adapter overhead.** On one clean page (72 words): default fast path 0.40 s/page; the PNG/protocol
entry point 0.43 s/page; the `command` engine with a trivial wrapper that prints precomputed words
0.055 s/page (best of 5). So the external-adapter path itself (process spawn, PNG handoff, JSON parse)
is tens of milliseconds per page; a real wrapper's own recognition time dominates. A wrapper that also
decodes the image per word measured 0.22 s/page, consistent with that split.

**What this does not say.** No comparative accuracy claim is made for any third-party engine: the
suite above scores the *default* pipeline's refactor. The only alternative engine measured by this
project remains RapidOCR in the 0.2.0 table above -- and that measurement was of its own pipeline, not
through the new adapter; a wrapped engine's quality is whatever its provider delivers.
