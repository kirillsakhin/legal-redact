from pathlib import Path

import pytest

from legal_redact.bench.frozen import FROZEN_TEMPLATES, PIN_FILE, fingerprint, generate
from legal_redact.bench.run import pages_for


def test_frozen_suite_matches_its_pin():
    """If this fails the generator changed: old frozen-suite numbers are void."""
    assert fingerprint() == PIN_FILE.read_text().strip()


@pytest.mark.parametrize("t", FROZEN_TEMPLATES)
def test_gold_aligns_with_extracted_text_after_wrapping(t):
    for s in range(1000, 1006):
        d = generate(t, s)
        assert pages_for(d)[0].text == d.text
        for g in d.gold:
            v = d.text[g.start:g.end]
            assert v and v == v.strip()


def test_wrapping_really_splits_some_entities():
    split = 0
    for t in FROZEN_TEMPLATES:
        for s in range(1000, 1025):
            d = generate(t, s)
            split += sum("\n" in d.text[g.start:g.end] for g in d.gold)
    assert split > 0          # otherwise the suite is not harder than the dev corpus
