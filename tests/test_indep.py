"""Tests for holdout v5: the independent-data suite built on CUAD contract text.

Same contract as the other held-out suites: the pin fails if generation changes,
offsets index the final text exactly, the renderer is deterministic and
one-page, and the vendored third-party excerpts match their recorded SHA-256.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter

import pymupdf
import pytest

from legal_redact.bench import indep
from legal_redact.bench.run import pages_for
from legal_redact.bench.synth import (ADDRESS, CARD, DOB, EMAIL, ID, IBAN,
                                      PERSON, PHONE)

GOLD_CATEGORIES = {PERSON, EMAIL, PHONE, ADDRESS, IBAN, CARD, ID, DOB}
PROTECTED_KINDS = {"party", "amount", "date", "law", "section"}


@pytest.fixture(scope="module")
def docs():
    return indep.indep_corpus()


def _compact(s: str) -> str:
    return "".join(ch for ch in s if not ch.isspace())


# ---- pin -------------------------------------------------------------------
def test_indep_suite_matches_its_pin():
    """If this fails the generator (or a vendored excerpt) changed: v5 numbers are void."""
    assert indep.fingerprint() == indep.PIN_FILE.read_text().strip()


def test_pin_looks_like_a_sha256():
    assert re.fullmatch(r"[0-9a-f]{64}\n?", indep.PIN_FILE.read_text())


def test_v5_is_not_wired_into_the_other_pins():
    """v5 must be scored on its own, not folded into the spent suites."""
    pins = [indep.PIN_FILE.read_text().strip()]
    for name in ("FROZEN_SHA256", "HOLDOUT_SHA256", "HOLDOUT4_SHA256"):
        other = indep.HERE / name
        assert other.exists()
        assert other.read_text().strip() not in pins
        pins.append(other.read_text().strip())
    assert len(set(pins)) == 4


# ---- corpus shape and metadata ---------------------------------------------
def test_corpus_size_and_shape(docs):
    assert len(docs) == 100 == len(indep.SOURCES) * indep.VARIANTS
    assert len({d.doc_id for d in docs}) == len(docs)
    per_template = Counter(d.template for d in docs)
    assert len(per_template) == 20
    assert set(per_template.values()) == {indep.VARIANTS}
    assert {d.template for d in docs} == {f"cuad-{s['id']}" for s in indep.SOURCES}
    assert {d.seed for d in docs} == {indep.SEED0 + v for v in range(indep.VARIANTS)}


def test_sources_are_vendored_and_hashed():
    assert len(indep.SOURCES) == 20
    assert len({s["id"] for s in indep.SOURCES}) == 20
    assert len({s["sha256"] for s in indep.SOURCES}) == 20
    base = ("https://huggingface.co/datasets/theatticusproject/cuad/resolve/"
            + indep.DATASET_REVISION + "/CUAD_v1/full_contract_txt/Part_I/")
    for src in indep.SOURCES:
        assert hashlib.sha256(src["text"].encode()).hexdigest() == src["sha256"]
        assert src["url"].startswith(base)
        assert src["text"].isascii()
        assert 1500 < len(src["text"]) < 3500     # one page, vendored not the dataset
        assert {"id", "contract_type", "source_file", "page_index",
                "url", "sha256", "text"} <= set(src)


def test_notice_records_the_provenance():
    notice = indep.NOTICE_FILE.read_text(encoding="utf-8")
    assert indep.DATASET in notice
    assert indep.LICENCE in notice
    assert indep.DATASET_REVISION in notice
    assert "TheAtticusProject/cuad" in notice
    assert "faker" in notice
    for src in indep.SOURCES:
        assert src["sha256"] in notice
        assert src["id"] in notice


# ---- labels ----------------------------------------------------------------
def test_every_document_covers_all_categories(docs):
    for d in docs:
        assert {g.category for g in d.gold} == GOLD_CATEGORIES
        assert {p.kind for p in d.protected} == PROTECTED_KINDS
    gold = Counter(g.category for d in docs for g in d.gold)
    assert min(gold.values()) >= 100
    kinds = Counter(p.kind for d in docs for p in d.protected)
    assert min(kinds.values()) >= 100


def test_gold_offsets_index_the_final_text(docs):
    """Every gold span must be the injected value, exactly, in the final text."""
    for d in docs:
        for g in d.gold:
            value = d.text[g.start:g.end]
            assert value and value == value.strip()
            assert g.category in GOLD_CATEGORIES
            compact = _compact(value)
            if g.category == EMAIL:
                assert re.fullmatch(r"[^\s@]+@[^\s@]+", value)
            elif g.category == IBAN:
                assert re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", compact)
            elif g.category == CARD:
                assert re.fullmatch(r"\d{15,16}", compact)
            elif g.category == PHONE:
                assert re.fullmatch(r"\+?[\d][\d ()-]{8,}\d", compact)
            elif g.category == DOB:
                assert re.fullmatch(r"\d{1,2}[A-Za-z]+\d{4}", compact)
            elif g.category == ADDRESS:
                assert "," in value and re.search(r"\d", value)
            elif g.category == ID:
                assert re.search(r"\d", compact)
            elif g.category == PERSON:
                assert re.fullmatch(r"[A-Za-z'.-]+(?:[\s]+[A-Za-z'.-]+)+", value)


def test_offsets_match_the_injected_values(docs):
    """Recompose the same (source, variant) and compare label text, not just shape."""
    by_id = {s["id"]: s for s in indep.SOURCES}
    for d in docs:
        src = by_id[d.template.removeprefix("cuad-")]
        logical, gold, prot = indep._compose(src, d.seed - indep.SEED0)
        assert len(gold) == len(d.gold) and len(prot) == len(d.protected)
        for got, want in zip(d.gold, gold):
            assert got.category == want[2]
            assert _compact(d.text[got.start:got.end]) == _compact(logical[want[0]:want[1]])
        for got, want in zip(d.protected, prot):
            assert got.kind == want[2]
            assert _compact(d.text[got.start:got.end]) == _compact(logical[want[0]:want[1]])


# ---- rendering and determinism --------------------------------------------
def test_pdf_is_one_page_and_reproduces_its_text(docs):
    for d in docs:
        assert d.pdf, f"{d.doc_id}: no PDF rendered"
        assert pymupdf.open(stream=d.pdf, filetype="pdf").page_count == 1
        assert pages_for(d)[0].text == d.text


def test_generation_is_deterministic():
    """Same seeds -> byte-identical text and labels (the pin is the cross-run guard)."""
    first = indep.indep_corpus()
    second = indep.indep_corpus()
    key = lambda d: (d.doc_id, d.template, d.seed, d.text, tuple(d.gold), tuple(d.protected))
    assert [key(d) for d in first] == [key(d) for d in second]


def test_renderer_is_neutral_and_deterministic():
    """One US Letter page with a real text layer; a second render gives the same text.

    (The raw PDF bytes are not compared: PyMuPDF stamps a per-run file ID, which
    is irrelevant here -- the pin is over text and labels, and the corpus
    determinism test above covers the rendered outcome.)
    """
    text, _, _ = indep._compose(indep.SOURCES[0], 0)
    data = indep._render(text)
    doc = pymupdf.open(stream=data, filetype="pdf")
    assert doc.page_count == 1
    assert doc[0].rect.width == 612 and doc[0].rect.height == 792   # US Letter
    assert doc[0].get_text().strip()          # a real text layer, not an image
    again = indep._render(text)
    assert (indep.extract_pages(pymupdf.open(stream=again, filetype="pdf"))[0].text
            == indep.extract_pages(pymupdf.open(stream=data, filetype="pdf"))[0].text)


def test_no_cjk_or_control_characters_in_text(docs):
    for d in docs:
        assert d.text.isascii()
        assert not any(ord(ch) < 32 and ch != "\n" for ch in d.text)
