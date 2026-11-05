"""Frozen test suite (v2): an independently written, harder benchmark.

Why it exists: the dev corpus in synth.py was written together with the rules,
so scores there flatter us. This suite is the one we report. Rules for keeping
it honest:

1. It is hash-pinned (FROZEN_SHA256). The pin covers the generated text and gold
   labels. If the generator changes, the test fails and the old numbers no
   longer apply.
2. Nothing is tuned on it. Thresholds are fitted on the dev corpus only.
3. The templates, name lists, formats and layout differ from the dev corpus.

What is harder here: paragraphs WRAP, so names and addresses split across lines;
names from many origins, with particles, apostrophes and accents; "Surname,
Firstname", initials and ALL-CAPS forms; firms named after people (must survive);
place names that look like people; foreign addresses, IBANs and ID formats; phone
numbers and IDs sometimes WITHOUT a keyword.

Not covered (still untested): hyphenation at line ends, multi-page documents,
tables, columns, and non-Latin scripts.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import random
from pathlib import Path

import pymupdf

from .synth import (ADDRESS, CARD, DOB, EMAIL, ID, IBAN, PERSON, PHONE, E, Gold, P, Protected,
                    SynthDoc)

FROZEN_TEMPLATES = ("employment", "settlement", "shareholders", "procurement")
PIN_FILE = Path(__file__).with_name("FROZEN_SHA256")

FIRST = ["Aigerim", "Bogdan", "Chinedu", "Dalia", "Emeka", "Fatima", "Giorgos", "Haruto",
         "Ioana", "Jin", "Kwame", "Leila", "Mateus", "Ngozi", "Olumide", "Pavel", "Qiu",
         "Ravi", "Sakura", "Tomasz", "Uzma", "Viktor", "Wei", "Ximena", "Yusuf", "Zainab",
         "Søren", "Zoë", "Inés", "Jürgen", "Aoife", "Dmitri"]
LAST = ["Abdullaev", "Bianchi", "Chukwu", "Dimitrov", "Eze", "Fernandes", "González",
        "Hashimoto", "Iyer", "Jovanovic", "Kim", "López-García", "Mbeki", "Nguyen",
        "Okonkwo", "Petrov", "Quispe", "Rahman", "Suzuki", "Tanaka", "Vargas", "Wang",
        "Yilmaz", "Zhang", "O'Brien", "de la Cruz", "van der Berg", "Müller", "Hernández",
        "Kowalski", "Ibrahim", "Sato"]
PLACE_LOOKALIKES = ["Washington Square", "Chelsea Road", "Jordan Valley Authority",
                    "Victoria Station", "Grace Period", "Angel Investors Network",
                    "Austin Heights", "Brooklyn Bridge Fund"]
CITIES = [("London", "NW1 6XE"), ("Madrid", "28013"), ("Berlin", "10115"),
          ("Amsterdam", "1012 AB"), ("Lyon", "69002"), ("Boston", "02108")]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def person(r):
    """-> (name text for gold, form tag)."""
    f, l = r.choice(FIRST), r.choice(LAST)
    x = r.random()
    if x < 0.55:
        return f"{f} {l}"
    if x < 0.70:
        return f"{f} {r.choice('ABCDEFGHJKLMNPRSTW')}. {l}"
    if x < 0.80:
        return f"{l}, {f}"
    if x < 0.90:
        return f"{f[0]}. {l}"
    return f"{f} {l}".upper()


def title_prefix(r):
    return r.choice(["", "", "", "Dr. ", "Ms ", "Mr "])


def firm(r):
    a, b = r.choice(LAST), r.choice(LAST)
    return r.choice([f"{a} & {b} Law Offices", f"{a} {b} Associates", f"Studio {a} {b}",
                     f"{a} Family Holdings Ltd"])


def company(r):
    return r.choice(["Helio", "Brightwater", "Kestrel", "Alder", "Monarch", "Vantage"]) + " " + \
        r.choice(["Systems", "Logistics GmbH", "Energy S.L.", "Capital BV", "Labs Inc"])


def email(r, name):
    toks = [t for t in name.replace(",", " ").replace(".", " ").lower().split() if t]
    local = ".".join(toks[:1] + toks[-1:]) if len(toks) > 1 else toks[0]
    local = "".join(ch for ch in local if ch.isascii() and (ch.isalnum() or ch in "._-")) or "contact"
    return f"{local}@{r.choice(['example.test', 'mail.example.org', 'firm.example.eu'])}"


def phone(r):
    """-> (text, prefix keyword or '')."""
    k = r.randrange(5)
    if k == 0:
        return f"(020) 7946 {r.randrange(1000, 9999)}", r.choice(["Tel: ", "Phone: "])
    if k == 1:
        return f"07700 {r.randrange(100000, 999999)}", "mobile: "
    if k == 2:
        return f"+34 91 {r.randrange(100, 999)} {r.randrange(10, 99)} {r.randrange(10, 99)}", ""
    if k == 3:
        return f"0044 20 7946 {r.randrange(1000, 9999)}", ""      # hard: no '+' and no keyword
    return f"+1 {r.randrange(200, 999)}-555-{r.randrange(1000, 9999)}", ""


def _iban_from(prefix, body):
    for check in range(2, 99):
        cand = f"{prefix}{check:02d}{body}"
        if int("".join(str(int(c, 36)) for c in cand[4:] + cand[:4])) % 97 == 1:
            return cand
    raise AssertionError("no check digits")


def iban(r):
    cc = r.choice(["DE", "ES", "FR", "NL", "GB"])
    digits = lambda n: "".join(str(r.randrange(10)) for _ in range(n))  # noqa: E731
    body = {"DE": digits(18), "ES": digits(20), "FR": digits(23),
            "NL": "".join(r.choice("ABCD") for _ in range(4)) + digits(10),
            "GB": "".join(r.choice("ABCDEFGH") for _ in range(4)) + digits(14)}[cc]
    s = _iban_from(cc, body)
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4)) if r.random() < 0.5 else s


def address(r):
    city, pc = r.choice(CITIES)
    n, k = r.randrange(1, 120), r.randrange(4)
    if k == 0:
        return f"{n} {r.choice(LAST).split()[-1]} Street, {city} {pc}"
    if k == 1:
        return f"Flat {r.randrange(1, 9)}, {n} Orchard Road, {city} {pc}"
    if k == 2:
        return f"Calle Mayor {n}, {pc} {city}"
    return f"Hauptstrasse {n}, {pc} {city}"


def national_id(r):
    k = r.randrange(4)
    d = lambda n: "".join(str(r.randrange(10)) for _ in range(n))  # noqa: E731
    if k == 0:
        return "passport number ", "X" + d(7)
    if k == 1:
        return "DNI ", d(8) + r.choice("ZQRTKE")
    if k == 2:
        return "National Insurance number ", "QQ" + d(6) + "C"
    return "SSN ", f"{d(3)}-{d(2)}-{d(4)}"


def dob(r):
    d, m, y = r.randrange(1, 28), r.randrange(1, 12), r.randrange(1948, 2003)
    k = r.randrange(4)
    if k == 0:
        return "born on ", f"{d} {MONTHS[m - 1]} {y}"
    if k == 1:
        return "date of birth: ", f"{d:02d}/{m:02d}/{y}"
    if k == 2:
        return "DOB ", f"{y}-{m:02d}-{d:02d}"
    return "born ", f"{d:02d}.{m:02d}.{y}"


def card(r):
    n = 15 if r.random() < 0.3 else 16
    digs = [3, 7] + [r.randrange(10) for _ in range(n - 3)] if n == 15 else \
        [4] + [r.randrange(10) for _ in range(n - 2)]
    total = 0
    for i, x in enumerate(reversed(digs)):
        if i % 2 == 0:
            x = x * 2 - (9 if x * 2 > 9 else 0)
        total += x
    digs.append((10 - total % 10) % 10)
    s = "".join(map(str, digs))
    return s if r.random() < 0.3 else " ".join(s[i:i + 4] for i in range(0, len(s), 4))


def date(r):
    return f"{r.randrange(1, 28)} {r.choice(MONTHS)} {r.randrange(2026, 2029)}"


def amount(r):
    return f"{r.choice(['USD', 'EUR', 'GBP'])} {r.randrange(5, 900) * 1000:,}"


def law(r):
    return r.choice(["the laws of the State of New York", "English law", "the laws of Spain"])


# ---- templates: lists of paragraphs; a paragraph is a list of segments ----------------
def t_employment(r):
    emp, boss, hr = person(r), person(r), person(r)
    kw, idv = national_id(r)
    dkw, dval = dob(r)
    pv, pkw = phone(r)
    return [
        ["EMPLOYMENT AGREEMENT"],
        ["This agreement is made on ", P("date", date(r)), " between ", P("party", company(r)),
         " (the Employer) and ", title_prefix(r), E(PERSON, emp), ", ", dkw, E(DOB, dval),
         ", holder of ", kw, E(ID, idv), ", residing at ", E(ADDRESS, address(r)), "."],
        ["The Employee reports to ", E(PERSON, boss), ". The base salary is ",
         P("amount", amount(r)), " per year, paid to ", E(IBAN, iban(r)), "."],
        ["Questions about this agreement go to ", E(PERSON, hr), " (", E(EMAIL, email(r, hr)),
         "; ", pkw, E(PHONE, pv), "). The Employer's office is near ",
         r.choice(PLACE_LOOKALIKES), "."],
        ["This agreement is governed by ", P("law", law(r)), ". See Clause 12.3 and Section 4."],
        ["Signed: ", E(PERSON, person(r)), " for the Employer; ", E(PERSON, emp), " (Employee)."],
    ]


def t_settlement(r):
    cl, w1, w2, counsel = person(r), person(r), person(r), person(r)
    pv2, pkw2 = phone(r)
    return [
        ["SETTLEMENT AGREEMENT"],
        ["Between ", title_prefix(r), E(PERSON, cl), " (the Claimant) and ", P("party", company(r)),
         " (the Respondent), concerning events near ", r.choice(PLACE_LOOKALIKES), "."],
        ["The Respondent shall pay ", P("amount", amount(r)), " within 14 days to the account ",
         E(IBAN, iban(r)), " held by the Claimant, whose address for service is ",
         E(ADDRESS, address(r)), "."],
        ["The Claimant was represented by ", E(PERSON, counsel), " of ", P("party", firm(r)),
         ". Witnesses: ", E(PERSON, w1), " and ", E(PERSON, w2), "."],
        ["Claimant's contact: ", E(EMAIL, email(r, cl)), ", ", pkw2, E(PHONE, pv2), "."],
        ["Confidentiality applies for 5 years; the courts of ", P("law", law(r)),
         " have jurisdiction."],
        ["Signed by ", E(PERSON, cl.upper() if r.random() < 0.5 else cl), "."],
    ]


def t_shareholders(r):
    s1, s2, s3, d1 = person(r), person(r), person(r), person(r)
    k1, v1 = national_id(r)
    k2, v2 = national_id(r)
    return [
        ["SHAREHOLDERS' AGREEMENT"],
        ["The shareholders of ", P("party", company(r)), " are ", E(PERSON, s1), " (", k1,
         E(ID, v1), "), ", E(PERSON, s2), " (", k2, E(ID, v2), ") and ", E(PERSON, s3), "."],
        ["Their registered addresses are ", E(ADDRESS, address(r)), " ; ", E(ADDRESS, address(r)),
         " ; and ", E(ADDRESS, address(r)), "."],
        ["Share capital is ", P("amount", amount(r)), ". The first board meets on ",
         P("date", date(r)), " at Washington Square."],
        ["The director ", E(PERSON, d1), " may be contacted at ", E(EMAIL, email(r, d1)), " or on ",
         E(PHONE, phone(r)[0]), "."],
        ["Disputes are resolved under ", P("law", law(r)), "."],
    ]


def t_procurement(r):
    c1, c2, dpo = person(r), person(r), person(r)
    pv3, pkw3 = phone(r)
    return [
        ["PROCUREMENT AND DATA PROCESSING AGREEMENT"],
        ["Supplier: ", P("party", company(r)), ". Customer: ", P("party", company(r)),
         ". Supplier lead: ", E(PERSON, c1), ", ", E(EMAIL, email(r, c1)), "."],
        ["Customer lead: ", E(PERSON, c2), ", ", pkw3, E(PHONE, pv3),
         ". Data protection officer: ", title_prefix(r), E(PERSON, dpo), "."],
        ["Invoices go to ", E(ADDRESS, address(r)), ". Payment by transfer to ", E(IBAN, iban(r)),
         " or by card ", E(CARD, card(r)), "."],
        ["Fees total ", P("amount", amount(r)), ". The Grace Period is 30 days from ",
         P("date", date(r)), "."],
        ["This agreement is governed by ", P("law", law(r)), "; Section 9 survives termination."],
    ]


_BUILD = {"employment": t_employment, "settlement": t_settlement,
          "shareholders": t_shareholders, "procurement": t_procurement}


# ---- assembly: generate -> render wrapped -> extract -> align gold ----------------------
def _gen(template: str, seed: int):
    r = random.Random(f"frozen:{template}:{seed}")
    paras = _BUILD[template](r)
    text, gold, prot = "", [], []
    for i, para in enumerate(paras):
        if i:
            text += "\n"
        for seg in para:
            if isinstance(seg, str):
                text += seg
                continue
            kind, label, s = seg
            a = len(text)
            text += s
            (gold if kind == "e" else prot).append(
                (a, len(text), label))
    return text, gold, prot


def _render(text: str) -> bytes:
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=1700)
    y = 60.0
    for para in text.split("\n"):
        rc = page.insert_textbox(pymupdf.Rect(60, y, 535, y + 400), para, fontsize=10,
                                 fontname="helv")
        assert rc >= 0, "paragraph overflowed its box"
        y += 400 - rc + 9
    data = pdf.tobytes()
    pdf.close()
    return data


def _nonws(s):
    return [i for i, ch in enumerate(s) if not ch.isspace()]


def _align(gen: str, ext: str, spans):
    g, x = _nonws(gen), _nonws(ext)
    if [gen[i] for i in g] != [ext[i] for i in x]:
        raise AssertionError("extracted text differs from generated text beyond whitespace")
    out = []
    for a, b, *rest in spans:
        na, nb = bisect.bisect_left(g, a), bisect.bisect_left(g, b)
        out.append((x[na], x[nb - 1] + 1, *rest))
    return out


def generate(template: str, seed: int) -> SynthDoc:
    from ..extract import extract_pages
    text, gold, prot = _gen(template, seed)
    pdf = _render(text)
    ext = extract_pages(pymupdf.open(stream=pdf, filetype="pdf"))[0].text
    gold, prot = _align(text, ext, gold), _align(text, ext, prot)
    return SynthDoc(template, seed, ext, [Gold(*g) for g in gold],
                    [Protected(*p) for p in prot], pdf=pdf)


def frozen_corpus(per_template: int = 25, seed0: int = 1000) -> list[SynthDoc]:
    return [generate(t, seed0 + i) for t in FROZEN_TEMPLATES for i in range(per_template)]


def fingerprint(per_template: int = 25, seed0: int = 1000) -> str:
    """Hash of the generator output (logical text + gold). Independent of PDF rendering."""
    h = hashlib.sha256()
    for t in FROZEN_TEMPLATES:
        for i in range(per_template):
            h.update(json.dumps([t, seed0 + i, _gen(t, seed0 + i)], ensure_ascii=False).encode())
    return h.hexdigest()
