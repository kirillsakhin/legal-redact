"""Synthetic labelled contracts.

Everything here is invented: names come from small word lists, identifiers are
generated with valid checksums so detectors are tested on realistic structure.
A "template" is a document layout; the benchmark holds out whole templates so
a detector cannot win by memorising one layout.

Gold labels are character offsets into the text that extraction should
produce. Two kinds of gold:
- Gold: personal data that SHOULD be redacted (category known).
- Protected: legally important text that should SURVIVE (parties, dates,
  amounts, governing law). Over-redaction is measured against these.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import pymupdf

PERSON, EMAIL, PHONE, ADDRESS, ID, IBAN, CARD, DOB = (
    "PERSON", "EMAIL", "PHONE", "ADDRESS", "ID", "IBAN", "CARD", "DOB")

FIRST = ["Alice", "Bob", "Carmen", "Dmitri", "Elena", "Farid", "Greta", "Hugo", "Ingrid",
         "Jonas", "Karim", "Lena", "Marco", "Nadia", "Oskar", "Priya", "Quentin", "Rosa",
         "Stefan", "Tanya"]
LAST = ["Example", "Sample", "Whitfield", "Marchetti", "Okafor", "Lindqvist", "Haddad",
        "Novak", "Brandt", "Castellano", "Duarte", "Eriksen", "Fontaine", "Garrido",
        "Holloway", "Ivanov", "Jankowski", "Kowalczyk", "Larsen", "Moreau"]
ORG_STEM = ["Northwind", "Harbour", "Ashgrove", "Bluepeak", "Cedarline", "Dunmore", "Eastgate"]
ORG_SUFFIX = ["Holdings Ltd", "Capital LLC", "Group GmbH", "Partners LLP", "Trading Inc"]
STREETS = ["Willow", "Mill", "Orchard", "Station", "Church", "Maple"]
SUFFIX = ["Lane", "Road", "Street", "Avenue", "Drive"]
CITIES = ["Brookfield", "Ashgrove", "Northvale", "Eastmere"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

TEMPLATES = ("nda", "services", "loan", "lease")
MAX_LINE = 110  # characters; at 8pt this fits the page width, so no line wraps


@dataclass(frozen=True)
class Gold:
    start: int
    end: int
    category: str


@dataclass(frozen=True)
class Protected:
    start: int
    end: int
    kind: str


@dataclass
class SynthDoc:
    template: str
    seed: int
    text: str
    gold: list[Gold] = field(default_factory=list)
    protected: list[Protected] = field(default_factory=list)

    @property
    def doc_id(self) -> str:
        return f"{self.template}-{self.seed}"


# ---- segment helpers -------------------------------------------------------
def E(cat, text):   # personal data that should be redacted
    return ("e", cat, text)


def P(kind, text):  # protected text that should survive
    return ("p", kind, text)


def _build(template: str, seed: int, lines) -> SynthDoc:
    text, gold, prot = "", [], []
    for i, line in enumerate(lines):
        if i:
            text += "\n"
        segs = [line] if isinstance(line, str) else line
        start_of_line = len(text)
        for seg in segs:
            if isinstance(seg, str):
                text += seg
                continue
            kind, label, s = seg
            a = len(text)
            text += s
            (gold if kind == "e" else prot).append(
                Gold(a, len(text), label) if kind == "e" else Protected(a, len(text), label))
        assert len(text) - start_of_line <= MAX_LINE, f"line too long in {template}"
    return SynthDoc(template, seed, text, gold, prot)


# ---- value generators ------------------------------------------------------
def person(r):
    f, l = r.choice(FIRST), r.choice(LAST)
    return f"{f} {r.choice('ABCDEFGH')}. {l}" if r.random() < 0.2 else f"{f} {l}"


def org(r):
    return f"{r.choice(ORG_STEM)} {r.choice(ORG_SUFFIX)}"


def email(r, name):
    parts = name.replace(".", "").split()
    return f"{parts[0].lower()}.{parts[-1].lower()}@{r.choice(['example.test', 'mail.example.test', 'corp.example.org'])}"


def phone(r):
    k = r.randrange(3)
    if k == 0:
        return f"+44 20 7946 {r.randrange(1000, 9999)}"
    if k == 1:
        return f"+1 ({r.randrange(200, 999)}) 555-{r.randrange(1000, 9999)}"
    return f"+49 30 {r.randrange(100000, 999999)}"


def addr(r):
    pc = f"{r.choice('BCDFGH')}{r.choice('FGHJ')}{r.randrange(1, 9)} {r.randrange(1, 9)}{r.choice('QRSTW')}{r.choice('QRSTW')}"
    return (f"{r.randrange(1, 199)} {r.choice(STREETS)} {r.choice(SUFFIX)}, "
            f"{r.choice(CITIES)}, {pc}")


def iban(r):
    body = "".join(r.choice("0123456789") for _ in range(14))
    bank = "".join(r.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4))
    tail = bank + body
    for check in range(2, 99):
        cand = f"GB{check:02d}{tail}"
        digits = "".join(str(int(c, 36)) for c in cand[4:] + cand[:4])
        if int(digits) % 97 == 1:
            break
    return " ".join(cand[i:i + 4] for i in range(0, len(cand), 4)) if r.random() < 0.6 else cand


def card(r):
    d = [4] + [r.randrange(10) for _ in range(14)]
    total = 0
    for i, x in enumerate(reversed(d)):  # check digit not yet appended
        if i % 2 == 0:
            x *= 2
            x -= 9 if x > 9 else 0
        total += x
    d.append((10 - total % 10) % 10)
    s = "".join(map(str, d))
    return " ".join(s[i:i + 4] for i in range(0, 16, 4))


def date(r, year_lo=2024, year_hi=2026):
    return f"{r.randrange(1, 28)} {r.choice(MONTHS)} {r.randrange(year_lo, year_hi)}"


def dob(r):
    return f"{r.randrange(1, 28)} {r.choice(MONTHS)} {r.randrange(1950, 2002)}"


def ident(r):
    return r.choice("ABCDEFGH") + "".join(str(r.randrange(10)) for _ in range(8))


def sig_name(r, name):
    return name.upper() if r.random() < 0.25 else name  # hard case: all caps


# ---- templates -------------------------------------------------------------
def t_nda(r):
    a, b, o1, o2 = person(r), person(r), org(r), org(r)
    return [
        "MUTUAL NON-DISCLOSURE AGREEMENT",
        ["This Agreement is made on ", P("date", date(r)), " between ", P("party", o1),
         " and ", P("party", o2), "."],
        "1. Definitions",
        "Confidential Information means any information disclosed by one Party to the other.",
        "2. Notices",
        ["Notices to ", P("party", o1), " shall be sent to ", E(PERSON, a), " at ",
         E(EMAIL, email(r, a)), "."],
        ["Notices to ", P("party", o2), " shall go to ", E(PERSON, b), ", tel. ", E(PHONE, phone(r)), "."],
        "3. Force Majeure",
        ["Liability is capped at ", P("amount", f"EUR {r.randrange(10, 90)},000"), " per claim."],
        "4. Governing Law",
        ["This Agreement is governed by ", P("law", "the laws of England and Wales"), "."],
        "Signed:",
        ["By: ", E(PERSON, sig_name(r, a)), ", Director"],
        ["By: ", E(PERSON, sig_name(r, b)), ", Managing Partner"],
    ]


def t_services(r):
    a, b, c = person(r), person(r), person(r)
    o1 = f"{r.choice(LAST)} {r.choice(LAST)} LLP"  # hard case: firm named after people
    o2 = org(r)
    return [
        "SERVICES AGREEMENT",
        ["Provider: ", P("party", o1), ". Client: ", P("party", o2), "."],
        ["Effective Date: ", P("date", date(r))],
        "1. Services",
        "The Provider shall deliver the Services described in Schedule 1.",
        "2. Payment Terms",
        ["The Client shall pay ", P("amount", f"EUR {r.randrange(5, 60)},500"),
         " within 30 days of invoice."],
        ["Payments go to IBAN ", E(IBAN, iban(r)), " (account holder: ", E(PERSON, a), ")."],
        ["Invoices are sent to ", E(PERSON, b), ", ", E(ADDRESS, addr(r)), "."],
        ["Project contact: ", E(PERSON, c), " (", E(EMAIL, email(r, c)), ")."],
        "3. Special Conditions",
        ["Governed by ", P("law", "the laws of Spain"), "."],
        ["Signed for the Provider by ", E(PERSON, sig_name(r, a)), "."],
    ]


def t_loan(r):
    a, g = person(r), person(r)
    return [
        "LOAN AGREEMENT",
        ["Lender: ", P("party", org(r)), ". Loan amount: ", P("amount", f"EUR {r.randrange(10, 99)},000"), "."],
        ["Borrower: ", E(PERSON, a), ", born on ", E(DOB, dob(r)), "."],
        ["Passport No. ", E(ID, ident(r)), ", residing at ", E(ADDRESS, addr(r)), "."],
        ["Repayments are collected from IBAN ", E(IBAN, iban(r)), "."],
        ["Card on file: ", E(CARD, card(r)), "."],
        "Guarantor",
        [E(PERSON, g), " guarantees all obligations of the Borrower."],
        ["Guarantor Tax ID: ", E(ID, ident(r)), ", mobile: ", E(PHONE, phone(r)), "."],
        "Entire Agreement",
        ["Interest accrues from ", P("date", date(r)), "; ", P("law", "the laws of Germany"), " apply."],
    ]


def t_lease(r):
    l, t = person(r), person(r)
    return [
        "RESIDENTIAL LEASE",
        ["Landlord: ", E(PERSON, l), ", e-mail ", E(EMAIL, email(r, l)), "."],
        ["Tenant: ", E(PERSON, t), ", born ", E(DOB, dob(r)), ", ID number ", E(ID, ident(r)), "."],
        ["Property: ", E(ADDRESS, addr(r)), "."],
        ["Monthly rent is ", P("amount", f"EUR {r.randrange(8, 30)}00"), "; deposit ",
         P("amount", f"EUR {r.randrange(16, 60)}00"), "."],
        ["The Tenant may be reached on phone: ", E(PHONE, phone(r)), "."],
        ["Lease start: ", P("date", date(r)), ". Term: 12 months."],
        "Special Conditions",
        "Pets are not permitted without the Landlord's written consent.",
        ["Disputes: ", P("law", "the laws of Spain"), "."],
        ["Signed: ", E(PERSON, sig_name(r, l)), " and ", E(PERSON, sig_name(r, t))],
    ]


_BUILDERS = {"nda": t_nda, "services": t_services, "loan": t_loan, "lease": t_lease}


def generate(template: str, seed: int) -> SynthDoc:
    r = random.Random(f"{template}:{seed}")
    return _build(template, seed, _BUILDERS[template](r))


def corpus(per_template: int = 30, seed0: int = 0, templates=TEMPLATES) -> list[SynthDoc]:
    return [generate(t, seed0 + i) for t in templates for i in range(per_template)]


def render_pdf(doc: SynthDoc) -> bytes:
    """One line of text = one line on the page, so extraction reproduces doc.text."""
    pdf = pymupdf.open()
    page = pdf.new_page()
    for i, line in enumerate(doc.text.split("\n")):
        page.insert_text((72, 72 + i * 13), line, fontsize=8, fontname="helv")
    data = pdf.tobytes()
    pdf.close()
    return data
