import pytest

from legal_redact.bench.metrics import score_doc, summarize
from legal_redact.bench.run import leave_template_out, pages_for
from legal_redact.bench.synth import TEMPLATES, corpus, generate
from legal_redact.detect.base import Suggestion
from legal_redact.detect.rules import RulesDetector, iban_ok, luhn_ok


def test_generation_is_deterministic():
    assert generate("nda", 3).text == generate("nda", 3).text
    assert generate("nda", 3).text != generate("nda", 4).text


@pytest.mark.parametrize("t", TEMPLATES)
def test_pdf_round_trip_reproduces_labelled_text(t):
    for s in range(5):
        d = generate(t, s)
        assert pages_for(d)[0].text == d.text      # raises if not


def test_gold_offsets_point_at_the_right_text():
    d = generate("loan", 1)
    for g in d.gold:
        assert d.text[g.start:g.end].strip() == d.text[g.start:g.end]
        assert d.text[g.start:g.end]


def test_generated_identifiers_pass_their_checksums():
    for s in range(20):
        d = generate("loan", s)
        for g in d.gold:
            v = d.text[g.start:g.end]
            if g.category == "IBAN":
                assert iban_ok(v)
            if g.category == "CARD":
                assert luhn_ok(v)


def test_checksum_validators_reject_garbage():
    assert not iban_ok("GB00 0000 0000 0000 0000 00")
    assert not luhn_ok("4111 1111 1111 1112")
    assert luhn_ok("4111 1111 1111 1111")


def _sug(a, b, cat="PERSON"):
    return Suggestion(0, a, b, cat, "x", 1.0, "t")


def test_scoring_leak_vs_cover_and_overredaction():
    d = generate("nda", 0)
    g = d.gold[0]
    full = score_doc(d, [_sug(g.start, g.end, g.category)])
    part = score_doc(d, [_sug(g.start, g.end - 1, g.category)])
    assert (g.category, g.start, g.end) not in full.leaks
    assert (g.category, g.start, g.end) in part.leaks          # partial cover = leak
    assert part.tp_r[g.category] == 1 and part.strict_r[g.category] == 0
    p = d.protected[0]
    assert score_doc(d, [_sug(p.start, p.end)]).protected_damaged == 1


def test_empty_detector_leaks_every_document():
    class Nothing:
        name = "nothing"
        def detect(self, pages): return []
    s, _ = leave_template_out(Nothing, corpus(3))
    assert s.doc_leak_rate == 1.0


def test_oracle_detector_has_zero_leaks_and_no_damage():
    """Guard check: the harness must be able to report a perfect run, too."""
    docs = {d.doc_id: d for d in corpus(3)}

    class Oracle:
        name = "oracle"
        def detect(self, pages):
            d = next(x for x in docs.values() if x.text == pages[0].text)
            return [_sug(g.start, g.end, g.category) for g in d.gold]
    s, _ = leave_template_out(Oracle, list(docs.values()))
    assert s.doc_leak_rate == 0.0 and s.protected_damage_rate == 0.0
    assert all(m["recall"] == 1.0 and m["precision"] == 1.0 for m in s.per_category.values())


def test_rules_baseline_runs_and_is_imperfect_on_names():
    s, _ = leave_template_out(RulesDetector, corpus(5))
    assert s.per_category["EMAIL"]["recall"] == 1.0
    assert s.per_category["IBAN"]["recall"] == 1.0
    assert s.per_category["PERSON"]["recall"] < 1.0   # all-caps signatures are missed
