"""Check a redacted output for the planted test values.

Usage:
    python scripts/check_planted_values.py testdocs/planted.json OUT [OUT ...]

`planted.json` is written by scripts/make_test_documents.py:
    {"must_be_removed": {label: value, ...}, "must_survive": {label: value, ...}}

For a PDF it reads the text layer; if there is none (a rasterised scan output) it
OCRs the pages when Tesseract is available, so the check still means something.
For a DOCX it scans the extracted text *and* the raw bytes of every package part.

Exit code 0 = none of the "must_be_removed" values found; 1 = at least one found.
Missing "must_survive" values are reported as over-redaction warnings (they do
not fail the check: a reviewer may have chosen to reject a protected lookalike).
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path


def _pdf_text(path: Path) -> tuple[str, str]:
    """Return (text, note). OCRs pages without a text layer when possible."""
    import pymupdf

    parts: list[str] = []
    note = ""
    doc = pymupdf.open(path)
    try:
        for page in doc:
            parts.append(page.get_text("text"))
        if not "".join(parts).strip():
            note = "no text layer"
            try:
                from legal_redact.ocr import ocr_available, ocr_page
                if ocr_available():
                    parts = [ocr_page(page).text for page in doc]
                    note = "no text layer (checked by OCR)"
                else:
                    note = "no text layer and OCR unavailable: inspect visually"
            except ImportError:
                note = "no text layer (the legal-redact package is not importable): inspect visually"
    finally:
        doc.close()
    return "\n".join(parts), note


def _docx_text(path: Path) -> str:
    """Extracted text plus the raw bytes of every XML/relationship part."""
    chunks: list[str] = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            data = z.read(name)
            if name.endswith((".xml", ".rels")):
                chunks.append(data.decode("utf-8", "ignore"))
            else:
                # part names are recoverable metadata too
                chunks.append(name)
    return "\n".join(chunks)


def check(planted: dict, path: Path) -> int:
    must_go = planted.get("must_be_removed", {})
    must_stay = planted.get("must_survive", {})
    suffix = path.suffix.lower()
    note = ""
    if suffix == ".pdf":
        text, note = _pdf_text(path)
    elif suffix in (".docx", ".docm", ".dotx"):
        text = _docx_text(path)
    else:
        print(f"  {path.name}: unsupported format, skipped")
        return 0

    leaked = [f"{label}={value!r}" for label, value in must_go.items() if value in text]
    missing = [f"{label}={value!r}" for label, value in must_stay.items() if value not in text]
    status = "FAIL" if leaked else "PASS"
    extra = f" ({note})" if note else ""
    print(f"  {status} {path.name}{extra}")
    if leaked:
        print("      recoverable planted values (must be removed): " + "; ".join(leaked))
    if missing:
        print("      protected lookalikes no longer found (possible over-redaction): "
              + "; ".join(missing))
    return 1 if leaked else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("planted", help="planted.json from scripts/make_test_documents.py")
    ap.add_argument("outputs", nargs="+", help="redacted file(s) to check")
    args = ap.parse_args(argv)
    planted = json.loads(Path(args.planted).read_text(encoding="utf-8"))
    failures = 0
    for name in args.outputs:
        path = Path(name)
        if not path.exists():
            print(f"  MISSING {path}")
            failures += 1
            continue
        failures += check(planted, path)
    print("leak check:", "FAILED - a planted value survived redaction" if failures
          else "passed - no planted value survived")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
