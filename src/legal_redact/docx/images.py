"""OCR, pixel redaction and image checks for images embedded in a .docx.

An image part is treated as a one-page document at a fixed geometry convention:
its native pixels are a page at ``IMAGE_DPI`` (300 dpi), so a 1200x600 px image is
a 288x144 pt page. The same convention is passed to the OCR engine
(``analyze(png, width_pt, height_pt, 300)``), and the engine's normalised boxes
are mapped straight back to pixels. That keeps the geometry story identical for
Tesseract and for a user's command engine, and it is documented in docs/DOCX.md.

What this module does:

- ``scan_images`` decodes every image part with PyMuPDF (no new dependency),
  refuses unsupported/corrupt/oversized/multi-frame images, and reports counts
  without ever naming a file or quoting content;
- ``read_images`` reads each image with the configured OCR engine into a
  ``PageText`` whose coordinates are image pixels, so the shared detectors and
  the writer/verifier use the same model as text;
- ``burn_image`` decodes the image again, paints the accepted boxes black in the
  pixel data (outwards-rounded, plus a one-pixel pad so antialiased edges are
  covered), and re-encodes lossless PNG with no metadata: the original bytes do
  not survive and the image keeps its exact pixel dimensions;
- ``verify_image_bytes`` helpers let the verifier independently decode the
  output image, measure the box areas, and re-OCR it with a preferred reader.

Nothing here writes to disk and nothing logs image content.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pymupdf

from ..models import PageText, Rect, Word
from ..ocr import OcrError, OcrUnavailable, ocr_available
from ..ocr_backends import EngineConfig, EngineConfigError, OcrEngine, TesseractEngine

IMAGE_DPI = 300                    # geometry convention: image pixels are a 300-dpi page
MAX_IMAGE_PIXELS = 40_000_000      # decode cap: ~40 MP (a large phone photo)
MAX_IMAGE_BYTES = 32 * 1024 * 1024
BURN_PAD_PX = 1                    # cover antialiased edges of the burned word
DECODABLE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff"})
_BRIGHT = 100                      # 0..255; pixels above this count as paper, not ink
_INK_EPSILON = 8                   # luminance difference from the box median that counts as ink
_INK_MIN_SHARE = 0.005             # below this share of contrasting pixels a word box is blank
_MAX_BLANK_WORD_SHARE = 0.25       # tolerated share of OCR word boxes on blank areas
TINY_WORD_HEIGHT_PX = 12.0         # word boxes below this native height are "very small text"
TINY_WORD_SHARE = 0.25             # share of tiny word boxes that flags an image for review
LOW_RES_DPI = 150.0                # effective resolution below this risks unread small text
LARGE_UNREAD_SHARE = 0.25          # an unread image this large cannot be certified by review
SINGLE_IMAGE_UNREAD_SHARE = 0.20   # a document's only image this large is content-bearing


class ImageError(Exception):
    """An image cannot be read or written safely. ``code`` is the pre-flight code."""

    def __init__(self, message: str, code: str = "PF127"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImagePart:
    name: str
    extension: str
    width: int
    height: int
    data: bytes

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def page_points(self) -> tuple[float, float]:
        """The image as a page at IMAGE_DPI: its size in points."""
        return (self.width * 72.0 / IMAGE_DPI, self.height * 72.0 / IMAGE_DPI)


@dataclass(frozen=True)
class ImageReading:
    part: ImagePart
    page: PageText
    rotation: int | None

    @property
    def words(self) -> int:
        return len(self.page.words)


def extension_of(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


# ---- multi-frame detection (stdlib bytes, before any decode) ---------------
def _gif_frames(data: bytes) -> int:
    """Count GIF image descriptors, skipping extensions/sub-blocks. Raises on malformed data."""
    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ImageError("an embedded GIF could not be parsed to establish its frame count")
    packed = data[10]
    position = 13
    if packed & 0x80:
        position += 3 * (2 ** ((packed & 0x07) + 1))
    frames = 0
    while position < len(data):
        marker = data[position]
        if marker == 0x3B:                    # trailer
            return frames
        if marker == 0x21:                    # extension: label + sub-blocks
            position += 2
            while True:
                if position >= len(data):
                    raise ImageError("an embedded GIF could not be parsed to establish its "
                                     "frame count")
                size = data[position]
                position += 1
                if size == 0:
                    break
                position += size
        elif marker == 0x2C:                  # image descriptor
            frames += 1
            if position + 10 > len(data):
                raise ImageError("an embedded GIF could not be parsed to establish its "
                                 "frame count")
            local = data[position + 9]
            position += 10
            if local & 0x80:
                position += 3 * (2 ** ((local & 0x07) + 1))
            position += 1                     # LZW minimum code size
            while True:
                if position >= len(data):
                    raise ImageError("an embedded GIF could not be parsed to establish its "
                                     "frame count")
                size = data[position]
                position += 1
                if size == 0:
                    break
                position += size
        else:
            raise ImageError("an embedded GIF could not be parsed to establish its frame count")
    raise ImageError("an embedded GIF could not be parsed to establish its frame count")


def _png_chunk_types(data: bytes) -> list[bytes]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImageError("an embedded PNG could not be parsed to establish its frame count")
    chunks: list[bytes] = []
    position = 8
    while position + 8 <= len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        chunk_type = data[position + 4:position + 8]
        end = position + 8 + length + 4
        if end > len(data) or length > len(data):
            raise ImageError("an embedded PNG could not be parsed to establish its frame count")
        chunks.append(chunk_type)
        position = end
        if chunk_type == b"IEND":
            return chunks
    raise ImageError("an embedded PNG could not be parsed to establish its frame count")


def _webp_frames(data: bytes) -> int:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ImageError("an embedded WebP could not be parsed to establish its frame count")
    position, frames = 12, 0
    while position + 8 <= len(data):
        fourcc = data[position:position + 4]
        size = int.from_bytes(data[position + 4:position + 8], "little")
        end = position + 8 + size + (size & 1)
        if end > len(data) or size > len(data):
            raise ImageError("an embedded WebP could not be parsed to establish its frame count")
        if fourcc in (b"ANIM", b"ANMF"):
            frames += 1
        elif fourcc == b"VP8X" and size >= 1:
            if data[position + 8] & 0x02:     # animation flag
                frames += 1
        position = end
    return frames


def frame_count_issue(data: bytes, extension: str) -> str | None:
    """Reason string when the image may hold frames we cannot read, else None.

    Containers that PyMuPDF decodes as a single frame but can legally hold several
    (GIF, APNG, animated WebP) are parsed here from the raw bytes. A single-frame
    file of those formats is accepted; a malformed file whose frame count cannot be
    established is refused. Formats that are single-frame by specification (JPEG,
    BMP, static PNG, WebP without animation chunks) need no further check.
    """
    if extension == "gif":
        frames = _gif_frames(data)
        if frames > 1:
            return ("an embedded GIF is animated (multiple frames); only one frame can be "
                    "read, so it cannot be redacted safely")
        return None
    if extension == "png":
        if b"acTL" in _png_chunk_types(data):
            return ("an embedded PNG is an animated PNG (APNG); only one frame can be read, "
                    "so it cannot be redacted safely")
        return None
    if extension == "webp":
        frames = _webp_frames(data)
        if frames:
            return ("an embedded WebP is animated (multiple frames); only one frame can be "
                    "read, so it cannot be redacted safely")
        return None
    if extension in ("tif", "tiff"):
        try:
            with pymupdf.open(stream=data) as document:
                frames = document.page_count
        except Exception as exc:
            raise ImageError("an embedded TIFF could not be parsed to establish its frame count"
                             ) from exc
        if frames > 1:
            return ("an embedded TIFF has multiple pages; only one page can be read, so it "
                    "cannot be redacted safely")
        return None
    return None


def canonical_image_names(package) -> dict[str, str]:
    """The writer's deterministic source->output image names: sorted parts -> redacted_N.png.

    Both writer and verifier derive the pairing from this rule instead of guessing by
    pixel dimensions; duplicating the small rule keeps the verifier independent.
    """
    taken = set(package.names)
    mapping: dict[str, str] = {}
    counter = 0
    for name in sorted(package.layout.media_parts):
        counter += 1
        candidate = f"word/media/redacted_{counter}.png"
        while candidate in taken:
            counter += 1
            candidate = f"word/media/redacted_{counter}.png"
        taken.add(candidate)
        mapping[name] = candidate
    return mapping


# ---- decoding --------------------------------------------------------------
def _flatten(pix: pymupdf.Pixmap) -> pymupdf.Pixmap:
    """Return an RGB pixmap with transparency flattened onto white."""
    if not pix.alpha:
        if pix.n == 3:
            return pix
        return pymupdf.Pixmap(pymupdf.csRGB, pix)
    samples = pix.samples
    width, height, components = pix.width, pix.height, pix.n - 1
    out = bytearray(width * height * 3)
    for index in range(width * height):
        base = index * pix.n
        alpha = samples[base + components]
        inverse = 255 - alpha
        target = index * 3
        if components == 3:
            out[target] = (samples[base] * alpha + 255 * inverse) // 255
            out[target + 1] = (samples[base + 1] * alpha + 255 * inverse) // 255
            out[target + 2] = (samples[base + 2] * alpha + 255 * inverse) // 255
        else:                                   # grayscale + alpha
            value = (samples[base] * alpha + 255 * inverse) // 255
            out[target] = out[target + 1] = out[target + 2] = value
    return pymupdf.Pixmap(pymupdf.csRGB, width, height, bytes(out), False)


_PNG_ONLY_CRITICAL = (b"IHDR", b"PLTE", b"IDAT", b"IEND")


# ---- needle matching for image text ---------------------------------------
def _norm(text: str) -> str:
    import re
    import unicodedata
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def _compact(text: str) -> str:
    import re
    return re.sub(r"[\W_]+", "", text)


def image_text_has_needle(haystack: str, needle: str) -> bool:
    """True when a redacted image passage is still readable in an image's OCR text.

    Same rule the PDF raster check uses: a normalised phrase match, or (for eight
    or more compact characters) a whitespace-insensitive match so a reader that
    splits a word differently is still caught.
    """
    hay = _norm(haystack)
    wanted = _norm(needle)
    if not wanted:
        return False
    if f" {wanted} " in f" {hay} ":
        return True
    compact_wanted = _compact(wanted)
    return len(compact_wanted) >= 8 and compact_wanted in _compact(hay)


def _strip_png_metadata(png: bytes) -> bytes:
    """Drop every non-critical PNG chunk (pHYs DPI, tEXt, iTXt, eXIf, XMP, ...).

    The redacted image is re-created from raw samples; this is the belt-and-braces
    step that also removes a DPI chunk the encoder may add. Critical image data is
    kept byte-for-byte, so the pixels are unchanged.
    """
    signature = b"\x89PNG\r\n\x1a\n"
    if not png.startswith(signature):
        raise ImageError("the redacted image could not be re-encoded as PNG")
    out = bytearray(signature)
    position = 8
    while position + 8 <= len(png):
        length = int.from_bytes(png[position:position + 4], "big")
        chunk_type = png[position + 4:position + 8]
        end = position + 8 + length + 4
        if end > len(png) or length < 0:
            raise ImageError("the redacted PNG is malformed")
        if chunk_type in _PNG_ONLY_CRITICAL:
            out += png[position:end]
        position = end
        if chunk_type == b"IEND":
            break
    if not out.endswith(b"IEND\xaeB`\x82"):
        raise ImageError("the redacted PNG has no end chunk")
    return bytes(out)


def decode_image(data: bytes) -> pymupdf.Pixmap:
    """Decode image bytes to a native-size RGB pixmap (alpha flattened to white)."""
    if not data:
        raise ImageError("an embedded image is empty")
    try:
        pix = pymupdf.Pixmap(data)
    except Exception as exc:                  # pymupdf raises several types for bad data
        raise ImageError("an embedded image could not be decoded") from exc
    if pix.width < 1 or pix.height < 1:
        raise ImageError("an embedded image has no pixels")
    if pix.width * pix.height > MAX_IMAGE_PIXELS:
        raise ImageError("an embedded image is too large to process safely")
    return _flatten(pix)


def scan_images(package) -> tuple[list[ImagePart], list[str]]:
    """Decode every image part; return (good parts, error messages).

    Errors are counts/reasons only -- never a file name, which can carry text.
    """
    parts: list[ImagePart] = []
    errors: list[str] = []
    for name in package.layout.media_parts:
        extension = extension_of(name)
        if extension not in DECODABLE_EXTENSIONS:
            errors.append(f"an embedded image has an unsupported format ({extension or 'none'})")
            continue
        data = package.parts[name]
        if len(data) > MAX_IMAGE_BYTES:
            errors.append("an embedded image is larger than the safe size limit")
            continue
        try:
            issue = frame_count_issue(data, extension)
        except ImageError as exc:
            errors.append(str(exc))
            continue
        if issue:
            errors.append(issue)
            continue
        try:
            pix = decode_image(data)
        except ImageError as exc:
            errors.append(str(exc))
            continue
        parts.append(ImagePart(name, extension, pix.width, pix.height, data))
        pix = None
    return parts, errors


# ---- OCR -------------------------------------------------------------------
def engine_summary(config: EngineConfig) -> tuple[str | None, str | None, bool, str | None]:
    """(name, version, available, error) for one engine configuration."""
    try:
        engine = config.engine()
    except EngineConfigError as exc:
        return None, None, False, str(exc)
    try:
        available = engine.available()
    except Exception as exc:                  # a broken wrapper is "unavailable"
        return getattr(engine, "name", None), None, False, type(exc).__name__
    return getattr(engine, "name", config.spec), engine.version(), bool(available), None


def _page_from_result(page: PageText, result) -> PageText:
    parts: list[str] = []
    words: list[Word] = []
    offset = 0
    width, height = page.width, page.height
    for index, word in enumerate(result.words):
        if index:
            parts.append(" ")
            offset += 1
        start = offset
        parts.append(word.text)
        offset += len(word.text)
        x0, y0, x1, y1 = word.box
        rect = Rect(x0 * width, y0 * height, x1 * width, y1 * height)
        words.append(Word(word.text, rect, index, start, offset,
                          confidence=float(word.confidence)))
    return PageText(page.page, page.width, page.height, "".join(parts), words, ocr=True)


def read_images(package, config: EngineConfig, page_offset: int) -> tuple[list[ImageReading], list[str]]:
    """Read every image with the configured engine; return (readings, errors)."""
    parts, errors = scan_images(package)
    if errors:
        return [], errors
    name, _version, available, error = engine_summary(config)
    if not available:
        return [], [error or f"the OCR engine {name or config.spec} is unavailable"]
    try:
        engine: OcrEngine = config.engine()
    except EngineConfigError as exc:
        return [], [str(exc)]
    readings: list[ImageReading] = []
    for index, part in enumerate(parts):
        try:
            pix = decode_image(part.data)
        except ImageError as exc:
            errors.append(str(exc))
            continue
        png = pix.tobytes("png")
        width_pt, height_pt = part.page_points()
        try:
            result = engine.analyze(png, width_pt, height_pt, IMAGE_DPI)
        except (OcrError, OcrUnavailable) as exc:
            errors.append(f"the OCR engine failed on an embedded image ({type(exc).__name__})")
            continue
        page = PageText(page_offset + index, float(part.width), float(part.height), "", [], ocr=True)
        readings.append(ImageReading(part, _page_from_result(page, result),
                                     result.rotation))
    return readings, errors


# ---- unread / untrustworthy engine output ---------------------------------
def ink_contrast(pix: pymupdf.Pixmap, rect) -> float:
    """Share of sampled pixels that contrast with the box's median luminance.

    Contrast/variance based, not absolute darkness: valid low-contrast glyphs
    (grey on white) count as ink even when no pixel is "dark". A pure uniform
    box (blank paper, a blank area an engine hallucinated words over) has a
    range/median share of zero and is treated as blank. This is pixel evidence,
    independent of the OCR engine, so a dishonest or weak engine cannot invent
    words.
    """
    left, top, right, bottom = clamp_rect(rect, pix.width, pix.height)
    samples, n, width = pix.samples, pix.n, pix.width
    step_x = max(1, (right - left) // 30)
    step_y = max(1, (bottom - top) // 15)
    values: list[int] = []
    for y in range(top, bottom, step_y):
        row = y * width * n
        for x in range(left, right, step_x):
            base = row + x * n
            values.append(samples[base] if n < 3
                          else (samples[base] + samples[base + 1] + samples[base + 2]) // 3)
    if not values:
        return 0.0
    ordered = sorted(values)
    median = ordered[len(ordered) // 2]
    if ordered[-1] - ordered[0] <= _INK_EPSILON:
        return 0.0                            # uniform box: no contrast at all
    differing = sum(1 for value in values if abs(value - median) > _INK_EPSILON)
    return differing / len(values)


def tiny_word_share(reading) -> float:
    """Share of OCR word boxes whose native pixel height is below TINY_WORD_HEIGHT_PX."""
    heights = [word.rect.y1 - word.rect.y0 for word in reading.page.words]
    if not heights:
        return 0.0
    tiny = sum(1 for height in heights if height < TINY_WORD_HEIGHT_PX)
    return tiny / len(heights)


def tiny_text(reading) -> bool:
    """The PF133 tiny-text condition, shared by pre-flight and the export disclosure.

    One constant, one helper: the finding that requires an acknowledgement and the
    V112 WARN in the export report must describe exactly the same images.
    """
    return tiny_word_share(reading) >= TINY_WORD_SHARE


def untrusted_word_boxes(readings) -> str | None:
    """Reason string when OCR word boxes are not supported by the source pixels."""
    total = 0
    blank = 0
    for reading in readings:
        try:
            pix = decode_image(reading.part.data)
        except ImageError:
            continue
        for word in reading.page.words:
            total += 1
            if ink_contrast(pix, word.rect) < _INK_MIN_SHARE:
                blank += 1
    if total and blank and blank >= max(1, int(total * _MAX_BLANK_WORD_SHARE)):
        return (f"OCR engine output does not match the image pixels: {blank} of {total} "
                "word box(es) sit on uniform/blank areas, so the engine cannot be trusted "
                "for this image")
    return None


# ---- pixel redaction -------------------------------------------------------
def clamp_rect(rect, width: int, height: int) -> tuple[int, int, int, int]:  # noqa: D401
    """Round a pixel rectangle outwards and clamp it to the image."""
    if hasattr(rect, "x0"):                     # models.Rect (pixel coordinates)
        x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    else:
        x0, y0, x1, y1 = (float(v) for v in rect)
    if not (math.isfinite(x0) and math.isfinite(y0) and math.isfinite(x1) and math.isfinite(y1)):
        raise ImageError("an accepted image span has non-finite pixel coordinates")
    if x1 <= x0 or y1 <= y0:
        raise ImageError("an accepted image span covers no pixels")
    left = max(0, math.floor(x0) - BURN_PAD_PX)
    top = max(0, math.floor(y0) - BURN_PAD_PX)
    right = min(width, math.ceil(x1) + BURN_PAD_PX)
    bottom = min(height, math.ceil(y1) + BURN_PAD_PX)
    if right <= left or bottom <= top:
        raise ImageError("an accepted image span falls outside the image")
    return left, top, right, bottom


def reencode_image(data: bytes, rects=()) -> bytes:
    """Decode an image, optionally burn rectangles, and return a metadata-free PNG.

    Used for EVERY image in an export (not only redacted ones): the pixels are
    preserved (unchanged images decode to the same RGB samples), the pixel
    dimensions stay the same, transparency is flattened to white and every
    non-critical PNG chunk (EXIF/XMP/ICC/DPI/text) is dropped.
    """
    pix = decode_image(data)
    for rect in rects:
        left, top, right, bottom = clamp_rect(rect, pix.width, pix.height)
        pix.set_rect(pymupdf.IRect(left, top, right, bottom), (0, 0, 0))
    return _strip_png_metadata(pix.tobytes("png"))


def burn_image(data: bytes, rects) -> bytes:
    """Backwards-compatible name for re-encoding with burn rectangles."""
    return reencode_image(data, rects)


def bright_fraction(pix: pymupdf.Pixmap, rect, *, above: int = _BRIGHT) -> float:
    """Share of sampled pixels brighter than `above` inside a pixel rectangle."""
    left, top, right, bottom = clamp_rect(rect, pix.width, pix.height)
    samples, n, width = pix.samples, pix.n, pix.width
    step_x = max(1, (right - left) // 40)
    step_y = max(1, (bottom - top) // 20)
    bright = total = 0
    for y in range(top, bottom, step_y):
        row = y * width * n
        for x in range(left, right, step_x):
            base = row + x * n
            value = samples[base] if n < 3 else (samples[base] + samples[base + 1]
                                                + samples[base + 2]) / 3
            bright += value > above
            total += 1
    return bright / total if total else 1.0


# ---- engine choice for the independent output check ------------------------
def verification_reader(config: EngineConfig) -> tuple[OcrEngine | None, bool, str]:
    """The reader used to re-OCR redacted output images: (engine, independent, label).

    Mirrors the PDF raster-page check: Tesseract is preferred whenever it exists
    (independent of a custom detection engine); only without Tesseract does the
    configured engine check its own output, labelled as a same-engine check.
    """
    if ocr_available():
        return TesseractEngine(), True, "Tesseract (independent reader)"
    if config.spec != "tesseract":
        try:
            candidate = config.engine()
        except EngineConfigError:
            return None, False, "configured engine (invalid)"
        if candidate.available():
            return candidate, False, f"configured engine {candidate.name} (SAME-ENGINE, weaker)"
    return None, False, "no OCR engine available"
