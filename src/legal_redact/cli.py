"""Command line entry point. Phase 1 ships only `inspect`.

Output never contains document text; only counts, codes and page numbers.
"""
from __future__ import annotations

import argparse
import sys

import pymupdf

from .extract import extract_pages
from .preflight import blocked, preflight


def _inspect(path: str) -> int:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # corrupt or not a PDF
        print(f"REFUSE PF000 cannot open file as PDF ({type(exc).__name__})")
        return 2
    findings = preflight(doc)
    print(f"pages={doc.page_count}")
    if not (doc.needs_pass or doc.is_encrypted):
        pages = extract_pages(doc)
        print(f"words={sum(len(p.words) for p in pages)}")
    for f in findings:
        where = f" p{f.page + 1}" if f.page is not None else ""
        print(f"{f.severity.value.upper():7} {f.code}{where}: {f.message}")
    if blocked(findings):
        print("RESULT: export would be refused")
        return 1
    print("RESULT: supported (review the findings above)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="legal-redact")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("inspect", help="pre-flight check of a PDF")
    ins.add_argument("pdf")
    bn = sub.add_parser("bench", help="run the synthetic benchmark")
    bn.add_argument("--detector", nargs="+", default=["rules"],
                    choices=["rules", "presidio", "gliner-kn", "gliner-ur", "ensemble"])
    bn.add_argument("--n", type=int, default=30, help="documents per template")
    bn.add_argument("--seed", type=int, default=0)
    bn.add_argument("--show-misses", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "inspect":
        return _inspect(args.pdf)
    if args.cmd == "bench":
        from .bench.run import main_bench
        return main_bench(args.detector, args.n, args.seed, args.show_misses)
    return 2


if __name__ == "__main__":
    sys.exit(main())
