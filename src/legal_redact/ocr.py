"""Stage 1b: OCR for pages whose text lives in pixels.

What this module does: render ONE page to PNG at a high dpi, run the local
Tesseract executable on it, parse its TSV word table and rebuild the SAME model
extract.py produces for a real text layer -- page text, one entry per word with
character offsets and a rectangle in PDF points. Detectors, the policy, the
review gate and the writer's box geometry therefore work unchanged: nothing
downstream needs to know the page was a picture.

Tuning decisions are measured, not assumed (fixtures + table in
docs/OCR_EVALUATION.md):

- OCR renders GRAYSCALE, not RGB: tesseract converts to grey anyway, so the
  result is identical while the page image costs a third of the memory and the
  pass is ~30% faster.
- No contrast stretching: a mild 1.3x stretch made a hard-blur page WORSE
  (8/9 -> 7/9 planted values) and never helped elsewhere at this Tesseract.
- No adaptive second pass (higher dpi / alternate --psm): it changed no
  planted-value recall on clean or degraded scans, 400 dpi was slightly worse,
  and mean word confidence is not a reliable failure signal (an 82-confidence
  read still missed 3 of 9 planted values). A failed read is not re-read; the
  human review gate is the control.
- Orientation: a page rotated 90/180 degrees reads as garbage (mean confidence
  ~30 vs ~95 upright). When the first read looks unreliable, Tesseract OSD
  detects the rotation; the page is re-rendered upright and every word box is
  mapped back into the ORIGINAL page coordinates, so redaction geometry stays
  correct. The 90/180 cases went 0/9 -> 9/9 planted values in evaluation.
  Pre-flight runs the same OSD check on every page that will be OCR'd and turns
  a rotated page into a REVIEW finding (PF017), and an undeterminable
  orientation into another one (PF018): a page is never silently mis-read.

What it does NOT do, and the honest limits that follow:

- English only (`-l eng`). Other scripts are out of scope for this MVP.
- OCR is a guess. A character it misreads is a character the lawyer cannot see
  was there, and therefore cannot redact. Pre-flight says so (PF015) and the
  review page shows the original pixels next to the suggestion, so the human
  check -- not the recognition -- is the control.
- Low-confidence words are KEPT, not dropped. Dropping them would hide exactly
  the text that most needs a human look; downstream they are ordinary words.
- Handwriting, signatures, stamps and photographs are not read at all; text
  over busy backgrounds, very low resolution and heavy skew degrade recall.
- Tesseract is an external local program (`tesseract`, or $TESSERACT_CMD, or
  the usual install paths). This module adds no Python dependency, makes no
  network call and sends nothing to a cloud service.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import pymupdf

from .models import PageText, Rect, Word

# 300 dpi is the usual document-scan sweet spot: at 8-11 pt body text the glyphs
# are ~30-45 px tall, enough for Tesseract. Lower dpi is faster and smaller;
# higher dpi adds nothing Tesseract can use for body text (and 400 dpi was
# measurably slightly worse in evaluation).
DEFAULT_DPI = 300

# A page with fewer than this many non-whitespace extractable characters is
# treated as an image page ("no or almost no text layer"). A pure page number or
# a one-word stamp must not switch a scan out of OCR: those pixels are the
# content. The threshold is shared with preflight so the two stages agree.
MIN_TEXT_CHARS = 10

# Grayscale OCR rendering (see module docstring: identical text, ~1/3 memory).
OCR_GRAYSCALE = True

# Adaptive second pass: evaluated and NOT adopted. Kept as a constant so the
# decision is visible in code: a read is not retried, however low its confidence.
ADAPTIVE_RETRY = False

# OSD orientation detection. 200 dpi is the lowest render where the OSD
# orientation confidence is reliable on these fixtures (at 150 dpi rotated pages
# scored 0.5-1.4 and upright pages 9.5, i.e. no usable separation; at 200 dpi
# genuine rotations score 8-15 and failures <0.2).
OSD_DPI = 200
OSD_MIN_CONFIDENCE = 2.0

# A first OCR read is "unreliable" -- and only then is OSD run -- when it finds
# almost nothing or its mean word confidence is this low. Measured: rotated pages
# land at 29-33, upright pages at 82-96; nothing upright in the fixture set fell
# below 82.
MIN_MEAN_CONFIDENCE = 65.0
MIN_WORDS = 5
PSM = 3

# Environment override, mainly for tests and unusual installs.
TESSERACT_ENV = "TESSERACT_CMD"

_WINDOWS_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]
_POSIX_CANDIDATES = [
    "/usr/bin/tesseract", "/usr/local/bin/tesseract", "/opt/homebrew/bin/tesseract",
    "/opt/local/bin/tesseract",
]


class OcrUnavailable(RuntimeError):
    """No usable Tesseract (executable + eng language data): OCR cannot be offered."""


class OcrError(RuntimeError):
    """Tesseract exists but this page could not be read. Never treated as 'no text'."""


def _find_tesseract() -> str | None:
    candidates: list[str] = []
    env = os.environ.get(TESSERACT_ENV)
    if env:
        candidates.append(env)
    which = shutil.which("tesseract")
    if which:
        candidates.append(which)
    candidates += _WINDOWS_CANDIDATES if os.name == "nt" else _POSIX_CANDIDATES
    for cand in candidates:
        try:
            if cand and Path(cand).is_file():
                return str(Path(cand))
        except OSError:                       # unreadable path: try the next one
            continue
    return None


@lru_cache(maxsize=1)
def _engine() -> tuple[str | None, tuple[str, ...]]:
    """(executable path or None, installed language codes).

    A Tesseract without the `eng` traineddata cannot do the one language this
    MVP supports, so it counts as unavailable: pre-flight then refuses scans
    cleanly instead of failing halfway through review.
    """
    exe = _find_tesseract()
    if exe is None:
        return None, ()
    try:
        proc = subprocess.run([exe, "--list-langs"], capture_output=True, timeout=20,
                              **_hidden_window())
        lines = (proc.stdout or b"").decode("utf-8", errors="replace").splitlines()
    except (OSError, subprocess.SubprocessError):
        return exe, ()
    langs = tuple(line.strip() for line in lines[1:] if line.strip())
    return exe, langs


@lru_cache(maxsize=1)
def tesseract_path() -> str | None:
    """Absolute path of the Tesseract executable, or None. Cached for the run."""
    return _engine()[0]


def ocr_languages() -> tuple[str, ...]:
    """Language codes the local engine can read (empty when it is unusable)."""
    return _engine()[1]


def ocr_available() -> bool:
    """True when a local Tesseract can read English (`eng` traineddata present).

    Pre-flight calls this indirectly; callers must re-check it rather than trust
    a cached flag, because the user can run a different machine or environment.
    """
    exe, langs = _engine()
    return exe is not None and "eng" in langs


@lru_cache(maxsize=1)
def ocr_version() -> str | None:
    """Short engine description for `inspect` (e.g. 'tesseract v5.5.3'), or None."""
    exe = tesseract_path()
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, timeout=20,
                              **_hidden_window())
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        return None
    first = out.splitlines()[0] if out else ""
    m = re.search(r"tesseract\s+v?([0-9][\w.\-]*)", first, re.IGNORECASE)
    return f"tesseract {m.group(1)}" if m else (first[:40] or None)


def needs_ocr(page: pymupdf.Page) -> bool:
    """True when the page has no usable text layer and its content is pixels.

    Only pages that fail this check are ever OCR'd: a page that already carries
    a text layer is never re-read by OCR, so its real text and coordinates stay
    authoritative. A page whose text layer cannot even be read is treated as an
    image page (fail towards OCR, never towards silently skipping content).
    """
    try:
        return len(page.get_text("text").strip()) < MIN_TEXT_CHARS
    except Exception:                                    # unreadable layer: assume pixels
        return True


def _hidden_window() -> dict:
    """Keep a console window from flashing on Windows when spawning Tesseract."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return {"creationflags": flags} if flags else {}


def _tesseract_stdout(exe: str, png: Path, *args: str, strict: bool = True) -> str:
    cmd = [exe, str(png), "stdout", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180, **_hidden_window())
    except (OSError, subprocess.SubprocessError) as exc:
        raise OcrError(f"could not run Tesseract ({type(exc).__name__})") from exc
    if strict and proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise OcrError(f"Tesseract exited {proc.returncode}: {err[:160]}")
    return (proc.stdout or b"").decode("utf-8", errors="replace")


def _tsv_rows(tsv: str) -> list[tuple[int, int, int, int, int, int, int, int, str, float]]:
    """(block, par, line, word_no, left, top, width, height, text, confidence)."""
    rows = []
    for line in tsv.splitlines():
        parts = line.split("\t")
        if len(parts) < 12 or parts[0] != "5":          # level 5 == word
            continue
        text = "\t".join(parts[11:]).strip()
        if not text:
            continue
        try:
            block, par, ln, word_no = (int(parts[2]), int(parts[3]), int(parts[4]),
                                       int(parts[5]))
            left, top, width, height = (int(parts[6]), int(parts[7]),
                                        int(parts[8]), int(parts[9]))
            conf = float(parts[10])
        except ValueError:                              # malformed row: skip, never guess
            continue
        rows.append((block, par, ln, word_no, left, top, width, height, text, conf))
    # Tesseract emits TSV in reading order already; sorting by the stable ids is
    # a belt-and-braces step that cannot invent an order.
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    return rows


def _render(page: pymupdf.Page, dpi: int) -> pymupdf.Pixmap:
    colorspace = pymupdf.csGRAY if OCR_GRAYSCALE else pymupdf.csRGB
    return page.get_pixmap(dpi=dpi, colorspace=colorspace, alpha=False)


def _render_rotated(page: pymupdf.Page, dpi: int, angle: int) -> pymupdf.Pixmap:
    """Render the page rotated clockwise by `angle` (90/180/270), for correction."""
    pix = _render(page, dpi)
    scratch = pymupdf.open()
    try:
        page_copy = scratch.new_page(width=page.rect.width, height=page.rect.height)
        page_copy.insert_image(page_copy.rect, pixmap=pix)
        page_copy.set_rotation(angle)
        return _render(page_copy, dpi)
    finally:
        scratch.close()


def _recognise(pix: pymupdf.Pixmap, psm: int = PSM) -> tuple[list, float]:
    """Run Tesseract on a pixmap; returns TSV word rows (pixels) and mean confidence."""
    exe = tesseract_path()
    if exe is None:
        raise OcrUnavailable("Tesseract is not installed or not on PATH; set TESSERACT_CMD")
    with tempfile.TemporaryDirectory(prefix="legal-redact-ocr-") as tmp:
        png = Path(tmp) / "page.png"
        pix.save(png)
        tsv = _tesseract_stdout(exe, png, "-l", "eng", "--psm", str(psm), "tsv")
    if not tsv.startswith("level"):
        raise OcrError("Tesseract returned no TSV word table")
    rows = _tsv_rows(tsv)
    mean_conf = sum(r[9] for r in rows) / len(rows) if rows else 0.0
    return rows, mean_conf


def ocr_orientation(page: pymupdf.Page, dpi: int = OSD_DPI) -> tuple[int, float]:
    """Tesseract OSD: (degrees to rotate clockwise, orientation confidence).

    Returns (0, 0.0) when OSD cannot decide (missing osd data, too little text, a
    blank page). Callers must treat that as "orientation unknown", not as
    "upright": preflight turns it into a REVIEW finding.
    """
    if not ocr_available():
        return 0, 0.0
    exe = tesseract_path()
    try:
        pix = _render(page, max(72, int(dpi)))
        with tempfile.TemporaryDirectory(prefix="legal-redact-osd-") as tmp:
            png = Path(tmp) / "page.png"
            pix.save(png)
            out = _tesseract_stdout(exe, png, "-l", "osd", "--psm", "0", strict=False)
    except (OcrError, OSError):
        return 0, 0.0
    info = {}
    for line in out.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            info[key.strip()] = value.strip()
    try:
        angle = int(info.get("Rotate", "0") or 0)
    except ValueError:
        angle = 0
    try:
        confidence = float(info.get("Orientation confidence", "0") or 0.0)
    except ValueError:
        confidence = 0.0
    return (angle if angle in (90, 180, 270) else 0), confidence


def _rows_to_words(rows, page: pymupdf.Page, pix: pymupdf.Pixmap,
                   rotation: int = 0) -> tuple[list[Word], str]:
    """TSV rows in image pixels -> words with PDF-point rects and text offsets.

    For a rotated read, the corrected image's boxes are mapped back into the
    ORIGINAL page coordinate system (exact 90-degree rotations, no resampling):
    the writer later paints over those rectangles on the original page.
    """
    width, height = page.rect.width, page.rect.height
    if rotation in (90, 270):
        corrected_w, corrected_h = height, width
    else:
        corrected_w, corrected_h = width, height
    sx = corrected_w / pix.width if pix.width else 1.0
    sy = corrected_h / pix.height if pix.height else 1.0
    parts: list[str] = []
    words: list[Word] = []
    offset = 0
    line_ids: dict[tuple[int, int, int], int] = {}
    prev_key: tuple[int, int, int] | None = None
    for block, par, ln, _word_no, left, top, box_w, box_h, text, _conf in rows:
        key = (block, par, ln)
        lid = line_ids.setdefault(key, len(line_ids))
        if prev_key is not None:
            sep = " " if key == prev_key else "\n"
            parts.append(sep)
            offset += 1
        start = offset
        parts.append(text)
        offset += len(text)
        u0, v0 = left * sx, top * sy
        u1, v1 = (left + box_w) * sx, (top + box_h) * sy
        if rotation == 90:
            x0, y0, x1, y1 = v0, height - u1, v1, height - u0
        elif rotation == 180:
            x0, y0, x1, y1 = width - u1, height - v1, width - u0, height - v0
        elif rotation == 270:
            x0, y0, x1, y1 = width - v1, u0, width - v0, u1
        else:
            x0, y0, x1, y1 = u0, v0, u1, v1
        rect = Rect(max(0.0, min(x0, x1)), max(0.0, min(y0, y1)),
                    min(width, max(x0, x1)), min(height, max(y0, y1)))
        words.append(Word(text, rect, lid, start, offset))
        prev_key = key
    return words, "".join(parts)


def _candidate_score(mean_confidence: float, word_count: int) -> float:
    """Compare two reads of the same page: confidence, strongly nudged by coverage."""
    return mean_confidence + 15.0 * math.log10(word_count + 1)


def ocr_page(page: pymupdf.Page, dpi: int = DEFAULT_DPI) -> PageText:
    """OCR one page into the same PageText model the text extractor produces.

    Rectangles are in PDF points in the page's own coordinate system (PyMuPDF
    convention, top-left origin), obtained by scaling the image pixel boxes by
    the actual rendered size -- not by assuming dpi, so rounded page dimensions
    cannot drift coordinates. If the first read is unreliable, Tesseract OSD
    decides whether the page is rotated; the rotated read is mapped back and
    only replaces the first one when it is genuinely better.
    """
    if not ocr_available():
        raise OcrUnavailable(
            "Tesseract with the 'eng' language data was not found; set TESSERACT_CMD "
            "or install it")
    dpi = max(72, min(1200, int(dpi)))
    pix = _render(page, dpi)
    rows, confidence = _recognise(pix)
    best = {"rows": rows, "conf": confidence, "pix": pix, "rotation": 0}
    best_score = _candidate_score(confidence, len(rows))
    if len(rows) < MIN_WORDS or confidence < MIN_MEAN_CONFIDENCE:
        angle, osd_confidence = ocr_orientation(page)
        if angle and osd_confidence >= OSD_MIN_CONFIDENCE:
            rotated_pix = _render_rotated(page, dpi, angle)
            rotated_rows, rotated_conf = _recognise(rotated_pix)
            score = _candidate_score(rotated_conf, len(rotated_rows))
            if score > best_score:
                best = {"rows": rotated_rows, "conf": rotated_conf,
                        "pix": rotated_pix, "rotation": angle}
    words, text = _rows_to_words(best["rows"], page, best["pix"], best["rotation"])
    return PageText(page.number, page.rect.width, page.rect.height, text, words, ocr=True)
