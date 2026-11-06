"""Stage 3b: the human review state machine.

This is the legal gate. The model recommends, the lawyer decides, and export is
impossible until:
  1. no pre-flight REFUSE finding exists,
  2. every proposed item has an explicit accept/reject, and
  3. every pre-flight REVIEW finding has been acknowledged by code.
The report produced here contains counts and codes only, never document text.
"""
from __future__ import annotations

from collections import Counter

from .models import Finding, Severity
from .policy import Decision

MANUAL = "MANUAL"

# Reviewer attestations required before an export. They do not make the tool a
# compliance certificate: they are the human steps a redaction workflow depends on
# (the detectors do not cover these), recorded so the evidence report can show them.
ATTESTATIONS: dict[str, str] = {
    "special_categories": (
        "I checked for special-category data (Art. 9: health, genetic, biometric, religion, "
        "sexual orientation, criminal-offence data) manually; the detectors do not cover it "
        "comprehensively."
    ),
    "indirect_identifiers": (
        "I considered indirect identifiers and re-identification risk (context, rare facts, "
        "combinations of quasi-identifiers, small populations)."
    ),
    "authorised": (
        "I am authorised to make this disclosure or export, applied the right policy, and "
        "reviewed the pre-flight findings and limitations."
    ),
}
REQUIRED_ATTESTATIONS: tuple[str, ...] = tuple(ATTESTATIONS)


def attestation_payload(session: "ReviewSession") -> dict:
    """The review page needs the required statements and which ones are already given."""
    return {"required": [{"id": i, "label": ATTESTATIONS[i]} for i in REQUIRED_ATTESTATIONS],
            "given": list(session.attestation),
            "context": dict(session.context)}


def missing_attestations(ids) -> list[str]:
    """Required attestation ids not present in `ids` (unknown ids are ignored here)."""
    given = {str(i) for i in (ids or ())}
    return [i for i in REQUIRED_ATTESTATIONS if i not in given]


class ReviewSession:
    def __init__(self, decisions: list[Decision], findings: list[Finding],
                 ocr_info: dict | None = None, policy: str | None = None):
        self.decisions = decisions
        self.findings = findings
        self.acknowledged: set[str] = set()
        # Engine pin recorded by suggest ({"engine","version","local","cloud"}); export
        # refuses when the current engine differs. None on sessions that never OCR'd.
        self.ocr_info = ocr_info
        # Policy name recorded by suggest; used by the compliance evidence report.
        self.policy = policy
        # Attestation ids the reviewer completed for this export (see ATTESTATIONS).
        self.attestation: list[str] = []
        # Optional per-matter context for the evidence report (controller, matter,
        # purpose, reviewer). Configuration only, never document text.
        self.context: dict[str, str] = {}

    # --- decisions ---------------------------------------------------------
    def accept(self, i: int) -> None:
        self.decisions[i].status = "accepted"

    def reject(self, i: int) -> None:
        self.decisions[i].status = "rejected"

    def accept_all_proposed_redactions(self) -> int:
        """Explicit bulk action: accept every pending item the policy proposes to redact.
        Items proposed as `review` stay pending: they need a human look."""
        n = 0
        for d in self.decisions:
            if d.status == "pending" and d.proposed == "redact":
                d.status, n = "accepted", n + 1
        return n

    def add_manual(self, page: int, start: int, end: int, note: str = "added by reviewer") -> int:
        self.decisions.append(Decision(page, start, end, MANUAL, note, "reviewer", 1.0,
                                       "redact", "accepted", "manual"))
        return len(self.decisions) - 1

    def add_manual_rect(self, page: int, rect: tuple, note: str = "added by reviewer") -> int:
        """A box the reviewer drew on the page; it has no text offsets."""
        self.decisions.append(Decision(page, 0, 0, MANUAL, note, "reviewer", 1.0, "redact",
                                       "accepted", "manual", (tuple(float(v) for v in rect),)))
        return len(self.decisions) - 1

    def remove_manual(self, i: int) -> None:
        if self.decisions[i].source != "manual":
            raise ValueError("only boxes added by the reviewer can be removed; reject detector suggestions instead")
        self.decisions[i].status = "rejected"

    def acknowledge(self, code: str, on: bool = True) -> None:
        (self.acknowledged.add if on else self.acknowledged.discard)(code)

    def set_attestation(self, ids) -> None:
        """Record completed attestation ids; raises ValueError on an unknown id."""
        known = [i for i in dict.fromkeys(str(x) for x in ids) if i]
        unknown = [i for i in known if i not in ATTESTATIONS]
        if unknown:
            raise ValueError(f"unknown attestation id(s): {', '.join(unknown)}; "
                             f"expected {', '.join(REQUIRED_ATTESTATIONS)}")
        self.attestation = known

    def missing_attestations(self) -> list[str]:
        return [i for i in REQUIRED_ATTESTATIONS if i not in self.attestation]

    # --- gate --------------------------------------------------------------
    def pending(self) -> list[int]:
        return [i for i, d in enumerate(self.decisions) if d.status == "pending"]

    def blockers(self) -> list[str]:
        out = [f"refused by pre-flight {f.code}" for f in self.findings
               if f.severity is Severity.REFUSE]
        if self.pending():
            out.append(f"{len(self.pending())} suggestion(s) have no decision")
        for code in sorted({f.code for f in self.findings
                            if f.severity is Severity.REVIEW} - self.acknowledged):
            out.append(f"pre-flight {code} not acknowledged")
        return out

    def can_export(self) -> bool:
        return not self.blockers()

    def redactions(self) -> list[Decision]:
        if not self.can_export():
            raise PermissionError("export blocked: " + "; ".join(self.blockers()))
        return [d for d in self.decisions if d.status == "accepted"]

    def report(self) -> dict:
        return {
            "by_status": dict(Counter(d.status for d in self.decisions)),
            "by_category": dict(Counter(d.category for d in self.decisions if d.status == "accepted")),
            "manual_additions": sum(d.source == "manual" for d in self.decisions),
            "acknowledged_findings": sorted(self.acknowledged),
            "policy": self.policy,
            "attestations": list(self.attestation),
            "missing_attestations": self.missing_attestations(),
            "context": dict(self.context),
            "blockers": self.blockers(),
        }
