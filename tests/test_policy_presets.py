"""Purpose policy presets: they load, cover every category, and rank review strictness.

These are the user-facing presets added with the compliance-positioning work
(docs/COMPLIANCE.md). The tests are intentionally about the data contract, not
about legal advice: they check that each preset can be loaded by name, states a
valid action for every detectable category, and that the strictness ordering the
descriptions promise is real (a higher bar sends more lone names to a human).
"""
import pytest

from legal_redact.detect.base import Category
from legal_redact.policy import _VALID, load_policy

PRESETS = ("contract-sharing", "dsar-disclosure", "publication")


@pytest.mark.parametrize("name", PRESETS)
def test_preset_loads_and_covers_every_category(name):
    policy = load_policy(name)
    assert policy.name == name
    assert policy.description, "a preset must say what it is for"
    assert set(policy.actions) == set(Category.ALL)
    assert all(action in _VALID for action in policy.actions.values())
    # All personal-data categories are proposed for redaction in every preset.
    assert all(policy.actions[c] == "redact" for c in Category.ALL)
    assert set(policy.consensus) <= set(Category.ALL)


def test_the_presets_rank_review_strictness_as_described():
    sharing = load_policy("contract-sharing")
    dsar = load_policy("dsar-disclosure")
    publication = load_policy("publication")
    # A higher strong_confidence means a lone detector must be surer before a name is
    # auto-proposed; everything below it goes to the human.
    assert sharing.strong_confidence < dsar.strong_confidence < publication.strong_confidence
    # Weak suggestions are routed to review in every preset, never auto-approved.
    assert all(p.min_confidence >= 0.5 for p in (sharing, dsar, publication))


def test_descriptions_state_the_limits_not_just_the_purpose():
    """A preset must not read like a compliance certificate."""
    for name in PRESETS:
        description = load_policy(name).description.lower()
        assert "not legal advice" in description
