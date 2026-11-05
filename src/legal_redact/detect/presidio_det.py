"""Presidio detector: its built-in recognizers plus a spaCy NER model.

`en_core_web_sm` is fast; `en_core_web_lg` is Presidio's documented default and
usually better on names. Both are benchmark arms so the choice is measured.
"""
from __future__ import annotations

from typing import Sequence

from ..models import PageText
from .base import Category as C
from .base import Suggestion

ENTITY_MAP = {
    "PERSON": C.PERSON,
    "EMAIL_ADDRESS": C.EMAIL,
    "PHONE_NUMBER": C.PHONE,
    "IBAN_CODE": C.IBAN,
    "CREDIT_CARD": C.CARD,
    "US_SSN": C.ID, "US_PASSPORT": C.ID, "UK_NHS": C.ID, "UK_NINO": C.ID,
    "US_DRIVER_LICENSE": C.ID, "US_ITIN": C.ID,
}
_ENGINES: dict = {}


def _engine(model: str = "en_core_web_sm"):
    if model not in _ENGINES:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        nlp = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": model}]}).create_engine()
        _ENGINES[model] = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
    return _ENGINES[model]


class PresidioDetector:
    def __init__(self, min_score: float = 0.3, model: str = "en_core_web_sm"):
        self.min_score, self.model = min_score, model
        self.name = "presidio" if model.endswith("_sm") else "presidio-" + model.split("_")[-1]

    def detect(self, pages: Sequence[PageText]) -> list[Suggestion]:
        eng, out = _engine(self.model), []
        for p in pages:
            for r in eng.analyze(text=p.text.replace("\n", " "), language="en", entities=list(ENTITY_MAP),
                                 score_threshold=self.min_score):
                out.append(Suggestion(
                    p.page, r.start, r.end, ENTITY_MAP[r.entity_type],
                    f"Presidio recognizer {r.entity_type} (score {r.score:.2f}).",
                    float(r.score), self.name))
        return out
