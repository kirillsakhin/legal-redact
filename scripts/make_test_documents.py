"""Create safe synthetic test documents for hands-on testing of legal-redact.

Everything here is invented: synthetic contracts with planted personal data and
protected lookalikes. Nothing is a real document.

Usage:
    python scripts/make_test_documents.py --out testdocs

It writes (into --out):
    contract.pdf               text-layer PDF contract (planted personal data)
    contract-scan.pdf          image-only scan of that page (for OCR testing)
    contract-rotated.pdf        the scan rotated 90 degrees (orientation handling)
    contract.docx              Word document: a name split across runs, a comment,
                               metadata, planted personal data
    contract-tracked.docx      the same plus a tracked insertion (expected REFUSAL)
    contract-large-image.docx  a full-page image (expected REFUSAL)
    planted.json               every planted value, so you can search the outputs
                               for them (none may survive a redaction)

The .docx files need python-docx (the `dev` extra). The PDFs need PyMuPDF (core).
"""

from __future__ import annotations

import argparse
import json
from io import BytesIO
from pathlib import Path

# --- the planted data and the protected lookalikes --------------------------
NAME = "Alice Example"
EMAIL = "alice.example@example.test"
PHONE = "+44 7700 900123"
IBAN = "DE89370400440532013000"
CARD = "4111 1111 1111 1111"
DOB = "12 March 1985"
ADDRESS = "12 Meadow Lane, Bristol BS1 4TT"
# Presentation data that must survive: amounts, dates, party, governing law.
AMOUNT = "USD 12,000"
DATE = "3 March 2026"
PARTY = "Northwind Trading Ltd"
LAW = "the laws of England and Wales"

PLANTED = {
    "PERSON (a name)": NAME,
    "EMAIL": EMAIL,
    "PHONE": PHONE,
    "IBAN": IBAN,
    "CARD (Luhn-valid)": CARD,
    "DOB": DOB,
    "ADDRESS": ADDRESS,
}
PROTECTED = {"amount": AMOUNT, "date": DATE, "party": PARTY, "governing law": LAW}

BODY = [
    "SERVICES AGREEMENT",
    "",
    f"This agreement is made on {DATE} between {PARTY} and the consultant {NAME},",
    f"of {ADDRESS}, e-mail {EMAIL}, telephone {PHONE}.",
    "",
    f"The day rate is {AMOUNT}, payable to IBAN {IBAN} or card {CARD}.",
    f"Date of birth: {DOB}.",
    "",
    f"Signed: {NAME}",
    f"This agreement is governed by {LAW}.",
]


def _lines():
    return [line for line in BODY]


# --- PDFs -------------------------------------------------------------------
def make_pdf(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    y = 72.0
    for line in _lines():
        if line:
            page.insert_text((72, y), line, fontsize=11)
        y += 20.0
    doc.save(path)
    doc.close()


def _image_only_pdf(src: Path, dst: Path, rotate: int = 0) -> None:
    import pymupdf

    src_doc = pymupdf.open(src)
    page = src_doc[0]
    width, height = page.rect.width, page.rect.height      # page is dead after close
    scale = 200 / 72                      # ~200 dpi
    matrix = pymupdf.Matrix(scale, scale)
    if rotate:
        matrix = matrix.prerotate(rotate)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    src_doc.close()

    # JPEG keeps the fixture small (a 200 dpi PNG page would be ~11 MB), and is
    # what a real scan looks like. OCR quality is unaffected at this size.
    jpeg = pix.tobytes("jpeg")
    out = pymupdf.open()
    out_page = out.new_page(width=width, height=height)
    out_page.insert_image(out_page.rect, stream=jpeg)
    out.save(dst)
    out.close()


# --- DOCX -------------------------------------------------------------------
def _docx_base():
    from docx import Document

    doc = Document()
    doc.add_heading("SERVICES AGREEMENT", 0)
    p = doc.add_paragraph("This agreement is made on ")
    p.add_run(DATE)
    p.add_run(" between ")
    p.add_run(PARTY)
    p.add_run(" and the consultant ")
    # The name is deliberately split across three runs.
    p.add_run("Alice ")
    p.add_run("Exa")
    p.add_run("mple")
    p.add_run(", of ")
    p.add_run(ADDRESS)
    p.add_run(", e-mail ")
    p.add_run(EMAIL)
    p.add_run(", telephone ")
    p.add_run(PHONE)
    p.add_run(".")
    doc.add_paragraph(
        f"The day rate is {AMOUNT}, payable to IBAN {IBAN} or card {CARD}."
    )
    doc.add_paragraph(f"Date of birth: {DOB}.")
    doc.add_paragraph(f"Signed: {NAME}.")
    doc.add_paragraph(f"This agreement is governed by {LAW}.")
    props = doc.core_properties
    props.author = "Jordan Blake"
    props.last_modified_by = "Jordan Blake"
    props.title = "Services agreement - Jordan Blake"
    props.comments = f"Prepared by Jordan Blake, {EMAIL}"
    return doc


def make_docx(path: Path) -> None:
    from docx import Document

    doc = _docx_base()
    # A comment whose text and author both contain personal data: it must be removed.
    para = doc.paragraphs[1]
    doc.add_comment(para.runs, text=f"{NAME} - passport copy on file, call {PHONE}",
                    author="Jordan Blake")
    doc.save(path)


def make_tracked_docx(path: Path) -> None:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = _docx_base()
    p = doc.add_paragraph("Additional clause: ")
    run = p.add_run(f"the fee is payable to {NAME} at IBAN {IBAN}")
    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), "1")
    ins.set(qn("w:author"), "Jordan Blake")
    ins.set(qn("w:date"), "2026-11-06T10:00:00Z")
    p._p.remove(run._r)                   # type: ignore[attr-defined]
    ins.append(run._r)                    # type: ignore[attr-defined]
    p._p.append(ins)                      # type: ignore[attr-defined]
    doc.save(path)


def make_large_image_docx(path: Path, tmp_pdf: Path) -> None:
    from docx import Document
    from docx.shared import Inches
    import pymupdf

    # Render the synthetic page to a PNG and paste it across the text area: a
    # "scanned page pasted into Word", which the tool must refuse (its text is
    # pixels it cannot read or redact).
    src = pymupdf.open(tmp_pdf)
    pix = src[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    src.close()

    doc = Document()
    doc.add_paragraph("Pasted scan (image only) - the tool should refuse this file.")
    section = doc.sections[0]
    width = section.page_width - section.left_margin - section.right_margin
    doc.add_picture(BytesIO(pix.tobytes("png")), width=width)
    doc.save(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="testdocs", help="output directory (default: testdocs)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    make_pdf(out / "contract.pdf")
    _image_only_pdf(out / "contract.pdf", out / "contract-scan.pdf")
    _image_only_pdf(out / "contract.pdf", out / "contract-rotated.pdf", rotate=90)

    try:
        make_docx(out / "contract.docx")
        make_tracked_docx(out / "contract-tracked.docx")
        make_large_image_docx(out / "contract-large-image.docx", out / "contract.pdf")
    except ImportError:
        print("python-docx is not installed: install the dev extra "
              "(pip install -e \".[dev]\") to generate the .docx fixtures.")

    (out / "planted.json").write_text(json.dumps(
        {"must_be_removed": PLANTED, "must_survive": PROTECTED}, indent=1), encoding="utf-8")
    print(f"wrote fixtures to {out.resolve()}")
    print("search every redacted output for the values in planted.json['must_be_removed']:")
    for label, value in PLANTED.items():
        print(f"  {label:22} {value}")
    print("and check that these are still there:")
    for label, value in PROTECTED.items():
        print(f"  {label:22} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
