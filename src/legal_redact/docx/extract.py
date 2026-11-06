"""DOCX text extraction with a stable run-level offset index.

The detectors are shared with the PDF pipeline: they only need ``PageText.text``
and return ``Suggestion(page, start, end, ...)``. So every text container in the
package (the document body, each header/footer, the notes, each text box) becomes
one synthetic ``PageText`` with ``words=[]``, and every character offset is
registered against the OOXML element that holds it (a ``w:t`` element). That is
what lets the writer replace exactly the characters the lawyer accepted.

Container order is deterministic and fixed by this module: document.xml first,
then headers by number, footers, footnotes, endnotes; inside a part the body
paragraphs come first and then each text box in document order. The same order
is rebuilt on export, so decision offsets keep their meaning.

Coverage invariant (fail closed): every non-whitespace text node in a text part
must be either inside a ``w:t`` we index or inside a field-instruction /
deleted-text element that the writer removes. A text node anywhere else raises
``DocxError`` -- a hostile producer must not be able to hide text in a place we
neither redact nor scrub.

Known limits (documented in docs/DOCX.md):
- One container per part, so a paragraph boundary is just "\\n" in the text;
  detectors see the same document context a text file would give them.
- Text boxes are read as their own containers, after the body of their part.
- ``mc:AlternateContent`` Choice and Fallback copies are both read (Word shows
  one of them); a redaction in either copy is applied to both, which is safe
  but can over-redact a duplicate.
- Text inside images is not read at all; pre-flight says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from .package import DocxError

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
W_SPACE = f"{{{XML_NS}}}space"
BLOCK = "\u2588"  # U+2588 FULL BLOCK: the redaction character

W = lambda tag: f"{{{W_NS}}}{tag}"          # noqa: E731
W_T, W_P, W_R, W_TXBX = W("t"), W("p"), W("r"), W("txbxContent")
W_TAB, W_PTAB = W("tab"), W("ptab")
W_BR, W_CR = W("br"), W("cr")
W_NBH, W_SH = W("noBreakHyphen"), W("softHyphen")
# Text nodes the writer removes outright; they are allowed to exist at extraction
# time (field codes and, in a refused document, deleted text).
SCRUBBED_TEXT_TAGS = {W("instrText"), W("delText"), W("delInstrText")}
# Word writes numeric layout values as element text (e.g.
# <wp:posOffset>0</wp:posOffset> in every anchored drawing). They are geometry,
# not document text, and refusing them would refuse ordinary floating text boxes.
# Only an explicit allowlist of (namespace, local name, parent) triples is
# accepted: a numeric value in any *other* element is treated as hidden text and
# refused, because an arbitrary "hidden number" is not a position value.
NUMERIC_TEXT_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
WP14_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
# tag -> (parents that Word actually puts it under, maximum absolute value)
LAYOUT_VALUE_TAGS = {
    f"{{{WP_NS}}}posOffset": ({"positionH", "positionV"}, 10_000_000.0),
    f"{{{WP14_NS}}}pctWidth": ({"sizeRelH"}, 1000.0),
    f"{{{WP14_NS}}}pctHeight": ({"sizeRelV"}, 1000.0),
}


def _is_numeric_layout_value(element) -> bool:
    """True only for a numeric value of a specifically known layout element."""
    if not isinstance(element.tag, str):
        return False
    spec = LAYOUT_VALUE_TAGS.get(element.tag)
    if spec is None:
        return False
    parents, maximum = spec
    parent = element.getparent()
    if parent is None or not isinstance(parent.tag, str):
        return False
    if parent.tag.rsplit("}", 1)[-1] not in parents:
        return False
    raw = (element.text or "").strip().replace(",", ".")
    try:
        return abs(float(raw)) <= maximum
    except ValueError:
        return False


def localname(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_xml(data: bytes) -> etree._Element:
    """Parse one OOXML part. External entities and DTDs are never loaded."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             huge_tree=False, remove_comments=False, strip_cdata=False)
    try:
        return etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise DocxError("a text part is not well-formed XML") from exc


@dataclass
class TextRun:
    """One ``w:t`` element and the character range it contributes."""
    start: int
    end: int
    element: object


@dataclass
class TextContainer:
    page: int
    part: str
    text: str
    runs: list[TextRun] = field(default_factory=list)
    paragraph_count: int = 0


@dataclass
class DocxText:
    containers: list[TextContainer] = field(default_factory=list)
    roots: dict[str, object] = field(default_factory=dict)


def _textbox_ancestor(element, root) -> bool:
    parent = element.getparent()
    while parent is not None and parent is not root:
        if parent.tag == W_TXBX:
            return True
        parent = parent.getparent()
    return False


def _nearest_textbox(element, root):
    parent = element.getparent()
    while parent is not None and parent is not root:
        if parent.tag == W_TXBX:
            return parent
        parent = parent.getparent()
    return None


def _paragraph_text(paragraph) -> tuple[str, list[tuple[int, int, object]]]:
    """Text of one paragraph plus the (start, end, element) range of each w:t.

    Walk order is document order. Text boxes nested inside the paragraph are a
    separate container and are skipped here. Tabs and explicit line breaks are
    mapped to single virtual characters: they occupy an offset (so detectors see
    sane context) but carry no document text, and a redaction that covers them
    simply leaves them in place.
    """
    pieces: list[str] = []
    runs: list[tuple[int, int, object]] = []
    offset = 0

    def walk(element) -> None:
        nonlocal offset
        for child in element:
            tag = child.tag
            if tag == W_TXBX:
                continue
            if tag == W_T:
                text = child.text or ""
                if text:
                    runs.append((offset, offset + len(text), child))
                    pieces.append(text)
                    offset += len(text)
            elif tag in (W_TAB, W_PTAB):
                pieces.append("\t")
                offset += 1
            elif tag in (W_BR, W_CR):
                pieces.append("\n")
                offset += 1
            elif tag == W_NBH:
                pieces.append("-")
                offset += 1
            elif tag == W_SH:
                pass
            else:
                walk(child)

    walk(paragraph)
    return "".join(pieces), runs


def _container(part: str, paragraphs, page: int) -> TextContainer:
    pieces: list[str] = []
    runs: list[TextRun] = []
    offset = 0
    for index, paragraph in enumerate(paragraphs):
        if index:
            pieces.append("\n")
            offset += 1
        text, paragraph_runs = _paragraph_text(paragraph)
        for start, end, element in paragraph_runs:
            runs.append(TextRun(offset + start, offset + end, element))
        pieces.append(text)
        offset += len(text)
    return TextContainer(page=page, part=part, text="".join(pieces), runs=runs,
                         paragraph_count=len(paragraphs))


def _check_text_coverage(root, part: str) -> None:
    """Refuse a part whose visible text is not all inside an element we understand."""
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        if element.text and element.text.strip():
            if element.tag == W_T or element.tag in SCRUBBED_TEXT_TAGS:
                continue
            if NUMERIC_TEXT_RE.match(element.text.strip()) and _is_numeric_layout_value(element):
                continue                      # a known drawing-layout value, not text
            raise DocxError("a text part holds text outside the supported containers",
                            code="PF108")
        if element.tail and element.tail.strip():
            raise DocxError("a text part holds text outside the supported containers",
                            code="PF108")


def _check_run_coverage(root, containers, part: str) -> None:
    """Every ``w:t`` in the part must belong to a container we can rewrite.

    A hostile producer can put a text element somewhere our paragraph walk never
    reaches (directly under the body, inside an unknown wrapper). If we cannot
    name the run that holds a character, we cannot promise to replace it, so the
    document is refused.
    """
    indexed = {id(run.element) for container in containers for run in container.runs}
    all_text = [element for element in root.iter(W_T)]
    if len(all_text) != len(indexed) or any(id(e) not in indexed for e in all_text):
        raise DocxError("a text part holds text in a structure this tool does not read",
                        code="PF108")


def extract_from_parts(parts: dict[str, bytes], text_parts: list[str]) -> DocxText:
    """Read every recognised text part into containers with run offsets."""
    roots: dict[str, object] = {}
    out = DocxText(roots=roots)
    page = 0
    for part in text_parts:
        root = parse_xml(parts[part])
        roots[part] = root
        _check_text_coverage(root, part)
        first = len(out.containers)
        body_paragraphs = [p for p in root.iter(W_P) if not _textbox_ancestor(p, root)]
        out.containers.append(_container(part, body_paragraphs, page))
        page += 1
        for txbx in root.iter(W_TXBX):
            inner = [p for p in txbx.iter(W_P) if _nearest_textbox(p, root) is txbx]
            out.containers.append(_container(part, inner, page))
            page += 1
        _check_run_coverage(root, out.containers[first:], part)
    return out


def extract_text(package) -> DocxText:
    """Extract from a ``package.Package``."""
    return extract_from_parts(package.parts, package.layout.text_parts)


def to_page_texts(docx_text: DocxText) -> list:
    """Synthetic PageText objects for the shared detectors (no geometry)."""
    from ..models import PageText
    return [PageText(page=c.page, width=0.0, height=0.0, text=c.text)
            for c in docx_text.containers]
