import pytest

from legal_redact.detect.base import Suggestion
from legal_redact.detect.ensemble import EnsembleDetector
from legal_redact.models import Finding, PageText, Severity
from legal_redact.policy import load_policy, propose
from legal_redact.review import ReviewSession


def S(a, b, cat="PERSON", conf=0.9, det="t"):
    return Suggestion(0, a, b, cat, "why", conf, det)


def test_bundled_policies_load_and_differ():
    c, a = load_policy("counterparty"), load_policy("auditor")
    assert c.actions["PERSON"] == "redact" and a.actions["PERSON"] == "review"


def test_policy_must_cover_every_category(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('name="x"\n[actions]\nPERSON="redact"\n')
    with pytest.raises(ValueError):
        load_policy(str(p))


def test_low_confidence_redaction_goes_to_human():
    pol = load_policy("counterparty")
    d = propose([S(0, 5, conf=0.2)], pol)[0]
    assert d.proposed == "review" and d.status == "pending"


def test_keep_is_not_redacted_by_default():
    pol = load_policy("auditor")
    d = propose([S(0, 5, "PERSON")], pol)[0]
    assert d.proposed == "review"


def test_export_blocked_until_every_item_decided_and_findings_acknowledged():
    pol = load_policy("counterparty")
    ds = propose([S(0, 5), S(10, 15)], pol)
    fs = [Finding("PF007", Severity.REVIEW, "annotation", 0)]
    rs = ReviewSession(ds, fs)
    assert not rs.can_export()
    with pytest.raises(PermissionError):
        rs.redactions()
    rs.accept(0)
    rs.reject(1)
    assert "PF007" in " ".join(rs.blockers())
    rs.acknowledge("PF007")
    assert rs.can_export() and len(rs.redactions()) == 1


def test_refuse_finding_cannot_be_acknowledged_away():
    rs = ReviewSession([], [Finding("PF003", Severity.REFUSE, "scan")])
    rs.acknowledge("PF003")
    assert not rs.can_export()


def test_bulk_accept_leaves_review_items_pending():
    ds = propose([S(0, 5, "PERSON"), S(10, 20, "IBAN")], load_policy("auditor"))
    rs = ReviewSession(ds, [])
    assert rs.accept_all_proposed_redactions() == 1
    assert len(rs.pending()) == 1


def test_manual_addition_is_accepted_and_reported_without_text():
    rs = ReviewSession([], [])
    rs.add_manual(0, 3, 9)
    r = rs.report()
    assert r["manual_additions"] == 1 and rs.can_export()
    assert "why" not in str(r)


def test_ensemble_merges_overlaps_by_union_and_names_sources():
    class A:
        name = "a"
        def detect(self, pages): return [S(0, 5, det="a", conf=0.6)]
    class B:
        name = "b"
        def detect(self, pages): return [S(3, 9, det="b", conf=0.8), S(20, 25, "EMAIL", det="b")]
    out = EnsembleDetector([A(), B()]).detect([PageText(0, 1, 1, "x" * 30)])
    assert [(s.start, s.end, s.category) for s in out] == [(0, 9, "PERSON"), (20, 25, "EMAIL")]
    assert "a, b" in out[0].reason
