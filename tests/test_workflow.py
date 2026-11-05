from legal_redact.bench.synth import corpus
from legal_redact.bench.workflow import workflow_summary
from legal_redact.detect.base import Suggestion
from legal_redact.policy import load_policy


def _rows(make):
    return [(d, make(d)) for d in corpus(2)]


def sug(g, support=1, conf=0.95):
    return Suggestion(0, g.start, g.end, g.category, "r", conf, "t", support)


def test_oracle_review_of_perfect_suggestions_leaks_nothing():
    pol = load_policy("counterparty")
    w = workflow_summary("perfect", _rows(lambda d: [sug(g) for g in d.gold]), pol)
    assert w.leak_reviewed[0.0] == 0.0 and w.auto_false_share == 0.0


def test_lone_unsure_person_goes_to_queue_not_auto():
    pol = load_policy("counterparty")
    rows = _rows(lambda d: [sug(g, support=1, conf=0.6 if g.category == "PERSON" else 0.95)
                            for g in d.gold])
    w = workflow_summary("weak", rows, pol)
    assert w.queue_per_doc > 0 and w.leak_auto_only == 1.0   # ignoring the queue leaks
    assert w.leak_reviewed[0.0] == 0.0                       # reading it does not


def test_agreement_or_high_confidence_skips_the_queue():
    pol = load_policy("counterparty")
    for kw in ({"support": 2, "conf": 0.6}, {"support": 1, "conf": 0.95}):
        rows = _rows(lambda d: [sug(g, **kw) for g in d.gold if g.category == "PERSON"])
        assert workflow_summary("x", rows, pol).queue_per_doc == 0


def test_reviewer_errors_increase_leaks():
    pol = load_policy("counterparty")
    rows = _rows(lambda d: [sug(g, conf=0.6) for g in d.gold])
    w = workflow_summary("weak", rows, pol)
    assert w.leak_reviewed[0.10] >= w.leak_reviewed[0.0]
