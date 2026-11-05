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


def _suggest(args) -> int:
    from .pipeline import save_decisions, suggest
    from .policy import load_policy
    if args.detector == "rules":
        from .detect.rules import RulesDetector
        det = RulesDetector(names=False)
    else:
        from .bench.run import registry
        det = registry()["ensemble"]()
    s = suggest(args.pdf, det, load_policy(args.policy))
    save_decisions(s, args.out)
    r = s.report()
    print(f"decisions written: {len(s.decisions)}  status={r['by_status']}")
    for b in s.blockers():
        print("blocker:", b)
    print("Edit statuses to 'accepted' or 'rejected', then run `legal-redact redact`.")
    return 0


def _review(args) -> int:
    from pathlib import Path
    from .pipeline import load_decisions, save_decisions, suggest
    from .policy import load_policy
    from .review_server import serve
    dec = args.decisions or str(Path(args.pdf).with_name(Path(args.pdf).name + ".decisions.json"))
    if Path(dec).exists():
        print(f"Resuming decisions from {dec}")
        session = load_decisions(args.pdf, dec)
    else:
        if args.detector == "rules":
            from .detect.rules import RulesDetector
            det = RulesDetector(names=False)
        else:
            from .bench.run import registry
            det = registry()["ensemble"]()
        print("Detecting (local models; the first run loads them)...")
        session = suggest(args.pdf, det, load_policy(args.policy))
        save_decisions(session, dec)
    serve(args.pdf, session, dec, args.port, open_browser=not args.no_browser)
    return 0


def _redact(args) -> int:
    from .pipeline import ExportRefused, export, load_decisions
    s = load_decisions(args.pdf, args.decisions)
    for code in filter(None, args.ack.split(",")):
        s.acknowledge(code.strip())
    if args.accept_proposed:
        print(f"bulk-accepted {s.accept_all_proposed_redactions()} proposed redaction(s)")
    try:
        rep = export(args.pdf, args.out, s)
    except ExportRefused as exc:
        print(f"REFUSED: {exc}")
        if exc.report:
            print(exc.report.format())
        for b in s.blockers():
            print("blocker:", b)
        return 1
    print(rep.format())
    print(f"written: {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="legal-redact")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("inspect", help="pre-flight check of a PDF")
    ins.add_argument("pdf")
    bn = sub.add_parser("bench", help="run the synthetic benchmark")
    bn.add_argument("--detector", nargs="+", default=["rules"],
                    choices=["rules", "presidio", "presidio-lg", "gliner-kn", "gliner-ur",
                             "ensemble", "ensemble-v1", "ensemble-v3", "ens+resolve",
                             "ens+resolve+lg", "ens+resolve+lg+veto"])
    bn.add_argument("--suite", choices=["dev", "frozen", "holdout", "holdout4"], default="dev")
    bn.add_argument("--policy", default=None)
    bn.add_argument("--n", type=int, default=30, help="documents per template")
    bn.add_argument("--seed", type=int, default=0)
    bn.add_argument("--show-misses", action="store_true")
    sg = sub.add_parser("suggest", help="detect and write a decision file (no document text in it)")
    sg.add_argument("pdf")
    sg.add_argument("-o", "--out", required=True, help="decisions JSON to write")
    sg.add_argument("--policy", default="counterparty")
    sg.add_argument("--detector", default="ensemble", choices=["ensemble", "rules"])
    rd = sub.add_parser("redact", help="export a redacted copy after review, then verify it")
    rd.add_argument("pdf")
    rd.add_argument("--decisions", required=True)
    rd.add_argument("-o", "--out", required=True)
    rd.add_argument("--ack", default="", help="comma-separated pre-flight REVIEW codes you acknowledge")
    rd.add_argument("--accept-proposed", action="store_true",
                    help="bulk-accept pending items the policy proposes to redact (review items stay pending)")
    rv = sub.add_parser("review", help="open the local review page (accept / reject / add boxes, export)")
    rv.add_argument("pdf")
    rv.add_argument("--policy", default="counterparty")
    rv.add_argument("--detector", default="ensemble", choices=["ensemble", "rules"])
    rv.add_argument("--decisions", default=None,
                    help="decisions file to resume or create (default: <pdf>.decisions.json)")
    rv.add_argument("--port", type=int, default=0)
    rv.add_argument("--no-browser", action="store_true")
    be = sub.add_parser("bench-export", help="run the seeded-leak suite against the export verifier")
    be.add_argument("--n", type=int, default=10, help="documents per template")
    args = ap.parse_args(argv)
    if args.cmd == "suggest":
        return _suggest(args)
    if args.cmd == "redact":
        return _redact(args)
    if args.cmd == "review":
        return _review(args)
    if args.cmd == "bench-export":
        from .bench import export_suite
        res = export_suite.run(args.n)
        print(export_suite.format_result(res))
        return 0 if not res["clean_fail"] and all(res["caught"][n] == res["total"][n]
                                                  for n in export_suite.INJECTORS) else 1
    if args.cmd == "inspect":
        return _inspect(args.pdf)
    if args.cmd == "bench":
        from .bench.run import main_bench
        return main_bench(args.detector, args.n, args.seed, args.show_misses,
                          args.suite, args.policy)
    return 2


if __name__ == "__main__":
    sys.exit(main())
