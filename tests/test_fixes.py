import pytest

from legal_redact.bench.holdout import HOLDOUT_TEMPLATES, fingerprint, holdout_corpus, PIN_FILE
from legal_redact.bench.run import pages_for
from legal_redact.detect.base import Suggestion
from legal_redact.detect.ensemble import EnsembleDetector, check_structured
from legal_redact.detect.rules import RulesDetector
from legal_redact.detect.textview import decapitalise, flatten
from legal_redact.models import PageText


def page(text):
    return [PageText(0, 1, 1, text)]


def test_rules_find_an_iban_that_wraps_across_lines():
    t = "pay to GB82 WEST 1234\n5698 7654 32 today"
    out = [s for s in RulesDetector(names=False).detect(page(t)) if s.category == "IBAN"]
    assert out and "\n" in t[out[0].start:out[0].end]


def test_names_switch_removes_person_heuristic():
    t = "Signed by Alice Example."
    assert any(s.category == "PERSON" for s in RulesDetector().detect(page(t)))
    assert not any(s.category == "PERSON" for s in RulesDetector(names=False).detect(page(t)))


def test_views_preserve_length_and_offsets():
    t = "Signed: ALICE EXAMPLE\nand BOB O'BRIEN"
    assert len(flatten(t)) == len(t) and "\n" not in flatten(t)
    d = decapitalise(t)
    assert d and len(d) == len(t) and d.startswith("Signed: Alice Example")
    assert decapitalise("nothing in caps here") is None


def test_card_failing_luhn_is_demoted_valid_one_is_raised():
    bad = Suggestion(0, 0, 19, "CARD", "m", 0.97, "g")
    good = Suggestion(0, 0, 19, "CARD", "m", 0.5, "g")
    assert check_structured(bad, "4111 1111 1111 1112").confidence <= 0.3
    assert check_structured(good, "4111 1111 1111 1111").confidence == 0.99


def test_model_mislabelled_iban_is_relabelled_by_checksum():
    s = Suggestion(0, 0, 22, "CARD", "m", 0.9, "g")
    out = check_structured(s, "GB82WEST12345698765432")
    assert out.category == "IBAN" and out.confidence == 0.99


def test_ensemble_applies_checks_before_merging():
    class M:
        name = "m"
        def detect(self, pages):
            return [Suggestion(0, 0, 19, "CARD", "m", 0.97, "m")]
    out = EnsembleDetector([M()]).detect(page("4111 1111 1111 1112"))
    assert out[0].confidence <= 0.3


def test_holdout_matches_its_pin_and_aligns():
    assert fingerprint() == PIN_FILE.read_text().strip()
    docs = holdout_corpus(per_template=2)
    assert {d.template for d in docs} == set(HOLDOUT_TEMPLATES)
    for d in docs:
        assert pages_for(d)[0].text == d.text


def test_holdout4_matches_its_pin_and_aligns():
    from legal_redact.bench.holdout import HOLDOUT4_TEMPLATES, PIN_FILE_4, fingerprint4, holdout4_corpus
    assert fingerprint4() == PIN_FILE_4.read_text().strip()
    docs = holdout4_corpus(per_template=2)
    assert {d.template for d in docs} == set(HOLDOUT4_TEMPLATES)
    for d in docs:
        assert pages_for(d)[0].text == d.text


def test_resolution_drops_amounts_and_contained_noise_but_never_coverage():
    from legal_redact.detect.ensemble import drop_amounts, suppress_contained
    t = {0: "pay GBP 381,000 to GB82WEST12345698765432 now"}
    amount = Suggestion(0, 4, 15, "ID", "m", 0.4, "g")
    iban = Suggestion(0, 19, 41, "IBAN", "m", 0.99, "r")
    inner = Suggestion(0, 19, 25, "PERSON", "m", 0.85, "p")
    assert drop_amounts([amount, iban], t) == [iban]
    assert suppress_contained([iban, inner]) == [iban]
    lone = Suggestion(0, 0, 10, "PERSON", "m", 0.9, "p")
    assert suppress_contained([lone]) == [lone]


def test_person_inside_organisation_is_demoted_not_dropped():
    from legal_redact.detect.ensemble import veto_persons
    p = Suggestion(0, 0, 10, "PERSON", "m", 0.9, "p")
    org = Suggestion(0, 0, 14, "NOT_PERSON", "o", 0.8, "g")
    out = veto_persons([p], [org])
    assert len(out) == 1 and out[0].confidence <= 0.3 and "organisation" in out[0].reason


def test_veto_evidence_never_leaves_the_ensemble():
    class G:
        name = "g"
        def detect(self, pages):
            return [Suggestion(0, 0, 4, "NOT_PERSON", "o", 0.9, "g"),
                    Suggestion(0, 6, 14, "EMAIL", "e", 0.9, "g")]
    out = EnsembleDetector([G()]).detect(page("Acme  a@b.test"))
    assert [s.category for s in out] == ["EMAIL"]


def test_bridge_joins_surname_comma_firstname_only_when_enabled():
    class P:
        name = "p"
        def detect(self, pages):
            return [Suggestion(0, 0, 3, "PERSON", "m", 0.9, "p"), Suggestion(0, 5, 10, "PERSON", "m", 0.9, "p")]
    pg = page("Kim, Aoife")
    assert len(EnsembleDetector([P()]).detect(pg)) == 2
    out = EnsembleDetector([P()], bridge=True).detect(pg)
    assert [(s.start, s.end) for s in out] == [(0, 10)]
    far = page("Kim and Aoife")
    assert len(EnsembleDetector([P()], bridge=True).detect(far)) == 2
