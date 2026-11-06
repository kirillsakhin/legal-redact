"""Phase 3: review-workload guards and policy tuning.

The review queue is mostly true personal data that is held back because only one
detector found it, plus two avoidable kinds of noise: a street name boxed as a
PERSON inside an address that is already being redacted, and a lone Presidio
name at its usual 0.85 score held for review by a 0.9 strong-confidence bar.
"""
from legal_redact.detect.base import Suggestion
from legal_redact.detect.ensemble import EnsembleDetector
from legal_redact.models import PageText
from legal_redact.policy import load_policy, propose


def page(text):
    return [PageText(0, 1, 1, text)]


class Fake:
    name = "fake"

    def __init__(self, suggestions):
        self.suggestions = suggestions

    def detect(self, pages):
        return list(self.suggestions)


def sug(category, text, needle, conf=0.7, detector="fake"):
    start = text.index(needle)
    return Suggestion(0, start, start + len(needle), category, "why", conf, detector)


def test_person_box_inside_a_street_address_is_not_suggested():
    """'Willow Lane' is the street in an address that the ADDRESS box already redacts."""
    t = "Invoices go to 14 Willow Lane, Brookfield, BC4 5QR."
    addr = sug("ADDRESS", t, "14 Willow Lane, Brookfield, BC4 5QR", conf=0.6, detector="g")
    person = sug("PERSON", t, "Willow Lane", conf=0.85)
    out = EnsembleDetector([Fake([addr, person])]).detect(page(t))
    assert [s.category for s in out] == ["ADDRESS"]


def test_person_name_beside_an_address_is_kept():
    t = "Notices to Alice Example at 14 Willow Lane, Brookfield."
    addr = sug("ADDRESS", t, "14 Willow Lane", conf=0.6, detector="g")
    person = sug("PERSON", t, "Alice Example")
    out = EnsembleDetector([Fake([addr, person])]).detect(page(t))
    assert {s.category for s in out} == {"ADDRESS", "PERSON"}


def test_the_address_guard_keeps_a_real_name_and_address_apart():
    t = "Provider: Carmen Duarte, 87 Mill Road, Eastmere."
    addr = sug("ADDRESS", t, "87 Mill Road", conf=0.6, detector="g")
    person = sug("PERSON", t, "Carmen Duarte")
    out = EnsembleDetector([Fake([addr, person])]).detect(page(t))
    assert {s.category for s in out} == {"ADDRESS", "PERSON"}



def test_counterparty_redacts_a_lone_presidio_score_name():
    """0.85 is Presidio's usual score for a clean single-detector name."""
    pol = load_policy("counterparty")
    d = propose([Suggestion(0, 0, 5, "PERSON", "why", 0.85, "presidio-lg")], pol)[0]
    assert d.proposed == "redact"


def test_counterparty_still_sends_a_lone_unsure_name_to_review():
    pol = load_policy("counterparty")
    d = propose([Suggestion(0, 0, 5, "PERSON", "why", 0.8, "gliner:gliner-pii-base-v1.0")], pol)[0]
    assert d.proposed == "review"


def test_counterparty_still_requires_two_detectors_below_the_strong_bar():
    pol = load_policy("counterparty")
    one = propose([Suggestion(0, 0, 5, "PERSON", "why", 0.7, "gliner:x", support=1)], pol)[0]
    two = propose([Suggestion(0, 0, 5, "PERSON", "why", 0.7, "ensemble", support=2)], pol)[0]
    assert one.proposed == "review" and two.proposed == "redact"


def test_min_confidence_still_routes_low_scores_to_review():
    pol = load_policy("counterparty")
    d = propose([Suggestion(0, 0, 19, "CARD", "why", 0.3, "gliner:x")], pol)[0]
    assert d.proposed == "review"
