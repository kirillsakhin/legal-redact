import pymupdf

from legal_redact.extract import extract_pages


def test_text_and_word_offsets_are_consistent(plain_pdf):
    pages = extract_pages(pymupdf.open(plain_pdf))
    p = pages[0]
    assert "Alice Example" in p.text
    for w in p.words:                      # every word must round-trip by offset
        assert p.text[w.start:w.end] == w.text


def test_locate_returns_box_over_the_right_words(plain_pdf):
    doc = pymupdf.open(plain_pdf)
    p = extract_pages(doc)[0]
    s = p.text.index("Alice Example")
    rects = p.locate(s, s + len("Alice Example"))
    assert len(rects) == 1                  # one line -> one rectangle
    r = rects[0]
    clip = pymupdf.Rect(r.x0, r.y0, r.x1, r.y1)
    # Independent check: asking PyMuPDF for the text under our box returns the name.
    assert "Alice Example" in doc[0].get_text("text", clip=clip)
    assert "Bob" not in doc[0].get_text("text", clip=clip)


def test_span_over_two_lines_gives_two_rectangles(plain_pdf):
    p = extract_pages(pymupdf.open(plain_pdf))[0]
    s = p.text.index("Bob Sample")
    e = p.text.index("alice@") + 5
    assert len(p.locate(s, e)) == 2


def test_multipage_pages_are_separate(make_pdf):
    pages = extract_pages(pymupdf.open(make_pdf(pages=3)))
    assert [p.page for p in pages] == [0, 1, 2]
