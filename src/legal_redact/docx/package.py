"""OOXML (.docx) package access: file-type sniffing, ZIP reading, part classification.

Standard library only (``zipfile`` + ``xml.etree``): this module is imported
before the XML editor so that a legacy .doc, an encrypted container or a macro
file can be refused without ever handing its bytes to a parser.

Fail-closed rule: a part we do not recognise as inert or as one of the text
parts we read/rewrite is a REFUSE, not a warning. OOXML lets any producer add
any part; an unknown part can carry text (a chart, a glossary, an embedded
object's HTML), so "unknown" means "we cannot promise it is scrubbed".

What is deliberately NOT covered here (see docs/DOCX.md): list-numbering
format text (``w:lvlText``), style names, document variables in parts other
than settings.xml, and text hidden in attributes this tool does not scrub.
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from xml.etree import ElementTree as ET

# File magic
CFB_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")          # OLE2 / Compound File Binary
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
PDF_MAGIC = b"%PDF"

# Package caps: a hostile file must not be able to make us decompress the world.
MAX_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024
MAX_PART_UNCOMPRESSED = 64 * 1024 * 1024
MAX_PARTS = 4096

CT_NAME = "[Content_Types].xml"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

# Content types that identify the package's main part.
MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": "docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml": "template",
    "application/vnd.ms-word.document.macroEnabled.main+xml": "macro",
    "application/vnd.ms-word.template.macroEnabled.main+xml": "macro_template",
}

# Parts whose text we read and redact (container order is deterministic).
TEXT_PART_RE = re.compile(
    r"^word/(document|header\d+|footer\d+|footnotes|endnotes|glossary/document)\.xml$")
COMMENT_PART_RE = re.compile(r"^word/comments(?:Extended|Ids|Extensible)?\.xml$")
VBA_PART_RE = re.compile(r"(?:^|/)(?:vbaProject|vbaData|vbaProjectSignature)[^/]*\.(?:bin|xml)$")
# Only these image/font extensions are accepted; their generated names are built
# from the allowlist, never from the producer's own string, so a file name cannot
# carry text into the output.
MEDIA_NAME_RE = re.compile(
    r"^word/media/[A-Za-z0-9._%-]+\.(?:png|jpe?g|gif|bmp|tiff?|emf|wmf|svg|webp|ico|jfif|heic|dib)$",
    re.IGNORECASE)
FONT_NAME_RE = re.compile(
    r"^word/fonts/[A-Za-z0-9._-]+\.(?:odttf|ttf|otf|fntdata|eot|woff2?)$", re.IGNORECASE)
THEME_NAME_RE = re.compile(r"^word/theme/(?:theme|themeOverride)\d+\.xml$")
SAFE_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "emf", "wmf",
                         "svg", "webp", "ico", "jfif", "heic", "dib"}
SAFE_FONT_EXTENSIONS = {"odttf", "ttf", "otf", "fntdata", "eot", "woff", "woff2"}
STRIP_EXACT = {
    "docProps/custom.xml",       # user-defined properties: arbitrary personal text
    "word/people.xml",           # comment author display names
}
STRIP_PREFIXES = (
    "customXml/",                # custom XML data (property bags copied from the document)
    "word/customXml/",
    "docProps/thumbnail",        # a rendered page thumbnail can show the redacted text
)
INERT_EXACT = {
    CT_NAME,
    "docProps/core.xml",
    "docProps/app.xml",
    "word/styles.xml",
    "word/stylesWithEffects.xml",
    "word/numbering.xml",
    "word/settings.xml",
    "word/webSettings.xml",
    "word/fontTable.xml",
}
INERT_PREFIXES = (
    "word/theme/",
    "word/media/",               # images: kept byte-identical, flagged for review
    "word/fonts/",               # embedded fonts: glyph data, no document text
)
MACRO_PREFIXES = ("word/activeX/",)
EMBED_PREFIXES = ("word/embeddings/",)


class FileKind(str, Enum):
    PDF = "pdf"
    OOXML = "ooxml"                  # looks like an OOXML (ZIP) package
    ENCRYPTED_OOXML = "encrypted"    # CFB container: encrypted OOXML
    LEGACY_DOC = "legacy_doc"        # CFB container: binary Word .doc
    OTHER_OLE = "other_ole"          # CFB container that is neither of the above
    UNKNOWN = "unknown"


class DocxError(Exception):
    """A package we cannot process. ``code`` is the pre-flight code to report."""

    def __init__(self, message: str, code: str = "PF104"):
        super().__init__(message)
        self.code = code


def _utf16(name: str) -> bytes:
    return name.encode("utf-16-le")


def file_kind(path: str | Path) -> FileKind:
    """Sniff the file type from magic bytes; the extension is the caller's business."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return FileKind.UNKNOWN
    if head.startswith(PDF_MAGIC):
        return FileKind.PDF
    if head.startswith(ZIP_MAGICS):
        return FileKind.OOXML
    if head.startswith(CFB_MAGIC):
        try:
            data = Path(path).read_bytes()
        except OSError:
            return FileKind.UNKNOWN
        # CFB directory entries store the stream name in UTF-16LE.
        if (_utf16("EncryptionInfo") in data) or (_utf16("EncryptedPackage") in data):
            return FileKind.ENCRYPTED_OOXML
        if _utf16("WordDocument") in data:
            return FileKind.LEGACY_DOC
        return FileKind.OTHER_OLE
    return FileKind.UNKNOWN


# ---- content types ---------------------------------------------------------
@dataclass(frozen=True)
class ContentTypes:
    defaults: dict[str, str] = field(default_factory=dict)   # extension -> content type
    overrides: dict[str, str] = field(default_factory=dict)  # part name (no leading /) -> type

    def content_type_of(self, part: str) -> str | None:
        if part in self.overrides:
            return self.overrides[part]
        ext = part.rsplit(".", 1)[-1].lower() if "." in part else ""
        return self.defaults.get(ext)

    @classmethod
    def parse(cls, data: bytes) -> "ContentTypes":
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise DocxError("content types part is not well-formed XML") from exc
        if root.tag != f"{{{CT_NS}}}Types":
            raise DocxError("content types part has the wrong root element")
        defaults, overrides = {}, {}
        for el in root:
            local = el.tag.rsplit("}", 1)[-1]
            if local == "Default":
                ext = (el.get("Extension") or "").lower()
                ctype = el.get("ContentType") or ""
                if ext and ctype:
                    defaults[ext] = ctype
            elif local == "Override":
                part = (el.get("PartName") or "").lstrip("/")
                ctype = el.get("ContentType") or ""
                if part and ctype:
                    overrides[part] = ctype
        return cls(defaults, overrides)


# ---- part names ------------------------------------------------------------
def is_rels_part(name: str) -> bool:
    return name.startswith("_rels/") or "/_rels/" in name


def rels_owner(name: str) -> str:
    """The part whose relationships live in this .rels part ('_rels/.rels' -> '')."""
    if name.startswith("_rels/"):
        return ""
    directory, _, base = name.rpartition("/_rels/")
    return posixpath.join(directory, base[:-5]) if base.endswith(".rels") else ""


def resolve_target(rels_name: str, target: str) -> str:
    """Resolve an internal relationship target to a package part name."""
    if target.startswith("/"):
        return target.lstrip("/")
    owner = rels_owner(rels_name)
    base = posixpath.dirname(owner)
    return posixpath.normpath(posixpath.join(base, target))


def _is_strip_part(name: str) -> bool:
    if name in STRIP_EXACT:
        return True
    return name.startswith(STRIP_PREFIXES)


def _is_inert_part(name: str) -> bool:
    if name in INERT_EXACT:
        return True
    return name.startswith(INERT_PREFIXES)


def _text_sort_key(name: str):
    if name.endswith("/document.xml"):
        return (0, 0)
    match = re.match(r"^word/(header|footer)(\d+)\.xml$", name)
    if match:
        return (1 if match.group(1) == "header" else 2, int(match.group(2)))
    if name.endswith("/footnotes.xml"):
        return (3, 0)
    if name.endswith("/endnotes.xml"):
        return (4, 0)
    if name.endswith("/glossary/document.xml"):
        return (5, 0)
    return (9, name)


# ---- layout ----------------------------------------------------------------
@dataclass
class Layout:
    main_part: str
    main_kind: str                       # docx | template | macro | macro_template
    text_parts: list[str] = field(default_factory=list)
    comment_parts: list[str] = field(default_factory=list)
    strip_parts: list[str] = field(default_factory=list)     # comment/custom/people/thumbnail
    rels_parts: list[str] = field(default_factory=list)
    media_parts: list[str] = field(default_factory=list)
    font_parts: list[str] = field(default_factory=list)
    macro_parts: list[str] = field(default_factory=list)
    embedded_parts: list[str] = field(default_factory=list)
    unknown_parts: list[str] = field(default_factory=list)
    inert_parts: list[str] = field(default_factory=list)

    def removed_rels_parts(self) -> list[str]:
        """.rels parts whose owner will be removed: they must not be copied."""
        owners = set(self.strip_parts)
        return [n for n in self.rels_parts if rels_owner(n) in owners]

    def dropped_parts(self) -> set[str]:
        removed_rels = set(self.removed_rels_parts())
        return set(self.strip_parts) | removed_rels


@dataclass
class Package:
    names: list[str]
    parts: dict[str, bytes]
    infos: dict[str, zipfile.ZipInfo]
    content_types: ContentTypes
    layout: Layout


SAFE_PART_NAME_RE = re.compile(r"^[A-Za-z0-9._%/-]+$")


def _validate_names(names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if "\\" in name or name.startswith("/") or any(p in ("..", ".") for p in name.split("/")):
            raise DocxError("package contains a part name with a path-traversal pattern")
        if name != CT_NAME and not SAFE_PART_NAME_RE.match(name):
            raise DocxError("package contains a part name outside the OOXML URI vocabulary",
                            code="PF121")
        folded = name.casefold()
        if folded in seen:
            raise DocxError("package contains duplicate part names", code="PF110")
        seen.add(folded)


def analyse(content_types: ContentTypes, names: list[str]) -> Layout:
    """Classify every part. Anything not recognised throws the file out later."""
    main_part, main_kind = "", "docx"
    for part, ctype in content_types.overrides.items():
        if ctype in MAIN_TYPES:
            main_part, main_kind = part, MAIN_TYPES[ctype]
            break
    if not main_part:
        if "word/document.xml" in names:
            main_part, main_kind = "word/document.xml", "docx"
        else:
            raise DocxError("no WordprocessingML main document part found")
    if main_part not in names:
        raise DocxError("the main document part is missing from the package")

    layout = Layout(main_part=main_part, main_kind=main_kind)
    for name in names:
        if is_rels_part(name):
            layout.rels_parts.append(name)
        elif TEXT_PART_RE.match(name):
            layout.text_parts.append(name)
        elif COMMENT_PART_RE.match(name):
            layout.comment_parts.append(name)
            layout.strip_parts.append(name)
        elif _is_strip_part(name):
            layout.strip_parts.append(name)
        elif name.startswith(MACRO_PREFIXES) or VBA_PART_RE.search(name):
            layout.macro_parts.append(name)
        elif name.startswith(EMBED_PREFIXES):
            layout.embedded_parts.append(name)
        elif name.startswith("word/media/"):
            if MEDIA_NAME_RE.match(name):
                layout.media_parts.append(name)
                layout.inert_parts.append(name)
            else:
                layout.unknown_parts.append(name)
        elif name.startswith("word/fonts/"):
            if FONT_NAME_RE.match(name):
                layout.font_parts.append(name)
                layout.inert_parts.append(name)
            else:
                layout.unknown_parts.append(name)
        elif name.startswith("word/theme/"):
            if THEME_NAME_RE.match(name):
                layout.inert_parts.append(name)
            else:
                layout.unknown_parts.append(name)
        elif _is_inert_part(name):
            layout.inert_parts.append(name)
        else:
            layout.unknown_parts.append(name)
    layout.text_parts.sort(key=_text_sort_key)
    layout.comment_parts.sort()
    layout.strip_parts.sort()
    layout.rels_parts.sort()
    layout.media_parts.sort()
    layout.font_parts.sort()
    if main_part not in layout.text_parts:
        raise DocxError("the main document part is not a recognised text part")
    return layout


def read_package_bytes(data: bytes) -> Package:
    if not data.startswith(ZIP_MAGICS):
        raise DocxError("file is not an OOXML (ZIP) package")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocxError("file is not a readable ZIP package") from exc
    try:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        if len(names) > MAX_PARTS:
            raise DocxError("package has too many parts", code="PF109")
        if any(i.flag_bits & 0x1 for i in infos):
            raise DocxError("package has encrypted entries", code="PF101")
        if sum(max(0, i.file_size) for i in infos) > MAX_TOTAL_UNCOMPRESSED:
            raise DocxError("package expands beyond the size limit", code="PF109")
        _validate_names(names)
        if zf.testzip() is not None:
            raise DocxError("a package part is corrupt (CRC mismatch)")
        parts: dict[str, bytes] = {}
        for info in infos:
            try:
                payload = zf.read(info)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                raise DocxError("a package part could not be read") from exc
            if len(payload) > MAX_PART_UNCOMPRESSED:
                raise DocxError("a package part expands beyond the size limit", code="PF109")
            parts[info.filename] = payload
    finally:
        zf.close()
    if CT_NAME not in parts:
        raise DocxError("package has no [Content_Types].xml part")
    content_types = ContentTypes.parse(parts[CT_NAME])
    layout = analyse(content_types, names)
    return Package(names, parts, {i.filename: i for i in infos}, content_types, layout)


def read_package(path: str | Path) -> Package:
    return read_package_bytes(Path(path).read_bytes())
