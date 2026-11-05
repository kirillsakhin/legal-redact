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

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

PASS, FAIL, WARN, NOT_CHECKED = "PASS", "FAIL", "WARN", "NOT_CHECKED"

LIMITS = [
    "Text rendered as images or vector outlines cannot be read by any extractor used here.",
    "Images are not inspected (no OCR); any image is reported for manual review.",
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
def verify_export(src, out, boxes, needles) -> VerifyReport:
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
    _structure(rep, out)
    _raw_scan(rep, out, needles)
    _revisions(rep, out)
    _collateral(rep, src, out, needles)
    _visual(rep, out, boxes)
    _integrity(rep, src, out)
    _layout_shift(rep, src, out, boxes)
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


def _structure(rep, out):
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
    annots = thumbs = images = 0
    for p in pdf.pages:
        annots += len(p.get("/Annots", []))
        thumbs += "/Thumb" in p
        for _, xo in p.get("/Resources", {}).get("/XObject", {}).items():
            images += xo.get("/Subtype") == "/Image"
    if annots:
        problems.append(f"{annots} annotation(s)/link(s)")
    if thumbs:
        problems.append("page thumbnails")
    add("V05", "hidden containers", FAIL if problems else (WARN if warns else PASS),
        "; ".join(problems + warns) if problems or warns else "no attachments, scripts, forms, bookmarks, annotations or thumbnails")
    add("V08", "images", WARN if images else PASS,
        f"{images} image(s) present; contents not inspected, review manually" if images else "no images")
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
