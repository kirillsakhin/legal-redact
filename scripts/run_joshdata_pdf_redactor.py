"""Run JoshData/pdf-redactor (CC0) as a library with its bundled example.py filters.

Usage: python run_joshdata_pdf_redactor.py IN_DIR OUT_DIR TOOL_DIR [LIMIT]

The repo's own command-line script is a documented no-op; its README says to use the module
as a library and pass in text-filtering functions written in Python, and points at
`example.py`. We use exactly example.py's filter set: the SSN-shaped content regex, dash
normalisation, the annotation-body filter, the link filter, and the metadata/XMP filters.

One documented deviation: example.py's `"Title": [lambda value: value.upper()]` raises
AttributeError on PDFs whose /Info has no /Title (the synthetic documents here have no
metadata), so the Title filter is made None-safe. Metadata is not read by the black-box
scorer (page text only); every page-text filter is verbatim.

Writes OUT_DIR/<doc_id>/<doc_id>_redacted.pdf and OUT_DIR/results.json, the format
`legal-redact bench-h2h --external "label=results.json"` reads.
Not part of the legal-redact package: it is an external-tool harness.
"""
import importlib.util
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path


def load_tool(tool_dir: Path):
    spec = importlib.util.spec_from_file_location("pdf_redactor", tool_dir / "pdf_redactor.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pdf_redactor"] = module
    spec.loader.exec_module(module)
    return module


def make_options(module):
    options = module.RedactorOptions()
    # ---- verbatim from example.py, except the None-safe Title filter noted above ----
    options.metadata_filters = {
        "Title": [lambda value: value.upper() if value else value],
        "Producer": [lambda value: "My Name"],
        "CreationDate": [lambda value: datetime.utcnow()],
        "DEFAULT": [lambda value: None],
    }
    options.xmp_filters = [lambda xml: None]
    options.content_filters = [
        (re.compile("[\u2212\u2013\u2014~\u2010]"), lambda m: "-"),
        (re.compile(r"(?<!\d)(?!666|000|9\d{2})([OoIli0-9]{3})([\s-]?)(?!00)([OoIli0-9]{2})\2"
                    r"(?!0{4})([OoIli0-9]{4})(?!\d)"),
         lambda m: "XXX-XX-XXXX"),
        (re.compile(r"comment!"), lambda m: "annotation?"),
    ]
    options.link_filters = [lambda href, annotation: "https://www.google.com"]
    return options


def main() -> int:
    in_dir, out_dir, tool_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else None
    out_dir.mkdir(parents=True, exist_ok=True)
    module = load_tool(tool_dir)
    results = {}
    for src in sorted(in_dir.glob("*.pdf"))[:limit]:
        doc_id = src.stem
        work = out_dir / doc_id
        work.mkdir(exist_ok=True)
        out = work / f"{doc_id}_redacted.pdf"
        options = make_options(module)
        t0 = time.perf_counter()
        err = ""
        try:
            with open(src, "rb") as fin, open(out, "wb") as fout:
                options.input_stream = fin
                options.output_stream = fout
                module.redactor(options)
            if not out.exists() or out.stat().st_size == 0:
                err = "no output written"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        results[doc_id] = {"seconds": round(time.perf_counter() - t0, 3),
                           "outputs": [str(out)], "error": err}
        print(doc_id, results[doc_id]["seconds"], err[:80], flush=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    ok = sum(1 for r in results.values() if not r["error"])
    print(f"done: {ok}/{len(results)} outputs written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
