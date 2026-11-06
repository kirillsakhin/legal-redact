# Audit corpus fixtures (image path)

These `.docx` files are **synthetic** fixtures copied from the v0.5.0 independent
audit corpus (`_legalredact_publish/docx_audit_v05/fixtures`, built by
`scripts/imgfix.py` with Pillow). They contain no real personal data: every
planted value is a canary such as `Alice Example` / `alice@example.test`.

Only the fixtures needed as regression tests for the audit findings F1-F5 are
copied here:

| File | Why it is kept |
|---|---|
| `I03_metadata_only_image.docx` | blank image with EXIF GPS / XMP author / ICC metadata (F4: must be re-encoded metadata-free with pixels preserved) |
| `I06_animated_gif.docx` | animated GIF, frames must be refused (F2/F5) |
| `I10_low_contrast_text.docx` | low-contrast but valid text: ink evidence must accept it and the export must pass (round-2 regression) |
| `I18_animated_gif_pii_frame2.docx` | animated GIF whose second frame carries PII (F2/F5) |
| `I19_single_frame_gif.docx` | single-frame GIF: accepted and normalised to PNG (F5 boundary) |
| `I20_animated_png_pii_frame2.docx` | APNG with a blank first frame and PII in frame 2 (F2) |
| `I21_rotated45_text.docx` | 45-degree rotated text the default engine may not read (F3) |
| `I22_tiny_text.docx` | ~9 px text the default engine may not read (F3) |
| `I23_gif_pii_first_frame.docx` | animated GIF with PII in frame 1 and a blank last frame (F2) |

Tests drive these through the public pipeline with the shipped fake command
engine, so CI needs no Tesseract, Word or LibreOffice. The audit report and
evidence are in `_legalredact_publish/docx_audit_v05/audit_report.md`.
