"""Run doc_redaction (installed in its own venv) over a folder of PDFs, in one process.

Usage: python dr_driver.py IN_DIR OUT_DIR ARM [LIMIT]
ARM: default  -> the tool's shipped default entity list
     extended -> default + IBAN_CODE, CREDIT_CARD, LOCATION
Writes OUT_DIR/<name>.pdf per input and OUT_DIR/results.json (timing, errors).
Not part of the legal-redact package: it is an external-tool harness.
"""
import json
import sys
import time
import traceback
from pathlib import Path

in_dir, out_dir, arm = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
limit = int(sys.argv[4]) if len(sys.argv) > 4 else None
out_dir.mkdir(parents=True, exist_ok=True)

t0 = time.perf_counter()
from doc_redaction.cli_redact import main  # noqa: E402
print(f"import took {time.perf_counter() - t0:.1f}s", flush=True)

ENT = ["TITLES", "PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "STREETNAME", "UKPOSTCODE", "CUSTOM"]
if arm == "extended":
    ENT += ["IBAN_CODE", "CREDIT_CARD", "LOCATION"]

results = {}
pdfs = sorted(in_dir.glob("*.pdf"))[:limit]
for p in pdfs:
    work = out_dir / p.stem
    work.mkdir(exist_ok=True)
    t = time.perf_counter()
    try:
        # WORKAROUND (documented in the report): doc_redaction 2.8.1 on this machine opens
        # 'placeholder_image_<n>.png' in the working directory and crashes if it is missing
        # (its own example PDF fails the same way). Poppler is not installed, so it never
        # creates the page images. We supply real renders of each page at its own 300 dpi.
        import os
        import pymupdf
        os.chdir(out_dir)
        with pymupdf.open(p) as _d:
            for _n, _pg in enumerate(_d):
                _pg.get_pixmap(dpi=300).save(f"placeholder_image_{_n}.png")
        main(direct_mode_args={
            "task": "redact", "input_file": str(p), "output_dir": str(work), "input_dir": str(in_dir),
            "ocr_method": "Local text", "pii_detector": "Local", "local_redact_entities": ENT,
            "save_logs_to_csv": False, "return_pdf_end_of_redaction": True,
            "efficient_ocr": False,     # the default path crashed on a missing placeholder image
        })
        found = sorted(work.rglob("*.pdf"))
        results[p.stem] = {"seconds": round(time.perf_counter() - t, 2),
                           "outputs": [str(f) for f in found], "error": ""}
    except BaseException as exc:      # SystemExit included: record, never stop the batch
        results[p.stem] = {"seconds": round(time.perf_counter() - t, 2), "outputs": [],
                           "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                           "trace": traceback.format_exc()[-600:]}
    print(p.stem, results[p.stem]["seconds"], results[p.stem]["error"][:80], flush=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=1))
print("done", flush=True)
