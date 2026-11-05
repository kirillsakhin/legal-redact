"""Model-facing views of page text.

Both views keep EXACTLY the same length as the original, so a span found in the
view maps back to the page text with no offset arithmetic.

- flatten: a wrapped line break is layout, not meaning. Models read
  "Mueller,\\nUzma" worse than "Mueller, Uzma", so breaks become spaces.
- decapitalise: names in signature blocks are often ALL CAPS, which NER models
  handle poorly. Capitalising such words gives the model a second look.
"""
from __future__ import annotations

import re

_CAPS = re.compile(r"\b[A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ'’-]{2,}\b")


def flatten(text: str) -> str:
    return text.replace("\n", " ")


def decapitalise(text: str) -> str | None:
    """Return the text with ALL-CAPS words capitalised, or None if nothing changed."""
    out = _CAPS.sub(lambda m: m.group().capitalize(), text)
    if out == text or len(out) != len(text):   # unchanged, or a length-changing edge case: skip
        return None
    return out
