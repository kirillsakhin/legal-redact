"""Presidio detector: its built-in recognizers plus a spaCy NER model.

We use the small English spaCy model for speed; the large one is slower and
usually somewhat better on names. That choice is a hypothesis to benchmark,
not a decision.
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
_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        nlp = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}).create_engine()
        _ENGINE = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
    return _ENGINE


class PresidioDetector:
    name = "presidio"

    def __init__(self, min_score: float = 0.3):
        self.min_score = min_score

    def detect(self, pages: Sequence[PageText]) -> list[Suggestion]:
        eng, out = _engine(), []
        for p in pages:
            for r in eng.analyze(text=p.text, language="en", entities=list(ENTITY_MAP),
                                 score_threshold=self.min_score):
                out.append(Suggestion(
                    p.page, r.start, r.end, ENTITY_MAP[r.entity_type],
                    f"Presidio recognizer {r.entity_type} (score {r.score:.2f}).",
                    float(r.score), self.name))
        return out
