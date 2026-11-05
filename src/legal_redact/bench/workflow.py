"""Review-workflow simulation: how much human work, and what is left behind.

The policy splits suggestions into AUTO (proposed for redaction) and QUEUE
(the lawyer must look). A simulated reviewer then decides the queue with an
error rate `err`: a true item is wrongly rejected, or a false item wrongly
accepted, with probability err. err=0 is an ORACLE reviewer and an upper bound
that no human reaches; 0.05 and 0.10 show how fast real-world fatigue erodes it.
This is a model of a reviewer, not a measurement of one.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..policy import Policy
from .metrics import _overlap, score_doc
from .synth import SynthDoc

ERRS = (0.0, 0.05, 0.10)


def _true(doc: SynthDoc, s) -> bool:
    return any(g.category == s.category and _overlap(g.start, g.end, s.start, s.end)
               for g in doc.gold)


@dataclass
class WorkflowSummary:
    detector: str
    docs: int
    gold_per_doc: float
    suggestions_per_doc: float
    queue_per_doc: float
    auto_false_share: float          # share of AUTO items that are not real personal data
    leak_auto_only: float            # queue ignored
    leak_reviewed: dict              # err -> share of docs with a residual leak
    damage_reviewed: dict            # err -> share of protected spans damaged


def workflow_summary(name: str, rows, policy: Policy, seed: int = 7) -> WorkflowSummary:
    n = len(rows)
    auto_n = auto_false = queue_n = sug_n = gold_n = 0
    leak_auto = 0
    leak = {e: 0 for e in ERRS}
    dmg = {e: [0, 0] for e in ERRS}
    for doc, sugs in rows:
        auto = [s for s in sugs if policy.action_for(s) == "redact"]
        queue = [s for s in sugs if policy.action_for(s) == "review"]
        auto_n += len(auto)
        auto_false += sum(not _true(doc, s) for s in auto)
        queue_n += len(queue)
        sug_n += len(sugs)
        gold_n += len(doc.gold)
        leak_auto += bool(score_doc(doc, auto).leaks)
        for e in ERRS:
            rng = random.Random(f"{seed}:{doc.doc_id}:{e}")
            final = list(auto)
            for s in queue:
                t = _true(doc, s)
                if (rng.random() >= e) if t else (rng.random() < e):
                    final.append(s)
            sc = score_doc(doc, final)
            leak[e] += bool(sc.leaks)
            dmg[e][0] += sc.protected_damaged
            dmg[e][1] += sc.protected_total
    return WorkflowSummary(
        name, n, gold_n / n, sug_n / n, queue_n / n,
        auto_false / auto_n if auto_n else 0.0, leak_auto / n,
        {e: leak[e] / n for e in ERRS}, {e: dmg[e][0] / dmg[e][1] for e in ERRS})


def format_workflow(ws: list[WorkflowSummary], policy: Policy) -> str:
    out = [f"review workflow under policy '{policy.name}' (simulated reviewer; err=0 is an oracle)",
           f"{'detector':38} {'sugg/doc':>8} {'queue/doc':>9} {'auto FP':>8} {'leak,no review':>14} "
           f"{'leak e=0':>8} {'e=5%':>6} {'e=10%':>6} {'prot dmg e=5%':>13}"]
    for w in ws:
        out.append(f"{w.detector:38} {w.suggestions_per_doc:8.1f} {w.queue_per_doc:9.1f} "
                   f"{w.auto_false_share:8.0%} {w.leak_auto_only:14.0%} {w.leak_reviewed[0.0]:8.0%} "
                   f"{w.leak_reviewed[0.05]:6.0%} {w.leak_reviewed[0.10]:6.0%} {w.damage_reviewed[0.05]:13.1%}")
    out.append(f"gold personal-data spans per doc: {ws[0].gold_per_doc:.1f} (the unavoidable minimum to review)")
    return "\n".join(out)
