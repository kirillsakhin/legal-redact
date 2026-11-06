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
            "blockers": self.blockers(),
        }
