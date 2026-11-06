"""Compliance evidence report for an export.

What it is: a record of what the tool checked and what the reviewer decided, so a
lawyer can attach it to the matter file. What it is NOT: a compliance
certificate, legal advice, or proof that every item requiring redaction was
identified. The human review decisions are part of the record; the detector and
the verifier only cover what they cover (docs/COMPLIANCE.md,
docs/THREAT_MODEL.md).

The report deliberately contains NO document text: paths, hashes, counts,
categories, finding/check codes and configuration only.
"""
from __future__ import annotations

import hashlib
import json
import platform
from collections import Counter
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from .detect.base import Category
from .policy import Decision
from .review import ATTESTATIONS, ReviewSession
from .verify import VerifyReport

SCHEMA = "legal-redact-evidence/1"
DISCLAIMER = (
    "This report records the automated checks and the human review decisions made with "
    "legal-redact. It is not legal advice, not a compliance certificate, and does not prove "
    "that every item requiring redaction was identified: context-dependent data, special "
    "categories and indirect identifiers are the reviewer's responsibility. Human review "
    "decisions remain part of the record."
)
NO_NETWORK = (
    "legal-redact makes no network calls of its own, has no telemetry, and processes files "
    "locally. A third-party/cloud OCR engine is used only when it was approved for the run "
    "(and per file); see the OCR engine section for the engine actually used."
)


def sha256_file(path: str | Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _tool_info() -> dict[str, str]:
    # The package constant is authoritative in a source tree; installed metadata can
    # lag behind it after a version bump without reinstalling.
    try:
        from . import __version__ as package_version
    except ImportError:                                  # pragma: no cover
        package_version = None
    version = package_version
    if not version or version == "unknown":
        try:
            version = metadata.version("legal-redact")
        except metadata.PackageNotFoundError:            # running from a source tree
            version = "unknown"
    return {"name": "legal-redact", "version": version,
            "python": platform.python_version(), "platform": platform.platform()}


def _file_entry(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    resolved = p.resolve()
    return {"path": str(resolved), "name": p.name, "bytes": p.stat().st_size,
            "sha256": sha256_file(p)}


def _accepted_by_category(decisions: list[Decision]) -> dict[str, int]:
    return dict(Counter(d.category for d in decisions if d.status == "accepted"))


def build_evidence(src: str, out: str, session: ReviewSession,
                   report: VerifyReport) -> dict[str, Any]:
    """Collect the evidence record (pure: reads the two files for hashes).

    Attestation ids and per-matter context come from the review session (set by the
    CLI flags or by the review page before export). They are configuration, never
    document text, and they are recorded as the reviewer's own statements.
    """
    decisions = list(session.decisions)
    review = session.report()
    checks = []
    for check in report.checks:
        checks.append({"id": getattr(check, "id", ""),
                       "name": getattr(check, "name", ""),
                       "status": getattr(check, "status", ""),
                       "detail": getattr(check, "detail", "")})
    engine = dict(session.ocr_info) if isinstance(session.ocr_info, dict) else None
    context = {k: str(v).strip() for k, v in dict(session.context or {}).items()
               if str(v).strip()}
    given = [i for i in session.attestation if i in ATTESTATIONS]
    attestation_rows = [{"id": i, "label": ATTESTATIONS[i], "attested": i in given}
                        for i in ATTESTATIONS]
    warnings = [c for c in checks if c["status"] == "WARN"]
    failures = [c for c in checks if c["status"] == "FAIL"]
    residual_risk = {
        "warnings": warnings,
        "failures": failures,
        "acknowledged_findings": review["acknowledged_findings"],
        "detector_coverage": {
            "detected_categories": list(Category.ALL),
            "not_detected_comprehensively": [
                "special-category data (Art. 9: health, genetic, biometric data, religion, "
                "sexual orientation, criminal-offence data)",
                "indirect identifiers and singling-out (rare facts, combinations of "
                "quasi-identifiers, small populations, narrative context)",
                "handwriting, signatures, stamps and photographs; text inside images the "
                "OCR engine did not read",
            ],
            "control": ("The reviewer must check the categories above manually. The tool "
                        "covers only the categories it detects and verifies removal of what "
                        "the reviewer chose to redact; it does not certify completeness."),
        },
    }
    return {
        "schema": SCHEMA,
        "disclaimer": DISCLAIMER,
        "no_network": NO_NETWORK,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": _tool_info(),
        "context": context,
        "attestation": {
            "required": list(ATTESTATIONS),
            "given": given,
            "rows": attestation_rows,
            "note": ("Reviewer-declared statements required by the review page before "
                     "export; the CLI records them only when passed explicitly."),
        },
        "files": {"source": _file_entry(src), "redacted": _file_entry(out)},
        "policy": session.policy,
        "review": {
            "decisions": len(decisions),
            "by_status": review["by_status"],
            "by_category": review["by_category"],
            "accepted_by_category": _accepted_by_category(decisions),
            "manual_additions": review["manual_additions"],
            "acknowledged_findings": review["acknowledged_findings"],
        },
        "ocr_engine": engine,
        "residual_risk": residual_risk,
        "verification": {
            "passed": report.passed,
            "checks": checks,
            "limits": list(report.limits),
        },
    }


def _md(evidence: dict[str, Any]) -> str:
    tool = evidence["tool"]
    files = evidence["files"]
    review = evidence["review"]
    verification = evidence["verification"]
    lines = [
        "# Redaction evidence report",
        "",
        "> " + evidence["disclaimer"],
        "",
        f"- **Tool:** {tool['name']} {tool['version']} (Python {tool['python']}, "
        f"{tool['platform']})",
        f"- **Generated (UTC):** {evidence['generated_at']}",
        f"- **Policy:** {evidence['policy'] or 'not recorded (decision file predates policy names)'}",
        f"- **Decisions:** {review['decisions']} total — "
        f"{review['by_status'].get('accepted', 0)} accepted, "
        f"{review['by_status'].get('rejected', 0)} rejected, "
        f"{review['by_status'].get('pending', 0)} pending; "
        f"{review['manual_additions']} added by the reviewer",
        f"- **Acknowledged pre-flight findings:** "
        f"{', '.join(review['acknowledged_findings']) or 'none'}",
        "",
    ]
    lines += _context_and_attestations(evidence)
    lines += [
        "## Files",
        "",
        "| file | path | size (bytes) | sha256 |",
        "|---|---|---|---|",
    ]
    for label, entry in (("source", files["source"]), ("redacted", files["redacted"])):
        lines.append(f"| {label} | `{entry['path']}` | {entry['bytes']} | `{entry['sha256']}` |")
    if evidence["ocr_engine"]:
        engine = evidence["ocr_engine"]
        locality = "local" if engine.get("local") else "third-party/cloud (approved)"
        lines += ["", "## OCR engine",
                  "", f"- Engine: `{engine.get('engine')}` {engine.get('version') or ''} "
                  f"({locality}); cloud approved: {bool(engine.get('cloud'))}"]
    lines += ["", "## Accepted redactions by category", ""]
    accepted = review["accepted_by_category"]
    if accepted:
        lines += ["| category | accepted |", "|---|---|"]
        lines += [f"| {category} | {count} |" for category, count in sorted(accepted.items())]
    else:
        lines.append("No redactions were accepted (the file was exported unchanged).")
    lines += ["", "## Verification", "",
              f"**Result: {'PASS — no leak found in the supported checks' if verification['passed'] else 'FAIL — do not release this file'}**",
              "", "| check | status | detail |", "|---|---|---|"]
    for check in verification["checks"]:
        lines.append(f"| {check['id']} {check['name']} | {check['status']} | {check['detail']} |")
    lines += ["", "## Not covered (the verifier's own limits)", ""]
    lines += [f"- {limit}" for limit in verification["limits"]]
    lines += _residual_risk(evidence)
    lines += _dpia_inputs(evidence)
    lines += ["", "## Data handling", "", evidence["no_network"], ""]
    return "\n".join(lines)


_CONTEXT_LABELS = {
    "controller": "Controller / firm",
    "matter": "Matter or request reference",
    "purpose": "Processing purpose",
    "reviewer": "Reviewer",
}


def _context_and_attestations(evidence: dict[str, Any]) -> list[str]:
    context = evidence.get("context") or {}
    lines = ["## Context", "",
             "| field | recorded value |", "|---|---|"]
    for key, label in _CONTEXT_LABELS.items():
        value = context.get(key) or "not recorded"
        lines.append(f"| {label} | {value} |")
    attestation = evidence.get("attestation") or {}
    lines += ["", "## Reviewer attestations", "",
              "These are the reviewer's own statements; they are required by the review "
              "page before export and are not checked by the tool.", "",
              "| attestation | given |", "|---|---|"]
    for row in attestation.get("rows", []):
        lines.append(f"| {row['label']} | {'yes' if row['attested'] else 'NOT RECORDED'} |")
    if not attestation.get("given"):
        lines += ["", "> No attestation was recorded for this export (for example, an "
                  "automated CLI export without `--attest`)."]
    lines.append("")
    return lines


def _residual_risk(evidence: dict[str, Any]) -> list[str]:
    risk = evidence.get("residual_risk") or {}
    warnings = risk.get("warnings") or []
    failures = risk.get("failures") or []
    acknowledged = risk.get("acknowledged_findings") or []
    coverage = risk.get("detector_coverage") or {}
    lines = ["", "## Residual risk (what this report does NOT certify)", ""]
    if warnings:
        lines += ["| warning | detail |", "|---|---|"]
        lines += [f"| {w['id']} {w['name']} | {w['detail']} |" for w in warnings]
    else:
        lines.append("No verifier warnings.")
    if failures:
        lines += ["", "**Verifier failures (do not release):**"]
        lines += [f"- {f['id']} {f['name']}: {f['detail']}" for f in failures]
    lines += ["", f"- Acknowledged pre-flight findings: "
              f"{', '.join(acknowledged) or 'none'}."]
    detected = coverage.get("detected_categories") or []
    if detected:
        lines.append(f"- Categories the detectors can propose: {', '.join(detected)}.")
    not_detected = coverage.get("not_detected_comprehensively") or []
    if not_detected:
        lines.append("- Not detected comprehensively (human check required):")
        lines += [f"  - {item}" for item in not_detected]
    control = coverage.get("control")
    if control:
        lines += ["", control]
    return lines


def _dpia_inputs(evidence: dict[str, Any]) -> list[str]:
    context = evidence.get("context") or {}
    review = evidence["review"]
    accepted = review.get("accepted_by_category") or {}
    checks = evidence["verification"]["checks"]
    warnings = sum(1 for c in checks if c["status"] == "WARN")
    failures = sum(1 for c in checks if c["status"] == "FAIL")
    engine = evidence.get("ocr_engine")
    engine_text = "not used for suggestions"
    if engine:
        locality = "local" if engine.get("local") else "third-party/cloud (approved)"
        engine_text = f"{engine.get('engine')} {engine.get('version') or ''} ({locality})"
    categories = (", ".join(f"{k} x{v}" for k, v in sorted(accepted.items()))
                  or "none accepted")
    decisions = review.get("decisions", 0)
    status = review.get("by_status") or {}
    lines = ["", "## Inputs for your DPIA / ROPA", "",
             "These are the fields this workflow can supply. The legal analysis, "
             "retention schedule and transfer assessment remain yours.", "",
             "| field | recorded value |", "|---|---|",
             f"| Controller | {context.get('controller') or 'to be completed'} |",
             f"| Matter / request | {context.get('matter') or 'to be completed'} |",
             f"| Purpose | {context.get('purpose') or 'to be completed'} |",
             f"| Reviewer | {context.get('reviewer') or 'to be completed'} |",
             f"| Data categories redacted | {categories} |",
             f"| Decisions | {decisions} ({status.get('accepted', 0)} accepted, "
             f"{status.get('rejected', 0)} rejected, {status.get('pending', 0)} pending) |",
             f"| Processing environment | local by default; OCR engine: {engine_text} |",
             "| Technical measures | human review gate; true content removal; independent "
             "verification of the export; this evidence report |",
             f"| Residual risks | {warnings} verifier warning(s), {failures} failure(s), "
             f"{len(review.get('acknowledged_findings') or [])} acknowledged finding(s); "
             "detector coverage gaps above |",
             "| Retention / transfers / legal basis | to be completed by the controller |",
             ""]
    return lines


def write_evidence(path: str | Path, evidence: dict[str, Any]) -> Path:
    """Write the report as Markdown (default) or JSON (path ends in .json)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() == ".json":
        target.write_text(json.dumps(evidence, indent=1), encoding="utf-8")
    else:
        target.write_text(_md(evidence), encoding="utf-8")
    return target
