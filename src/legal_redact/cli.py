"""Command line entry point.

Output never contains document text; only counts, codes and page numbers.
OCR (local Tesseract) is used automatically on pages with no usable text layer;
`--no-ocr` on every command restores the old fail-closed refusal of scans.

PDFs and .docx files are both accepted by `inspect`, `suggest` and `redact`.
The browser review page renders PDF pages, so `review` stays PDF-only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymupdf

from .extract import extract_pages
from .ocr import ocr_available, ocr_version
from .preflight import blocked, preflight

# Word container extensions this tool routes to the DOCX pipeline: .docx is the
# only supported one, the others are refused there with a specific finding.
_DOCX_SUFFIXES = {".docx", ".doc", ".docm", ".dotx", ".dotm"}


def _pipeline_format(path: str) -> str:
    """'pdf' or 'docx': extension first, then file magic for extensionless files."""
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in _DOCX_SUFFIXES:
        return "docx"
    try:
        from .docx.package import (FileKind, file_kind)
        kind = file_kind(path)
    except Exception:
        return "pdf"
    if kind in (FileKind.OOXML, FileKind.ENCRYPTED_OOXML, FileKind.LEGACY_DOC,
                FileKind.OTHER_OLE):
        return "docx"
    return "pdf"


def _inspect_docx(path: str) -> int:
    from .docx.package import DocxError
    from .docx.pipeline import summary_docx
    from .docx.preflight import blocked as blocked_docx, preflight_docx
    findings = preflight_docx(path)
    print("format=docx")
    if not blocked_docx(findings):
        try:
            counts = summary_docx(path)
        except DocxError:
            pass                       # the findings below say why
        else:
            print(f"text_parts={counts['text_parts']} containers={counts['containers']} "
                  f"characters={counts['characters']}")
    for f in findings:
        print(f"{f.severity.value.upper():7} {f.code}: {f.message}")
    if blocked_docx(findings):
        print("RESULT: export would be refused")
        return 1
    print("RESULT: supported (review the findings above)")
    return 0



def _inspect(path: str, ocr: bool = True) -> int:
    if _pipeline_format(path) == "docx":
        return _inspect_docx(path)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # corrupt or not a PDF
        print(f"REFUSE PF000 cannot open file as PDF ({type(exc).__name__})")
        return 2
    findings = preflight(doc, ocr=ocr)
    print(f"pages={doc.page_count}")
    if ocr_available():
        print(f"ocr=available ({ocr_version() or 'tesseract'})")
    else:
        print("ocr=unavailable (pages without a text layer will be refused)")
    if not (doc.needs_pass or doc.is_encrypted):
        pages = extract_pages(doc, ocr=ocr)
        print(f"words={sum(len(p.words) for p in pages)}")
        print(f"ocr_pages={sum(1 for p in pages if p.ocr)}")
    for f in findings:
        where = f" p{f.page + 1}" if f.page is not None else ""
        print(f"{f.severity.value.upper():7} {f.code}{where}: {f.message}")
    if blocked(findings):
        print("RESULT: export would be refused")
        return 1
    print("RESULT: supported (review the findings above)")
    return 0


def _detector(name: str):
    """The rules detector needs nothing extra; the ensemble needs the 'ml' extra and models."""
    if name == "rules":
        from .detect.rules import RulesDetector
        return RulesDetector(names=False)
    hint = ("  pip install -e \".[ml]\" && python -m spacy download en_core_web_lg\n"
            "or run with --detector rules (structured identifiers only; no names or addresses).")
    try:                      # the detectors import their libraries lazily: check up front
        import gliner  # noqa: F401
        import presidio_analyzer  # noqa: F401
        import spacy
        from .bench.run import registry
    except ImportError as exc:
        print(f"The ensemble needs the 'ml' extra ({exc.name} is missing). Install it with\n{hint}")
        return None
    if not spacy.util.is_package("en_core_web_lg"):
        print(f"The ensemble needs the spaCy model en_core_web_lg. Install it with\n{hint}")
        return None
    return registry()["ensemble"]()


def _suggest(args) -> int:
    from .pipeline import save_decisions
    from .policy import load_policy
    det = _detector(args.detector)
    if det is None:
        return 2
    if _pipeline_format(args.pdf) == "docx":
        from .docx.pipeline import suggest_docx as suggest_fn
    else:
        from .pipeline import suggest as suggest_fn
    s = suggest_fn(args.pdf, det, load_policy(args.policy), ocr=not args.no_ocr)
    save_decisions(s, args.out)
    r = s.report()
    print(f"decisions written: {len(s.decisions)}  status={r['by_status']}")
    for b in s.blockers():
        print("blocker:", b)
    print("Edit statuses to 'accepted' or 'rejected', then run `legal-redact redact`.")
    return 0


def _h2h(args) -> int:
    from pathlib import Path
    from .bench import h2h
    from .bench.holdout import holdout4_corpus
    from .bench.indep import indep_corpus
    from .bench.synth import corpus
    det = _detector("ensemble")
    if det is None:
        return 2
    suites = {"holdout4": holdout4_corpus, "indep": indep_corpus}
    docs, work = suites[args.suite](), Path(args.work)
    src = h2h.write_sources(docs, work / "in")
    det.fit(corpus(per_template=30, seed0=0))          # threshold fitted on the dev corpus only
    h2h_out = {}
    arms = [h2h.score_arm("control: unredacted source", docs, src),
            h2h.score_arm("control: oracle export", docs, h2h.make_oracle(docs, src, work / "oracle"))]
    for mode, rep in (("all", False), ("auto", False), ("all", True), ("auto", True)):
        tag = mode + ("+repeats" if rep else "")
        out, info = h2h.make_ours(docs, src, work / f"ours_{tag}", mode, det, repeats=rep)
        if not rep:
            h2h_out[mode] = out
        r = info["repeats"]
        print(f"legal-redact ({tag}): {info['seconds']}s, exports refused by the verifier: {info['refused']}"
              + (f"; repeats added {r['added']}, real personal data {r['true']}, "
                 f"touching protected text {r['protected']}" if rep else ""))
        arms.append(h2h.score_arm(
            f"legal-redact (accept {'all' if mode == 'all' else 'auto-proposed only'}"
            f"{', + repeats' if rep else ''})", docs, out))
    for spec in args.external:
        label, path = spec.split("=", 1)
        arms.append(h2h.score_arm(label, docs, h2h.load_external(Path(path))))
    print()
    print(h2h.table(arms))
    # Fairer cut: only the categories a tool exposes. doc_redaction's command line has no ID or
    # date-of-birth entity, and its shipped default has no IBAN or card.
    six = {"PERSON", "EMAIL", "PHONE", "ADDRESS", "IBAN", "CARD"}
    four = {"PERSON", "EMAIL", "PHONE", "ADDRESS"}
    maps = {"all": h2h_out["all"], "auto": h2h_out["auto"]}
    rows = []
    for label, cats in (("PERSON/EMAIL/PHONE/ADDRESS", four), ("+ IBAN, CARD", six)):
        rows.append(h2h.score_arm(f"legal-redact (accept all) [{label}]", docs, maps["all"], cats))
        for spec in args.external:
            ext_label, path = spec.split("=", 1)
            if ("shipped" in ext_label) == (cats is four):
                rows.append(h2h.score_arm(f"{ext_label} [{label}]", docs, h2h.load_external(Path(path)), cats))
    print()
    print("restricted to the categories each tool exposes (a document leaks only if one of these leaked):")
    print(h2h.table(rows))
    return 0


def _review(args) -> int:
    from pathlib import Path
    if _pipeline_format(args.pdf) == "docx":
        print("The review page renders PDF pages, so it does not support .docx yet.")
        print("For a .docx file use: legal-redact inspect | suggest | redact, and set each")
        print('decision\'s "status" to "accepted" or "rejected" in the decisions JSON.')
        return 2
    from .pipeline import load_decisions, save_decisions, suggest
    from .policy import load_policy
    from .review_server import serve
    dec = args.decisions or str(Path(args.pdf).with_name(Path(args.pdf).name + ".decisions.json"))
    if Path(dec).exists():
        print(f"Resuming decisions from {dec}")
        session = load_decisions(args.pdf, dec, ocr=not args.no_ocr)
    else:
        det = _detector(args.detector)
        if det is None:
            return 2
        print("Detecting (local models; the first run loads them)...")
        session = suggest(args.pdf, det, load_policy(args.policy), ocr=not args.no_ocr)
        save_decisions(session, dec)
    serve(args.pdf, session, dec, args.port, open_browser=not args.no_browser,
          ocr=not args.no_ocr)
    return 0


def _redact(args) -> int:
    from .pipeline import ExportRefused
    docx = _pipeline_format(args.pdf) == "docx"
    if docx:
        from .docx.pipeline import export_docx as export_fn
        from .docx.pipeline import load_decisions_docx as load_fn
    else:
        from .pipeline import export as export_fn
        from .pipeline import load_decisions as load_fn
    s = load_fn(args.pdf, args.decisions, ocr=not args.no_ocr)
    for code in filter(None, args.ack.split(",")):
        s.acknowledge(code.strip())
    if args.accept_proposed:
        print(f"bulk-accepted {s.accept_all_proposed_redactions()} proposed redaction(s)")
    if args.accept_repeats:
        if docx:
            print("note: --accept-repeats is not implemented for .docx yet; ignored")
        else:
            from .pipeline import add_repeats
            print(f"accepted {add_repeats(args.pdf, s, accept=True, ocr=not args.no_ocr)} "
                  "other occurrence(s) of accepted text")
    try:
        rep = export_fn(args.pdf, args.out, s, ocr=not args.no_ocr)
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
    ins = sub.add_parser("inspect", help="pre-flight check of a PDF or .docx file")
    ins.add_argument("pdf", help="PDF or .docx file")
    ins.add_argument("--no-ocr", action="store_true",
                     help="do not use OCR; refuse pages without a text layer (old behaviour; "
                          "no effect on .docx)")
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
    sg.add_argument("pdf", help="PDF or .docx file")
    sg.add_argument("-o", "--out", required=True, help="decisions JSON to write")
    sg.add_argument("--policy", default="counterparty")
    sg.add_argument("--detector", default="ensemble", choices=["ensemble", "rules"])
    sg.add_argument("--no-ocr", action="store_true",
                    help="do not use OCR; refuse pages without a text layer (old behaviour; "
                         "no effect on .docx)")
    rd = sub.add_parser("redact", help="export a redacted copy after review, then verify it")
    rd.add_argument("pdf", help="PDF or .docx file")
    rd.add_argument("--decisions", required=True)
    rd.add_argument("-o", "--out", required=True)
    rd.add_argument("--ack", default="", help="comma-separated pre-flight REVIEW codes you acknowledge")
    rd.add_argument("--accept-proposed", action="store_true",
                    help="bulk-accept pending items the policy proposes to redact (review items stay pending)")
    rd.add_argument("--no-ocr", action="store_true",
                    help="do not use OCR; refuse pages without a text layer (old behaviour; "
                         "no effect on .docx)")
    rv = sub.add_parser("review", help="open the local review page (accept / reject / add boxes, export)")
    rv.add_argument("pdf", help="PDF file (the review page is PDF-only for now)")
    rv.add_argument("--policy", default="counterparty")
    rv.add_argument("--detector", default="ensemble", choices=["ensemble", "rules"])
    rv.add_argument("--decisions", default=None,
                    help="decisions file to resume or create (default: <pdf>.decisions.json)")
    rv.add_argument("--port", type=int, default=0)
    rv.add_argument("--no-browser", action="store_true")
    rv.add_argument("--no-ocr", action="store_true",
                    help="do not use OCR; refuse pages without a text layer (old behaviour)")
    hh = sub.add_parser("bench-h2h", help="score our tool and external tools' output PDFs identically")
    hh.add_argument("--work", required=True, help="working folder (sources and our outputs go here)")
    hh.add_argument("--suite", default="holdout4", choices=["holdout4", "indep"],
                    help="holdout4: our synthetic suite (home-field); indep: independent-data v5 "
                         "(third-party contract text, independently injected PII)")
    hh.add_argument("--external", action="append", default=[], metavar="LABEL=results.json",
                    help="an external tool's results.json (from its own harness); repeatable")
    rd.add_argument("--accept-repeats", action="store_true",
                    help="also accept every other occurrence of text you accepted (explicit bulk action)")
    be = sub.add_parser("bench-export", help="run the seeded-leak suite against the export verifier")
    be.add_argument("--n", type=int, default=10, help="documents per template")
    args = ap.parse_args(argv)
    if args.cmd == "suggest":
        return _suggest(args)
    if args.cmd == "redact":
        return _redact(args)
    if args.cmd == "review":
        return _review(args)
    if args.cmd == "bench-h2h":
        return _h2h(args)
    if args.cmd == "bench-export":
        from .bench import export_suite
        res = export_suite.run(args.n)
        print(export_suite.format_result(res))
        return 0 if not res["clean_fail"] and all(res["caught"][n] == res["total"][n]
                                                  for n in export_suite.INJECTORS) else 1
    if args.cmd == "inspect":
        return _inspect(args.pdf, ocr=not args.no_ocr)
    if args.cmd == "bench":
        from .bench.run import main_bench
        return main_bench(args.detector, args.n, args.seed, args.show_misses,
                          args.suite, args.policy)
    return 2


if __name__ == "__main__":
    sys.exit(main())
