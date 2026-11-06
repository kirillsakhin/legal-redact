"""Holdout v5: independent-data benchmark built on third-party contract text.

Why this suite exists: the dev corpus (synth.py) and the held-out suites v2-v4
(frozen.py, holdout.py) are written by the same generator family as the
detectors, so comparisons on them are home-field. v5 replaces invented contract
text with short excerpts of real, public contract language from CUAD (the
Contract Understanding Atticus Dataset) -- text that nobody on this project
wrote. The personal data in the documents is still synthetic: faker values are
injected with pinned seeds, and protected decoys (amounts, dates, party names,
governing-law strings, section references) are injected alongside them.

Rules, same as the other held-out suites:
1. Hash-pinned before any scoring: INDEP_SHA256 covers the generated logical
   text and labels. If this generator changes, the test fails and old numbers
   are void.
2. Nothing is tuned on it: no detector threshold is fitted on these documents,
   and it is scored once per change set.
3. It is not part of the dev corpus and is not wired into the existing pin
   files, tests or the leave-template-out protocol.

Source and licence: CUAD v1 (The Atticus Project, CC BY 4.0), dataset revision
recorded in INDEP_NOTICE.md together with the exact per-excerpt SHA-256, source
file URLs and the editorial normalisation applied. The inserted PII/logistics
paragraphs are ours, not CUAD's; the vendored excerpts are not.

Limits: English prose only, one page per document, no tables, columns or
scans; 20 source excerpts x 5 variants = 100 documents. There are no deliberate
hard cases: plain names, keyworded identifiers, no ALL-CAPS/apostrophe/particle
name tricks, no hyphenation at line ends and no odd date formats. Excerpt text
wraps naturally, so an injected value can still be split by a line break; the
labels index the final extracted text exactly (tests assert it).
"""
from __future__ import annotations

import bisect
import hashlib
import json
import random
import re
from pathlib import Path

import pymupdf
from faker import Faker

from ..extract import extract_pages
from .synth import (ADDRESS, CARD, DOB, EMAIL, ID, IBAN, PERSON, PHONE,
                    Gold, Protected, SynthDoc)

PIN_FILE = Path(__file__).with_name("INDEP_SHA256")
SOURCES_FILE = Path(__file__).with_name("indep_sources.json")
NOTICE_FILE = Path(__file__).with_name("INDEP_NOTICE.md")

HERE = Path(__file__).parent

# Dataset provenance (also written into INDEP_NOTICE.md and the scratch sources).
DATASET = "CUAD v1 (Contract Understanding Atticus Dataset)"
DATASET_REVISION = "a3c393f5d103fd0c516374e4fdff676c8176dcb1"
GITHUB_COMMIT = "67faa0e6023b04fcaae6cc09497ab00e5d63a2a2"
DATASET_URL = ("https://huggingface.co/datasets/theatticusproject/cuad/tree/"
               + DATASET_REVISION)
LICENCE = "CC BY 4.0"
CITATION = ("Hendrycks, Burns, Chen & Ball, CUAD: An Expert-Annotated NLP Dataset "
            "for Legal Contract Review, NeurIPS 2021 (arXiv:2103.06268)")
# Existing suites and their pins; v5 must never be added to these.
PIN_NAME = "INDEP_SHA256"

SEED0 = 12000       # distinct from the dev, frozen, holdout and holdout4 seeds
VARIANTS = 5        # 5 PII variants of each source excerpt


def _load_sources() -> list[dict]:
    """Vendored CUAD excerpts. The per-source SHA-256 is checked on load so a
    corrupted or edited excerpt cannot silently change the suite."""
    raw = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    for src in raw:
        digest = hashlib.sha256(src["text"].encode()).hexdigest()
        if src["sha256"] != digest:
            raise RuntimeError(f"{src['id']}: vendored excerpt does not match its SHA-256")
        if not src["text"].isascii():
            raise RuntimeError(f"{src['id']}: excerpt is not ASCII")
    return raw


SOURCES = _load_sources()

# ---- injected values -------------------------------------------------------
# These pools are deliberately our own: v5 must not reuse the synth.py or
# frozen.py pools. Everything personal is invented; emails use the reserved
# example.* domains and phone numbers the +1 555 / +44 20 7946 0xxx ranges.
COMPANY_STEMS = ["Meridian Field", "Cobalt Ridge", "Sable & Wren", "Tessellate",
                 "Ironbark", "Quarrystone", "Vellum & Vale", "Alderpoint",
                 "Kestrel Bay", "Marrowgate", "Pinecrest", "Silverbrook"]
COMPANY_SUFFIXES = ["Holdings Inc.", "Capital LLC", "Systems Ltd.", "Partners LLP",
                    "Group GmbH", "Industries Ltd."]
LAWS = ["the laws of the State of Delaware",
        "the laws of the State of California",
        "the laws of the Commonwealth of Massachusetts",
        "the laws of England and Wales",
        "the laws of the State of Texas"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
EMAIL_DOMAINS = ["example.org", "example.com", "example.eu"]

_FAKE: Faker | None = None


def _faker(seed: int) -> Faker:
    global _FAKE
    if _FAKE is None:
        _FAKE = Faker("en_US")
    _FAKE.seed_instance(seed)
    return _FAKE


def _company(r) -> str:
    return f"{r.choice(COMPANY_STEMS)} {r.choice(COMPANY_SUFFIXES)}"


def _person(fake: Faker) -> str:
    return f"{fake.first_name()} {fake.last_name()}"


def _email(r, fake: Faker) -> str:
    return f"{fake.user_name()}@{r.choice(EMAIL_DOMAINS)}"


def _phone(r) -> str:
    k = r.randrange(3)
    if k == 0:
        return f"+1 ({r.randrange(200, 999)}) 555-{r.randrange(1000, 10000):04d}"
    if k == 1:
        return f"+44 20 7946 {r.randrange(1000, 10000):04d}"
    return f"+1 {r.randrange(200, 999)}-555-{r.randrange(1000, 10000):04d}"


def _address(fake: Faker) -> str:
    return (f"{fake.building_number()} {fake.street_name()}, "
            f"{fake.city()}, {fake.state_abbr()} {fake.postcode()}")


def _iban(r) -> str:
    """A checksum-valid IBAN in a real format, unspaced (no deliberate hard case)."""
    cc, body_len = r.choice([("DE", 18), ("FR", 23), ("ES", 20)])
    body = "".join(str(r.randrange(10)) for _ in range(body_len))
    for check in range(2, 99):
        cand = f"{cc}{check:02d}{body}"
        digits = "".join(str(int(c, 36)) for c in cand[4:] + cand[:4])
        if int(digits) % 97 == 1:
            return cand
    raise AssertionError("no IBAN check digits")  # pragma: no cover


def _card(r) -> str:
    """A Luhn-valid card number, unspaced."""
    prefix = r.choice([[4], [5, 1], [3, 7]])
    n = 15 if prefix == [3, 7] else 16
    digits = prefix + [r.randrange(10) for _ in range(n - len(prefix) - 1)]
    total = 0
    for i, x in enumerate(reversed(digits)):
        if i % 2 == 0:
            x *= 2
            x -= 9 if x > 9 else 0
        total += x
    digits.append((10 - total % 10) % 10)
    return "".join(map(str, digits))


def _national_id(r) -> str:
    d = lambda n: "".join(str(r.randrange(10)) for _ in range(n))  # noqa: E731
    k = r.randrange(3)
    if k == 0:
        return "X" + d(8)                        # passport-shaped
    if k == 1:
        return f"9{d(2)}-{d(2)}-{d(4)}"          # SSN-shaped, reserved 9xx area
    return d(8) + r.choice("ZQRTKE")             # DNI-shaped


def _dob(r) -> str:
    return f"{r.randrange(1, 29)} {r.choice(MONTHS)} {r.randrange(1950, 2003)}"


def _amount(r) -> str:
    return f"{r.choice(['USD', 'EUR', 'GBP'])} {r.randrange(5, 900) * 1000:,}"


def _date(r) -> str:
    return f"{r.randrange(1, 29)} {r.choice(MONTHS)} {r.randrange(2024, 2028)}"


def _section(r) -> str:
    return f"Section {r.randrange(1, 10)}.{r.randrange(1, 10)}"


# ---- paragraph assembly ----------------------------------------------------
def E(category: str, text: str):
    """Personal data that should be redacted."""
    return ("e", category, text)


def P(kind: str, text: str):
    """Protected text that should survive redaction."""
    return ("p", kind, text)


def _inject(r, fake: Faker) -> list[list]:
    """The three synthetic paragraphs injected into every document.

    Every gold category appears at least once per document, and so do the
    protected kinds (party, amount, date, law, section). The wording is ours.
    """
    company1, company2 = _company(r), _company(r)
    person1, person2, person3 = _person(fake), _person(fake), _person(fake)
    notice = [
        "For the purposes of this Agreement, notices to ", P("party", company1),
        " shall be addressed to ", E(PERSON, person1), ", ",
        E(ADDRESS, _address(fake)), ", and may also be sent by electronic mail to ",
        E(EMAIL, _email(r, fake)), " or by telephone to ", E(PHONE, _phone(r)), "."]
    payment = [
        "All payments due under this Agreement shall be made in ",
        P("amount", _amount(r)), " to the account with IBAN ",
        E(IBAN, _iban(r)), ", or by card ", E(CARD, _card(r)), ", by ",
        P("date", _date(r)), ". The annual charge of ", P("amount", _amount(r)),
        " is due on ", P("date", _date(r)), ". The authorised signatory, ",
        E(PERSON, person2), ", born on ", E(DOB, _dob(r)),
        " and holding identification number ", E(ID, _national_id(r)),
        ", confirms these payment instructions."]
    governing = [
        "This Agreement is governed by ", P("law", r.choice(LAWS)), ". ",
        P("section", _section(r)), " and ", P("section", _section(r)),
        " survive termination, and any notice under ", P("section", _section(r)),
        " must be given in writing. Signed for ", P("party", company2), " by ",
        E(PERSON, person3), "."]
    return [notice, payment, governing]


_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'\d])")


def _paragraphs(text: str, max_chars: int = 480) -> list[list]:
    """Split an excerpt into paragraphs of a few sentences (our layout choice)."""
    sentences = [s for s in _SENTENCE_BREAK.split(text) if s]
    paras, cur = [], ""
    for s in sentences:
        if cur and len(cur) + 1 + len(s) > max_chars:
            paras.append([cur])
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    if cur:
        paras.append([cur])
    return paras


def _assemble(paras: list[list]) -> tuple[str, list[tuple], list[tuple]]:
    text, gold, prot = "", [], []
    for i, para in enumerate(paras):
        if i:
            text += "\n"
        for seg in para:
            if isinstance(seg, str):
                text += seg
                continue
            kind, label, value = seg
            start = len(text)
            text += value
            (gold if kind == "e" else prot).append((start, len(text), label))
    return text, gold, prot


def _compose(src: dict, variant: int) -> tuple[str, list[tuple], list[tuple]]:
    """Logical text + label offsets before rendering (stable, renderer-independent)."""
    r = random.Random(f"indep:v5:{src['id']}:{variant}")
    fake = _faker(r.getrandbits(32))
    notices, payment, governing = _inject(r, fake)
    body = _paragraphs(src["text"])
    pos1, pos2 = len(body) // 3, (2 * len(body)) // 3
    paras = ([[src["contract_type"]]]
             + body[:pos1] + [notices]
             + body[pos1:pos2] + [payment]
             + body[pos2:] + [governing])
    return _assemble(paras)


# ---- neutral renderer (deliberately unlike synth.py / frozen.py) -------------
PAGE_W, PAGE_H = 612.0, 792.0        # US Letter, one page per document
MARGIN_X = 84.0                       # synth.py uses 72pt and A4
TITLE_Y0, TITLE_Y1 = 64.0, 92.0
RULE_Y = 100.0
BODY_TOP = 134.0
BODY_BOTTOM = 726.0
FONT_SIZE = 10.0                      # synth.py uses 8pt Helvetica
LINEHEIGHT = 1.42
PARA_GAP = 5.0


def _render(text: str) -> bytes:
    """Letterhead (centred Times title + rule), justified Times body, 84pt sides."""
    pdf = pymupdf.open()
    page = pdf.new_page(width=PAGE_W, height=PAGE_H)
    lines = text.split("\n")
    page.insert_textbox(pymupdf.Rect(MARGIN_X, TITLE_Y0, PAGE_W - MARGIN_X, TITLE_Y1),
                        lines[0], fontsize=12.5, fontname="tibo",
                        align=1, color=(0, 0, 0))
    page.draw_line(pymupdf.Point(MARGIN_X, RULE_Y),
                   pymupdf.Point(PAGE_W - MARGIN_X, RULE_Y),
                   color=(0.45, 0.45, 0.45), width=0.8)
    y = BODY_TOP
    for para in lines[1:]:
        rect = pymupdf.Rect(MARGIN_X, y, PAGE_W - MARGIN_X, BODY_BOTTOM + PARA_GAP)
        rc = page.insert_textbox(rect, para, fontsize=FONT_SIZE, fontname="tiro",
                                 lineheight=LINEHEIGHT, align=3)
        if rc < 0:
            raise AssertionError(f"paragraph overflowed the one-page layout: {para[:60]!r}")
        y = rect.y1 - rc + PARA_GAP
    if y > BODY_BOTTOM:
        raise AssertionError("document does not fit on one page")
    data = pdf.tobytes()
    pdf.close()
    return data


# ---- offset alignment: logical text -> extracted text -----------------------
def _nonws(s: str) -> list[int]:
    return [i for i, ch in enumerate(s) if not ch.isspace()]


def _align(text: str, extracted: str, spans: list[tuple]) -> list[tuple]:
    """Map logical offsets onto the extracted text; only whitespace may differ."""
    a, b = _nonws(text), _nonws(extracted)
    if len(a) != len(b):
        raise AssertionError("extracted text differs in length beyond whitespace")
    for i, j in zip(a, b):
        if text[i] != extracted[j]:
            off = extracted[max(0, j - 40):j + 40]
            raise AssertionError(f"extracted text differs beyond whitespace near {off!r}")
    out = []
    for start, end, label in spans:
        lo = bisect.bisect_left(a, start)
        hi = bisect.bisect_left(a, end)
        out.append((b[lo], b[hi - 1] + 1, label))
    return out


def _build(src: dict, variant: int) -> SynthDoc:
    text, gold, prot = _compose(src, variant)
    pdf = _render(text)
    pages = extract_pages(pymupdf.open(stream=pdf, filetype="pdf"))
    if len(pages) != 1:
        raise AssertionError(f"{src['id']}: rendered {len(pages)} pages, want 1")
    extracted = pages[0].text
    gold, prot = _align(text, extracted, gold), _align(text, extracted, prot)
    return SynthDoc(f"cuad-{src['id']}", SEED0 + variant, extracted,
                    [Gold(*g) for g in gold], [Protected(*p) for p in prot], pdf=pdf)


# ---- public interface ------------------------------------------------------
def indep_corpus(variants: int = VARIANTS) -> list[SynthDoc]:
    """The pinned v5 suite: 20 sources x 5 variants = 100 one-page documents."""
    return [_build(src, v) for src in SOURCES for v in range(variants)]


def fingerprint(variants: int = VARIANTS) -> str:
    """SHA-256 over the generated logical text and labels (renderer-independent)."""
    h = hashlib.sha256()
    for src in SOURCES:
        for v in range(variants):
            text, gold, prot = _compose(src, v)
            record = [f"cuad-{src['id']}", SEED0 + v, text, gold, prot]
            h.update(json.dumps(record, ensure_ascii=True,
                                separators=(",", ":")).encode())
    return h.hexdigest()
