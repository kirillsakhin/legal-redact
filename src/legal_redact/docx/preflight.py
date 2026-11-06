"""Fail-closed pre-flight for .docx files.

Same principle as the PDF pre-flight: if we cannot be sure we can remove
everything that can carry text, say so BEFORE the lawyer spends time reviewing.
Findings carry stable codes and never contain document text -- only counts,
fixed field names and structural facts.

REFUSE  -> export is blocked.
REVIEW  -> export only after explicit acknowledgement (see `review.py`).
INFO    -> something will be changed automatically; no decision needed.
"""
from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

from ..models import Finding, Severity
from .extract import extract_from_parts, localname
from .package import CT_NAME, DocxError, FileKind, file_kind, read_package_bytes

REFUSE, REVIEW, INFO = Severity.REFUSE, Severity.REVIEW, Severity.INFO

# Elements that mean the document has tracked changes: deleted text, insertions,
# moves, table-row/cell revisions and the *PrChange formatting revisions.
TRACKED_ELEMENTS = {
    "ins", "del", "moveFrom", "moveTo", "delText", "delInstrText",
    "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
    "cellIns", "cellDel", "cellMerge",
    "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlDelRangeStart",
    "customXmlDelRangeEnd", "customXmlMoveFromRangeStart", "customXmlMoveFromRangeEnd",
    "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd",
}
# Content merged at open time from a part we do not read (HTML/RTF chunks,
# subdocuments) or an embedded object: REFUSE rather than guess.
UNSUPPORTED_ELEMENTS = {"altChunk": "PF119", "subDoc": "PF119", "object": "PF106",
                        "control": "PF106", "ffData": "PF122"}

# Attributes that can hold text a reader sees or can recover, and that this tool
# removes on export. Counted for the REVIEW that warns about the removal.
ALT_TEXT_ATTRS = {"descr", "title", "tooltip", "tblCaption", "tblDescription"}

APP_SAFE_TEXT = {"Application", "AppVersion"}
CORE_SCRUBBED = "docProps/core.xml"
APP_SCRUBBED = "docProps/app.xml"
SETTINGS = "word/settings.xml"

# An image covering at least this share of a page is treated as "page content may
# live in pixels" (the same 25% rule the PDF pre-flight uses). A .docx cannot
# redact pixels, so a large image is a refusal rather than a warning.
LARGE_IMAGE_SHARE = 0.25
EMU_PER_TWIP = 635
DEFAULT_PAGE_TWIPS = (12240, 15840)          # US Letter, used when no sectPr size exists
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
EMU_LENGTH_RE = re.compile(r"^\s*([\d.]+)\s*(pt|in|cm|mm|px|emu)?\s*$")
_EMU_FACTOR = {"pt": 12700, "in": 914400, "cm": 360000, "mm": 36000, "px": 9525, "emu": 1}


def _attr(element, name: str) -> str:
    for key, value in element.attrib.items():
        if localname(key) == name:
            return value or ""
    return ""


def _page_area_emu2(roots) -> float:
    twips: list[tuple[float, float]] = []
    root = roots.get("word/document.xml")
    if root is not None:
        for element in root.iter():
            if isinstance(element.tag, str) and localname(element.tag) == "pgSz":
                try:
                    twips.append((float(_attr(element, "w")), float(_attr(element, "h"))))
                except ValueError:
                    continue
    if not twips:
        twips = [tuple(float(v) for v in DEFAULT_PAGE_TWIPS)]  # type: ignore[list-item]
    width, height = max(twips, key=lambda pair: pair[0] * pair[1])
    return (width * EMU_PER_TWIP) * (height * EMU_PER_TWIP)


def _length_to_emu(value: str) -> float | None:
    match = EMU_LENGTH_RE.match(value or "")
    if not match:
        return None
    unit = (match.group(2) or "px").lower()
    return float(match.group(1)) * _EMU_FACTOR[unit]


def _vml_style_size(style: str) -> tuple[float, float] | None:
    width = height = None
    for part in (style or "").split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip().lower()
        if key not in ("width", "height"):
            continue
        emu = _length_to_emu(value)
        if emu is None:
            continue
        if key == "width":
            width = emu
        else:
            height = emu
    if width is None or height is None:
        return None
    return width, height


def _image_areas_emu2(roots) -> list[float]:
    """Display area of every picture found in the text parts.

    Only pictures count: a floating text box has a `wp:extent` too, but its
    content IS text we redact, so it must not be treated as an image.
    """
    areas: list[float] = []
    wp_extent = f"{{{WP_NS}}}extent"
    for root in roots.values():
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            name = localname(element.tag)
            if name == "drawing":
                if not any(isinstance(x.tag, str) and localname(x.tag) == "blip"
                           for x in element.iter()):
                    continue
                extent = next((x for x in element.iter() if x.tag == wp_extent), None)
                if extent is None:
                    extent = next((x for x in element.iter()
                                   if isinstance(x.tag, str) and localname(x.tag) == "ext"
                                   and _attr(x, "cx")), None)
                if extent is None:
                    continue
                try:
                    areas.append(float(_attr(extent, "cx")) * float(_attr(extent, "cy")))
                except ValueError:
                    continue
            elif name == "pict":
                if not any(isinstance(x.tag, str) and localname(x.tag) == "imagedata"
                           for x in element.iter()):
                    continue
                shape = next((x for x in element.iter()
                              if isinstance(x.tag, str) and localname(x.tag) == "shape"), None)
                if shape is not None:
                    size = _vml_style_size(_attr(shape, "style"))
                    if size:
                        areas.append(size[0] * size[1])
    return areas


def _presentation_text(parts: dict[str, bytes]) -> list[str]:
    """Strings from parts the writer copies verbatim but that can hold user text."""
    values: list[str] = []

    def collect(part: str, localname_attr: dict[str, str]) -> None:
        data = parts.get(part)
        if data is None:
            return
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            attr = localname_attr.get(localname(element.tag))
            if attr is None:
                continue
            value = _attr(element, attr).strip()
            if value:
                values.append(value)

    collect("word/styles.xml", {"name": "val", "lsdException": "name"})
    collect("word/numbering.xml", {"lvlText": "val"})
    collect("word/fontTable.xml", {"font": "name"})
    for name in parts:
        if name.startswith("word/theme/"):
            collect(name, {"theme": "name", "font": "typeface"})
    return values


def _part_name_refusal(package) -> Finding | None:
    """Refuse when a copied metadata string reads like a personal identifier.

    Part names end up in the archive directory and presentation names (style,
    font, list-numbering) are copied verbatim; the writer renames image/font
    parts, but a name that reads like an e-mail/ID/IBAN/card number is refused
    rather than released. This also covers the font-face names in an inert part.
    """
    from ..detect.rules import RulesDetector
    from ..models import PageText
    streams = list(package.names) + _presentation_text(package.parts)
    flattened = "\n".join(re.sub(r"[^A-Za-z0-9]+", " ", value) for value in streams).lower()
    suggestions = RulesDetector(names=False).detect([PageText(0, 0.0, 0.0, flattened)])
    if suggestions:
        return _ext(REFUSE, "PF124",
                    "Package metadata (part names, style/font/list-numbering names) "
                    "contains text that looks like a personal identifier. This tool will "
                    "not release a file that keeps one, so the document is refused.")
    return None


def _copied_text_counts(parts: dict[str, bytes]) -> dict[str, int]:
    """Count presentation-only text the writer copies unchanged (reported, not read)."""
    counts = {"style_names": 0, "numbering_formats": 0, "font_names": 0}
    for name, kind in (("word/styles.xml", "style"),
                       ("word/numbering.xml", "numbering"),
                       ("word/fontTable.xml", "font")):
        data = parts.get(name)
        if data is None:
            continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            continue
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            local = localname(element.tag)
            if kind == "style" and local == "name" and _attr(element, "val").strip():
                counts["style_names"] += 1
            elif kind == "numbering" and local == "lvlText" and _attr(element, "val").strip():
                counts["numbering_formats"] += 1
            elif kind == "font" and local == "font" and _attr(element, "name").strip():
                counts["font_names"] += 1
    return counts


# XML constructs that can carry text but are not part of the OOXML data model.
# Word does not write them; we refuse rather than try to scrub them.
SAFE_EXTENSION = re.compile(r"^[A-Za-z0-9]+$")
SAFE_CONTENT_TYPE = re.compile(r"^[A-Za-z0-9.+/-]+$")
SAFE_PART_NAME = re.compile(r"^/[A-Za-z0-9._%/-]+$")
SAFE_REL_ID = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_REL_INTERNAL = re.compile(r"^[A-Za-z0-9._/:%#?=&+@~-]+$")


def _ext(kind: Severity, code: str, message: str) -> Finding:
    return Finding(code, kind, message, None)


def _extension_refusal(path: Path) -> Finding | None:
    suffix = path.suffix.lower()
    if suffix in (".docm", ".dotm"):
        return _ext(REFUSE, "PF103",
                    "Macro-enabled Word container (.docm/.dotm) is not supported; "
                    "save the file as .docx without macros.")
    if suffix == ".doc":
        return _ext(REFUSE, "PF102",
                    "Legacy binary Word document (.doc) is not supported; "
                    "save it as .docx first.")
    if suffix in (".dotx", ".dot"):
        return _ext(REFUSE, "PF104",
                    "Word template (.dotx/.dot) is not supported; "
                    "save it as a .docx document first.")
    if suffix != ".docx":
        return _ext(REFUSE, "PF104",
                    "Only .docx files are supported for DOCX processing.")
    return None


def _kind_finding(kind: FileKind) -> Finding | None:
    if kind == FileKind.ENCRYPTED_OOXML:
        return _ext(REFUSE, "PF101",
                    "Document is encrypted or password-protected (Office encryption "
                    "container); it cannot be inspected or redacted.")
    if kind == FileKind.LEGACY_DOC:
        return _ext(REFUSE, "PF102",
                    "File is a legacy binary Word document (.doc), not an OOXML .docx.")
    if kind == FileKind.OTHER_OLE:
        return _ext(REFUSE, "PF104",
                    "File is an OLE/Compound File container that is not a Word document.")
    if kind == FileKind.PDF:
        return _ext(REFUSE, "PF104", "File is a PDF, not a .docx package.")
    if kind == FileKind.UNKNOWN:
        return _ext(REFUSE, "PF104", "File is not a readable .docx (OOXML) package.")
    return None


def _rels_with_external_targets(parts: dict[str, bytes], names) -> int:
    count = 0
    for name in names:
        if not (name.startswith("_rels/") or "/_rels/" in name):
            continue
        data = parts.get(name)
        if data is None:
            continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            continue
        for el in root:
            if el.get("TargetMode") == "External":
                count += 1
    return count


def _count_carriers(roots) -> dict[str, int]:
    """Count elements/attributes that can hold text and are removed on export."""
    seen = {"alt_text": 0, "hyperlink": 0, "field_code": 0, "bookmark": 0,
            "content_control": 0, "comment_mark": 0, "custom_xml": 0}
    for root in roots.values():
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            local = localname(el.tag)
            if local == "customXml":
                seen["custom_xml"] += 1
            for attr in el.attrib:
                if localname(attr) in ALT_TEXT_ATTRS and (el.get(attr) or "").strip():
                    seen["alt_text"] += 1
            if local == "hyperlink":
                seen["hyperlink"] += 1
            elif local in ("instrText", "delInstrText") and (el.text or "").strip():
                seen["field_code"] += 1
            elif local == "fldSimple":
                for attr in el.attrib:
                    if localname(attr) == "instr" and (el.get(attr) or "").strip():
                        seen["field_code"] += 1
            elif local == "bookmarkStart":
                seen["bookmark"] += 1
            elif local in ("alias", "tag"):
                for attr in el.attrib:
                    if localname(attr) == "val" and (el.get(attr) or "").strip():
                        seen["content_control"] += 1
            elif local in ("commentReference", "commentRangeStart", "commentRangeEnd"):
                seen["comment_mark"] += 1
    return seen


def _core_has_values(parts: dict[str, bytes]) -> int:
    data = parts.get(CORE_SCRUBBED)
    if data is None:
        return 0
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return 1
    return sum(1 for el in root.iter() if (el.text or "").strip())


def _app_has_values(parts: dict[str, bytes]) -> int:
    data = parts.get(APP_SCRUBBED)
    if data is None:
        return 0
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return 1
    return sum(1 for el in root.iter() if (el.text or "").strip())


def _xml_construct_refusal(parts: dict[str, bytes]) -> Finding | None:
    """Refuse text-carrying XML constructs that are not OOXML data.

    XML comments, processing instructions and DOCTYPE/entity declarations can
    hold arbitrary text and are invisible to every extraction step, so they are
    refused. CDATA is refused outside the text parts for the same reason; inside
    a text part its content lands in the text model and is redacted normally.
    """
    for name, data in parts.items():
        if not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        if b"<!--" in data:
            return _ext(REFUSE, "PF121",
                        "XML comments found in a package part. They can carry text and "
                        "are not scrubbed; the file is refused.")
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            return _ext(REFUSE, "PF121",
                        "A document-type or entity declaration was found. It is not "
                        "supported and is refused.")
        tail = data.lstrip(b"\xef\xbb\xbf \t\r\n")
        if tail.startswith(b"<?xml"):
            end = tail.find(b"?>")
            tail = tail[end + 2:] if end >= 0 else b""
        if b"<?" in tail:
            return _ext(REFUSE, "PF121",
                        "Processing instructions were found in a package part. They can "
                        "carry text and are not scrubbed; the file is refused.")
        if b"<![CDATA[" in data and not TEXT_PART_RE.match(name):
            return _ext(REFUSE, "PF121",
                        "CDATA sections were found outside the text parts. They can carry "
                        "text and are not scrubbed; the file is refused.")
    return None


def _structural_value_refusal(parts: dict[str, bytes], layout) -> Finding | None:
    """Structural attributes must not double as a text channel.

    Content types and relationships are copied or only lightly rewritten; a
    producer that hides a name in an attribute value there would otherwise
    survive export. Values must match the shapes the format allows.
    """
    ct_bytes = parts.get(CT_NAME)
    if ct_bytes is not None:
        try:
            root = ET.fromstring(ct_bytes)
        except ET.ParseError:
            return _ext(REFUSE, "PF104", "The content-types part is malformed.")
        for element in root.iter():
            for attr, value in element.attrib.items():
                bad = ((attr == "Extension" and not SAFE_EXTENSION.match(value))
                       or (attr == "ContentType" and not SAFE_CONTENT_TYPE.match(value))
                       or (attr == "PartName" and not SAFE_PART_NAME.match(value)))
                if bad:
                    return _ext(REFUSE, "PF121",
                                "The content-types part contains a value outside the "
                                "structural vocabulary; it could double as hidden text.")
    for name in layout.rels_parts:
        try:
            root = ET.fromstring(parts[name])
        except ET.ParseError:
            return _ext(REFUSE, "PF104", "A relationships part is malformed.")
        for element in root.iter():
            local = element.tag.rsplit("}", 1)[-1]
            if local != "Relationship":
                continue
            if not SAFE_REL_ID.match(element.get("Id") or ""):
                return _ext(REFUSE, "PF121",
                            "A relationship id is outside the structural vocabulary.")
            if not SAFE_REL_INTERNAL.match(element.get("Type") or ""):
                return _ext(REFUSE, "PF121",
                            "A relationship type is outside the structural vocabulary; "
                            "it could double as hidden text.")
            # External relationships (hyperlinks, attached templates) are removed on
            # export, so whatever text they hold leaves with them: those targets are
            # not a leak channel and are not inspected here.
            if element.get("TargetMode") == "External":
                continue
            if not SAFE_REL_INTERNAL.match(element.get("Target") or ""):
                return _ext(REFUSE, "PF121",
                            "A relationship target is outside the structural vocabulary; "
                            "it could double as hidden text.")
    return None


def preflight_docx(path: str | Path) -> list[Finding]:
    """Inspect a .docx; return findings. Never raises for hostile input."""
    out: list[Finding] = []
    add = lambda kind, code, message: out.append(_ext(kind, code, message))  # noqa: E731
    path = Path(path)

    ext = _extension_refusal(path)
    if ext is not None:
        return [ext]

    try:
        data = path.read_bytes()
    except OSError:
        return [_ext(REFUSE, "PF104", "File could not be read.")]

    kind = file_kind(path)
    refused = _kind_finding(kind)
    if refused is not None:
        return [refused]

    try:
        package = read_package_bytes(data)
    except DocxError as exc:
        return [_ext(REFUSE, exc.code,
                     {"PF101": "Package entries are encrypted.",
                      "PF109": "Package is too large to inspect safely.",
                      "PF110": "Package contains duplicate part names."}
                     .get(exc.code, "File is not a supported OOXML .docx package."))]
    layout = package.layout

    # --- constructs and attribute values that must not double as text --------
    structure_refusal = (_xml_construct_refusal(package.parts)
                         or _structural_value_refusal(package.parts, layout))
    if structure_refusal is not None:
        return [structure_refusal]

    # --- structural refusals ------------------------------------------------
    if layout.main_kind in ("macro", "macro_template"):
        add(REFUSE, "PF103", "The main document part is macro-enabled.")
    if layout.macro_parts:
        add(REFUSE, "PF103", f"{len(layout.macro_parts)} macro/ActiveX part(s) found.")
    if layout.embedded_parts:
        add(REFUSE, "PF106", f"{len(layout.embedded_parts)} embedded OLE/package object(s) found.")
    if layout.unknown_parts:
        add(REFUSE, "PF107",
            f"{len(layout.unknown_parts)} part(s) are outside the recognised set and may "
            "carry text; this tool cannot promise they are scrubbed.")
    if any(f.code in ("PF103", "PF106", "PF107") for f in out):
        return out                     # nothing else can be promised about this file

    name_refusal = _part_name_refusal(package)
    if name_refusal is not None:
        return [name_refusal]

    for name in (CORE_SCRUBBED, APP_SCRUBBED):
        data = package.parts.get(name)
        if data is None:
            continue
        try:
            ET.fromstring(data)
        except ET.ParseError:
            add(REFUSE, "PF104", "A document-properties part is not well-formed XML.")
            return out

    # --- parse the text parts (also refuses malformed or oddly-structured XML) --
    try:
        docx_text = extract_from_parts(package.parts, layout.text_parts)
    except DocxError as exc:
        message = ("A text part is not well-formed XML."
                   if exc.code == "PF104" else
                   "A text part holds text in a structure this tool does not read and "
                   "cannot scrub (for example math, DrawingML text or form-field data).")
        add(REFUSE, exc.code, message)
        return out
    roots = docx_text.roots

    # --- tracked changes and unsupported embedded content -------------------
    tracked, unsupported = 0, {}
    for part, root in roots.items():
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            local = localname(el.tag)
            if local in TRACKED_ELEMENTS or local.endswith("Change"):
                tracked += 1
            code = UNSUPPORTED_ELEMENTS.get(local)
            if code:
                unsupported.setdefault(code, []).append(local)
    if tracked:
        add(REFUSE, "PF105",
            f"{tracked} tracked-change or revision element(s) present. Accept or reject "
            "all changes in Word before redacting.")
    for code, elements in sorted(unsupported.items()):
        add(REFUSE, code,
            "The document embeds content this tool does not read ("
            + ", ".join(sorted(set(elements))) + "); it is refused rather than "
            "released with unredacted text.")
    if any(f.severity is REFUSE for f in out):
        return out

    # --- what will be read ---------------------------------------------------
    characters = sum(len(c.text) for c in docx_text.containers)
    textbox_count = len(docx_text.containers) - len(layout.text_parts)
    add(INFO, "PF118",
        f"Read {len(layout.text_parts)} text part(s) "
        f"({', '.join(_part_label(n) for n in layout.text_parts)}) into "
        f"{len(docx_text.containers)} text container(s), {characters} character(s).")
    if textbox_count:
        add(INFO, "PF115",
            f"{textbox_count} text box(es) found; their text is included in detection "
            "and redacted.")

    # --- things that can be removed only if the lawyer knows -----------------
    carriers = _count_carriers(roots)
    external = _rels_with_external_targets(package.parts, layout.rels_parts)
    carrier_total = (carriers["alt_text"] + carriers["hyperlink"] + carriers["field_code"]
                     + carriers["content_control"] + carriers["custom_xml"] + external)
    if carrier_total:
        add(REVIEW, "PF111",
            f"{carrier_total} hidden-text carrier(s) found (alternate text, tooltips, "
            "hyperlink/link targets, field codes, content-control or custom-XML bindings). "
            "They are not part of the document body the detectors read; all are removed "
            "on export (hyperlinks become plain text, fields lose their instructions).")
    if layout.comment_parts or carriers["comment_mark"]:
        add(REVIEW, "PF112",
            f"Comments present ({len(layout.comment_parts)} comment part(s)). Comment "
            "content is not read for detection; all comments are removed on export.")

    # --- images -------------------------------------------------------------
    # A .docx writer cannot remove text burnt into pixels. A large picture is
    # therefore refused (it may be a pasted scan or a photographed page), while a
    # small one (logo, icon, signature block) is a REVIEW the lawyer acknowledges.
    areas = _image_areas_emu2(roots)
    page_area = _page_area_emu2(roots)
    large = [area for area in areas if page_area and area / page_area >= LARGE_IMAGE_SHARE]
    if large:
        share = max(large) / page_area if page_area else 0.0
        add(REFUSE, "PF123",
            f"{len(large)} image(s) cover at least {LARGE_IMAGE_SHARE:.0%} of a page "
            f"(largest about {share:.0%}). A .docx export cannot remove text that lives "
            "in pixels, so a pasted scan or photographed page cannot be redacted here: "
            "convert that page to PDF and redact it as a scan, or remove the image.")
    elif layout.media_parts:
        add(REVIEW, "PF113",
            f"{len(layout.media_parts)} image file(s) present. Text inside images is not "
            "read and images are kept unchanged; check every image yourself. An image "
            "that is a scan or photo of text cannot be redacted in .docx — convert that "
            "page to PDF and redact it as a scan.")
    if layout.font_parts:
        add(INFO, "PF126",
            f"{len(layout.font_parts)} embedded font file(s) present; they are copied "
            "unchanged under neutral generated names.")
    if any(name.startswith(("customXml/", "word/customXml/")) for name in layout.strip_parts):
        add(REVIEW, "PF114",
            "Custom XML parts are present. They can carry document-derived text, so they "
            "are removed on export; content controls bound to them may lose their values.")

    # --- metadata -----------------------------------------------------------
    meta_fields = _core_has_values(package.parts) + _app_has_values(package.parts)
    if meta_fields:
        add(INFO, "PF116",
            f"{meta_fields} document metadata field(s) present (author, editor, company, "
            "title and similar). They will be scrubbed on export.")
    stripped = [n for n in layout.strip_parts if not n.startswith("word/comments")]
    if stripped:
        add(INFO, "PF117",
            f"{len(stripped)} property/thumbnail/personal-data part(s) will be removed "
            "(docProps/custom.xml, people.xml, custom XML, page thumbnail).")

    # --- presentation-only text kept verbatim (reported, not silently copied) --
    copied = _copied_text_counts(package.parts)
    copied_total = sum(copied.values())
    if copied_total:
        add(INFO, "PF126",
            "Presentation-only text is copied unchanged and not inspected: "
            f"{copied['style_names']} style name(s), {copied['numbering_formats']} "
            f"list-numbering format value(s), {copied['font_names']} font name(s). If "
            "personal data is hidden in them it cannot be redacted; check the source if "
            "that matters.")

    # --- settings.xml: document variables can hold text ---------------------
    settings = package.parts.get(SETTINGS)
    if settings is not None:
        try:
            sroot = ET.fromstring(settings)
        except ET.ParseError:
            add(REFUSE, "PF104", "The settings part is malformed.")
            return out
        doc_vars = sum(1 for el in sroot.iter() if localname(el.tag) == "docVars")
        if doc_vars:
            add(INFO, "PF117", "Document variables are present; they will be removed on export.")
    return out


def _part_label(name: str) -> str:
    """A structural label only; never a document value."""
    if name.endswith("/document.xml"):
        return "main document"
    if name.endswith("/footnotes.xml"):
        return "footnotes"
    if name.endswith("/endnotes.xml"):
        return "endnotes"
    if name.endswith("/glossary/document.xml"):
        return "glossary"
    if "/header" in name:
        return "header"
    if "/footer" in name:
        return "footer"
    return "text part"


def blocked(findings: list[Finding]) -> bool:
    return any(f.severity is Severity.REFUSE for f in findings)
