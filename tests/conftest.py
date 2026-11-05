"""Synthetic fixtures only. Every name below is invented."""
import pymupdf
import pytest

BODY = ["This Agreement is made between Alice Example and Bob Sample.",
        "Notice address: alice@example.test, tel +1 555 0100."]


def _pdf(path, lines=BODY, pages=1):
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 20
    doc.save(path)
    doc.close()
    return str(path)


@pytest.fixture
def make_pdf(tmp_path):
    def _make(name="doc.pdf", **kw):
        return _pdf(tmp_path / name, **kw)
    return _make


@pytest.fixture
def plain_pdf(make_pdf):
    return make_pdf()
