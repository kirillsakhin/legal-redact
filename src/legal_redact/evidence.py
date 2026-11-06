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

from .policy import Decision
from .review import ReviewSession
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
    """Collect the evidence record (pure: reads the two files for hashes)."""
    decisions = list(session.decisions)
    review = session.report()
    checks = []
    for check in report.checks:
        checks.append({"id": getattr(check, "id", ""),
                       "name": getattr(check, "name", ""),
                       "status": getattr(check, "status", ""),
                       "detail": getattr(check, "detail", "")})
    engine = dict(session.ocr_info) if isinstance(session.ocr_info, dict) else None
    return {
        "schema": SCHEMA,
        "disclaimer": DISCLAIMER,
        "no_network": NO_NETWORK,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": _tool_info(),
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
    lines += ["", "## Data handling", "", evidence["no_network"], ""]
    return "\n".join(lines)


def write_evidence(path: str | Path, evidence: dict[str, Any]) -> Path:
    """Write the report as Markdown (default) or JSON (path ends in .json)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() == ".json":
        target.write_text(json.dumps(evidence, indent=1), encoding="utf-8")
    else:
        target.write_text(_md(evidence), encoding="utf-8")
    return target
