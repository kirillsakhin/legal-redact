"""pdfredeval adapter for the local `legal-redact` CLI (out-of-tree, not upstream).

Tool surface: `legal-redact:desktop`.
The framework's tool id grammar is `vendor:surface` with surface in
{web, api, desktop} (pdfredeval/tools/base.py:TOOL_ID_RE), so the task's suggested
`legal-redact:cli` cannot be registered without patching upstream. `:desktop` is the
documented surface for "the installed binary", which is exactly what a local CLI is.

Everything downstream of submission is the framework's: this module only drives the
tool's documented CLI and returns the bytes it produced.

Documented CLI flow (legal-redact/README.md):

    legal-redact suggest IN.pdf -o decisions.json --policy counterparty --detector ensemble
    # every decision's status set to accepted (no document text in the decision file)
    legal-redact redact IN.pdf --decisions decisions.json -o OUT.pdf

`redact` runs the tool's independent export verifier; when it finds a leak it withholds
the file and exits non-zero. That refusal is a *result*, not an adapter error: the
shipped tool fails closed. The adapter records it and raises SubmissionRejected, so
the run is documented (and not scored) under the framework's own protocol.

Optional profile key `diagnostic_keep_failed: true` exists ONLY for the evaluator's
diagnostic: it calls the tool's own Python API with `keep_failed=True`, which writes the
file the verifier refused to release (as `<name>.FAILED-VERIFICATION`). A run made with
it is NOT the shipped tool's behaviour; the flag is recorded verbatim in the manifest so
no reader can mistake it for one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar, Mapping

from pdfredeval.capabilities import Capabilities
from pdfredeval.errors import SubmissionFailed, SubmissionRejected
from pdfredeval.tools.base import Handle, Tool
from pdfredeval.types import (
    ORIENTATIONS,
    POLARITIES,
    Case,
    Mode,
    Stage,
    Status,
    Transport,
)

# --- where the tool lives (overridable by environment; never hard-coded into results) ---
# default: the repository this adapter is shipped in (scripts/pdfredeval_adapter/legal_redact_adapter/adapter.py)
_DEFAULT_HOME = Path(__file__).resolve().parents[3]
LEGAL_REDACT_HOME = Path(os.environ.get("LEGAL_REDACT_HOME") or _DEFAULT_HOME)
LEGAL_REDACT_BIN = Path(
    os.environ.get(
        "LEGAL_REDACT_BIN", str(LEGAL_REDACT_HOME / ".venv" / "Scripts" / "legal-redact.exe")
    )
)
LEGAL_REDACT_PY = Path(
    os.environ.get(
        "LEGAL_REDACT_PY", str(LEGAL_REDACT_HOME / ".venv" / "Scripts" / "python.exe")
    )
)
LEGAL_REDACT_SRC = LEGAL_REDACT_HOME / "src"

#: The HF cache that actually holds the GLiNER weights on this machine. The ambient
#: HF_HOME here points at an empty directory, so the adapter pins the working one.
HF_HOME = os.environ.get("LEGAL_REDACT_HF_HOME", str(Path.home() / ".cache" / "huggingface"))

#: Categories the shipped policies can act on. Benchmark category names are mapped to the
#: tool's own categories (detect/base.py): PERSON, EMAIL, PHONE, ADDRESS, ID (passport /
#: tax / national identifiers), IBAN, CARD, DOB. NATIONAL_ID/PASSPORT/VAT_ID are the
#: benchmark's names for the one `ID` category; the rest are declared unsupported.
SUPPORTED_CATEGORIES = frozenset(
    {"PERSON", "EMAIL", "PHONE", "ADDRESS", "IBAN", "CARD", "DOB",
     "NATIONAL_ID", "PASSPORT", "VAT_ID"}
)

CAPABILITIES = Capabilities(
    # Text extraction (PyMuPDF), detection (rules + Presidio + GLiNER ensemble), real
    # removal (text deleted from the content stream, fresh file) - all three stages.
    stages=frozenset({Stage.EXTRACTION, Stage.DETECTION, Stage.REDACTION}),
    categories=SUPPORTED_CATEGORIES,
    # Text layer only: no OCR, so no probe that exists solely as pixels is claimed.
    # Metadata is claimed because the export always scrubs every metadata value.
    channels=frozenset({"text_layer", "metadata"}),
    # Policies are per-category, but the benchmark profile does not select a policy, so
    # per-type control is not exercised by this run - do not claim it.
    per_type_control=False,
    max_pages=None,
    max_bytes=None,
    formats=frozenset({"pdf"}),
    mode=Mode.SYNC,
    deterministic=True,
    # The text layer is read whatever the orientation / polarity: no rendering-condition
    # support is refused here, so a rotated or stacked run that survives is a real miss.
    orientations=frozenset(ORIENTATIONS),
    polarities=frozenset(POLARITIES),
    # Born-digital vector text only; every print-*/hand-* level is pixels (no OCR).
    provenances=frozenset({"vector"}),
)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HF_HOME": HF_HOME,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    return env


def _run(cmd: list[str], *, cwd: Path, timeout: float = 1800.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class LegalRedactDesktop(Tool):
    tool_id: ClassVar[str] = "legal-redact:desktop"
    transport: ClassVar[Transport] = Transport.API
    url: ClassVar[str | None] = "local CLI (offline)"

    @property
    def capabilities(self) -> Capabilities:
        return CAPABILITIES

    @property
    def version(self) -> str | None:
        """Package version + git commit of the checkout being measured."""
        try:
            out = subprocess.run(
                ["git", "-C", str(LEGAL_REDACT_HOME), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=30,
            )
            commit = out.stdout.strip() if out.returncode == 0 else "unknown"
        except Exception:
            commit = "unknown"
        return f"0.0.1+{commit}"

    # --- settings ------------------------------------------------------------------

    def normalize_settings(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        """Map the normalized profile onto the tool's CLI options, verbatim in the manifest."""
        return {
            "command": "legal-redact suggest|redact (documented batch CLI)",
            "policy": str(profile.get("policy", "counterparty")),
            "detector": str(profile.get("detector", "ensemble")),
            "accept": str(profile.get("accept", "all")),
            "acknowledge_preflight": "all REVIEW codes from `legal-redact inspect`",
            "diagnostic_keep_failed": bool(profile.get("diagnostic_keep_failed", False)),
            "binary": str(LEGAL_REDACT_BIN),
        }

    # --- submission ----------------------------------------------------------------

    def _submit(self, case: Case, settings: Mapping[str, Any], handle: Handle) -> None:
        work = handle.run_dir / "tool"
        work.mkdir(parents=True, exist_ok=True)
        inp = work / "input.pdf"
        inp.write_bytes(case.pdf_bytes)

        diagnostic = bool(settings.get("diagnostic_keep_failed"))
        notes: list[str] = []

        # 1. pre-flight: collect the REVIEW codes the export gate needs acknowledged.
        pre = _run([str(LEGAL_REDACT_BIN), "inspect", str(inp)], cwd=work)
        _write(work / "01-inspect.log", pre.stdout + pre.stderr)
        review_codes = sorted(
            {m.group(1) for m in re.finditer(r"^REVIEW\s+(PF\d+)", pre.stdout, re.M)}
        )
        if "REFUSE" in pre.stdout:
            handle.extra["notes"] = "tool pre-flight refuses this PDF"
            raise SubmissionRejected(
                f"{self.tool_id}: legal-redact pre-flight refuses the input "
                f"({', '.join(review_codes) or 'REFUSE'})"
            )

        # 2. suggest -> decision file; then accept every suggestion (documented batch flow).
        sug = _run(
            [str(LEGAL_REDACT_BIN), "suggest", str(inp), "-o", "decisions.json",
             "--policy", str(settings["policy"]), "--detector", str(settings["detector"])],
            cwd=work,
        )
        _write(work / "02-suggest.log", sug.stdout + sug.stderr)
        dec_path = work / "decisions.json"
        if sug.returncode != 0 or not dec_path.exists():
            raise SubmissionFailed(f"{self.tool_id}: `legal-redact suggest` failed")

        data = json.loads(dec_path.read_text(encoding="utf-8"))
        decisions = data.get("decisions", [])
        accepted = 0
        for d in decisions:
            if settings["accept"] == "all":
                if d.get("status") != "accepted":
                    d["status"] = "accepted"
                    accepted += 1
            elif d.get("proposed") == "redact":
                if d.get("status") != "accepted":
                    d["status"] = "accepted"
                    accepted += 1
        dec_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        _write(
            work / "03-decisions-summary.json",
            json.dumps(
                {
                    "suggestions": len(decisions),
                    "accepted": sum(1 for d in decisions if d.get("status") == "accepted"),
                    "newly_accepted": accepted,
                    "by_category": _counts(d.get("category", "") for d in decisions),
                    "by_proposed": _counts(d.get("proposed", "") for d in decisions),
                },
                indent=2, sort_keys=True,
            ),
        )
        notes.append(f"{len(decisions)} suggestions, all accepted")

        ack = ",".join(review_codes)

        # 3. redact -> the tool's own verifier decides whether the file may be released.
        if diagnostic:
            produced = work / "engine-only.pdf"
            _write(work / "keep_failed.py", _KEEP_FAILED_SCRIPT.format(
                src=str(LEGAL_REDACT_SRC), inp=str(inp), out=str(produced),
                dec=str(dec_path),
            ))
            proc = _run(
                [str(LEGAL_REDACT_PY), str(work / "keep_failed.py")], cwd=work
            )
            _write(work / "04-redact-diagnostic.log", proc.stdout + proc.stderr)
            failed = Path(str(produced) + ".FAILED-VERIFICATION")
            delivered = failed if failed.exists() else produced
            if not delivered.exists():
                raise SubmissionFailed(
                    f"{self.tool_id}: diagnostic keep_failed export produced no file"
                )
            notes.append(
                "DIAGNOSTIC ONLY - the file the shipped verifier withheld "
                "(keep_failed); the tool would not release it"
            )
        else:
            red = _run(
                [str(LEGAL_REDACT_BIN), "redact", str(inp), "--decisions", "decisions.json",
                 "-o", "output.pdf", "--ack", ack],
                cwd=work,
            )
            _write(work / "04-redact.log", red.stdout + red.stderr)
            delivered = work / "output.pdf"
            if red.returncode != 0 or not delivered.exists():
                reason = _refusal_summary(red.stdout + red.stderr)
                handle.extra["notes"] = reason
                _write(work / "STATUS.json", json.dumps(
                    {"status": "rejected", "reason": reason}, indent=2))
                raise SubmissionRejected(f"{self.tool_id}: {reason}")

        # 4. hand the delivered bytes to `collect` through a private copy; the framework
        #    writes them byte-identically into the run directory and hashes them.
        private = work / "delivered.bin"
        private.write_bytes(delivered.read_bytes())
        handle.extra["delivered_rel"] = str(private.relative_to(handle.run_dir))
        handle.extra["notes"] = "; ".join(notes) if notes else None
        _write(work / "STATUS.json", json.dumps({"status": "ready"}, indent=2))

    # --- collection ----------------------------------------------------------------

    def poll(self, handle: Handle) -> Status:
        status_file = handle.run_dir / "tool" / "STATUS.json"
        if not status_file.exists():
            return Status.PENDING
        state = json.loads(status_file.read_text(encoding="utf-8")).get("status")
        return {
            "ready": Status.READY,
            "rejected": Status.REJECTED,
            "failed": Status.FAILED,
        }.get(state, Status.PENDING)

    def fetch(self, handle: Handle) -> bytes:
        rel = handle.extra.get("delivered_rel")
        if not rel:
            raise SubmissionFailed(f"{self.tool_id}: no delivered bytes recorded")
        path = handle.run_dir / rel
        if not path.exists():
            raise SubmissionFailed(f"{self.tool_id}: {path} is missing")
        return path.read_bytes()

    def manifest(self, handle: Handle, *, output_sha256: str | None = None):
        m = super().manifest(handle, output_sha256=output_sha256)
        m.notes = handle.extra.get("notes") or None
        return m


def round_trip(name: str) -> str:
    return name


#: Diagnostic only: the tool's own API with keep_failed=True. `export` still runs the
#: independent verifier, still refuses to release the file, and writes what it rejected.
_KEEP_FAILED_SCRIPT = """import sys
sys.path.insert(0, {src!r})
from legal_redact.pipeline import ExportRefused, export, load_decisions

s = load_decisions({inp!r}, {dec!r})
for finding in s.findings:
    s.acknowledge(finding.code)
try:
    report = export({inp!r}, {out!r}, s, keep_failed=True)
    print("EXPORTED", report.passed)
except ExportRefused as exc:
    print("EXPORT_REFUSED", exc)
    if exc.report:
        print(exc.report.format())
"""


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))


def _refusal_summary(text: str) -> str:
    """The verifier's own one-line verdict plus the failing check details."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    refused = next((ln for ln in lines if ln.startswith("REFUSED")), "")
    fails = [ln for ln in lines if ln.startswith("FAIL")]
    head = refused or "export refused"
    if fails:
        head += " | " + " | ".join(fails)
    return head
