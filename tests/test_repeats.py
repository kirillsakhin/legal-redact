import os

import pymupdf
import pytest

from legal_redact.extract import extract_pages
from legal_redact.pipeline import ExportRefused, add_repeats, export, load_decisions, save_decisions
from legal_redact.policy import Decision
from legal_redact.repeats import find_repeats
from legal_redact.review import ReviewSession

LINES = ["Agreement with Alice Example today.",
         "Signed: ALICE EXAMPLE and Kimberly Stone.",
         "Alice Example again, and Kim alone."]


def make_pdf(tmp_path, lines=LINES):
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((72, 80 + 14 * i), line, fontsize=9)
    p = tmp_path / "in.pdf"
    doc.save(p)
    return str(p), extract_pages(pymupdf.open(p))


def dec(pages, text, status="accepted", category="PERSON", nth=0):
    t, s = pages[0].text, -1
    for _ in range(nth + 1):
        s = t.index(text, s + 1)
    d = Decision(0, s, s + len(text), category, "r", "d", 0.9, "redact", status)
    d.rects = tuple((r.x0, r.y0, r.x1, r.y1) for r in pages[0].locate(s, s + len(text)))
    return d


def texts(pages, found):
    return [pages[0].text[d.start:d.end] for d in found]


def test_other_occurrences_are_proposed_ignoring_case(tmp_path):
    _, pages = make_pdf(tmp_path)
    out = find_repeats([dec(pages, "Alice Example")], pages)
    assert texts(pages, out) == ["ALICE EXAMPLE", "Alice Example"]
    assert all(d.status == "pending" and d.source == "repeat" and d.rects for d in out)


def test_word_boundaries_kim_does_not_match_inside_kimberly(tmp_path):
    _, pages = make_pdf(tmp_path, ["Kim met Kimberly. Later Kim left. Kimberly stayed."])
    got = find_repeats([dec(pages, "Kim")], pages)           # accepts the first, standalone "Kim"
    assert [pages[0].text[d.start:d.end] for d in got] == ["Kim"]       # only the second standalone Kim
    assert pages[0].text[got[0].start - 6:got[0].start] == "Later "


def test_already_covered_text_is_not_proposed_again(tmp_path):
    _, pages = make_pdf(tmp_path)
    first, second = dec(pages, "Alice Example"), dec(pages, "ALICE EXAMPLE")
    assert texts(pages, find_repeats([first, second], pages)) == ["Alice Example"]


def test_text_the_reviewer_rejected_elsewhere_is_never_proposed(tmp_path):
    _, pages = make_pdf(tmp_path)
    accepted = dec(pages, "Alice Example")
    rejected = dec(pages, "Alice Example", status="rejected", nth=1)
    assert find_repeats([accepted, rejected], pages) == []


def test_pending_decisions_do_not_trigger_proposals(tmp_path):
    _, pages = make_pdf(tmp_path)
    assert find_repeats([dec(pages, "Alice Example", status="pending")], pages) == []


def test_short_texts_are_skipped(tmp_path):
    _, pages = make_pdf(tmp_path, ["We met on the 7th, ok? We, ok."])
    assert find_repeats([dec(pages, "ok")], pages) == []


def test_text_under_a_manual_box_is_used(tmp_path):
    _, pages = make_pdf(tmp_path)
    base = dec(pages, "Alice Example")
    manual = Decision(0, 0, 0, "MANUAL", "m", "reviewer", 1.0, "redact", "accepted", "manual", base.rects)
    assert texts(pages, find_repeats([manual], pages)) == ["ALICE EXAMPLE", "Alice Example"]


def test_occurrence_split_across_a_line_break_is_found(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.insert_textbox(pymupdf.Rect(20, 20, 110, 120),
                        "Meet Alice Example now. Later Alice Example left.", fontsize=11)
    p = tmp_path / "w.pdf"
    doc.save(p)
    pages = extract_pages(pymupdf.open(p))
    first = pages[0].text.index("Alice")
    d = Decision(0, first, first + len("Alice Example"), "PERSON", "r", "d", 0.9, "redact", "accepted")
    got = find_repeats([d], pages)
    assert len(got) == 1 and got[0].start > first


def test_unredacted_repeat_blocks_export_until_repeats_are_added(tmp_path):
    path, pages = make_pdf(tmp_path)
    s = ReviewSession([dec(pages, "Alice Example")], [])
    out = str(tmp_path / "o.pdf")
    with pytest.raises(ExportRefused) as e:
        export(path, out, s)
    assert e.value.report and not e.value.report.passed and not os.path.exists(out)
    assert add_repeats(path, s, accept=True) == 2
    assert export(path, out, s).passed
    text = pymupdf.open(out)[0].get_text().lower()
    assert "alice" not in text and "kimberly stone" in text            # unrelated text survives


def test_repeats_survive_the_decision_file_roundtrip(tmp_path):
    path, pages = make_pdf(tmp_path)
    s = ReviewSession([dec(pages, "Alice Example")], [])
    add_repeats(path, s)
    f = str(tmp_path / "d.json")
    save_decisions(s, f)
    assert [d.source for d in load_decisions(path, f).decisions].count("repeat") == 2


def test_wrapped_name_is_one_needle_so_a_kept_surname_elsewhere_only_warns(tmp_path):
    """A name wrapped over two lines is one redacted passage. Its surname surviving inside a kept
    firm name is a warning to look at, not proof of a leak; judging each line alone refused it."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=300)
    page.insert_textbox(pymupdf.Rect(20, 20, 66, 250),
                        "Signed by Alice Example today. Counsel: Example Holdings Ltd.", fontsize=11)
    p = tmp_path / "wrap.pdf"
    doc.save(p)
    pages = extract_pages(pymupdf.open(p))
    t = pages[0].text
    s = t.index("Alice")
    d = Decision(0, s, s + len("Alice Example"), "PERSON", "r", "d", 0.9, "redact", "accepted")
    d.rects = tuple((r.x0, r.y0, r.x1, r.y1) for r in pages[0].locate(d.start, d.end))
    assert len(d.rects) == 2, "the name must wrap for this test to mean anything"
    out = str(tmp_path / "o.pdf")
    report = export(str(p), out, ReviewSession([d], []))
    assert report.passed
    assert any(c.status == "WARN" and "fragment" in c.detail for c in report.checks)
