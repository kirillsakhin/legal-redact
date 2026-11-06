"""Stage 5 for .docx: independent checks of the exported file.

Independence is the point. The writer is lxml; everything here reads the output
with a different stack: stdlib ``zipfile``, stdlib ``xml.etree.ElementTree`` and
raw-byte scanning of every decompressed part. A bug in the writer's idea of
"removed" cannot hide from a reader that does not share its code.

Input is the source and output paths plus the redacted passages as needles. The
needles live only in memory; reports carry check ids, categories, counts and
structural facts, never document text. ``verify_docx`` never raises: any
exception inside a check becomes a FAIL, so the caller withholds the file.

What a PASS means, and does not mean: the supported leak routes in
``DOCX_LIMITS`` were checked and none was found. It does NOT prove the document
is safe (see the limits listed in every report).
"""
from __future__ import annotations

import html
import re
import unicodedata
import zipfile
from collections import Counter
from xml.etree import ElementTree as ET

from ..verify import FAIL, PASS, WARN, Check, VerifyReport
from . import images as image_tools

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

TEXT_PART_RE = re.compile(
    r"^word/(document|header\d+|footer\d+|footnotes|endnotes|glossary/document)\.xml$")
COMMENT_PART_RE = re.compile(r"^word/comments(?:Extended|Ids|Extensible)?\.xml$")
VBA_PART_RE = re.compile(r"(?:^|/)(?:vbaProject|vbaData|vbaProjectSignature)[^/]*\.(bin|xml)$")
CUSTOM_XML_PREFIXES = ("customXml/", "word/customXml/")
BANNED_PART_PREFIXES = ("word/embeddings/", "word/activeX/", "docProps/thumbnail")
BANNED_PARTS = {"docProps/custom.xml", "word/people.xml"}

MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

BANNED_ELEMENTS = {
    "ins", "del", "moveFrom", "moveTo", "delText", "delInstrText",
    "commentReference", "commentRangeStart", "commentRangeEnd",
    "instrText", "hyperlink", "altChunk", "subDoc", "object", "control",
    "docVars", "attachedTemplate", "saveThroughXslt", "mailMerge",
    "tblCaption", "tblDescription", "smartTagType", "captions",
    "customXmlPr", "ffData",
}
ALT_TEXT_ATTRS = {"descr", "title", "tooltip", "tblCaption", "tblDescription", "instr", "alt"}
BOOKMARK_NAME_RE = re.compile(r"^_redacted_bm_\d+$")
MEDIA_NAME_RE = re.compile(r"^word/media/redacted_\d+\.[a-z0-9]+$")
FONT_NAME_RE = re.compile(r"^word/fonts/redacted_font_\d+\.[a-z0-9]+$")
THEME_NAME_RE = re.compile(r"^word/theme/(?:theme|themeOverride)\d+\.xml$")
SAFE_EXTENSION = re.compile(r"^[A-Za-z0-9]+$")
SAFE_CONTENT_TYPE = re.compile(r"^[A-Za-z0-9.+/-]+$")
SAFE_PART_NAME = re.compile(r"^/[A-Za-z0-9._%/-]+$")
SAFE_REL_ID = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_REL_VALUE = re.compile(r"^[A-Za-z0-9._/:%#?=&+@~-]+$")

DOCX_LIMITS = [
    "Images are read with local English OCR (or the OCR engine you configured): a character it misreads is invisible to both detection and the output check; handwriting, stamps, signatures, photographs and text at very low resolution are not understood. An image in which the engine finds no text is refused when it is large or its displayed size is unknown, and needs an explicit acknowledgement when it is small.",
    "Animated GIF, animated PNG (APNG), animated WebP and multi-page TIFF are refused (only one frame could be read, so another frame could leak). A single-frame GIF is accepted and normalised to PNG.",
    "Every image in the export is re-encoded as metadata-free PNG: pixels are preserved and EXIF/XMP/ICC/DPI/text metadata is stripped, so file size can grow and an untouched image's bytes change while its pixels do not.",
    "The image OCR leak search can raise a false alarm when a redacted phrase is a compact prefix of unredacted text in the same image (for example a redacted name next to an e-mail address containing it): the output is withheld rather than released.",
    "Images in which OCR read no text or only very small text at native resolution (rotated, tiny, low-contrast, handwriting) are reviewer-acknowledged unverified pixels: a WARN check (V112) names them in every export report, but the tool cannot certify or redact text in them. PF131 requires an explicit acknowledgement for small unread images and PF133 for very small text; large unread images are refused.",
    "Ink evidence is contrast-based (pixel variance inside the engine's word boxes), so an installed-but-dishonest engine that invents words over genuinely textured or noisy pixels is not distinguishable; pure uniform/blank boxes are refused.",    "Tracked changes are refused before export, never resolved; a document with revisions cannot be processed.",
    "Comments are removed entirely, not redacted; their content is never reviewed for detection.",
    "Presentation-only text is copied unchanged and not inspected: style names, list-numbering format text, font names, and most settings/theme values.",
    "A decomposed-Unicode (NFD) form of a name can evade detection: the detectors see the characters as extracted, and no NFC/NFD normalised detection view is built.",
    "A name split across a field boundary, a drawing and its fallback copy, or two text boxes is matched only where the tool's text model joins it.",
    "Over-redaction is possible where Word stores the same content twice (AlternateContent choice and fallback).",
    "Indirect identification (a job title plus a company, a rare fact) is a human judgment, not checked.",
    "Digital signatures and document protection are not preserved or assessed.",
]


# ---- normalisation (mirrors the PDF verifier's rules) ----------------------
def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def _compact(text: str) -> str:
    return text.replace(" ", "")


def _has(haystack_norm: str, needle_norm: str) -> bool:
    return bool(needle_norm) and f" {needle_norm} " in f" {haystack_norm} "


def _leaks(haystack: str, needles) -> tuple[Counter, Counter]:
    full, token = Counter(), Counter()
    hay = norm(haystack)
    for category, text in needles:
        needle = norm(text)
        if not needle:
            continue
        if _has(hay, needle) or (len(_compact(needle)) >= 8 and _compact(needle) in _compact(hay)):
            full[category] += 1
        elif category == "PERSON":
            if any(len(t) >= 3 and _has(hay, t) for t in needle.split()):
                token[category] += 1
    return full, token


def _fmt(counter: Counter) -> str:
    return ", ".join(f"{name}x{count}" for name, count in sorted(counter.items()))


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


# ---- independent readers ---------------------------------------------------
def _read_parts(path) -> tuple[list[str], dict[str, bytes]]:
    with zipfile.ZipFile(str(path)) as zf:
        infos = zf.infolist()
        names = [info.filename for info in infos]
        folded = [n.casefold() for n in names]
        if len(set(folded)) != len(folded):
            raise ValueError("duplicate part names")
        if any(info.flag_bits & 0x1 for info in infos):
            raise ValueError("encrypted entries")
        parts = {}
        for info in infos:
            parts[info.filename] = zf.read(info)
    return names, parts


def _xml_roots(parts: dict[str, bytes]) -> tuple[dict[str, object], list[str]]:
    """Parse every XML-ish part with ElementTree; return roots and unparsable names."""
    roots, broken = {}, []
    for name, data in parts.items():
        if not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        try:
            roots[name] = ET.fromstring(data)
        except ET.ParseError:
            broken.append(name)
    return roots, broken


def _tree_text(root) -> str:
    """All character data and attribute values below one element, with separators."""
    chunks = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        if element.text:
            chunks.append(element.text)
        if element.tail:
            chunks.append(element.tail)
        for value in element.attrib.values():
            if value:
                chunks.append(value)
    return "\n".join(chunks)


def _localnames(root, wanted: set[str]) -> Counter:
    found: Counter = Counter()
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = _local(element.tag)
        if name in wanted or name.endswith("Change"):
            found[name] += 1
    return found


def _count_elements(root, name: str) -> int:
    return sum(1 for element in root.iter()
               if isinstance(element.tag, str) and _local(element.tag) == name)


def _rels_owner(name: str) -> str:
    import posixpath
    if name.startswith("_rels/"):
        return ""
    directory, _, base = name.rpartition("/_rels/")
    if not base.endswith(".rels"):
        return ""
    return posixpath.join(directory, base[:-5])


def _resolve(rels_name: str, target: str) -> str:
    import posixpath
    if target.startswith("/"):
        return target.lstrip("/")
    base = posixpath.dirname(_rels_owner(rels_name))
    return posixpath.normpath(posixpath.join(base, target))


def _editable(name: str) -> bool:
    if TEXT_PART_RE.match(name) or name == "[Content_Types].xml":
        return True
    if name in ("docProps/core.xml", "docProps/app.xml", "word/settings.xml"):
        return True
    return name.startswith("_rels/") or "/_rels/" in name


def _is_media(name: str) -> bool:
    return name.startswith("word/media/")


def _is_font(name: str) -> bool:
    return name.startswith("word/fonts/")


def _may_be_absent(name: str) -> bool:
    if _is_comment_part(name) or name in BANNED_PARTS:
        return True
    if name.startswith(CUSTOM_XML_PREFIXES) or name.startswith(BANNED_PART_PREFIXES):
        return True
    # a .rels part goes away with the stripped part that owns it
    owner = _rels_owner(name)
    return bool(owner) and (owner in BANNED_PARTS
                            or owner.startswith(CUSTOM_XML_PREFIXES)
                            or _is_comment_part(owner))


def _is_comment_part(name: str) -> bool:
    return bool(COMMENT_PART_RE.match(name))


# ---- the checks ------------------------------------------------------------
def verify_docx(src, out, needles, *, image_edits=None, image_needles=None,
                unverified_images=None, engine_config=None) -> VerifyReport:
    report = VerifyReport(limits=list(DOCX_LIMITS))
    try:
        _run(report, src, out, needles, image_edits or {}, image_needles or [],
             unverified_images or [], engine_config)
    except Exception as exc:                      # a checker crash is not a pass
        report.checks.append(Check("V100", "verification completed", FAIL,
                                   f"verifier crashed ({type(exc).__name__}); "
                                   "the output must be withheld"))
    return report


def _run(report: VerifyReport, src, out, needles, image_edits, image_needles,
         unverified_images, engine_config) -> None:
    add = lambda cid, name, status, detail: report.checks.append(  # noqa: E731
        Check(cid, name, status, detail))

    # V101 the output is a readable OOXML package
    src_names, src_parts = [], {}
    try:
        src_names, src_parts = _read_parts(src)
    except Exception as exc:
        add("V101", "output is a readable OOXML package", FAIL,
            f"the source could not be re-read ({type(exc).__name__}); output withheld")
        return
    try:
        names, parts = _read_parts(out)
    except Exception as exc:
        add("V101", "output is a readable OOXML package", FAIL,
            f"output is not a readable OOXML package ({type(exc).__name__})")
        return
    roots, broken = _xml_roots(parts)
    if broken:
        add("V101", "output is a readable OOXML package", FAIL,
            f"{len(broken)} part(s) do not parse as XML")
        return
    if "[Content_Types].xml" not in parts:
        add("V101", "output is a readable OOXML package", FAIL,
            "output has no [Content_Types].xml")
        return
    text_parts = [n for n in roots if TEXT_PART_RE.match(n)]
    if not text_parts:
        add("V101", "output is a readable OOXML package", FAIL,
            "output has no recognisable main text part")
        return
    add("V101", "output is a readable OOXML package", PASS,
        f"{len(parts)} part(s), all XML parseable")

    # V102 the redacted passages are gone from the extracted text and attributes
    haystack = "\n".join(_tree_text(root) for root in roots.values())
    full, token = _leaks(haystack, needles)
    if full:
        add("V102", "text extraction: redacted text gone", FAIL,
            "redacted text still present in the output's text or attributes: " + _fmt(full))
    elif token:
        add("V102", "text extraction: redacted text gone", WARN,
            "a fragment of a redacted name appears in the extracted text "
            "(may be coincidence): " + _fmt(token))
    else:
        add("V102", "text extraction: redacted text gone", PASS,
            f"none of {len(needles)} redacted passage(s) found in extracted text or attributes")

    # V103 raw bytes of every part
    patterns = []
    for _, text in needles:
        stripped = text.strip()
        if len(stripped) < 4:
            continue
        for variant in {stripped, stripped.lower(), " ".join(stripped.split())}:
            patterns.append(variant)
    hits = 0
    if patterns:
        for data in parts.values():
            try:
                decoded = data.decode("utf-8", "replace")
            except Exception:
                continue
            candidates = (decoded, html.unescape(decoded))
            if any(pattern in candidate for pattern in patterns for candidate in candidates):
                hits += 1
    if hits:
        add("V103", "raw part scan: redacted text gone", FAIL,
            f"redacted text found in the raw bytes of {hits} part(s)")
    else:
        add("V103", "raw part scan: redacted text gone", PASS,
            f"redacted text not found in the raw bytes of any of {len(parts)} part(s) "
            f"({len(patterns)} byte pattern(s))")

    # V104 no comments, revisions, macros, embedded objects or hidden carriers
    _containers(report, names, parts, roots, add)

    # V105 document properties scrubbed
    _metadata(report, parts, roots, add)

    # V106 document structure and untouched parts preserved
    _structure(report, src_names, src_parts, names, parts, add, image_edits)

    # V107 package integrity
    _integrity(report, names, parts, roots, add)

    # V108 part names are structural or generated, never a source-chosen string
    _names(report, names, add)

    # V109-V111 every exported image: canonical mapping, pixels, metadata, OCR leak;
    # V112 discloses images whose text was never read (WARN, does not withhold).
    if any(_is_media(name) for name in src_parts) or image_edits:
        _images(report, src_parts, parts, image_edits, image_needles,
                unverified_images, engine_config, add)


def _containers(report, names, parts, roots, add) -> None:
    problems = []
    constructs = set()
    for name, data in parts.items():
        if not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        if b"<!--" in data:
            constructs.add("XML comment")
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            constructs.add("doctype/entity declaration")
        if b"<![CDATA[" in data and not TEXT_PART_RE.match(name):
            constructs.add("CDATA outside the text parts")
        tail = data.lstrip(b"\xef\xbb\xbf \t\r\n")
        if tail.startswith(b"<?xml"):
            end = tail.find(b"?>")
            tail = tail[end + 2:] if end >= 0 else b""
        if b"<?" in tail:
            constructs.add("processing instruction")
    if constructs:
        problems.append("text-carrying XML construct survives: " + ", ".join(sorted(constructs)))
    if any(_is_comment_part(n) for n in names):
        problems.append("comment part(s)")
    if any(n in BANNED_PARTS for n in names):
        problems.append("personal-data property part(s)")
    if any(n.startswith(CUSTOM_XML_PREFIXES) for n in names):
        problems.append("custom XML part(s)")
    if any(n.startswith(BANNED_PART_PREFIXES) for n in names):
        problems.append("thumbnail/embedded/ActiveX part(s)")
    if any(VBA_PART_RE.search(n) for n in names):
        problems.append("macro part(s)")

    elements: Counter = Counter()
    external_rels = 0
    for name, root in roots.items():
        elements.update(_localnames(root, BANNED_ELEMENTS))
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            local = _local(element.tag)
            for attr, value in element.attrib.items():
                attr_local = _local(attr)
                if attr_local in ALT_TEXT_ATTRS and (value or "").strip():
                    elements[f"attribute:{attr_local}"] += 1
                elif attr_local == "name" and local in ("docPr", "cNvPr") and (value or "").strip():
                    elements["shape name"] += 1
                elif local in ("alias", "tag") and attr_local == "val" and (value or "").strip():
                    elements["content-control metadata"] += 1
            if local == "bookmarkStart":
                for attr, value in element.attrib.items():
                    if _local(attr) == "name" and not BOOKMARK_NAME_RE.match(value or ""):
                        elements["bookmark name"] += 1
        if name.startswith("_rels/") or "/_rels/" in name:
            for element in root:
                if _local(element.tag) != "Relationship":
                    continue
                if element.get("TargetMode") == "External":
                    external_rels += 1
                if not SAFE_REL_ID.match(element.get("Id") or ""):
                    elements["relationship id"] += 1
                if not SAFE_REL_VALUE.match(element.get("Type") or ""):
                    elements["relationship type"] += 1
                target = element.get("Target") or ""
                if element.get("TargetMode") == "External":
                    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in target):
                        elements["external relationship target"] += 1
                elif not SAFE_REL_VALUE.match(target):
                    elements["relationship target"] += 1
        elif name == "[Content_Types].xml":
            for element in root.iter():
                for attr, value in element.attrib.items():
                    local_attr = _local(attr)
                    bad = ((local_attr == "Extension" and not SAFE_EXTENSION.match(value))
                           or (local_attr == "ContentType"
                               and not SAFE_CONTENT_TYPE.match(value))
                           or (local_attr == "PartName" and not SAFE_PART_NAME.match(value)))
                    if bad:
                        elements["content-type attribute"] += 1
        elif name == "word/settings.xml":
            for element in root.iter():
                for attr, value in element.attrib.items():
                    if _local(attr) == "name" and (value or "").strip():
                        elements["settings name attribute"] += 1
    if elements:
        problems.append("residual elements/attributes: "
                        + ", ".join(f"{k}x{v}" for k, v in sorted(elements.items())))
    if external_rels:
        problems.append(f"{external_rels} external relationship(s)")

    if problems:
        add("V104", "comments, revisions and hidden carriers removed", FAIL,
            "; ".join(problems))
    else:
        add("V104", "comments, revisions and hidden carriers removed", PASS,
            "no comment, revision, macro, embedded-object, external-link or "
            "alternate-carrier element survives")


def _metadata(report, parts, roots, add) -> None:
    problems = []
    for name, label in (("docProps/core.xml", "core"), ("docProps/app.xml", "application")):
        root = roots.get(name)
        if root is None:
            continue
        if len(root):
            problems.append(f"{label} property part still carries {len(root)} element(s)")
        if root.attrib:
            problems.append(f"{label} property part still carries attribute values")
        if (root.text or "").strip():
            problems.append(f"{label} property part still carries text")
    for banned in ("docProps/custom.xml", "docProps/thumbnail.jpeg", "word/people.xml"):
        if banned in parts:
            problems.append(f"{banned} survives")
    if problems:
        add("V105", "document properties scrubbed", FAIL, "; ".join(problems))
    else:
        add("V105", "document properties scrubbed", PASS,
            "creator/editor/company/title values gone, no custom properties, "
            "no thumbnail, no comment-author names")


def _structure(report, src_names, src_parts, out_names, parts, add,
               image_edits=None) -> None:
    problems = []
    src_text = sorted(n for n in src_parts if TEXT_PART_RE.match(n))
    out_text = sorted(n for n in parts if TEXT_PART_RE.match(n))
    if src_text != out_text:
        problems.append(f"text part set changed: {len(src_text)} in source, "
                        f"{len(out_text)} in output")
    src_roots, _ = _xml_roots({n: src_parts[n] for n in src_text})
    out_roots, _ = _xml_roots({n: parts[n] for n in out_text})
    for name in src_text:
        if name not in out_roots:
            continue
        for kind in ("p", "r"):
            before = _count_elements(src_roots[name], kind)
            after = _count_elements(out_roots[name], kind)
            if before != after:
                problems.append(f"{kind} count changed in a text part: {before} -> {after}")
    # images: every image is re-encoded (metadata-free PNG) and renamed to a
    # generated canonical name; the bytes no longer have to be identical, so the
    # pixel-level checks live in V109/V110/V111 below.
    src_media = {name: data for name, data in src_parts.items() if _is_media(name)}
    out_media = {name: data for name, data in parts.items() if _is_media(name)}
    if len(out_media) != len(src_media):
        problems.append("image count changed (every source image must be re-encoded)")
    if any(not MEDIA_NAME_RE.match(name) for name in out_media):
        problems.append("an image part name was not neutralised")
    src_fonts = sorted(data for name, data in src_parts.items() if _is_font(name))
    out_fonts = sorted(data for name, data in parts.items() if _is_font(name))
    if src_fonts != out_fonts:
        problems.append("font part content changed or count changed")
    if any(not FONT_NAME_RE.match(name) for name in parts if _is_font(name)):
        problems.append("a font part name was not neutralised")
    # every part the writer may not edit is byte-identical
    missing, changed, added = [], [], []
    for name, data in src_parts.items():
        if name not in parts:
            if not _may_be_absent(name) and not _is_media(name) and not _is_font(name):
                missing.append(name)
        elif not _editable(name) and parts[name] != data:
            changed.append(name)
        else:
            continue
    for name in parts:
        if name not in src_parts and not _is_media(name) and not _is_font(name):
            added.append(name)
    if missing:
        problems.append(f"{len(missing)} part(s) dropped that should have been kept")
    if changed:
        problems.append(f"{len(changed)} part(s) edited that should have been copied byte-for-byte")
    if added:
        problems.append(f"{len(added)} part(s) added that were not in the source")
    if problems:
        add("V106", "structure and untouched parts preserved", FAIL, "; ".join(problems))
    else:
        add("V106", "structure and untouched parts preserved", PASS,
            f"paragraph and run counts unchanged in {len(src_text)} text part(s); "
            f"{len(out_media)} image(s) preserved with generated names and checked by "
            "V109/V110/V111; non-text parts byte-identical")


def _images(report, src_parts, parts, image_edits, image_needles, unverified_images,
            engine_config, add) -> None:
    """V109-V111: every exported image is re-decoded and checked.

    F1: the writer renames image parts canonically (sorted source media part ->
    ``word/media/redacted_N.png``), so the verifier rebuilds that exact mapping
    from the source package instead of pairing by pixel dimensions. Two images
    with equal dimensions therefore pair correctly, while a missing/extra/
    renamed output image fails closed.

    F4: every image (not only redacted ones) is re-encoded metadata-free PNG by
    the writer, so an untouched image must decode to the same pixels and V111
    scans every image for metadata and original bytes.

    F3/F5: multi-frame images never reach this point (pre-flight refuses them);
    V110 re-reads every output image with the preferred independent reader and
    fails if an accepted image passage is still readable anywhere.
    """
    from ..ocr import OcrError, OcrUnavailable
    from ..ocr_backends import default_config

    edited = dict(image_edits or {})
    src_media = {name: data for name, data in src_parts.items() if _is_media(name)}
    out_media = {name: data for name, data in parts.items() if _is_media(name)}

    # Canonical source->output mapping: the writer's deterministic rule, rebuilt
    # from the source package alone (never from dimensions).
    expected: dict[str, str] = {}
    taken = set(src_parts)
    counter = 0
    for src_name in sorted(src_media):
        counter += 1
        candidate = f"word/media/redacted_{counter}.png"
        while candidate in taken:
            counter += 1
            candidate = f"word/media/redacted_{counter}.png"
        taken.add(candidate)
        expected[src_name] = candidate

    problems: list[str] = []
    if set(out_media) != set(expected.values()):
        problems.append("output image parts are not the generated canonical set "
                        "(missing, extra or not renamed)")
    src_decoded: dict[str, object] = {}
    out_decoded: dict[str, object] = {}
    for src_name, data in src_media.items():
        try:
            src_decoded[src_name] = image_tools.decode_image(data)
        except image_tools.ImageError as exc:
            problems.append(f"a source image cannot be decoded ({type(exc).__name__})")
    for src_name, out_name in expected.items():
        data = out_media.get(out_name)
        if data is None:
            continue
        try:
            out_decoded[src_name] = image_tools.decode_image(data)
        except image_tools.ImageError as exc:
            problems.append(f"an output image cannot be decoded ({type(exc).__name__})")
    if problems or len(out_decoded) != len(src_media) or len(src_decoded) != len(src_media):
        detail = ("; ".join(sorted(set(problems))[:4])
                  or "the output does not contain one decodable image per source image")
        add("V109", "image export: canonical mapping and pixels", FAIL, detail)
        add("V110", "image export: OCR leak search", FAIL,
            "images could not be mapped to the source; no image can be trusted")
        add("V111", "image export: metadata and original bytes", FAIL,
            "images could not be mapped to the source; the output is withheld")
        return

    # V109: same pixel dimensions; edited areas black; untouched images
    # pixel-identical (metadata re-encode may change bytes, never pixels).
    pixel_problems: list[str] = []
    edited_areas = 0
    for src_name in sorted(src_media):
        src_pix = src_decoded[src_name]
        out_pix = out_decoded[src_name]
        if (src_pix.width, src_pix.height) != (out_pix.width, out_pix.height):
            pixel_problems.append("an image's pixel dimensions changed")
            continue
        rects = list(edited.get(src_name, ()))
        if rects:
            edited_areas += len(rects)
            for rect in rects:
                if image_tools.bright_fraction(out_pix, rect) > 0.02:
                    pixel_problems.append("a redacted area is not black in the output pixels")
                    break
        elif bytes(src_pix.samples) != bytes(out_pix.samples):
            pixel_problems.append("an untouched image's pixels changed")
    if pixel_problems:
        add("V109", "image export: canonical mapping and pixels", FAIL,
            "; ".join(sorted(set(pixel_problems))[:3]))
    else:
        add("V109", "image export: canonical mapping and pixels", PASS,
            f"{len(expected)} image(s) matched by canonical name and re-decoded at the "
            f"same pixel size; {edited_areas} redacted area(s) are black; "
            "untouched images are pixel-identical")

    # V111: no image metadata and no original image bytes anywhere in the package.
    metadata_problems: list[str] = []
    originals = list(src_media.values())
    for src_name, out_name in expected.items():
        data = out_media[out_name]
        for marker in (b"Exif\x00\x00", b"<x:xmpmeta", b"http://ns.adobe.com/xap",
                       b"pHYs", b"iCCP", b"iTXt"):
            if marker in data:
                metadata_problems.append("image metadata survives in the output")
                break
        if src_media[src_name] in data:
            metadata_problems.append("an original image's bytes survive in its output image")
    for original in originals:
        if any(original in payload for payload in parts.values()):
            metadata_problems.append("original image bytes survive somewhere in the package")
            break
    if metadata_problems:
        add("V111", "image export: metadata and original bytes", FAIL,
            "; ".join(sorted(set(metadata_problems))[:3]))
    else:
        add("V111", "image export: metadata and original bytes", PASS,
            f"no EXIF/XMP/ICC/DPI metadata and no original image bytes in "
            f"{len(expected)} re-encoded image(s)")

    # V112: disclose images whose pixels were not fully verified for text. Two
    # cases: OCR read no text at all (unread / OCR not used), and PF133 tiny-text
    # images where OCR returned words too small to trust (highlighted text may hide
    # among them). Re-encoded pixels are not a certification of hidden text, so a
    # PASS must be visibly qualified. WARN only, never withholds.
    if unverified_images:
        entries = []
        for index, width, height, reason in sorted(unverified_images)[:8]:
            identity = f"image {index} ({width}x{height} px)"
            if reason == "tiny":
                entries.append(f"{identity}: text was very small, OCR may have missed "
                               "it; its pixels were NOT verified for hidden text")
            elif reason == "unused":
                entries.append(f"{identity}: text not read (OCR was not used for this "
                               "export); its pixels were NOT verified for text")
            else:
                entries.append(f"{identity}: text not read; its pixels were NOT "
                               "verified for text")
        if len(unverified_images) > 8:
            entries.append(f"and {len(unverified_images) - 8} more")
        add("V112", "image export: text not read or too small to read reliably / "
                    "pixels unverified", WARN,
            f"{len(unverified_images)} image(s) were not fully verified: "
            + "; ".join(entries) + ". Review every such image yourself.")
    else:
        add("V112", "image export: text not read or too small to read reliably / "
                    "pixels unverified", PASS,
            f"all {len(expected)} exported image(s) had their text read and none was "
            "flagged as tiny text")

    # V110: re-read every output image and search for the accepted image passages.
    if not image_needles:
        add("V110", "image export: OCR leak search", PASS,
            "no OCR-derived image passage was accepted; the image pixels and metadata "
            "were checked, and no OCR run is needed")
        return
    config = engine_config or default_config()
    reader, independent, label = image_tools.verification_reader(config)
    if reader is None:
        add("V110", "image export: OCR leak search", FAIL,
            "no OCR engine is available to re-read the exported images, so the "
            "output is withheld (fail closed)")
        return
    name, version, _available, _error = image_tools.engine_summary(config)
    report.ocr_engine = {
        "configured": {"engine": config.spec, "name": name, "version": version,
                       "local": config.local},
        "verification_reader": label,
        "independent": independent,
    }
    failures: list[str] = []
    texts: list[str] = []
    for src_name in sorted(src_media):
        out_pix = out_decoded[src_name]
        try:
            result = reader.analyze(out_pix.tobytes("png"),
                                    out_pix.width * 72.0 / image_tools.IMAGE_DPI,
                                    out_pix.height * 72.0 / image_tools.IMAGE_DPI,
                                    image_tools.IMAGE_DPI)
            texts.append(" ".join(word.text for word in result.words))
        except (OcrError, OcrUnavailable):
            failures.append("unavailable")
        except Exception as exc:                  # a broken reader is never a pass
            failures.append(type(exc).__name__)
    if failures:
        add("V110", "image export: OCR leak search", FAIL,
            f"could not re-read {len(failures)} exported image(s) with {label}; an "
            "unreadable image is not a pass")
        return
    suffix = "" if independent else (" -- SAME-ENGINE cross-check (weakest available; "
                                     "Tesseract absent, PF130 acknowledged)")
    if not image_needles:
        add("V110", "image export: OCR leak search", PASS,
            f"no OCR-derived image passage was accepted; re-read {len(texts)} image(s) "
            "for the record" + suffix)
        return
    needles_as_pairs = [("IMAGE", needle) for needle in image_needles]
    full, token = _leaks("\n".join(texts), needles_as_pairs)
    if full:
        add("V110", "image export: OCR leak search", FAIL,
            "an accepted image passage is still readable by OCR in an exported image: "
            + _fmt(full))
    elif token:
        add("V110", "image export: OCR leak search", FAIL,
            "a fragment of an accepted image passage is still readable by OCR: " + _fmt(token))
    else:
        add("V110", "image export: OCR leak search", PASS,
            f"none of {len(image_needles)} accepted image passage(s) readable by {label} "
            f"in {len(texts)} exported image(s)" + suffix)

INERT_FIXED_NAMES = {
    "docProps/core.xml", "docProps/app.xml",
    "word/styles.xml", "word/stylesWithEffects.xml", "word/numbering.xml",
    "word/settings.xml", "word/webSettings.xml", "word/fontTable.xml",
}


def _is_allowed_name(name: str) -> bool:
    """The closed vocabulary of part names a redacted output may use."""
    if name == "[Content_Types].xml":
        return True
    if TEXT_PART_RE.match(name) or name in INERT_FIXED_NAMES:
        return True
    if name.startswith("_rels/") or "/_rels/" in name:
        return True
    return bool(THEME_NAME_RE.match(name) or MEDIA_NAME_RE.match(name)
                or FONT_NAME_RE.match(name))


def _names(report, names, add) -> None:
    """V108: no source-chosen part name may survive (a name is recoverable text)."""
    bad = sorted(name for name in names if not _is_allowed_name(name))
    if bad:
        add("V108", "part names neutralised", FAIL,
            f"{len(bad)} part name(s) survive that are not structural or generated")
    else:
        add("V108", "part names neutralised", PASS,
            f"all {len(names)} part names are structural or generated")


def _integrity(report, names, parts, roots, add) -> None:
    problems = []
    ct = roots.get("[Content_Types].xml")
    if ct is None or ct.tag != f"{{{CT_NS}}}Types":
        problems.append("content types part missing or malformed")
        defaults, overrides = {}, {}
    else:
        defaults, overrides = {}, {}
        for element in ct:
            local = _local(element.tag)
            if local == "Default":
                defaults[(element.get("Extension") or "").lower()] = element.get("ContentType") or ""
            elif local == "Override":
                overrides[(element.get("PartName") or "").lstrip("/")] = element.get("ContentType") or ""
        if overrides.get("word/document.xml") != MAIN_TYPE:
            problems.append("main document content type is not a plain .docx document")
        for part in overrides:
            if part not in parts:
                problems.append(f"content-type override points at a missing part ({len(overrides)} checked)")
                break
    part_set = set(parts)
    for name in parts:
        if name == "[Content_Types].xml":
            continue
        ctype = overrides.get(name)
        if not ctype and "." in name:
            ctype = defaults.get(name.rsplit(".", 1)[-1].lower())
        if not ctype:
            problems.append("a part has no content type; Word may repair the file")
            break
    for name, root in roots.items():
        if not (name.startswith("_rels/") or "/_rels/" in name):
            continue
        for element in root:
            if _local(element.tag) != "Relationship":
                continue
            if element.get("TargetMode") == "External":
                problems.append("external relationship survives")
                break
            target = _resolve(name, element.get("Target") or "")
            if target not in part_set:
                problems.append("a relationship points at a missing part; Word may repair "
                                "the file and drop content")
                break
    if problems:
        add("V107", "package integrity", FAIL, "; ".join(sorted(set(problems))[:4]))
    else:
        add("V107", "package integrity", PASS,
            f"content types and relationship targets resolve for {len(parts)} part(s)")
