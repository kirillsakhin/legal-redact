"""Command line entry point.

Output never contains document text; only counts, codes and page numbers.
OCR (local Tesseract by default) is used automatically on pages with no usable
text layer; `--no-ocr` on every command restores the old fail-closed refusal of
scans. A custom engine can be selected with `--ocr-engine`/`--ocr-command` (see
docs/OCR_BACKENDS.md); a third-party/cloud engine is refused unless the run is
approved with `--allow-cloud-ocr`, and the verbatim cloud disclaimer is printed
on stderr and must be acknowledged per file before export.

PDFs and .docx files are both accepted by `inspect`, `suggest`, `redact` and
`review`. OCR also reads text inside images embedded in a .docx (with the same
engine abstraction); a redacted image is repainted in its pixels and verified
again on export. `--no-ocr` restores the old fail-closed rule that image text is
not read. The review page for a .docx is rendered through an installed Microsoft
Word (Windows COM) or LibreOffice when one is found; without a renderer — or when
the text cannot be aligned to the rendered pages with confidence — it falls back
to a text review page. The .docx itself is always the source of truth: the
rendered page is a view only, and export runs the normal writer and verifier.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pymupdf

from .extract import extract_pages
from .ocr import OcrError, OcrUnavailable, ocr_available, ocr_version
from .ocr_backends import (CLOUD_OCR_DISCLAIMER, CloudOcrNotApproved, EngineConfig,
                           EngineConfigError, resolve_config)
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


def _inspect_docx(path: str, ocr: bool = True,
                  engine_config: EngineConfig | None = None) -> int:
    from .docx.package import DocxError
    from .docx.pipeline import summary_docx
    from .docx.preflight import blocked as blocked_docx, preflight_docx
    from .docx.render import detect_renderer, renderer_label
    findings = preflight_docx(path, ocr=ocr, engine_config=engine_config)
    print("format=docx")
    print(f"review_page={renderer_label(detect_renderer())}")
    if not blocked_docx(findings):
        try:
            counts = summary_docx(path)
        except DocxError:
            pass                       # the findings below say why
        else:
            print(f"text_parts={counts['text_parts']} containers={counts['containers']} "
                  f"characters={counts['characters']} images={counts['media_parts']}")
            if counts["media_parts"] and ocr and engine_config is not None:
                from .docx import images as image_tools
                name, version, available, _error = image_tools.engine_summary(engine_config)
                status = "available" if available else "unavailable"
                print(f"image_ocr={status} (engine={name or engine_config.spec} "
                      f"version={version or 'unknown'} "
                      f"local={'yes' if engine_config.local else 'no'})")
    for f in findings:
        print(f"{f.severity.value.upper():7} {f.code}: {f.message}")
    if blocked_docx(findings):
        print("RESULT: export would be refused")
        return 1
    print("RESULT: supported (review the findings above)")
    return 0



def _resolve_engine(args) -> EngineConfig | None:
    """Resolve/gate the OCR engine for this run; prints and returns None on refusal.

    This runs BEFORE any document is opened for OCR: a non-local engine without
    `--allow-cloud-ocr` is refused here, so no page is ever rendered or sent.
    For a non-local engine the verbatim cloud disclaimer goes to stderr on every
    command (inspect included), before any OCR work.
    """
    try:
        config = resolve_config(engine=getattr(args, "ocr_engine", None),
                                command=getattr(args, "ocr_command", None),
                                allow_cloud=getattr(args, "allow_cloud_ocr", False),
                                local=True if getattr(args, "ocr_local", False) else None,
                                env=os.environ)
    except EngineConfigError as exc:
        print(f"REFUSE PF020 OCR engine configuration error: {exc}", file=sys.stderr)
        return None
    if not config.local:
        print(CLOUD_OCR_DISCLAIMER, file=sys.stderr, flush=True)
        try:
            config.check_gate()
        except CloudOcrNotApproved as exc:
            print(f"REFUSE PF020 {exc}", file=sys.stderr)
            return None
    return config


def _add_ocr_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ocr-engine", default=None, metavar="SPEC",
                        help="OCR engine: 'tesseract' (default) or 'command:<cmdline>' "
                             "(see docs/OCR_BACKENDS.md)")
    parser.add_argument("--ocr-command", default=None, metavar="CMD",
                        help="external OCR wrapper command (same as --ocr-engine command:<CMD>)")
    parser.add_argument("--ocr-local", action="store_true",
                        help="declare that the custom OCR command runs locally; without this a "
                             "command engine is treated as third-party (cloud)")
    parser.add_argument("--allow-cloud-ocr", action="store_true",
                        help="approve sending page images to a third-party OCR engine for this "
                             "run; the per-file cloud finding must also be acknowledged")


def _inspect(path: str, ocr: bool = True, engine_config: EngineConfig | None = None) -> int:
    if _pipeline_format(path) == "docx":
        return _inspect_docx(path)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # corrupt or not a PDF
        print(f"REFUSE PF000 cannot open file as PDF ({type(exc).__name__})")
        return 2
    try:
        findings = preflight(doc, ocr=ocr, engine_config=engine_config)
        print(f"pages={doc.page_count}")
        if ocr and engine_config is not None:
            info = engine_config.info()
            status = "available" if engine_config.engine().available() else "unavailable"
            print(f"ocr={status} (engine={info['name']} version={info['version'] or 'unknown'} "
                  f"local={'yes' if info['local'] else 'no'})")
        elif ocr_available():
            print(f"ocr=available ({ocr_version() or 'tesseract'})")
        else:
            print("ocr=unavailable (pages without a text layer will be refused)")
        if not (doc.needs_pass or doc.is_encrypted):
            try:
                pages = extract_pages(doc, ocr=ocr, engine_config=engine_config)
            except (OcrError, OcrUnavailable, EngineConfigError) as exc:
                print(f"REFUSE PF020 OCR engine failed: {exc}", file=sys.stderr)
                return 1
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
    finally:
        doc.close()


def _inspect_command(args) -> int:
    config = None
    if not args.no_ocr:
        config = _resolve_engine(args)
        if config is None:
            return 2
    if _pipeline_format(args.pdf) == "docx":
        return _inspect_docx(args.pdf, ocr=not args.no_ocr, engine_config=config)
    return _inspect(args.pdf, ocr=not args.no_ocr, engine_config=config)


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
    docx = _pipeline_format(args.pdf) == "docx"
    config = None
    if not args.no_ocr:
        config = _resolve_engine(args)
        if config is None:
            return 2
    det = _detector(args.detector)
    if det is None:
        return 2
    try:
        if docx:
            from .docx.pipeline import suggest_docx as suggest_fn
            s = suggest_fn(args.pdf, det, load_policy(args.policy),
                           ocr=not args.no_ocr, engine_config=config)
        else:
            from .pipeline import suggest as suggest_fn
            s = suggest_fn(args.pdf, det, load_policy(args.policy),
                           ocr=not args.no_ocr, engine_config=config)
    except (OcrError, OcrUnavailable, EngineConfigError) as exc:
        print(f"REFUSE PF020 OCR engine failed: {exc}", file=sys.stderr)
        return 1
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


def _review_docx(args) -> int:
    """`legal-redact review file.docx`: rendered review when possible, else text review."""
    from pathlib import Path
    from .docx.package import DocxError
    from .docx.pipeline import load_decisions_docx, suggest_docx
    from .docx.review import serve_docx
    from .pipeline import save_decisions
    from .policy import load_policy
    dec = args.decisions or str(Path(args.pdf).with_name(Path(args.pdf).name + ".decisions.json"))
    config = None
    if not args.no_ocr:
        config = _resolve_engine(args)
        if config is None:
            return 2
    try:
        if Path(dec).exists():
            print(f"Resuming decisions from {dec}")
            session = load_decisions_docx(args.pdf, dec, ocr=not args.no_ocr,
                                          engine_config=config)
        else:
            det = _detector(args.detector)
            if det is None:
                return 2
            print("Detecting (local models; the first run loads them)...")
            session = suggest_docx(args.pdf, det, load_policy(args.policy),
                                   ocr=not args.no_ocr, engine_config=config)
            save_decisions(session, dec)
    except (OSError, ValueError, DocxError) as exc:
        print(f"REFUSE PF104 could not prepare the review session ({type(exc).__name__}).",
              file=sys.stderr)
        return 1
    if not _apply_context_and_attestation(session, args):
        return 2
    try:
        serve_docx(args.pdf, session, dec, args.port, open_browser=not args.no_browser,
                   engine_config=config, ocr=not args.no_ocr)
    except (OSError, ValueError) as exc:
        print(f"REFUSE PF104 the review page could not start ({type(exc).__name__}).",
              file=sys.stderr)
        return 1
    return 0


def _add_context_args(parser: argparse.ArgumentParser) -> None:
    """Per-matter context recorded in the compliance evidence report (optional)."""
    for name, metavar, help_text in (
        ("controller", "NAME", "controller / firm name for the evidence report"),
        ("matter", "REF", "matter or data-subject-request reference for the evidence report"),
        ("purpose", "TEXT", "processing purpose for the evidence report"),
        ("reviewer", "NAME", "reviewer name for the evidence report"),
    ):
        parser.add_argument(f"--{name}", default=None, metavar=metavar, help=help_text)


def _apply_context_and_attestation(session, args, *, require_attestation: bool = False) -> bool:
    """Record the per-matter context and (optionally) the CLI attestations.

    Returns False after printing a clean refusal when the caller supplied an unknown
    attestation id. The review page collects the attestations itself; the CLI is
    explicit via --attest so automation stays scriptable.
    """
    context = {}
    for key in ("controller", "matter", "purpose", "reviewer"):
        value = getattr(args, key, None)
        if value:
            context[key] = str(value).strip()
    session.context = context
    attest = getattr(args, "attest", None)
    if attest:
        try:
            session.set_attestation([a.strip() for a in str(attest).split(",") if a.strip()])
        except ValueError as exc:
            print(f"REFUSE {exc}", file=sys.stderr)
            return False
    elif require_attestation:
        print("REFUSE reviewer attestation required: pass --attest "
              + ",".join(("special_categories", "indirect_identifiers", "authorised")),
              file=sys.stderr)
        return False
    return True


def _review(args) -> int:
    from pathlib import Path
    if _pipeline_format(args.pdf) == "docx":
        return _review_docx(args)
    from .pipeline import load_decisions, save_decisions, suggest
    from .policy import load_policy
    from .review_server import serve
    config = None
    if not args.no_ocr:
        config = _resolve_engine(args)
        if config is None:
            return 2
    dec = args.decisions or str(Path(args.pdf).with_name(Path(args.pdf).name + ".decisions.json"))
    try:
        if Path(dec).exists():
            print(f"Resuming decisions from {dec}")
            session = load_decisions(args.pdf, dec, ocr=not args.no_ocr,
                                     engine_config=config)
        else:
            det = _detector(args.detector)
            if det is None:
                return 2
            print("Detecting (local models; the first run loads them)...")
            session = suggest(args.pdf, det, load_policy(args.policy),
                              ocr=not args.no_ocr, engine_config=config)
            save_decisions(session, dec)
    except (OcrError, OcrUnavailable, EngineConfigError) as exc:
        print(f"REFUSE PF020 OCR engine failed: {exc}", file=sys.stderr)
        return 1
    if not _apply_context_and_attestation(session, args):
        return 2
    serve(args.pdf, session, dec, args.port, open_browser=not args.no_browser,
          ocr=not args.no_ocr, engine_config=config)
    return 0


def _redact(args) -> int:
    from .pipeline import ExportRefused
    docx = _pipeline_format(args.pdf) == "docx"
    config = None
    if not args.no_ocr:
        config = _resolve_engine(args)
        if config is None:
            return 2
    try:
        if docx:
            from .docx.pipeline import export_docx as export_fn
            from .docx.pipeline import load_decisions_docx as load_fn
        else:
            from .pipeline import export as export_fn
            from .pipeline import load_decisions as load_fn
        if docx:
            s = load_fn(args.pdf, args.decisions, ocr=not args.no_ocr,
                        engine_config=config)
        else:
            s = load_fn(args.pdf, args.decisions, ocr=not args.no_ocr,
                        engine_config=config)
    except EngineConfigError as exc:
        print(f"REFUSE PF020 OCR engine configuration error: {exc}", file=sys.stderr)
        return 1
    if not _apply_context_and_attestation(s, args):
        return 2
    for code in filter(None, args.ack.split(",")):
        s.acknowledge(code.strip())
    if args.accept_proposed:
        print(f"bulk-accepted {s.accept_all_proposed_redactions()} proposed redaction(s)")
    if args.accept_repeats:
        if docx:
            print("note: --accept-repeats is not implemented for .docx yet; ignored")
        else:
            from .pipeline import add_repeats
            added = add_repeats(args.pdf, s, accept=True, ocr=not args.no_ocr,
                                engine_config=config)
            print(f"accepted {added} other occurrence(s) of accepted text")
    try:
        if docx:
            rep = export_fn(args.pdf, args.out, s, ocr=not args.no_ocr,
                            engine_config=config)
        else:
            rep = export_fn(args.pdf, args.out, s, ocr=not args.no_ocr,
                            engine_config=config)
    except ExportRefused as exc:
        print(f"REFUSED: {exc}")
        if exc.report:
            print(exc.report.format())
        for b in s.blockers():
            print("blocker:", b)
        return 1
    print(rep.format())
    print(f"written: {args.out}")
    evidence_path = getattr(args, "evidence", None)
    dpia_path = getattr(args, "dpia", None)
    if evidence_path or dpia_path:
        try:
            from .evidence import (build_evidence, write_dpia_extract, write_evidence)
            evidence = build_evidence(args.pdf, args.out, s, rep)
            if evidence_path:
                print(f"evidence: {write_evidence(evidence_path, evidence)}")
            if dpia_path:
                print(f"DPIA/ROPA extract: {write_dpia_extract(dpia_path, evidence)}")
        except OSError as exc:
            print(f"WARNING: the redacted file was written but an artifact could not be "
                  f"written ({type(exc).__name__}: {exc})", file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="legal-redact")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("inspect", help="pre-flight check of a PDF or .docx file")
    ins.add_argument("pdf", help="PDF or .docx file")
    ins.add_argument("--no-ocr", action="store_true",
                     help="do not use OCR; refuse pages without a text layer (old behaviour; "
                          "also disables reading text inside .docx images)")
    _add_ocr_args(ins)
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
                         "also disables reading text inside .docx images)")
    _add_ocr_args(sg)
    rd = sub.add_parser("redact", help="export a redacted copy after review, then verify it")
    rd.add_argument("pdf", help="PDF or .docx file")
    rd.add_argument("--decisions", required=True)
    rd.add_argument("-o", "--out", required=True)
    rd.add_argument("--ack", default="", help="comma-separated pre-flight REVIEW codes you acknowledge")
    rd.add_argument("--accept-proposed", action="store_true",
                    help="bulk-accept pending items the policy proposes to redact (review items stay pending)")
    rd.add_argument("--no-ocr", action="store_true",
                    help="do not use OCR; refuse pages without a text layer (old behaviour; "
                         "also disables reading text inside .docx images)")
    _add_ocr_args(rd)
    rv = sub.add_parser("review", help="open the local review page (accept / reject / add boxes, export)")
    rv.add_argument("pdf", help="PDF or .docx file (the .docx page is rendered when Word or "
                                "LibreOffice is available, otherwise it is a text review)")
    rv.add_argument("--policy", default="counterparty")
    rv.add_argument("--detector", default="ensemble", choices=["ensemble", "rules"])
    rv.add_argument("--decisions", default=None,
                    help="decisions file to resume or create (default: <pdf>.decisions.json)")
    rv.add_argument("--port", type=int, default=0)
    rv.add_argument("--no-browser", action="store_true")
    rv.add_argument("--no-ocr", action="store_true",
                    help="do not use OCR; refuse pages without a text layer (old behaviour)")
    _add_ocr_args(rv)
    _add_context_args(rv)
    hh = sub.add_parser("bench-h2h", help="score our tool and external tools' output PDFs identically")
    hh.add_argument("--work", required=True, help="working folder (sources and our outputs go here)")
    hh.add_argument("--suite", default="holdout4", choices=["holdout4", "indep"],
                    help="holdout4: our synthetic suite (home-field); indep: independent-data v5 "
                         "(third-party contract text, independently injected PII)")
    hh.add_argument("--external", action="append", default=[], metavar="LABEL=results.json",
                    help="an external tool's results.json (from its own harness); repeatable")
    rd.add_argument("--accept-repeats", action="store_true",
                    help="also accept every other occurrence of text you accepted (explicit bulk action)")
    _add_context_args(rd)
    rd.add_argument("--attest", default=None, metavar="IDS",
                    help="comma-separated reviewer attestations recorded in the evidence "
                         "report: special_categories,indirect_identifiers,authorised (the "
                         "review page collects these itself before export)")
    rd.add_argument("--evidence", default=None, metavar="PATH",
                    help="after a successful export, write a compliance evidence report "
                         "(Markdown; use a .json path for JSON). No document text is included")
    rd.add_argument("--dpia", default=None, metavar="PATH",
                    help="after a successful export, write a one-page DPIA/ROPA input extract "
                         "(Markdown; use a .json path for JSON). It is not a DPIA; the controller "
                         "completes the legal analysis, retention and sign-off fields")
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
        return _inspect_command(args)
    if args.cmd == "bench":
        from .bench.run import main_bench
        return main_bench(args.detector, args.n, args.seed, args.show_misses,
                          args.suite, args.policy)
    return 2


if __name__ == "__main__":
    sys.exit(main())
