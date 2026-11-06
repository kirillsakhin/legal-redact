"""Build the committed docx-review fixture and render it with Word (dev-time only)."""
from __future__ import annotations

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
FIXTURE_DIR = REPO / "tests" / "fixtures" / "docx_review"
FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

import docx
from docx.shared import Pt

TEXTBOX_XML = (
    '<w:p><w:r><w:drawing>'
    '<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
    'relativeHeight="251658240" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
    '<wp:simplePos x="0" y="0"/>'
    '<wp:positionH relativeFrom="column"><wp:posOffset>720000</wp:posOffset></wp:positionH>'
    '<wp:positionV relativeFrom="paragraph"><wp:posOffset>180000</wp:posOffset></wp:positionV>'
    '<wp:extent cx="3600000" cy="500000"/>'
    '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
    '<wp:wrapNone/>'
    '<wp:docPr id="77" name="Side note"/>'
    '<wp:cNvGraphicFramePr/>'
    '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
    '<wps:wsp xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
    '<wps:cNvSpPr txBox="1"/>'
    '<wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="3600000" cy="500000"/></a:xfrm>'
    '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></wps:spPr>'
    '<wps:txbx><w:txbxContent><w:p><w:r><w:t>Floating note: Alice Example</w:t></w:r></w:p>'
    "</w:txbxContent></wps:txbx><wps:bodyPr/></wps:wsp>"
    "</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r></w:p>"
)

FILLER = [
    "The Consultant shall provide the Services with reasonable skill and care and in "
    "accordance with the terms of this Agreement and the applicable schedule.",
    "The Client shall pay the fees set out in Schedule 1 within thirty days of the date "
    "of each valid invoice, without set-off or deduction unless required by law.",
    "Either party may terminate this Agreement on thirty days written notice to the other "
    "party, without prejudice to rights accrued before termination.",
    "The Consultant shall keep confidential all information provided by the Client and "
    "shall use it only for the purposes of providing the Services.",
    "Nothing in this Agreement creates a partnership, joint venture or employment "
    "relationship between the parties or their respective personnel.",
    "This Agreement is governed by the laws of England and Wales and the parties submit "
    "to the exclusive jurisdiction of the courts of England and Wales.",
    "The Consultant shall maintain adequate insurance cover in respect of the Services "
    "and shall provide evidence of cover on request.",
    "Any notice under this Agreement shall be in writing and delivered by hand, recorded "
    "delivery or electronic mail to the address of the relevant party.",
    "The parties acknowledge that time is not of the essence in respect of any obligation "
    "under this Agreement except where expressly stated otherwise.",
    "This Agreement and the schedules constitute the entire agreement between the parties "
    "and supersede all prior discussions and arrangements.",
    "No variation of this Agreement shall be effective unless made in writing and signed "
    "by authorised representatives of both parties.",
    "The Consultant may engage subcontractors only with the prior written consent of the "
    "Client, and remains responsible for their acts and omissions.",
]


def build(tmp: Path) -> Path:
    document = docx.Document()
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    document.core_properties.creator = "Synthetic Fixture"
    document.core_properties.last_modified_by = "Synthetic Fixture"
    document.add_heading("Consulting Agreement", 0)
    document.add_paragraph(
        "This Consulting Agreement is made between Alice Example (the Consultant) and "
        "Nova Systems Ltd (the Client).")
    document.add_paragraph(
        "The Consultant's contact details are alice@example.test and +1 555 0100.")
    document.add_paragraph("Notice address: 82 Station Lane, Northvale, GF8 7TR.")
    document.add_paragraph(
        "The parties record that the Consultant, born on 12 May 1985, is engaged for the "
        "Services described in Schedule 1.")
    document.add_heading("1. The Services", level=1)
    for paragraph in FILLER[:6]:
        document.add_paragraph(paragraph)
    document.add_heading("2. Schedule 1", level=1)
    table = document.add_table(rows=4, cols=2)
    table.style = "Table Grid"
    for row, (key, value) in enumerate([("Client", "Alice Example"),
                                       ("Email", "alice@example.test"),
                                       ("Phone", "+1 555 0100"),
                                       ("Account", "GB53 PSCR 5001 9062 0132 78")]):
        table.cell(row, 0).text = key
        table.cell(row, 1).text = value
    for paragraph in FILLER[6:]:
        document.add_paragraph(paragraph)
    document.add_paragraph("Signed for the Client by Bob Sample on 27 August 2026.")
    section = document.sections[0]
    section.header.paragraphs[0].text = "Confidential - Harley Voss LLP"
    section.footer.paragraphs[0].text = "Harley Voss LLP - alice@example.test"
    raw = tmp / "contract_raw.docx"
    document.save(str(raw))

    # inject one floating text box so text-box alignment has a real rendered case
    patched = tmp / "contract.docx"
    with zipfile.ZipFile(raw) as source, zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == "word/document.xml":
                text = payload.decode("utf-8")
                at = text.find("<w:sectPr")
                if at < 0:
                    raise SystemExit("no sectPr in document.xml")
                text = text[:at] + TEXTBOX_XML + text[at:]
                payload = text.encode("utf-8")
            target.writestr(item, payload)
    return patched


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="legal-redact-fixture-"))
    try:
        src = build(tmp)
        docx_out = FIXTURE_DIR / "contract.docx"
        shutil.copyfile(src, docx_out)
        pdf_out = FIXTURE_DIR / "contract.pdf"
        from legal_redact.docx.render import render_docx_to_pdf, detect_renderer
        print("renderer:", detect_renderer())
        render_docx_to_pdf(docx_out, pdf_out)
        import pymupdf
        with pymupdf.open(pdf_out) as pdf:
            print("pages:", pdf.page_count, "size:", pdf_out.stat().st_size)
            for page_no, page in enumerate(pdf):
                text = page.get_text()
                print(f"--- page {page_no} text head: {text[:200]!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("fixture written to", FIXTURE_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())

