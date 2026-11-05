"""Holdout suite (v3): the numbers we headline.

Created AFTER the v2 suite had been scored and its failures read, and after the
fixes those failures motivated (line-wrap tolerant patterns, flattened and
de-capitalised model views, checksum checks, dropping the naive name rule). v2
therefore no longer measures generalisation. This suite does:

- new names, from pools disjoint from the dev corpus and from v2;
- two new templates (consulting, assignment) plus the four v2 layouts re-seeded;
- new seeds.

It is NOT independent of the generator family: same wrapping renderer, same style
of sentences. It tests whether fixes generalise to new names, layouts and seeds;
it says nothing about real contracts. Rule: it is scored once per change set, and
nothing is tuned on it.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from . import frozen
from .frozen import (E, P, address, amount, company, date, email, firm, iban, law, national_id,
                     person, phone, title_prefix)
from .synth import ADDRESS, EMAIL, ID, IBAN, PERSON, PHONE, SynthDoc

HOLDOUT_TEMPLATES = frozen.FROZEN_TEMPLATES + ("consulting", "assignment")
PIN_FILE = Path(__file__).with_name("HOLDOUT_SHA256")
SEED0, PER_TEMPLATE = 5000, 25

FIRST3 = ["Amara", "Boris", "Catalina", "Dev", "Esperanza", "Farouk", "Gwen", "Hiroshi", "Ilse",
          "Jamal", "Katarzyna", "Lucía", "Mehmet", "Nia", "Orla", "Prakash", "Rania", "Sven",
          "Thandi", "Ulrich", "Vera", "Wojciech", "Xavier", "Yara", "Zubair", "Åsa", "Élodie",
          "Björn", "Renée", "Nikolai"]
LAST3 = ["Achebe", "Barzani", "Cohen", "Delgado", "Eriksson", "Farah", "Greenberg", "Horvat",
         "Ito", "Johansson", "Khalil", "Lindgren", "Moreno", "Nakamura", "Olsen", "Pereira",
         "Qureshi", "Romero", "Silva", "Takahashi", "Usman", "Volkov", "Weiss", "Xu",
         "Yamamoto", "Zielinski", "D'Angelo", "Mac Donald", "Ben-David", "Álvarez"]


def t_consulting(r):
    c, sub1, sub2 = person(r), person(r), person(r)
    kw, idv = national_id(r)
    pv, pkw = phone(r)
    return [
        ["CONSULTING AGREEMENT"],
        ["This agreement is made on ", P("date", date(r)), " between ", P("party", company(r)),
         " and the consultant ", title_prefix(r), E(PERSON, c), ", ", kw, E(ID, idv),
         ", of ", E(ADDRESS, address(r)), "."],
        ["The day rate is ", P("amount", amount(r)), ", paid to ", E(IBAN, iban(r)),
         ". Questions go to ", E(EMAIL, email(r, c)), " or ", pkw, E(PHONE, pv), "."],
        ["Approved sub-contractors: ", E(PERSON, sub1), "; ", E(PERSON, sub2),
         ". Work takes place near ", r.choice(frozen.PLACE_LOOKALIKES), "."],
        ["The consultant is introduced by ", P("party", firm(r)), "; ",
         P("law", law(r)), " governs."],
        ["Signed: ", E(PERSON, c.upper() if r.random() < 0.5 else c), "."],
    ]


def t_assignment(r):
    a, b, w = person(r), person(r), person(r)
    pv, pkw = phone(r)
    return [
        ["ASSIGNMENT OF LEASE"],
        ["The Assignor, ", E(PERSON, a), " (", E(EMAIL, email(r, a)), "), assigns to the Assignee, ",
         E(PERSON, b), ", the lease of ", E(ADDRESS, address(r)), " held from ", P("party", company(r)),
         "."],
        ["Consideration: ", P("amount", amount(r)), ", payable on ", P("date", date(r)),
         " to ", E(IBAN, iban(r)), "."],
        ["Witnessed by ", E(PERSON, w), ", ", pkw, E(PHONE, pv), ". Section 3 applies."],
        ["This deed is governed by ", P("law", law(r)), "."],
    ]


frozen._BUILD.setdefault("consulting", t_consulting)
frozen._BUILD.setdefault("assignment", t_assignment)


@contextmanager
def _pools():
    old = frozen.FIRST, frozen.LAST
    frozen.FIRST, frozen.LAST = FIRST3, LAST3
    try:
        yield
    finally:
        frozen.FIRST, frozen.LAST = old


def holdout_corpus(per_template: int = PER_TEMPLATE, seed0: int = SEED0) -> list[SynthDoc]:
    with _pools():
        return [frozen.generate(t, seed0 + i) for t in HOLDOUT_TEMPLATES for i in range(per_template)]


def fingerprint(per_template: int = PER_TEMPLATE, seed0: int = SEED0) -> str:
    h = hashlib.sha256()
    with _pools():
        for t in HOLDOUT_TEMPLATES:
            for i in range(per_template):
                h.update(json.dumps([t, seed0 + i, frozen._gen(t, seed0 + i)],
                                    ensure_ascii=False).encode())
    return h.hexdigest()


# ---- v4: created before the v3-informed ensemble changes were scored ---------------------
# v3 has now been scored on the ensemble, and its aggregate results informed the next change
# set (e.g. trying Presidio's large model). So v3 can no longer judge that change set; v4 can.
# Same rules: new names (disjoint pools), new seeds, one new template; scored once per change
# set; nothing tuned on it.
HOLDOUT4_TEMPLATES = HOLDOUT_TEMPLATES + ("guarantee",)
PIN_FILE_4 = Path(__file__).with_name("HOLDOUT4_SHA256")
SEED0_4 = 9000

FIRST4 = ["Anselm", "Bianca", "Cormac", "Dagny", "Elio", "Freya", "Gaspar", "Hana", "Idris",
          "Joaquim", "Kalinda", "Lorenzo", "Maren", "Niall", "Ottilie", "Pilar", "Quinn",
          "Rafael", "Saoirse", "Teodor", "Una", "Valentin", "Wilhelmina", "Xiomara", "Yannick",
          "Zeynep", "Ágnes", "Hélène", "Mårten", "Siobhán", "Tiago", "Ørjan"]
LAST4 = ["Abernathy", "Bergström", "Castellani", "Dragomir", "Eklund", "Fitzgerald", "Galvão",
         "Hartmann", "Ionescu", "Jensen", "Kowal", "Lombardi", "Magnusson", "Novotny",
         "Oliveira", "Papadakis", "Quintana", "Rinaldi", "Sorensen", "Teixeira", "Urban",
         "Vasquez", "Wojcik", "Yadav", "Zimmermann", "McAllister", "de Souza", "von Trapp",
         "Al-Rashid", "Ó Briain"]


def t_guarantee(r):
    g, d = person(r), person(r)
    kw, idv = national_id(r)
    pv, pkw = phone(r)
    return [
        ["DEED OF GUARANTEE"],
        ["The Guarantor, ", title_prefix(r), E(PERSON, g), ", ", kw, E(ID, idv), ", of ",
         E(ADDRESS, address(r)), ", guarantees the debts of ", E(PERSON, d), " to ",
         P("party", company(r)), "."],
        ["The guaranteed amount is ", P("amount", amount(r)), ". Payments are made from ",
         E(IBAN, iban(r)), "; the Guarantor may be reached at ", E(EMAIL, email(r, g)),
         " or ", pkw, E(PHONE, pv), "."],
        ["Signed on ", P("date", date(r)), " near ", r.choice(frozen.PLACE_LOOKALIKES), " by ",
         E(PERSON, g.upper() if r.random() < 0.5 else g), ". ", P("law", law(r)), " applies."],
    ]


frozen._BUILD.setdefault("guarantee", t_guarantee)


@contextmanager
def _pools4():
    old = frozen.FIRST, frozen.LAST
    frozen.FIRST, frozen.LAST = FIRST4, LAST4
    try:
        yield
    finally:
        frozen.FIRST, frozen.LAST = old


def holdout4_corpus(per_template: int = PER_TEMPLATE, seed0: int = SEED0_4) -> list[SynthDoc]:
    with _pools4():
        return [frozen.generate(t, seed0 + i) for t in HOLDOUT4_TEMPLATES for i in range(per_template)]


def fingerprint4(per_template: int = PER_TEMPLATE, seed0: int = SEED0_4) -> str:
    h = hashlib.sha256()
    with _pools4():
        for t in HOLDOUT4_TEMPLATES:
            for i in range(per_template):
                h.update(json.dumps([t, seed0 + i, frozen._gen(t, seed0 + i)],
                                    ensure_ascii=False).encode())
    return h.hexdigest()
