"""Tests for the .docx pipeline: extraction, pre-flight, writer, verifier, CLI.

Fixtures are hand-built OOXML packages (zipfile + strings), not produced by
python-docx, because the interesting cases are adversarial: a name split across
two runs, a text box, a comment, a hyperlink target, a macro part. python-docx
is used in one test only -- as an independent third-party reader of the output.

All names and addresses below are invented. Every refusal test checks the
specific pre-flight code; every mutation test proves the verifier actually
withholds a leaky export, which is the project's rule for "a guard you cannot
make fail is not a guard".
"""
from __future__ import annotations

import json
import zipfile

import pytest

from legal_redact.detect.base import Category, Suggestion
from legal_redact.detect.rules import RulesDetector
from legal_redact.docx.extract import extract_text, to_page_texts
from legal_redact.docx.package import read_package
from legal_redact.docx.pipeline import export_docx, suggest_docx
from legal_redact.docx.preflight import blocked, preflight_docx
from legal_redact.pipeline import ExportRefused
from legal_redact.policy import Policy

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
V = "urn:schemas-microsoft-com:vml"
O = "urn:schemas-microsoft-com:office:office"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
CUST = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"

MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
HEADER = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
FOOTER = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
FOOTNOTES = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
COMMENTS = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
CORE_CT = "application/vnd.openxmlformats-package.core-properties+xml"
APP_CT = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
CUSTOM_CT = "application/vnd.openxmlformats-officedocument.custom-properties+xml"

POLICY = Policy("test", "redact everything", {c: "redact" for c in Category.ALL})
BODY = "This Agreement is between Alice Example and Bob Sample."


# ---- fixture construction --------------------------------------------------
def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def run(text: str, preserve: bool = False) -> str:
    space = ' xml:space="preserve"' if preserve else ""
    return f'<w:r><w:t{space}>{esc(text)}</w:t></w:r>'


def para(*runs: str) -> str:
    return "<w:p>" + "".join(runs) + "</w:p>"


def textbox(inner: str, vml: bool = False) -> str:
    content = f"<w:txbxContent>{inner}</w:txbxContent>"
    if vml:
        shape = (f'<w:p><w:r><w:pict><v:shape id="tb1" style="width:100pt;height:20pt">'
                 f"<v:textbox>{content}</v:textbox></v:shape></w:pict></w:r></w:p>")
        return shape
    return (f'<w:p><w:r><w:drawing><wp:inline><wp:extent cx="100" cy="100"/>'
            f'<wp:docPr id="1" name="TextBox 1"/>'
            f'<a:graphic><a:graphicData uri="{WPS}"><wps:wsp><wps:txbx>{content}'
            f"</wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:inline></w:drawing>"
            f"</w:r></w:p>")


def anchored_textbox(inner: str) -> str:
    """A floating text box exactly as Word writes one (wp:anchor + wp:posOffset)."""
    return ('<w:p><w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" '
            'simplePos="0" relativeHeight="251658240" behindDoc="0" locked="0" '
            'layoutInCell="1" allowOverlap="1">'
            '<wp:simplePos x="0" y="0"/>'
            '<wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>'
            '<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>'
            '<wp:extent cx="2000000" cy="500000"/><wp:docPr id="2" name="TextBox 2"/>'
            f'<a:graphic><a:graphicData uri="{WPS}"><wps:wsp><wps:txbx>'
            f"<w:txbxContent>{inner}</w:txbxContent></wps:txbx></wps:wsp>"
            "</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r></w:p>")


def picture(cx: int = 914400, cy: int = 914400, anchored: bool = False) -> str:
    drawing = (f'<w:drawing><wp:inline><wp:extent cx="{cx}" cy="{cy}"/>'
               '<wp:docPr id="1" name="Picture 1"/>'
               f'<a:graphic><a:graphicData uri="{PIC}"><pic:pic><pic:nvPicPr>'
               '<pic:cNvPr id="1" name="image1.png"/><pic:cNvPicPr/></pic:nvPicPr>'
               '<pic:blipFill><a:blip r:embed="rId40"/></pic:blipFill></pic:pic>'
               "</a:graphicData></a:graphic></wp:inline></w:drawing>")
    if anchored:
        drawing = drawing.replace("<wp:inline>", "<wp:anchor>").replace(
            "</wp:inline>", "</wp:anchor>").replace(
            '<wp:extent', '<wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset>'
            '</wp:positionH><wp:positionV relativeFrom="paragraph">'
            '<wp:posOffset>0</wp:posOffset></wp:positionV><wp:extent')
    return "<w:r>" + drawing + "</w:r>"


def _content_types(overrides: dict[str, str], defaults: dict[str, str]) -> str:
    rows = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="xml" ContentType="application/xml"/>']
    rows += [f'<Default Extension="{ext}" ContentType="{ctype}"/>' for ext, ctype in defaults.items()]
    rows += [f'<Override PartName="/{part}" ContentType="{ctype}"/>' for part, ctype in overrides.items()]
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Types xmlns="{CT}">' + "".join(rows) + "</Types>")


def _rels(items) -> str:
    rows = []
    for rid, rtype, target, external in items:
        mode = ' TargetMode="External"' if external else ""
        rows.append(f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"{mode}/>')
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Relationships xmlns="{PR}">' + "".join(rows) + "</Relationships>")


def _core(props: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<cp:coreProperties xmlns:cp="{CP}" xmlns:dc="{DC}" '
            f'xmlns:dcterms="{DCTERMS}" xmlns:xsi="{XSI}">{props}</cp:coreProperties>')


def _app(props: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Properties xmlns="{EP}" xmlns:vt="{VT}">{props}</Properties>')


def make_docx(tmp_path, name: str = "doc.docx", *, body: str | None = None,
              header: str | None = None, footer: str | None = None,
              footnotes: str | None = None, comments: str | None = None,
              core_props: str | None = None, app_props: str | None = None,
              custom_props: str | None = None, people: str | None = None,
              settings: str | None = None, media: bool = False,
              extra_parts: dict[str, bytes] | None = None,
              extra_overrides: dict[str, str] | None = None,
              extra_defaults: dict[str, str] | None = None,
              extra_doc_rels: list | None = None,
              extra_pkg_rels: list | None = None):
    body = body if body is not None else para(run(BODY))
    doc_rels = list(extra_doc_rels or [])
    package_rels = [
        ("rId1", R + "/officeDocument", "word/document.xml", False),
        ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/"
                 "metadata/core-properties", "docProps/core.xml", False),
        ("rId3", R + "/extended-properties", "docProps/app.xml", False),
    ] + list(extra_pkg_rels or [])
    overrides: dict[str, str] = {"word/document.xml": MAIN,
                                 "docProps/core.xml": CORE_CT,
                                 "docProps/app.xml": APP_CT}
    defaults: dict[str, str] = {}
    parts: dict[str, bytes] = {}
    sect = ""

    if header is not None:
        parts["word/header1.xml"] = _header_part(header)
        overrides["word/header1.xml"] = HEADER
        doc_rels.append(("rId10", R + "/header", "header1.xml", False))
        sect += '<w:headerReference w:type="default" r:id="rId10"/>'
    if footer is not None:
        parts["word/footer1.xml"] = _footer_part(footer)
        overrides["word/footer1.xml"] = FOOTER
        doc_rels.append(("rId11", R + "/footer", "footer1.xml", False))
        sect += '<w:footerReference w:type="default" r:id="rId11"/>'
    if footnotes is not None:
        parts["word/footnotes.xml"] = _footnotes_part(footnotes)
        overrides["word/footnotes.xml"] = FOOTNOTES
        doc_rels.append(("rId20", R + "/footnotes", "footnotes.xml", False))
    if comments is not None:
        parts["word/comments.xml"] = _comments_part(comments)
        overrides["word/comments.xml"] = COMMENTS
        doc_rels.append(("rId30", R + "/comments", "comments.xml", False))
    if people is not None:
        parts["word/people.xml"] = people
        overrides["word/people.xml"] = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.people+xml")
        package_rels.append(("rId8", R + "/people", "word/people.xml", False))
    if media:
        parts["word/media/image1.png"] = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        defaults["png"] = "image/png"
        doc_rels.append(("rId40", R + "/image", "media/image1.png", False))
    sect = f'<w:sectPr>{sect}<w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:wp="{WP}" xmlns:a="{A}" '
           f'xmlns:pic="{PIC}" xmlns:v="{V}" xmlns:o="{O}" xmlns:wps="{WPS}">'
           f"<w:body>{body}{sect}</w:body></w:document>")
    parts["word/document.xml"] = doc.encode()
    parts["word/_rels/document.xml.rels"] = _rels(doc_rels).encode()
    parts["docProps/core.xml"] = _core(core_props if core_props is not None else
                                       "<dc:title>Alice Example contract</dc:title>"
                                       "<dc:creator>Alice Example</dc:creator>"
                                       "<cp:lastModifiedBy>Alice Example</cp:lastModifiedBy>"
                                       '<cp:lastPrinted>2024-01-01T00:00:00Z</cp:lastPrinted>')
    parts["docProps/app.xml"] = _app(app_props if app_props is not None else
                                     "<Application>Microsoft Office Word</Application>"
                                     "<Company>Alice Example Ltd</Company>"
                                     "<Manager>Alice Example</Manager><Pages>1</Pages>")
    if custom_props is not None:
        parts["docProps/custom.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Properties xmlns="{CUST}" xmlns:vt="{VT}">{custom_props}</Properties>').encode()
        overrides["docProps/custom.xml"] = CUSTOM_CT
        package_rels.append(("rId4", R + "/custom-properties", "docProps/custom.xml", False))
    if settings is not None:
        parts["word/settings.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:settings xmlns:w="{W}" xmlns:r="{R}">{settings}</w:settings>').encode()
        overrides["word/settings.xml"] = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml")
    parts["_rels/.rels"] = _rels(package_rels).encode()
    overrides.update(extra_overrides or {})
    defaults.update(extra_defaults or {})
    parts["[Content_Types].xml"] = _content_types(overrides, defaults).encode()
    for part_name, payload in (extra_parts or {}).items():
        parts[part_name] = payload
    return write_zip(tmp_path / name, parts)


def _header_part(inner: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:hdr xmlns:w="{W}" xmlns:r="{R}">{inner}</w:hdr>').encode()


def _footer_part(inner: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:ftr xmlns:w="{W}" xmlns:r="{R}">{inner}</w:ftr>').encode()


def _footnotes_part(inner: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:footnotes xmlns:w="{W}" xmlns:r="{R}">{inner}</w:footnotes>').encode()


def _comments_part(inner: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:comments xmlns:w="{W}">{inner}</w:comments>').encode()


def write_zip(path, parts: dict[str, bytes]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in parts.items():
            zf.writestr(name, payload)
    return path


def raw_parts(path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def rewrite(path, changes: dict[str, bytes] | None = None,
            delete=()) -> None:
    parts = raw_parts(path)
    parts.update(changes or {})
    for name in delete:
        parts.pop(name, None)
    write_zip(path, parts)


def all_bytes(path) -> str:
    return b"\n".join(raw_parts(path).values()).decode("utf-8", "replace")


# ---- shared helpers --------------------------------------------------------
class FakeDetector:
    """Returns one suggestion per occurrence of each needle it is given."""

    name = "fake"

    def __init__(self, needles):
        self.needles = needles

    def detect(self, pages):
        out = []
        for page in pages:
            for text, category in self.needles:
                start = page.text.find(text)
                while start != -1:
                    out.append(Suggestion(page.page, start, start + len(text),
                                          category, "test", 0.9, "fake"))
                    start = page.text.find(text, start + 1)
        return out


def suggest(src, needles):
    return suggest_docx(str(src), FakeDetector(needles), POLICY)


def accept_all(session) -> None:
    for decision in session.decisions:
        if decision.status == "pending":
            decision.status = "accepted"


def acknowledge_reviews(session) -> None:
    from legal_redact.models import Severity
    for finding in session.findings:
        if finding.severity is Severity.REVIEW:
            session.acknowledge(finding.code)


def export(session, src, out):
    return export_docx(str(src), str(out), session)


def accept_and_export(src, out, needles):
    session = suggest(src, needles)
    accept_all(session)
    return session, export(session, src, out)


def codes(path):
    return {f.code for f in preflight_docx(path)}


# ---- extraction ------------------------------------------------------------
def test_plain_docx_extraction_keeps_paragraph_boundaries(tmp_path):
    src = make_docx(tmp_path, body=para(run("First line."))
                    + para(run("Email alice@example.test here.")))
    package = read_package(src)
    text = extract_text(package)
    assert [c.text for c in text.containers] == [
        "First line.\nEmail alice@example.test here."]
    pages = to_page_texts(text)
    found = [s for s in RulesDetector(names=False).detect(pages) if s.category == Category.EMAIL]
    assert found and pages[0].text[found[0].start:found[0].end] == "alice@example.test"


def test_text_is_joined_across_runs_without_spurious_spaces(tmp_path):
    src = make_docx(tmp_path, body=para(run("Ali"), run("ce Exa"), run("mple")))
    text = extract_text(read_package(src))
    assert text.containers[0].text == "Alice Example"


# ---- writer: run replacement ----------------------------------------------
def test_name_split_across_runs_is_fully_removed(tmp_path):
    src = make_docx(tmp_path, body=para(run("Borrower: Ali"), run("ce Example, here.")))
    out = tmp_path / "out.docx"
    session, report = accept_and_export(src, out, [("Alice Example", Category.PERSON)])
    assert report.passed
    output = extract_text(read_package(out)).containers[0].text
    assert "\u2588" * len("Alice Example") in output
    assert "Alice" not in output and "ce Example" not in output
    assert "Alice" not in all_bytes(out)          # not in the XML either
    # the paragraph still has both runs: structure is preserved
    assert output.startswith("Borrower: ") and output.endswith(", here.")


def test_header_footer_footnote_are_extracted_and_redacted(tmp_path):
    src = make_docx(
        tmp_path,
        header=para(run("Header: Alice Example")),
        footer=para(run("Footer: alice@example.test")),
        footnotes=(f'<w:footnote w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
                   f'<w:footnote w:id="1">{para(run("Note: Bob Sample"))}</w:footnote>'))
    text = extract_text(read_package(src))
    assert any(c.text == "Header: Alice Example" for c in text.containers)
    assert any("alice@example.test" in c.text for c in text.containers)
    assert any("Bob Sample" in c.text for c in text.containers)
    out = tmp_path / "out.docx"
    session, report = accept_and_export(
        src, out, [("Alice Example", Category.PERSON), ("alice@example.test", Category.EMAIL),
                   ("Bob Sample", Category.PERSON)])
    assert report.passed
    blob = all_bytes(out)
    for leaked in ("Alice Example", "alice@example.test", "Bob Sample"):
        assert leaked not in blob
    header = raw_parts(out)["word/header1.xml"].decode()
    assert "\u2588" in header


def test_text_boxes_are_extracted_and_redacted(tmp_path):
    body = (para(run("Body text."))
            + textbox(para(run("VML box: Alice Example")))
            + textbox(para(run("Drawing box: Bob Sample")))
            + para(run("End.")))
    src = make_docx(tmp_path, body=body)
    containers = extract_text(read_package(src)).containers
    assert any("VML box: Alice Example" in c.text for c in containers)
    assert any("Drawing box: Bob Sample" in c.text for c in containers)
    out = tmp_path / "out.docx"
    session, report = accept_and_export(
        src, out, [("Alice Example", Category.PERSON), ("Bob Sample", Category.PERSON)])
    assert report.passed
    assert "Alice Example" not in all_bytes(out)
    assert "Bob Sample" not in all_bytes(out)


# ---- writer: hidden carriers ----------------------------------------------
def test_alt_text_hyperlink_field_codes_and_tooltips_are_removed(tmp_path):
    body = (para(run("Plain paragraph."))
            + '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="100" cy="100"/>'
              '<wp:docPr id="1" name="Alice Example shape" descr="Alice Example alt text"'
              ' title="Bob Sample title"/>'
              f'<a:graphic><a:graphicData uri="{PIC}"><pic:pic><pic:nvPicPr>'
              '<pic:cNvPr id="1" name="Alice" descr="Bob Sample alt"/>'
              '</pic:nvPicPr></pic:pic></a:graphicData></a:graphic>'
              '</wp:inline></w:drawing></w:r></w:p>'
            + '<w:p><w:hyperlink r:id="rId50" w:tooltip="Alice Example tooltip">'
              + run("click here") + "</w:hyperlink></w:p>"
            + '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
              '<w:r><w:instrText xml:space="preserve"> MERGEFIELD Alice Example </w:instrText></w:r>'
              '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
              '<w:r><w:t>field result</w:t></w:r>'
              '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
            + '<w:p><w:bookmarkStart w:id="0" w:name="Alice Example bookmark"/>'
              + run("marked") + '<w:bookmarkEnd w:id="0"/></w:p>'
            + '<w:p><w:sdt><w:sdtPr><w:alias w:val="Alice Example alias"/>'
              '<w:tag w:val="Bob Sample tag"/></w:sdtPr><w:sdtContent>'
              + run("content") + "</w:sdtContent></w:sdt></w:p>")
    src = make_docx(tmp_path, body=body, extra_doc_rels=[
        ("rId50", R + "/hyperlink", "mailto:alice@example.test", True)])
    finding_codes = codes(src)
    assert "PF111" in finding_codes                       # carriers found -> REVIEW
    out = tmp_path / "out.docx"
    session = suggest(src, [("Plain paragraph.", Category.PERSON)])
    accept_all(session)
    session.acknowledge("PF111")                          # the reviewer accepted the removal
    report = export(session, src, out)
    assert report.passed
    blob = all_bytes(out)
    for leaked in ("Alice Example", "Bob Sample", "alice@example.test", "MERGEFIELD",
                   "mailto:"):
        assert leaked not in blob, leaked
    document = raw_parts(out)["word/document.xml"].decode()
    assert '<w:hyperlink' not in document and "w:instrText" not in document
    assert "_redacted_bm_0" in document                   # bookmark name neutralised


def test_comments_are_stripped_after_acknowledgement(tmp_path):
    body = (para('<w:r><w:commentRangeStart w:id="0"/>' + run("Bob Sample")
                 + '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:r>'))
    src = make_docx(tmp_path, body=body,
                    comments='<w:comment w:id="0" w:author="Alice Example">'
                             + para(run("secret comment about Alice Example")) + "</w:comment>")
    findings = preflight_docx(src)
    assert "PF112" in {f.code for f in findings}
    session = suggest(src, [("Bob Sample", Category.PERSON)])
    accept_all(session)
    with pytest.raises(ExportRefused):                    # REVIEW not acknowledged
        export(session, src, tmp_path / "nope.docx")
    session.acknowledge("PF112")
    out = tmp_path / "out.docx"
    report = export(session, src, out)
    assert report.passed
    names = set(raw_parts(out))
    assert not any("comment" in name for name in names)
    assert "Alice Example" not in all_bytes(out)
    assert "commentReference" not in all_bytes(out)


def test_metadata_custom_properties_and_people_are_scrubbed(tmp_path):
    src = make_docx(
        tmp_path, body=para(run("Borrower: Alice Example.")),
        core_props="<dc:creator>Zelda Quixote</dc:creator>"
                   "<cp:lastModifiedBy>Zelda Quixote</cp:lastModifiedBy>"
                   '<cp:lastPrinted>2024-02-03T04:05:06Z</cp:lastPrinted>',
        app_props="<Application>Microsoft Office Word</Application>"
                  "<Company>Zelda Quixote Ltd</Company><Manager>Zelda Quixote</Manager>",
        custom_props='<property name="Client" fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"'
                     ' pid="2"><vt:lpwstr>Zelda Quixote</vt:lpwstr></property>',
        people=('<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
                '<w15:person w15:author="Zelda Quixote"/></w15:people>').encode(),
        settings='<w:docVars><w:docVar w:name="client" w:val="Zelda Quixote"/></w:docVars>')
    out = tmp_path / "out.docx"
    session, report = accept_and_export(src, out, [("Alice Example", Category.PERSON)])
    assert report.passed
    parts = raw_parts(out)
    assert "docProps/custom.xml" not in parts and "word/people.xml" not in parts
    blob = all_bytes(out)
    for leaked in ("Zelda Quixote", "Zelda Quixote Ltd"):
        assert leaked not in blob, leaked
    from xml.etree import ElementTree as ET
    core = ET.fromstring(parts["docProps/core.xml"])
    assert all(not (el.text or "").strip() for el in core.iter())
    app = ET.fromstring(parts["docProps/app.xml"])
    assert all(not (el.text or "").strip() for el in app.iter())
    assert "docVars" not in parts["word/settings.xml"].decode()


def test_custom_xml_parts_are_stripped_and_flagged(tmp_path):
    src = make_docx(
        tmp_path,
        extra_parts={
            "customXml/item1.xml": b'<?xml version="1.0"?><root><v>Alice Example</v></root>',
            "customXml/itemProps1.xml": b'<?xml version="1.0"?><ds:datastoreItem '
                                        b'xmlns:ds="http://schemas.openxmlformats.org/'
                                        b'officeDocument/2006/customXml" ds:itemID="{1}"/>',
        },
        extra_overrides={"customXml/itemProps1.xml":
                         "application/vnd.openxmlformats-officedocument.customXmlProperties+xml"},
        extra_pkg_rels=[("rId7", R + "/customXml", "customXml/item1.xml", False)])
    finding_codes = codes(src)
    assert "PF114" in finding_codes
    session = suggest(src, [("Alice Example", Category.PERSON)])
    accept_all(session)
    session.acknowledge("PF114")
    out = tmp_path / "out.docx"
    report = export(session, src, out)
    assert report.passed
    assert not any(name.startswith("customXml/") for name in raw_parts(out))
    assert "Alice Example" not in all_bytes(out)


def test_images_are_flagged_for_review_and_copied_unchanged(tmp_path):
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example.")) + para(picture()),
                    media=True)
    assert "PF113" in codes(src)
    session = suggest(src, [("Alice Example", Category.PERSON)])
    accept_all(session)
    acknowledge_reviews(session)
    out = tmp_path / "out.docx"
    report = export(session, src, out)
    assert report.passed
    media = {name: data for name, data in raw_parts(out).items()
             if name.startswith("word/media/")}
    assert len(media) == 1
    name, payload = next(iter(media.items()))
    assert name.startswith("word/media/redacted_")          # neutral generated name
    assert payload == raw_parts(src)["word/media/image1.png"]


def test_anchored_textbox_is_not_a_false_refusal(tmp_path):
    """Word writes <wp:posOffset>0</wp:posOffset> in every floating drawing."""
    src = make_docx(tmp_path, body=para(run("Body."))
                    + anchored_textbox(para(run("Floating box: Alice Example"))))
    findings = preflight_docx(src)
    assert not blocked(findings)
    assert not [f for f in findings if f.code == "PF108"]
    containers = extract_text(read_package(src)).containers
    assert any("Floating box: Alice Example" in c.text for c in containers)
    out = tmp_path / "out.docx"
    session, report = accept_and_export(src, out, [("Alice Example", Category.PERSON)])
    assert report.passed
    assert "Alice Example" not in all_bytes(out)


def test_large_image_is_refused_as_unreadable_pixels(tmp_path):
    # page is US Letter: 12240 x 15840 twips; a ~7.3 x 5.2 inch picture is >25%
    body = para(run("Body.")) + para(picture(cx=6700000, cy=4800000))
    src = make_docx(tmp_path, body=body, media=True)
    findings = preflight_docx(src)
    assert "PF123" in {f.code for f in findings}
    assert blocked(findings)
    # a small logo stays an acknowledgeable REVIEW, not a refusal
    small = make_docx(tmp_path, name="small.docx",
                      body=para(run("Body.")) + para(picture(cx=900000, cy=600000)),
                      media=True)
    small_findings = preflight_docx(small)
    assert not blocked(small_findings)
    assert "PF113" in {f.code for f in small_findings}


def test_pii_in_package_part_names_is_refused(tmp_path):
    src = make_docx(tmp_path, extra_parts={
        "word/media/Ivanova_passport_4510123456.png": b"\x89PNG-fake",
    }, media=True)
    assert "PF124" in codes(src)
    assert blocked(preflight_docx(src))


def test_pii_in_embedded_font_names_is_refused(tmp_path):
    src = make_docx(tmp_path, extra_parts={
        "word/fonts/passport_4510123456.odttf": b"font-bytes",
    }, extra_defaults={"odttf":
                       "application/vnd.openxmlformats-officedocument.obfuscatedFont"})
    assert "PF124" in codes(src)


def test_embedded_fonts_are_renamed_and_copied(tmp_path):
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example.")),
                    extra_parts={"word/fonts/font1.odttf": b"font-bytes"},
                    extra_defaults={"odttf":
                                    "application/vnd.openxmlformats-officedocument.obfuscatedFont"})
    out = tmp_path / "out.docx"
    session, report = accept_and_export(src, out, [("Alice Example", Category.PERSON)])
    assert report.passed
    fonts = {name: data for name, data in raw_parts(out).items()
             if name.startswith("word/fonts/")}
    assert len(fonts) == 1
    name, payload = next(iter(fonts.items()))
    assert name.startswith("word/fonts/redacted_font_")
    assert payload == b"font-bytes"


def test_junk_image_extension_is_refused(tmp_path):
    # an extension outside the allowlist means the part cannot be renamed safely
    src = make_docx(tmp_path, extra_parts={"word/media/Ivanova.weird": b"x"})
    assert "PF107" in codes(src)


def test_glossary_text_is_read_and_redacted(tmp_path):
    glossary = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:glossaryDocument xmlns:w="{W}"><w:docParts><w:docPart>'
                f"<w:docPartBody>{para(run('AutoText: Alice Example'))}</w:docPartBody>"
                f"</w:docPart></w:docParts></w:glossaryDocument>").encode()
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example.")),
                    extra_parts={"word/glossary/document.xml": glossary},
                    extra_overrides={"word/glossary/document.xml":
                                     "application/vnd.openxmlformats-officedocument."
                                     "wordprocessingml.glossary+xml"})
    findings = preflight_docx(src)
    assert not blocked(findings)
    containers = extract_text(read_package(src)).containers
    assert any("AutoText: Alice Example" in c.text for c in containers)
    out = tmp_path / "out.docx"
    session, report = accept_and_export(src, out, [("Alice Example", Category.PERSON)])
    assert report.passed
    assert "Alice Example" not in all_bytes(out)


def test_numeric_text_outside_layout_elements_is_refused(tmp_path):
    custom = make_docx(tmp_path, name="custom.docx",
                       body=para(run("Body.") + "<w:customNumeric>4510123456</w:customNumeric>"))
    assert "PF108" in codes(custom)
    # a known layout element is allowed, but an implausible value is not a position
    huge = make_docx(tmp_path, name="huge.docx", body=para(run("Body.")) + anchored_textbox(
        para(run("box"))).replace("<wp:posOffset>0</wp:posOffset>",
                                  "<wp:posOffset>4510123456</wp:posOffset>"))
    assert "PF108" in codes(huge)


def test_unknown_drawing_element_with_numeric_text_is_refused(tmp_path):
    """A numeric value is only geometry in the exact elements Word writes it into."""
    body = para(run("Body.")) + anchored_textbox(para(run("box"))).replace(
        "<wp:extent", "<wp:hiddenNum>120385</wp:hiddenNum><wp:extent")
    src = make_docx(tmp_path, body=body)
    assert "PF108" in codes(src)


def test_known_drawing_layout_values_are_accepted(tmp_path):
    wp14 = "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
    body = para(run("Body.")) + anchored_textbox(para(run("box"))).replace(
        "<wp:extent", f'<wp14:sizeRelH xmlns:wp14="{wp14}" relativeFrom="margin">'
                      "<wp14:pctWidth>500</wp14:pctWidth></wp14:sizeRelH><wp:extent")
    src = make_docx(tmp_path, body=body)
    assert not blocked(preflight_docx(src))
    assert any("box" in c.text for c in extract_text(read_package(src)).containers)


def test_pii_in_an_embedded_font_face_name_is_refused(tmp_path):
    font_table = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                  f'<w:fonts xmlns:w="{W}"><w:font w:name="4111 1111 1111 1111"/>'
                  "</w:fonts>").encode()
    src = make_docx(tmp_path, extra_parts={"word/fontTable.xml": font_table},
                    extra_overrides={"word/fontTable.xml":
                                     "application/vnd.openxmlformats-officedocument."
                                     "wordprocessingml.fontTable+xml"})
    assert "PF124" in codes(src)


def test_inline_custom_xml_bindings_are_scrubbed(tmp_path):
    body = ('<w:p><w:customXml w:element="Alice">'
            '<w:customXmlPr><w:attr w:name="Bob Sample" w:val="Alice Example"/>'
            "</w:customXmlPr>" + run("Borrower: Alice Example.") + "</w:customXml></w:p>")
    src = make_docx(tmp_path, body=body)
    assert "PF111" in codes(src)                 # the binding is a hidden-text carrier
    out = tmp_path / "out.docx"
    session = suggest(src, [("Alice Example", Category.PERSON)])
    accept_all(session)
    session.acknowledge("PF111")
    report = export(session, src, out)
    assert report.passed
    blob = all_bytes(out)
    assert "Bob Sample" not in blob and "customXmlPr" not in blob


def test_alternate_content_both_copies_are_read_and_redacted(tmp_path):
    choice = ('<w:drawing><wp:inline><wp:extent cx="100" cy="100"/>'
              '<wp:docPr id="1" name="b"/><a:graphic><a:graphicData uri="x"><wps:wsp>'
              "<wps:txbx><w:txbxContent>" + para(run("Box: Alice Example"))
              + "</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic>"
              "</wp:inline></w:drawing>")
    fallback = ("<w:pict><v:shape><v:textbox><w:txbxContent>"
                + para(run("Box: Alice Example")) + "</w:txbxContent></v:textbox>"
                "</v:shape></w:pict>")
    body = para(run("Body.")) + (
        '<w:p><w:r><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/'
        'markup-compatibility/2006">'
        f'<mc:Choice Requires="wps">{choice}</mc:Choice>'
        f"<mc:Fallback>{fallback}</mc:Fallback></mc:AlternateContent></w:r></w:p>")
    src = make_docx(tmp_path, body=body)
    containers = extract_text(read_package(src)).containers
    assert sum("Alice Example" in c.text for c in containers) == 2
    out = tmp_path / "out.docx"
    session, report = accept_and_export(src, out, [("Alice Example", Category.PERSON)])
    assert report.passed
    assert "Alice Example" not in all_bytes(out)


def test_preflight_accepts_a_python_docx_generated_document(tmp_path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("Loan Agreement", 0)
    document.add_paragraph("This Agreement is between Alice Example and Bob Sample.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Email"
    table.cell(0, 1).text = "alice@example.test"
    document.sections[0].header.paragraphs[0].text = "Confidential - Alice Example"
    src = tmp_path / "generated.docx"
    document.save(str(src))
    findings = preflight_docx(src)
    assert not blocked(findings)
    out = tmp_path / "out.docx"
    session = suggest(src, [("Alice Example", Category.PERSON), ("Bob Sample", Category.PERSON),
                            ("alice@example.test", Category.EMAIL)])
    accept_all(session)
    acknowledge_reviews(session)
    report = export(session, src, out)
    assert report.passed
    blob = all_bytes(out)
    for leaked in ("Alice Example", "Bob Sample", "alice@example.test"):
        assert leaked not in blob


# ---- pre-flight refusals ---------------------------------------------------
def test_encrypted_container_is_refused(tmp_path):
    path = tmp_path / "enc.docx"
    path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 64
                     + "EncryptionInfo".encode("utf-16-le"))
    assert "PF101" in codes(path)
    assert blocked(preflight_docx(path))


def test_legacy_doc_is_refused(tmp_path):
    path = tmp_path / "legacy.doc"
    path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 64
                     + "WordDocument".encode("utf-16-le"))
    assert "PF102" in codes(path)


def test_macro_part_is_refused(tmp_path):
    src = make_docx(tmp_path, extra_parts={"word/vbaProject.bin": b"macro"})
    assert "PF103" in codes(src)


def test_tracked_changes_are_refused(tmp_path):
    src = make_docx(tmp_path, body='<w:p><w:ins w:id="1" w:author="A">'
                                    + run("Alice Example") + "</w:ins></w:p>")
    assert "PF105" in codes(src)


def test_embedded_ole_object_is_refused(tmp_path):
    src = make_docx(tmp_path, extra_parts={"word/embeddings/oleObject1.bin": b"ole"})
    assert "PF106" in codes(src)


def test_unknown_part_is_refused(tmp_path):
    src = make_docx(tmp_path, extra_parts={"word/charts/chart1.xml": b"<c/>"})
    assert "PF107" in codes(src)


def test_altchunk_is_refused(tmp_path):
    src = make_docx(tmp_path, body='<w:altChunk r:id="rId99"/>')
    assert "PF119" in codes(src)


def test_text_outside_supported_containers_is_refused(tmp_path):
    src = make_docx(tmp_path, body="<w:t>Alice Example</w:t>")
    assert "PF108" in codes(src)


@pytest.mark.parametrize("construction,code", [
    (lambda doc: doc.replace(b"<w:body>", b"<!-- Alice Example --><w:body>"), "PF121"),
    (lambda doc: doc.replace(b"<w:body>", b"<?pi Alice Example?><w:body>"), "PF121"),
    (lambda doc: doc.replace(b"<w:document ", b"<!DOCTYPE d><w:document "), "PF121"),
])
def test_text_carrying_xml_constructs_are_refused(tmp_path, construction, code):
    src = make_docx(tmp_path)
    rewrite(src, {"word/document.xml": construction(raw_parts(src)["word/document.xml"])})
    assert code in codes(src)


def test_hidden_text_in_content_types_is_refused(tmp_path):
    src = make_docx(tmp_path)
    ct = raw_parts(src)["[Content_Types].xml"].replace(b'Extension="rels"',
                                                        b'Extension="Alice Example"')
    rewrite(src, {"[Content_Types].xml": ct})
    assert "PF121" in codes(src)


def test_duplicate_part_names_are_refused(tmp_path):
    src = make_docx(tmp_path)
    parts = raw_parts(src)
    path = tmp_path / "dup.docx"
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in parts.items():
            zf.writestr(name, payload)
        zf.writestr("word/document.xml", b"<w:document/>")   # second copy of the same part
    assert "PF110" in codes(path)


def test_junk_file_is_refused(tmp_path):
    path = tmp_path / "junk.docx"
    path.write_text("not a docx")
    assert "PF104" in codes(path)


# ---- gate and verifier-withholding (mutation evidence) ---------------------
def test_export_blocked_while_a_suggestion_is_undecided(tmp_path):
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example.")))
    session = suggest(src, [("Alice Example", Category.PERSON)])
    with pytest.raises(ExportRefused):
        export(session, src, tmp_path / "out.docx")
    assert not (tmp_path / "out.docx").exists()


def test_verifier_withholds_when_run_replacement_is_disabled(tmp_path, monkeypatch):
    import legal_redact.docx.write as docx_write
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example, here.")))
    monkeypatch.setattr(docx_write, "_apply_span", lambda container, start, end: 0)
    out = tmp_path / "out.docx"
    session = suggest(src, [("Alice Example", Category.PERSON)])
    accept_all(session)
    with pytest.raises(ExportRefused) as caught:
        export(session, src, out)
    assert not out.exists()
    failed = {check.id for check in caught.value.report.failed()}
    assert failed & {"V102", "V103"}


def test_verifier_withholds_when_a_header_copy_is_left(tmp_path, monkeypatch):
    import legal_redact.docx.write as docx_write
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example, here.")),
                    header=para(run("Header: Alice Example")))
    original = docx_write._apply_span

    def skip_headers(container, start, end):
        if "header" in container.part:
            return 0
        return original(container, start, end)

    monkeypatch.setattr(docx_write, "_apply_span", skip_headers)
    out = tmp_path / "out.docx"
    session = suggest(src, [("Alice Example", Category.PERSON)])
    accept_all(session)
    with pytest.raises(ExportRefused) as caught:
        export(session, src, out)
    assert not out.exists()
    assert {"V102", "V103"} & {c.id for c in caught.value.report.failed()}


def test_verifier_withholds_when_a_comment_part_is_kept(tmp_path, monkeypatch):
    from legal_redact.docx.package import Layout
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example.")),
                    comments='<w:comment w:id="0" w:author="A">' + para(run("note")) + "</w:comment>")
    monkeypatch.setattr(Layout, "dropped_parts", lambda self: set())
    out = tmp_path / "out.docx"
    session = suggest(src, [("Alice Example", Category.PERSON)])
    accept_all(session)
    session.acknowledge("PF112")
    with pytest.raises(ExportRefused) as caught:
        export(session, src, out)
    assert not out.exists()
    assert "V104" in {c.id for c in caught.value.report.failed()}


def test_verifier_withholds_when_metadata_is_kept(tmp_path, monkeypatch):
    import legal_redact.docx.write as docx_write
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example.")),
                    core_props="<dc:creator>Zelda Quixote</dc:creator>")
    monkeypatch.setattr(docx_write, "_scrub_core", lambda root: None)
    out = tmp_path / "out.docx"
    session = suggest(src, [("Alice Example", Category.PERSON)])
    accept_all(session)
    with pytest.raises(ExportRefused) as caught:
        export(session, src, out)
    assert not out.exists()
    assert "V105" in {c.id for c in caught.value.report.failed()}


def test_verifier_withholds_when_part_names_are_kept(tmp_path, monkeypatch):
    import legal_redact.docx.write as docx_write
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example.")) + para(picture()),
                    media=True)
    monkeypatch.setattr(docx_write, "_neutral_renames", lambda layout, names: {})
    out = tmp_path / "out.docx"
    session = suggest(src, [("Alice Example", Category.PERSON)])
    accept_all(session)
    acknowledge_reviews(session)
    with pytest.raises(ExportRefused) as caught:
        export(session, src, out)
    assert not out.exists()
    assert {"V106", "V108"} & {c.id for c in caught.value.report.failed()}


def test_reports_and_decisions_never_contain_document_text(tmp_path):
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example, alice@example.test.")))
    session = suggest(src, [("Alice Example", Category.PERSON),
                            ("alice@example.test", Category.EMAIL)])
    accept_all(session)
    out = tmp_path / "out.docx"
    report = export(session, src, out)
    decisions = json.dumps([d.__dict__ for d in session.decisions])
    rendered = report.format() + json.dumps(report.to_dict())
    for leaked in ("Alice", "alice@example.test", "Borrower"):
        assert leaked not in decisions
        assert leaked not in rendered


# ---- CLI ---------------------------------------------------------------
def test_cli_inspect_suggest_redact_end_to_end(tmp_path, capsys):
    from legal_redact.cli import main
    src = make_docx(tmp_path, body=para(
        run("Contact Alice Example at alice@example.test or +1 555 0100.")),
        header=para(run("Bureau: alice@example.test")))
    assert main(["inspect", str(src)]) == 0
    out = capsys.readouterr().out
    assert "format=docx" in out and "alice@example.test" not in out
    decisions = tmp_path / "decisions.json"
    assert main(["suggest", str(src), "-o", str(decisions), "--detector", "rules"]) == 0
    data = json.loads(decisions.read_text())
    assert data["decisions"] and all(d["status"] == "pending" for d in data["decisions"])
    for decision in data["decisions"]:
        decision["status"] = "accepted"
    decisions.write_text(json.dumps(data))
    redacted = tmp_path / "redacted.docx"
    assert main(["redact", str(src), "--decisions", str(decisions),
                 "-o", str(redacted)]) == 0
    assert redacted.exists()
    blob = all_bytes(redacted)
    assert "alice@example.test" not in blob and "555 0100" not in blob
    printed = capsys.readouterr().out
    assert "written:" in printed and "RESULT: no leak found" in printed
    assert "alice@example.test" not in printed


def test_cli_review_reports_pdf_only(tmp_path, capsys):
    from legal_redact.cli import main
    src = make_docx(tmp_path)
    assert main(["review", str(src)]) == 2
    assert "PDF" in capsys.readouterr().out


def test_output_opens_in_python_docx_and_shows_blocks(tmp_path):
    docx = pytest.importorskip("docx")                 # test-only dependency
    src = make_docx(tmp_path, body=para(run("Borrower: Alice Example.")),
                    header=para(run("Header: Alice Example")))
    out = tmp_path / "out.docx"
    session, report = accept_and_export(src, out, [("Alice Example", Category.PERSON)])
    assert report.passed
    document = docx.Document(str(out))
    body = "\n".join(p.text for p in document.paragraphs)
    assert "Alice" not in body and "\u2588" in body
    header = "\n".join(p.text for p in document.sections[0].header.paragraphs)
    assert "Alice" not in header and "\u2588" in header
