"""Stage 5: independent checks of the exported file.

Independence is the point. The writer is PyMuPDF; everything here uses other
libraries: pdfminer.six and pypdfium2 to read text, pikepdf to read PDF structure.
A bug in the writer's idea of "removed" cannot hide from a reader that does not
share its code.

Input is the source file, the output file, the boxes that were meant to be
redacted, and the text under those boxes in the SOURCE (the "needles"). Needles
live only in memory: reports carry check ids, categories, page numbers and counts,
never document text.

What a PASS means, and does not mean: the supported leak routes listed in LIMITS
were checked and none was found. It does NOT prove the document is safe. Text
turned into pixels or vector outlines, text under images, unusual font encodings
and indirect identification are outside every check here. See LIMITS.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

PASS, FAIL, WARN, NOT_CHECKED = "PASS", "FAIL", "WARN", "NOT_CHECKED"

LIMITS = [
    "Text rendered as images or vector outlines on a page that HAS a text layer cannot be read by any extractor used here.",
    "Images on text pages are not inspected (no OCR of pictures inside a text page); any image is reported for manual review.",
    "Scanned pages are read by local English OCR: a character it misreads is invisible to both detection and this export check.",
    "A negative text search proves absence only for the extractors and encodings tried.",
    "Literal byte search cannot see text inside font-encoded page content; extraction checks cover that.",
    "Indirect identification (job title, rare facts, context) is a human judgment, not checked.",
    "Digital signatures are invalidated by any redaction; not assessed.",
]


@dataclass(frozen=True)
class Check:
    id: str
    name: str
    status: str
    detail: str            # never contains document text


@dataclass
class VerifyReport:
    checks: list[Check] = field(default_factory=list)
    limits: list[str] = field(default_factory=lambda: list(LIMITS))

    @property
    def passed(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    def to_dict(self) -> dict:
        return {"passed": self.passed,
                "checks": [c.__dict__ for c in self.checks], "limits": self.limits}

    def format(self) -> str:
        lines = [f"{c.status:11} {c.id} {c.name}: {c.detail}" for c in self.checks]
        lines.append("RESULT: " + ("no leak found in the supported checks" if self.passed
                                   else "LEAK DETECTED - do not release this file"))
        lines += ["", "Not covered (these need your judgment):"] + [f"  - {x}" for x in self.limits]
        return "\n".join(lines)


# ---- normalisation ---------------------------------------------------------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def _has(hay_norm: str, needle_norm: str) -> bool:
    return bool(needle_norm) and f" {needle_norm} " in f" {hay_norm} "


def _compact(s: str) -> str:
    return s.replace(" ", "")


# ---- needles ---------------------------------------------------------------
def needles_from_source(src: str | Path, boxes, categories: list[str]) -> list[tuple[str, str]]:
    """Text under each box in the SOURCE: words whose centre lies inside the box."""
    doc = pymupdf.open(src)
    out = []
    for (pno, r), cat in zip(boxes, categories):
        words = [w for w in doc[pno].get_text("words", sort=True)
                 if r[0] <= (w[0] + w[2]) / 2 <= r[2] and r[1] <= (w[1] + w[3]) / 2 <= r[3]]
        txt = " ".join(w[4] for w in words)
        if txt.strip():
            out.append((cat, txt))
    doc.close()
    return out


def needles_from_groups(src: str | Path, groups, pages=None,
                        raster_pages=frozenset()) -> list[tuple[str, str]]:
    """One needle per redacted passage, however many lines it spans.

    `groups` is [(category, [(page, rect), ...]), ...]: the boxes of one decision together.
    Judging each line of a wrapped name separately turns a surname into a stand-alone needle
    that then matches the same word in kept text (a firm name, say) and refuses a correct export.

    For boxes on TEXT pages the words under the box are read from the source PDF as before.
    For boxes on RASTER (scanned) pages there are no source words to read, so the OCR page text
    from stage 1b (`pages`, when given) supplies them: that is the text the user chose to redact.
    """
    by_num = {p.page: p for p in (pages or [])}
    text_pages = any(pno not in raster_pages for _, boxes in groups for pno, _ in boxes)
    doc = pymupdf.open(src) if text_pages else None
    try:
        out = []
        for cat, boxes in groups:
            words: list[str] = []
            for pno, r in boxes:
                if pno in raster_pages:
                    page_text = by_num.get(pno)
                    if page_text is not None:
                        words += [w.text for w in page_text.words
                                  if r[0] <= (w.rect.x0 + w.rect.x1) / 2 <= r[2]
                                  and r[1] <= (w.rect.y0 + w.rect.y1) / 2 <= r[3]]
                elif doc is not None:
                    words += [w[4] for w in doc[pno].get_text("words", sort=True)
                              if r[0] <= (w[0] + w[2]) / 2 <= r[2] and r[1] <= (w[1] + w[3]) / 2 <= r[3]]
            txt = " ".join(words)
            if txt.strip():
                out.append((cat, txt))
        return out
    finally:
        if doc is not None:
            doc.close()


# ---- readers (independent of the writer) -----------------------------------
def _pdfminer_pages(path) -> list[str]:
    from pdfminer.high_level import extract_text
    n = len(pymupdf.open(path))
    return [extract_text(str(path), page_numbers=[i]) for i in range(n)]


def _pdfium_pages(path) -> list[str]:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    out = [pdf[i].get_textpage().get_text_range() for i in range(len(pdf))]
    pdf.close()
    return out


def _leaks(page_texts: list[str], needles) -> tuple[Counter, Counter]:
    full, token = Counter(), Counter()
    hay = [norm(t) for t in page_texts]
    whole = " ".join(hay)
    for cat, text in needles:
        n = norm(text)
        if not n:
            continue
        if _has(whole, n) or (len(_compact(n)) >= 8 and _compact(n) in _compact(whole)):
            full[cat] += 1
        elif cat == "PERSON":
            if any(len(t) >= 3 and _has(whole, t) for t in n.split()):
                token[cat] += 1
    return full, token


# ---- the checks ------------------------------------------------------------
def verify_export(src, out, boxes, needles, *, raster_pages=frozenset(),
                  raster_needles=(), source_pages=None) -> VerifyReport:
    """Check the exported file. `raster_pages` are pages rebuilt as images (scans).

    `raster_needles` is the subset of needles whose boxes live on raster pages:
    those pages have no text layer, so the leak check for them is OCR of the
    OUTPUT image. `source_pages` (the stage-1b extraction of the source) is
    optional and only used to warn about content lost by rasterisation.
    """
    rep = VerifyReport()
    add = lambda i, n, s, d: rep.checks.append(Check(i, n, s, d))  # noqa: E731

    # V01/V02 text extraction by two independent readers
    for cid, name, reader in (("V01", "text search (pdfminer.six)", _pdfminer_pages),
                              ("V02", "text search (pypdfium2)", _pdfium_pages)):
        try:
            full, token = _leaks(reader(out), needles)
        except Exception as exc:                       # an unreadable output is not a pass
            add(cid, name, FAIL, f"could not read output ({type(exc).__name__})")
            continue
        if full:
            add(cid, name, FAIL, "redacted text still extractable: " + _fmt(full))
        elif token:
            add(cid, name, WARN, "name fragment found in extracted text (may be coincidence): " + _fmt(token))
        else:
            add(cid, name, PASS, f"none of {len(needles)} redacted passages found")

    _geometry(rep, out, boxes, needles)
    _structure(rep, out, raster_pages)
    _raw_scan(rep, out, needles)
    _revisions(rep, out)
    _collateral(rep, src, out, needles)
    _visual(rep, out, boxes)
    _integrity(rep, src, out)
    _layout_shift(rep, src, out, boxes)
    _raster(rep, out, boxes, raster_pages, raster_needles, source_pages)
    return rep


def _layout_shift(rep, src, out, boxes):
    """Kept words that moved: the writer (MuPDF) can pull text after a removed word leftwards."""
    import difflib
    a, b = pymupdf.open(src), pymupdf.open(out)
    moved = total = 0
    for pno in range(min(a.page_count, b.page_count)):
        boxes_p = [r for p, r in boxes if p == pno]
        kept = [w for w in a[pno].get_text("words", sort=True)
                if not any(r[0] <= (w[0] + w[2]) / 2 <= r[2] and r[1] <= (w[1] + w[3]) / 2 <= r[3]
                           for r in boxes_p)]
        after = b[pno].get_text("words", sort=True)
        sm = difflib.SequenceMatcher(None, [w[4] for w in kept], [w[4] for w in after], autojunk=False)
        for blk in sm.get_matching_blocks():
            for k in range(blk.size):
                w0, w1 = kept[blk.a + k], after[blk.b + k]
                total += 1
                tol = 0.4 * (w0[3] - w0[1])          # about one space width at this font size
                moved += abs(w0[0] - w1[0]) > tol or abs(w0[1] - w1[1]) > tol
    rep.checks.append(Check("V12", "layout shift of kept text", WARN if moved else PASS,
                            f"{moved} of {total} kept word(s) moved more than a space width; "
                            "check the page visually" if moved
                            else f"{total} kept word(s) within one space width of their place"))


def _fmt(c: Counter) -> str:
    return ", ".join(f"{k}x{v}" for k, v in sorted(c.items()))


def _inner(r, h_shrink=1.0, v_frac=0.25):
    h = r[3] - r[1]
    return (r[0] + h_shrink, r[1] + h * v_frac, r[2] - h_shrink, r[3] - h * v_frac)


def _geometry(rep, out, boxes, needles):
    import pypdfium2 as pdfium
    # A character counts as "inside" a box when its CENTRE is inside. Bounding-box
    # overlap would flag the neighbouring word whose glyph box merely touches the box.
    pdf = pdfium.PdfDocument(str(out))
    leak, stray = Counter(), Counter()
    whole_needles = "".join(norm(t).replace(" ", "") + "|" for _, t in needles)
    pages = {}
    for pno, r in boxes:
        if pno not in pages:
            pg = pdf[pno]
            tp = pg.get_textpage()
            pages[pno] = (pg.get_height(), tp, [tp.get_charbox(i) for i in range(tp.count_chars())])
        H, tp, cb = pages[pno]
        m = min(2.0, (r[2] - r[0]) * 0.25)      # tolerance: neighbours' glyph boxes touch the edge
        inside = "".join(tp.get_text_range(i, 1) for i, (l, b, rr, t) in enumerate(cb)
                         if r[0] + m <= (l + rr) / 2 <= r[2] - m
                         and r[1] <= H - (b + t) / 2 <= r[3])
        inside = norm(inside).replace(" ", "")
        if not inside:
            continue
        # Text in a box is a LEAK if it is made of redacted content. If it is not, it is
        # kept text that the writer shifted into the box (see V12): a layout warning.
        if len(inside) >= 3 and inside in whole_needles:
            leak[pno + 1] += 1
        else:
            stray[pno + 1] += 1
    pdf.close()
    if leak:
        rep.checks.append(Check("V03", "text inside redaction boxes (geometry)", FAIL,
                                f"{sum(leak.values())} box(es) still contain redacted text; pages {sorted(leak)}"))
    elif stray:
        rep.checks.append(Check("V03", "text inside redaction boxes (geometry)", WARN,
                                f"{sum(stray.values())} box(es) contain a few kept characters "
                                "(neighbouring text shifted into the box); check visually"))
    else:
        rep.checks.append(Check("V03", "text inside redaction boxes (geometry)", PASS,
                                f"{len(boxes)} box(es) empty of text"))


def _structure(rep, out, raster_pages=frozenset()):
    import pikepdf
    add = lambda i, n, s, d: rep.checks.append(Check(i, n, s, d))  # noqa: E731
    pdf = pikepdf.open(out)
    root = pdf.Root
    info = {k: str(v) for k, v in dict(pdf.docinfo).items() if str(v).strip()}
    xmp = "/Metadata" in root
    add("V04", "metadata", FAIL if (info or xmp) else PASS,
        (f"{len(info)} info field(s)" if info else "no info fields") + ("; XMP present" if xmp else "; no XMP"))
    problems, warns = [], []
    names = root.get("/Names", {})
    if "/EmbeddedFiles" in names or len(pdf.attachments):
        problems.append("embedded files")
    if "/JavaScript" in names or "/OpenAction" in root or "/AA" in root:
        problems.append("JavaScript/actions")
    if "/AcroForm" in root:
        problems.append("form fields")
    if "/Outlines" in root:
        problems.append("bookmarks")
    if "/OCProperties" in root:
        warns.append("optional-content layers")
    annots = thumbs = 0
    uninspected_images = raster_images = 0
    for pno, p in enumerate(pdf.pages):
        annots += len(p.get("/Annots", []))
        thumbs += "/Thumb" in p
        for _, xo in p.get("/Resources", {}).get("/XObject", {}).items():
            if xo.get("/Subtype") == "/Image":
                if pno in raster_pages:
                    raster_images += 1          # rebuilt pages: the image IS the page, checked by V13-V16
                else:
                    uninspected_images += 1
    if annots:
        problems.append(f"{annots} annotation(s)/link(s)")
    if thumbs:
        problems.append("page thumbnails")
    add("V05", "hidden containers", FAIL if problems else (WARN if warns else PASS),
        "; ".join(problems + warns) if problems or warns else "no attachments, scripts, forms, bookmarks, annotations or thumbnails")
    if raster_images:
        add("V08", "images", PASS if not uninspected_images else WARN,
            f"{raster_images} raster page image(s) checked by the OCR checks"
            + (f"; {uninspected_images} image(s) on text pages are not inspected, review manually"
               if uninspected_images else ""))
    else:
        add("V08", "images", WARN if uninspected_images else PASS,
            f"{uninspected_images} image(s) present; contents not inspected, review manually"
            if uninspected_images else "no images")
    pdf.close()


def _raw_scan(rep, out, needles):
    """Literal search of every object and decoded stream for the needle text."""
    import pikepdf
    pats = set()
    for _, text in needles:
        t = text.strip()
        if len(t) < 4:
            continue
        for s in {t, t.lower(), " ".join(t.split())}:
            for enc in ("utf-8", "utf-16-be", "latin-1"):
                try:
                    pats.add(s.encode(enc))
                except UnicodeEncodeError:
                    pass
    hits = 0
    pdf = pikepdf.open(out)
    for obj in pdf.objects:
        blobs = []
        try:
            blobs.append(obj.unparse())
            if isinstance(obj, pikepdf.Stream):
                blobs.append(obj.read_bytes())
        except Exception:
            continue
        low = [b.lower() for b in blobs]
        if any(p in b for p in pats for b in blobs + low):
            hits += 1
    pdf.close()
    rep.checks.append(Check("V06", "raw object and stream scan", FAIL if hits else PASS,
                            f"redacted text found in {hits} PDF object(s)" if hits
                            else f"redacted text not found in any decoded object ({len(pats)} byte patterns)"))


def _revisions(rep, out):
    data = Path(out).read_bytes()
    eofs, prev = data.count(b"%%EOF"), b"/Prev" in data[-4096:]
    bad = eofs != 1 or prev
    rep.checks.append(Check("V07", "old revisions", FAIL if bad else PASS,
                            f"{eofs} end-of-file markers; previous-xref link {'present' if prev else 'absent'}"
                            if bad else "single revision, no previous-xref link"))


def _collateral(rep, src, out, needles):
    try:
        a = Counter(norm(" ".join(_pdfminer_pages(src))).split())
        b = Counter(norm(" ".join(_pdfminer_pages(out))).split())
    except Exception as exc:
        rep.checks.append(Check("V09", "collateral loss", NOT_CHECKED, type(exc).__name__))
        return
    expected = Counter(w for _, t in needles for w in norm(t).split())
    unexpected = sum(((a - b) - expected).values())
    rep.checks.append(Check("V09", "collateral loss", WARN if unexpected else PASS,
                            f"{unexpected} word(s) outside the redacted passages also disappeared"
                            if unexpected else "no words lost outside the redacted passages"))


def _visual(rep, out, boxes):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(out))
    cache, light = {}, 0
    for pno, r in boxes:
        if pno not in cache:
            cache[pno] = pdf[pno].render(scale=1).to_numpy()
        x0, y0, x1, y1 = (int(round(v)) for v in _inner(r, 0.5, 0.2))
        crop = cache[pno][max(y0, 0):y1, max(x0, 0):x1, :3]
        if crop.size and crop.mean() > 80:
            light += 1
    pdf.close()
    rep.checks.append(Check("V10", "boxes visibly filled", WARN if light else PASS,
                            f"{light} box(es) not visibly black" if light else "all boxes render dark"))


def _integrity(rep, src, out):
    a, b = pymupdf.open(src), pymupdf.open(out)
    same = a.page_count == b.page_count and all(
        abs(a[i].rect.width - b[i].rect.width) < 0.5 and abs(a[i].rect.height - b[i].rect.height) < 0.5
        for i in range(a.page_count))
    rep.checks.append(Check("V11", "page count and size", PASS if same else FAIL,
                            "unchanged" if same else "page count or size differs from the source"))


# ---- raster (OCR'd / scanned) pages ----------------------------------------
def _raster(rep, out, boxes, raster_pages, raster_needles, source_pages):
    if not raster_pages:
        return
    _raster_text_layer(rep, out, raster_pages)
    _raster_image_data(rep, out, boxes, raster_pages)
    _raster_ocr(rep, out, boxes, raster_pages, raster_needles, source_pages)


def _bright_fraction(pix: pymupdf.Pixmap, x0: int, y0: int, x1: int, y1: int,
                     above: int = 100) -> float:
    """Share of sampled pixels brighter than `above` in a pixel rectangle.

    A burnt-in black box is 0 (lossless image, integer pixel bounds). Any part
    of the original scan left uncovered -- a shifted box, a box too small, a
    vector rectangle drawn over the untouched image -- shows paper/background
    pixels above the threshold. Counting bright pixels is far more sensitive to
    a small under-cover than a region mean, which a mostly-black box can dilute.
    """
    n = pix.n
    comps = 1 if n < 3 else 3
    samples = pix.samples
    stride_x = max(1, (x1 - x0) // 40)
    stride_y = max(1, (y1 - y0) // 20)
    bright = count = 0
    for y in range(y0, y1, stride_y):
        row = y * pix.width * n
        for x in range(x0, x1, stride_x):
            base = row + x * n
            if comps == 1:
                v = samples[base]
            else:
                v = (samples[base] + samples[base + 1] + samples[base + 2]) / 3
            bright += v > above
            count += 1
    return bright / count if count else 1.0


def _raster_text_layer(rep, out, raster_pages):
    """V13: a rebuilt page must carry no text layer at all (independent readers)."""
    import pypdfium2 as pdfium
    from pdfminer.high_level import extract_text
    survivors, reader_failures = [], []
    doc = pymupdf.open(out)
    pdf = pdfium.PdfDocument(str(out))
    try:
        for pno in sorted(raster_pages):
            if pno >= doc.page_count or pno >= len(pdf):
                survivors.append(pno + 1)
                continue
            mu = doc[pno].get_text().strip()
            fu = pdf[pno].get_textpage().get_text_range().strip()
            try:
                mi = extract_text(str(out), page_numbers=[pno]).strip()
            except Exception as exc:
                reader_failures.append(f"p{pno + 1}:{type(exc).__name__}")
                mi = ""
            if mu or fu or mi:
                survivors.append(pno + 1)
    finally:
        doc.close()
        pdf.close()
    if reader_failures:
        rep.checks.append(Check("V13", "raster pages: no text layer", FAIL,
                                "could not read output page(s) with pdfminer ("
                                + ", ".join(reader_failures[:3]) + "); an unreadable output is not a pass"))
    elif survivors:
        rep.checks.append(Check("V13", "raster pages: no text layer", FAIL,
                                f"text layer survived on page(s) {survivors}: the original text was not removed"))
    else:
        rep.checks.append(Check("V13", "raster pages: no text layer", PASS,
                                f"none of {len(raster_pages)} rebuilt page(s) has extractable text"))


def _raster_image_data(rep, out, boxes, raster_pages):
    """V14: the boxes must be black IN THE PAGE IMAGE, not just drawn on top of it.

    Extract the image XObject of each rebuilt page and measure the pixels under
    every redaction box. A writer that inserted the original scan and painted
    vector rectangles over it would render black (V10 would pass) while the
    original pixels stayed recoverable by deleting the rectangles; this check
    fails that writer. Exactly one full-page image per rebuilt page is required:
    an original image plus a separate overlay image is not a redaction either.
    """
    doc = pymupdf.open(out)
    problems, light, checked = [], 0, 0
    try:
        for pno in sorted(raster_pages):
            if pno >= doc.page_count:
                problems.append(f"page {pno + 1}: missing from output")
                continue
            page = doc[pno]
            page_boxes = [r for p, r in boxes if p == pno]
            checked += len(page_boxes)
            images = page.get_images(full=True)
            if len(images) != 1:
                problems.append(f"page {pno + 1}: {len(images)} images (expected one rebuilt page image)")
                continue
            xref = images[0][0]
            rects = page.get_image_rects(xref)
            area = page.rect.width * page.rect.height
            covered = sum(max(0.0, min(page.rect.x1, r.x1) - max(page.rect.x0, r.x0))
                          * max(0.0, min(page.rect.y1, r.y1) - max(page.rect.y0, r.y0))
                          for r in rects)
            if not area or covered / area < 0.95:
                problems.append(f"page {pno + 1}: page image covers {covered / area:.0%} of the page")
                continue
            pix = pymupdf.Pixmap(doc, xref)
            sx = pix.width / page.rect.width if page.rect.width else 1.0
            sy = pix.height / page.rect.height if page.rect.height else 1.0
            for r in page_boxes:
                ix0 = max(0, math.floor((r[0] - page.rect.x0) * sx))
                iy0 = max(0, math.floor((r[1] - page.rect.y0) * sy))
                ix1 = min(pix.width, math.ceil((r[2] - page.rect.x0) * sx))
                iy1 = min(pix.height, math.ceil((r[3] - page.rect.y0) * sy))
                if ix1 <= ix0 or iy1 <= iy0:
                    continue
                if _bright_fraction(pix, ix0, iy0, ix1, iy1) > 0.02:
                    light += 1
    finally:
        doc.close()
    if problems:
        rep.checks.append(Check("V14", "raster pages: boxes burnt into image", FAIL,
                                "; ".join(problems[:3])))
    elif light:
        rep.checks.append(Check("V14", "raster pages: boxes burnt into image", FAIL,
                                f"{light} of {checked} box(es) are not dark in the page image: "
                                "the original pixels under them are still recoverable"))
    else:
        rep.checks.append(Check("V14", "raster pages: boxes burnt into image", PASS,
                                f"{checked} box(es) dark in the rebuilt page image"))


def _raster_ocr(rep, out, boxes, raster_pages, raster_needles, source_pages):
    """V15: OCR the OUTPUT image and search for the redacted strings.

    For an image page this is the real leak check: if OCR can still read a
    string the lawyer chose to redact, a human can too. Fragments of a redacted
    person name are FAIL here (not the WARN used for text pages): an image page
    cannot be "mostly redacted", and a half-covered name is a readable half-name.

    V16 is the other half: the content that should have SURVIVED should still be
    readable after rasterisation (a blank or truncated rebuilt page is a data-loss
    bug; it is warned, not failed, because it is not a leak).
    """
    from .ocr import OcrError, OcrUnavailable, ocr_available, ocr_page
    if not ocr_available():
        rep.checks.append(Check("V15", "raster pages: OCR leak search", FAIL,
                                "Tesseract is unavailable, so rebuilt image pages cannot be "
                                "checked; the output is withheld (fail closed)"))
        return
    doc = pymupdf.open(out)
    texts: dict[int, str] = {}
    failures = []
    try:
        for pno in sorted(raster_pages):
            try:
                texts[pno] = ocr_page(doc[pno]).text
            except (OcrError, OcrUnavailable) as exc:
                failures.append(f"p{pno + 1}:{type(exc).__name__}")
    finally:
        doc.close()
    if failures:
        rep.checks.append(Check("V15", "raster pages: OCR leak search", FAIL,
                                "could not OCR " + ", ".join(failures[:3]) + " of the output; "
                                "an unreadable rebuilt page is not a pass"))
        return
    if not raster_needles:
        rep.checks.append(Check("V15", "raster pages: OCR leak search", WARN,
                                "no OCR-derived redacted text on raster pages (manual boxes only); "
                                "their black-out is checked by V14 and the page image needs a human look"))
    else:
        full, token = _leaks(list(texts.values()), raster_needles)
        if full:
            rep.checks.append(Check("V15", "raster pages: OCR leak search", FAIL,
                                    "redacted text is still readable by OCR on a rebuilt image page: "
                                    + _fmt(full)))
        elif token:
            rep.checks.append(Check("V15", "raster pages: OCR leak search", FAIL,
                                    "a fragment of a redacted name is still readable by OCR on a "
                                    "rebuilt image page: " + _fmt(token)))
        else:
            rep.checks.append(Check("V15", "raster pages: OCR leak search", PASS,
                                    f"none of {len(raster_needles)} redacted passages readable by OCR "
                                    f"on {len(raster_pages)} rebuilt page(s)"))
    _raster_retention(rep, boxes, raster_pages, source_pages, texts)


def _raster_retention(rep, boxes, raster_pages, source_pages, out_texts):
    """V16: was the content the OCR read outside the boxes kept in the rebuilt page?"""
    if source_pages is None:
        return
    by_num = {p.page: p for p in source_pages}
    missing = total = 0
    for pno in sorted(raster_pages):
        page_text = by_num.get(pno)
        if page_text is None or pno not in out_texts:
            continue
        page_boxes = [r for p, r in boxes if p == pno]
        hay = norm(out_texts[pno])
        for w in page_text.words:
            if len(w.text) < 3:
                continue
            if any(r[0] <= (w.rect.x0 + w.rect.x1) / 2 <= r[2]
                   and r[1] <= (w.rect.y0 + w.rect.y1) / 2 <= r[3] for r in page_boxes):
                continue                                     # this word was meant to go
            total += 1
            if norm(w.text) not in hay:
                missing += 1
    if not total:
        return
    ratio = 1.0 - missing / total
    rep.checks.append(Check("V16", "raster pages: kept content", WARN if ratio < 0.9 else PASS,
                            f"{missing} of {total} kept word(s) no longer readable after "
                            "rasterisation; check the page visually" if ratio < 0.9
                            else f"{total} kept word(s) still readable after rasterisation"))
