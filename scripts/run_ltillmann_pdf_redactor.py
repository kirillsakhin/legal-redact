"""Run ltillmann/pdf-redactor (MIT) as a black box over a folder of PDFs.

Usage: python run_ltillmann_pdf_redactor.py IN_DIR OUT_DIR TOOL_PDF_REDACTOR_PY TOOL_PYTHON [LIMIT]

The tool is run as a separate program, in its own environment, exactly as its README shows:
one subprocess per document, its shipped CLI, every built-in detector flag it documents
enabled, default settings otherwise:

    TOOL_PYTHON pdf_redactor.py -i IN.pdf -o OUT.pdf -e -l -p -s -b -d -f -q -r --quiet

No custom -m text patterns (none are shipped), no -g geographic hint, default colours.
Writes OUT_DIR/<doc_id>/<doc_id>_redacted.pdf and OUT_DIR/results.json, the format
`legal-redact bench-h2h --external "label=results.json"` reads.
Not part of the legal-redact package: it is an external-tool harness.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

FLAGS = ["-e", "-l", "-p", "-s", "-b", "-d", "-f", "-q", "-r", "--quiet"]


def main() -> int:
    in_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    tool_py, tool_python = Path(sys.argv[3]), Path(sys.argv[4])
    limit = int(sys.argv[5]) if len(sys.argv) > 5 else None
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for src in sorted(in_dir.glob("*.pdf"))[:limit]:
        doc_id = src.stem
        work = out_dir / doc_id
        work.mkdir(exist_ok=True)
        out = work / f"{doc_id}_redacted.pdf"
        t0 = time.perf_counter()
        err = ""
        try:
            proc = subprocess.run(
                [str(tool_python), str(tool_py), "-i", str(src), "-o", str(out)] + FLAGS,
                capture_output=True, text=True, timeout=180)
            if proc.returncode != 0:
                err = f"exit {proc.returncode}: " + (proc.stderr or proc.stdout or "").strip()[-400:]
            elif not out.exists() or out.stat().st_size == 0:
                err = "no output written"
        except Exception as exc:                        # timeout, OS error
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
