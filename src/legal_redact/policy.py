"""Stage 3a: turn detector suggestions into proposed decisions under a policy.

A policy answers one question per category, for ONE disclosure purpose:
redact, keep, or review. The bundled policies are illustrative examples, not
legal advice: what must go depends on the matter, the recipient and the law.
Low-confidence suggestions are never auto-proposed for redaction; they are
routed to `review` so a human looks at them.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .detect.base import Category, Suggestion

REDACT, KEEP, REVIEW = "redact", "keep", "review"
_VALID = {REDACT, KEEP, REVIEW}


@dataclass(frozen=True)
class Policy:
    name: str
    description: str
    actions: dict[str, str]
    min_confidence: float = 0.0

    def action_for(self, s: Suggestion) -> str:
        a = self.actions.get(s.category, REVIEW)       # unknown category -> human
        if a == REDACT and s.confidence < self.min_confidence:
            return REVIEW
        return a


def _parse(data: dict) -> Policy:
    actions = data.get("actions", {})
    bad = {k: v for k, v in actions.items() if v not in _VALID or k not in Category.ALL}
    if bad:
        raise ValueError(f"invalid policy entries: {sorted(bad)}")
    missing = [c for c in Category.ALL if c not in actions]
    if missing:
        raise ValueError(f"policy must state an action for every category; missing {missing}")
    return Policy(data["name"], data.get("description", ""), dict(actions),
                  float(data.get("min_confidence", 0.0)))


def load_policy(name_or_path: str) -> Policy:
    p = Path(name_or_path)
    if p.suffix == ".toml" and p.exists():
        return _parse(tomllib.loads(p.read_text(encoding="utf-8")))
    ref = resources.files("legal_redact.policies").joinpath(f"{name_or_path}.toml")
    return _parse(tomllib.loads(ref.read_text(encoding="utf-8")))


@dataclass
class Decision:
    page: int
    start: int
    end: int
    category: str
    reason: str
    detector: str
    confidence: float
    proposed: str                      # redact | keep | review (from policy)
    status: str = "pending"            # pending | accepted | rejected
    source: str = "detector"           # detector | manual


def propose(suggestions: list[Suggestion], policy: Policy) -> list[Decision]:
    out = []
    for s in suggestions:
        a = policy.action_for(s)
        out.append(Decision(s.page, s.start, s.end, s.category, s.reason, s.detector,
                            s.confidence, a, "rejected" if a == KEEP else "pending"))
    return out
