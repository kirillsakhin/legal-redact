"""GLiNER detector: zero-shot entity spans from a local model.

GLiNER takes the text plus a list of label names in plain words and returns
spans with scores. We (a) chunk long pages by lines so nothing is silently
truncated, (b) map GLiNER labels to our categories, and (c) keep every span
above a LOW floor so the threshold can be tuned later on training templates
only (see `fit`), never on the held-out one.
"""
from __future__ import annotations

from typing import Sequence

from ..models import PageText
from .base import Category as C
from .base import Suggestion

LABELS = {
    "person name": C.PERSON,
    "email address": C.EMAIL,
    "phone number": C.PHONE,
    "street address": C.ADDRESS,
    "passport number": C.ID,
    "tax identification number": C.ID,
    "iban bank account number": C.IBAN,
    "credit card number": C.CARD,
    "date of birth": C.DOB,
}
FLOOR = 0.2          # raw spans below this are never kept
CHUNK_CHARS = 1200   # lines are grouped up to this size
_MODELS: dict = {}   # one load per process, not per fold


def _model(name: str):
    if name not in _MODELS:
        import torch
        from gliner import GLiNER
        m = GLiNER.from_pretrained(name)
        if torch.cuda.is_available():
            m = m.to("cuda")
        _MODELS[name] = m
    return _MODELS[name]


def chunks(text: str, limit: int = CHUNK_CHARS):
    """Yield (offset, chunk) so that chunks end on line breaks."""
    pos, buf_start, buf = 0, 0, []
    for line in text.split("\n"):
        if buf and sum(len(b) + 1 for b in buf) + len(line) > limit:
            yield buf_start, "\n".join(buf)
            buf_start, buf = pos, []
        buf.append(line)
        pos += len(line) + 1
    if buf:
        yield buf_start, "\n".join(buf)


def _trim(text: str, s: int, e: int) -> tuple[int, int]:
    while s < e and text[s] in " \t\n.,;:()":
        s += 1
    while e > s and text[e - 1] in " \t\n.,;:()":
        e -= 1
    return s, e


class GlinerDetector:
    def __init__(self, model: str = "knowledgator/gliner-pii-base-v1.0", threshold: float = 0.5):
        self.model_name = model
        self.name = "gliner:" + model.split("/")[-1]
        self.threshold = threshold

    def _raw(self, pages: Sequence[PageText]) -> list[Suggestion]:
        model, out = _model(self.model_name), []
        for p in pages:
            for off, chunk in chunks(p.text):
                for ent in model.predict_entities(chunk, list(LABELS), threshold=FLOOR):
                    s, e = _trim(p.text, off + ent["start"], off + ent["end"])
                    if e <= s:
                        continue
                    out.append(Suggestion(
                        p.page, s, e, LABELS[ent["label"]],
                        f"Model labelled this '{ent['label']}' (score {ent['score']:.2f}).",
                        float(ent["score"]), self.name))
        return out

    def detect(self, pages: Sequence[PageText]) -> list[Suggestion]:
        return [s for s in self._raw(pages) if s.confidence >= self.threshold]

    def fit(self, train_docs) -> None:
        """Pick the threshold on TRAINING templates only, maximising F2.

        F2 weights recall twice as much as precision: a missed identifier is a
        leak, a wrong suggestion costs the reviewer a click.
        """
        from ..bench.metrics import score_doc
        from ..bench.run import pages_for
        cached = [(d, self._raw(pages_for(d))) for d in train_docs]
        best, best_t = -1.0, self.threshold
        for t in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            tp_p = n_p = tp_r = n_g = 0
            for d, raw in cached:
                sc = score_doc(d, [s for s in raw if s.confidence >= t])
                tp_p += sum(sc.tp_p.values()); n_p += sum(sc.n_p.values())
                tp_r += sum(sc.tp_r.values()); n_g += sum(sc.n_g.values())
            prec, rec = (tp_p / n_p if n_p else 0.0), (tp_r / n_g if n_g else 0.0)
            f2 = 5 * prec * rec / (4 * prec + rec) if prec + rec else 0.0
            if f2 > best:
                best, best_t = f2, t
        self.threshold = best_t
