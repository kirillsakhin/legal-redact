"""Guards against the two biggest sources of collateral damage, and the
name-resolution fixes that the v4 damage diagnosis pointed at.

Each of these tests failed before the guard existed and passes after it (see
`docs/BENCHMARKS.md` and the change report). The project rule is that a guard
you cannot make fail is not a guard; the companion tests here fail if the guard
is made too aggressive and drops real personal data.
"""
import pytest

from legal_redact.detect.base import Suggestion
from legal_redact.detect.ensemble import EnsembleDetector
from legal_redact.models import PageText


def page(text):
    return [PageText(0, 1, 1, text)]


class Fake:
    """A detector that returns fixed suggestions, so a guard can be tested alone."""

    name = "fake"

    def __init__(self, suggestions):
        self.suggestions = suggestions

    def detect(self, pages):
        return list(self.suggestions)


def sug(category, text, needle, conf=0.7, detector="fake"):
    start = text.index(needle)
    return Suggestion(0, start, start + len(needle), category, "why", conf, detector)


# ---- a contract date is not a date of birth -------------------------------------------

def test_signed_on_date_is_not_suggested_as_date_of_birth():
    """The v4 damage: GLiNER called the 'Signed on <date>' line a DOB with no birth keyword."""
    t = "Signed on 27 August 2026 near Chelsea Road by Alice Example."
    out = EnsembleDetector([Fake([sug("DOB", t, "27 August 2026")])]).detect(page(t))
    assert [s.category for s in out] == []


@pytest.mark.parametrize("text,needle", [
    ("The Borrower, born on 12 May 1985, accepts.", "12 May 1985"),
    ("Date of birth: 12/05/1985.", "12/05/1985"),
    ("DOB 1985-05-12 on file.", "1985-05-12"),
    ("DOB        12 May 1985", "12 May 1985"),            # table-header form, same line
    ("born on 3 January 1990 and still resident.", "3 January 1990"),
])
def test_date_with_birth_keyword_stays_a_dob(text, needle):
    out = EnsembleDetector([Fake([sug("DOB", text, needle)])]).detect(page(text))
    assert [s.category for s in out] == ["DOB"]


def test_birth_keyword_on_the_value_line_is_enough_even_in_a_long_line():
    prefix = "The parties record the following particulars: " * 2
    text = prefix + "date of birth 1 February 1991"
    out = EnsembleDetector([Fake([sug("DOB", text, "1 February 1991")])]).detect(page(text))
    assert [s.category for s in out] == ["DOB"]


# ---- a personal-name box inside an organisation name is not personal data ------------

@pytest.mark.parametrize("text,needle", [
    ("Notices to Studio Lombardi Sorensen shall be sent.", "Lombardi Sorensen"),
    ("Payments to Ivanov Okafor LLP on receipt.", "Ivanov Okafor LLP"),
    ("Signed for Al-Rashid Hartmann Associates.", "Al-Rashid Hartmann Associates"),
    ("Introduced by Studio von Trapp Papadakis; English law governs.", "Trapp Papadakis"),
    ("The provider is Whitfield & Co today.", "Whitfield"),
])
def test_person_inside_a_firm_name_is_not_suggested(text, needle):
    out = EnsembleDetector([Fake([sug("PERSON", text, needle)])]).detect(page(text))
    assert [s.category for s in out] == []


@pytest.mark.parametrize("text,needle", [
    ("Notices to Alice Example at the London office.", "Alice Example"),
    ("Signed by Ms Alice Example, Director.", "Alice Example"),
    ("The Borrower is Bob Sample of Orchard Road.", "Bob Sample"),
    ("Provider: Carmen Duarte. Client: Hugo Brandt.", "Carmen Duarte"),
])
def test_real_person_names_are_kept_next_to_org_words(text, needle):
    out = EnsembleDetector([Fake([sug("PERSON", text, needle)])]).detect(page(text))
    assert [s.category for s in out] == ["PERSON"]


# ---- a currency amount is not an identifier -------------------------------------------

@pytest.mark.parametrize("text,needle", [
    ("The guaranteed amount is USD 837,000. Payments follow.", "837,000"),
    ("Fees total GBP 707,000 per year.", "707,000"),
    ("Pay EUR 12,000 on signature.", "12,000"),
    ("Fees total 837,000 USD per year.", "837,000"),
])
def test_number_with_adjacent_currency_is_not_an_identifier(text, needle):
    out = EnsembleDetector([Fake([sug("ID", text, needle, conf=0.31)])]).detect(page(text))
    assert [s.category for s in out] == []


def test_identifier_without_currency_context_is_kept():
    t = "Passport No. X1234567 on file."
    out = EnsembleDetector([Fake([sug("ID", t, "X1234567", conf=0.9)])]).detect(page(t))
    assert [s.category for s in out] == ["ID"]
