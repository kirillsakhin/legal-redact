"""Stage 4 for .docx: replace the accepted characters, strip the hidden carriers.

The writer is the only module that edits OOXML. It uses lxml; every part it does
not have to touch is copied byte-for-byte, and the ZIP entry order, compression
and timestamps are preserved so the package is as close to the source as the
required edits allow.

What is destructive, on purpose:

- each character of an accepted span becomes U+2588 in the very ``w:t`` element
  that held it, with ``xml:space="preserve"`` set; a span crossing runs is
  replaced run by run, so no character of the original text survives anywhere;
- comments (parts, range marks and references), comment-author names, custom XML,
  document-property bags and page thumbnails are removed;
- alternate text, tooltips, table captions, field instructions (``w:instrText``)
  and content-control metadata are removed; hyperlinks are unwrapped into plain
  text and every external relationship is dropped;
- bookmark names are renamed to generated neutral names;
- core/app document properties are emptied; document variables, mail-merge
  settings and attached-template references are removed.

Structural characters (tabs, explicit line breaks) inside an accepted span are
left in place: they carry no document text. Paragraphs and runs are never
removed, which is what lets the verifier check that the document's structure
survived.
"""
from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path

from lxml import etree

from .extract import (BLOCK, W_SPACE, extract_text, localname, parse_xml)
from .package import (CT_NAME, DocxError, read_package, rels_owner, resolve_target)

W = lambda tag: f"{{http://schemas.openxmlformats.org/wordprocessingml/2006/main}}{tag}"  # noqa: E731

CORE = "docProps/core.xml"
APP = "docProps/app.xml"
SETTINGS = "word/settings.xml"

COMMENT_ELEMENTS = {W("commentReference"), W("commentRangeStart"), W("commentRangeEnd")}
INSTRUCTION_ELEMENTS = {W("instrText"), W("delInstrText"), W("delText")}
# Table captions/descriptions are elements whose value is the caption text.
CAPTION_ELEMENTS = {W("tblCaption"), W("tblDescription")}
TRACKED_ELEMENTS = {
    W("ins"), W("del"), W("moveFrom"), W("moveTo"),
    W("moveFromRangeStart"), W("moveFromRangeEnd"),
    W("moveToRangeStart"), W("moveToRangeEnd"),
}
ALT_TEXT_ATTRS = {"descr", "title", "tooltip", "tblCaption", "tblDescription", "instr", "alt"}
SHAPE_NAME_LOCALS = {"docPr", "cNvPr"}


# ---- text replacement ------------------------------------------------------
def _apply_span(container, start: int, end: int) -> int:
    """Replace every original character of text[start:end] with U+2588.

    A replacement is the same number of characters as the removed text, so
    offsets do not move while several spans are applied. Returns the number of
    ``w:t`` elements touched (0 for a span that covers only virtual characters
    such as a tab: those hold no document text).
    """
    if not isinstance(start, int) or not isinstance(end, int):
        raise DocxError("a decision has non-integer text offsets")
    if start < 0 or end > len(container.text) or end <= start:
        raise DocxError("a decision does not map to text in this document "
                        "(was the file changed after the decision file was written?)")
    touched = 0
    for run in container.runs:
        if run.end <= start or run.start >= end:
            continue
        lo = max(start, run.start) - run.start
        hi = min(end, run.end) - run.start
        element = run.element
        old = element.text or ""
        element.text = old[:lo] + BLOCK * (hi - lo) + old[hi:]
        element.set(W_SPACE, "preserve")
        touched += 1
    return touched


# ---- scrubbing -------------------------------------------------------------
def _remove_elements(root, tags: set[str]) -> int:
    doomed = [el for el in root.iter() if isinstance(el.tag, str) and el.tag in tags]
    for el in doomed:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    return len(doomed)


def _scrub_text_part(root) -> None:
    """Remove every supported text carrier that is not the document body."""
    _remove_elements(root, COMMENT_ELEMENTS | INSTRUCTION_ELEMENTS | TRACKED_ELEMENTS
                     | CAPTION_ELEMENTS | {W("customXmlPr")})
    # Field instructions can also live in an attribute of a simple field.
    for element in root.iter(W("fldSimple")):
        for attr in list(element.attrib):
            if localname(attr) == "instr":
                del element.attrib[attr]
    # A hyperlink's visible runs stay; the link target and its relationship go.
    etree.strip_tags(root, W("hyperlink"), W("customXml"))
    bookmark = 0
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        local = localname(element.tag)
        for attr in list(element.attrib):
            if localname(attr) in ALT_TEXT_ATTRS:
                del element.attrib[attr]
        if local in SHAPE_NAME_LOCALS:
            for attr in list(element.attrib):
                if localname(attr) == "name":
                    element.set(attr, "")
        elif local == "bookmarkStart":
            for attr in list(element.attrib):
                if localname(attr) == "name":
                    element.set(attr, f"_redacted_bm_{bookmark}")
                    bookmark += 1
        elif local in ("alias", "tag"):
            for attr in list(element.attrib):
                if localname(attr) == "val":
                    element.set(attr, "")


def _scrub_core(root) -> None:
    """Leave an empty core-properties part: no value, no attribute, no child name."""
    for child in list(root):
        root.remove(child)
    root.attrib.clear()


def _scrub_app(root) -> None:
    """Leave an empty application-properties part: no value, no attribute, no child."""
    for child in list(root):
        root.remove(child)
    root.attrib.clear()


def _scrub_settings(root) -> None:
    """Drop settings that can carry text, and neutralise remaining name attributes."""
    _remove_elements(root, {W("docVars"), W("attachedTemplate"), W("saveThroughXslt"),
                            W("mailMerge"), W("smartTagType"), W("captions")})
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for attr in list(element.attrib):
            if localname(attr) == "name":
                element.set(attr, "")


def _neutral_renames(layout, names) -> dict[str, str]:
    """Rename image and embedded-font parts to neutral generated names.

    A part name is recoverable text (it sits in the ZIP directory), so an image
    file named after a person would leak the name even though the image is only
    flagged for review. The extension is taken from a fixed allowlist (enforced
    at pre-flight), never from the producer's own string.
    """
    renames: dict[str, str] = {}
    taken = set(names)
    counter = 0
    for name in sorted(layout.media_parts):
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
        counter += 1
        candidate = f"word/media/redacted_{counter}.{extension}"
        while candidate in taken:
            counter += 1
            candidate = f"word/media/redacted_{counter}.{extension}"
        taken.add(candidate)
        renames[name] = candidate
    for name in sorted(layout.font_parts):
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
        counter += 1
        candidate = f"word/fonts/redacted_font_{counter}.{extension}"
        while candidate in taken:
            counter += 1
            candidate = f"word/fonts/redacted_font_{counter}.{extension}"
        taken.add(candidate)
        renames[name] = candidate
    return renames


def _clean_content_types(root, dropped: set[str], renames: dict[str, str]) -> bool:
    changed = False
    for element in list(root):
        if localname(element.tag) != "Override":
            continue
        part = (element.get("PartName") or "").lstrip("/")
        if part in dropped:
            root.remove(element)
            changed = True
        elif part in renames:
            element.set("PartName", "/" + renames[part])
            changed = True
    return changed


def _clean_rels(root, rels_name: str, dropped: set[str], surviving: set[str],
                renames: dict[str, str]) -> bool:
    changed = False
    owner_dir = posixpath.dirname(rels_owner(rels_name))
    for element in list(root):
        if localname(element.tag) != "Relationship":
            continue
        if element.get("TargetMode") == "External":
            root.remove(element)
            changed = True
            continue
        target = element.get("Target") or ""
        resolved = resolve_target(rels_name, target)
        if resolved in dropped or resolved not in surviving:
            root.remove(element)
            changed = True
            continue
        if resolved in renames:
            new_name = renames[resolved]
            if target.startswith("/"):
                element.set("Target", "/" + new_name)
            else:
                element.set("Target", posixpath.relpath(new_name, owner_dir or "."))
            changed = True
    return changed


# ---- serialisation ---------------------------------------------------------
def _standalone(original: bytes) -> bool | None:
    head = original[:300]
    if not head.lstrip().startswith(b"<?xml"):
        return None
    if b'standalone="yes"' in head or b"standalone='yes'" in head:
        return True
    if b'standalone="no"' in head or b"standalone='no'" in head:
        return False
    return None


def _strip_comments(root) -> None:
    doomed = [el for el in root.iter() if isinstance(el, etree._Comment)]
    for comment in doomed:
        parent = comment.getparent()
        if parent is not None:
            parent.remove(comment)


def _serialise(original: bytes, root) -> bytes:
    _strip_comments(root)          # an XML comment can carry text we do not redact
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=_standalone(original))


# ---- the writer ------------------------------------------------------------
def redact_docx(src: str | Path, out: str | Path, spans) -> None:
    """Write `out` with the spans removed. `spans` is [(container_page, start, end)]."""
    from .preflight import blocked, preflight_docx

    src, out = Path(src).resolve(), Path(out).resolve()
    if src == out:
        raise DocxError("refusing to overwrite the source file")
    findings = preflight_docx(src)
    if blocked(findings):
        raise DocxError("the input is refused by pre-flight; nothing was written")
    package = read_package(src)
    layout = package.layout
    if layout.unknown_parts or layout.macro_parts or layout.embedded_parts:
        raise DocxError("the input has parts this writer cannot process")

    docx_text = extract_text(package)
    by_page: dict[int, list[tuple[int, int]]] = {}
    for page, start, end in spans:
        try:
            page, start, end = int(page), int(start), int(end)
        except (TypeError, ValueError):
            raise DocxError("a decision has non-integer text offsets") from None
        by_page.setdefault(page, []).append((start, end))
    if by_page:
        known = {c.page for c in docx_text.containers}
        unknown_pages = sorted(set(by_page) - known)
        if unknown_pages:
            raise DocxError("a decision points at a text container that no longer exists")
    for container in docx_text.containers:
        for start, end in by_page.get(container.page, ()):
            _apply_span(container, start, end)

    dropped = layout.dropped_parts()
    surviving = set(package.names) - dropped
    renames = _neutral_renames(layout, package.names)
    edited: dict[str, bytes] = {}

    for part in layout.text_parts:
        root = docx_text.roots[part]
        _scrub_text_part(root)
        edited[part] = _serialise(package.parts[part], root)

    for name, scrub in ((CORE, _scrub_core), (APP, _scrub_app)):
        original = package.parts.get(name)
        if original is None:
            continue
        root = parse_xml(original)
        scrub(root)
        edited[name] = _serialise(original, root)

    if SETTINGS in package.parts:
        root = parse_xml(package.parts[SETTINGS])
        _scrub_settings(root)
        edited[SETTINGS] = _serialise(package.parts[SETTINGS], root)

    root = parse_xml(package.parts[CT_NAME])
    if _clean_content_types(root, dropped, renames):
        edited[CT_NAME] = _serialise(package.parts[CT_NAME], root)

    for rels_name in layout.rels_parts:
        if rels_owner(rels_name) in layout.strip_parts:
            continue
        root = parse_xml(package.parts[rels_name])
        if _clean_rels(root, rels_name, dropped, surviving, renames):
            edited[rels_name] = _serialise(package.parts[rels_name], root)

    with zipfile.ZipFile(str(out), "w") as zf:
        for name in package.names:
            if name in dropped:
                continue
            payload = edited.get(name, package.parts[name])
            info = package.infos[name]
            entry = zipfile.ZipInfo(renames.get(name, name), date_time=info.date_time)
            entry.compress_type = info.compress_type
            entry.external_attr = info.external_attr
            entry.internal_attr = info.internal_attr
            entry.create_system = info.create_system
            zf.writestr(entry, payload)
