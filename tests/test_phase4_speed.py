"""Phase 4: safe speed-ups must not change a single decision.

Two properties are covered here:
- GLiNER receives every view of a page in one batched call, and the batched
  results map back to the right page and character offsets;
- repeated detection of the same page text within one run is served from the
  detector's cache (cleared by fit), so the head-to-head harness does not run the
  models four times over the same sources.
"""
from legal_redact.detect import gliner_det
from legal_redact.detect.base import Suggestion
from legal_redact.detect.ensemble import EnsembleDetector
from legal_redact.detect.gliner_det import GlinerDetector
from legal_redact.models import PageText


def page(number, text):
    return PageText(number, 1, 1, text)


class RecordingModel:
    """Fake GLiNER model: records the batched inputs and returns spans per text."""

    def __init__(self):
        self.calls = []

    def inference(self, texts, labels, threshold=0.5, **kwargs):
        self.calls.append(list(texts))
        out = []
        for text in texts:
            if "Alice Example" in text:
                start = text.index("Alice Example")
                out.append([{"start": start, "end": start + len("Alice Example"),
                             "label": "person name", "score": 0.91}])
            else:
                out.append([])
        return out


def test_gliner_sends_all_views_of_a_document_in_one_batched_call(monkeypatch):
    model = RecordingModel()
    monkeypatch.setattr(gliner_det, "_model", lambda name: model)
    det = GlinerDetector()
    pages = [page(0, "MUTUAL AGREEMENT\nNotices to Alice Example."),
             page(1, "SECOND PAGE\nSigned by Alice Example.")]
    out = det._raw(pages)
    assert len(model.calls) == 1                       # one batched call, not four
    assert len(model.calls[0]) == 4                    # flat + decapitalised view per page
    # the batched results still map back to the right page and offsets
    assert len(out) == 2 and {s.page for s in out} == {0, 1}
    for s in out:
        assert pages[s.page].text[s.start:s.end] == "Alice Example"
        assert s.category == "PERSON" and s.confidence == 0.91


def test_gliner_batch_keeps_offsets_when_a_view_is_flattened(monkeypatch):
    """flatten() swaps a line break for a space, so offsets still line up."""
    model = RecordingModel()
    monkeypatch.setattr(gliner_det, "_model", lambda name: model)
    det = GlinerDetector()
    text = "Notices\nto Alice Example shall be sent."
    out = det._raw([page(0, text)])
    start = text.index("Alice Example")
    assert [(s.start, s.end) for s in out] == [(start, start + len("Alice Example"))]
    assert out[0].support == 1


class CountingDetector:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def detect(self, pages):
        self.calls += 1
        return [Suggestion(0, 0, 5, "PERSON", "why", 0.9, "counting")]


class FittedCountingDetector(CountingDetector):
    def __init__(self):
        super().__init__()
        self.fits = 0

    def fit(self, train_docs):
        self.fits += 1


def test_repeated_detection_of_the_same_pages_is_cached():
    part = CountingDetector()
    det = EnsembleDetector([part])
    pages = [page(0, "Notices to Alice Example.")]
    first = det.detect(pages)
    second = det.detect([page(0, "Notices to Alice Example.")])
    assert part.calls == 1
    assert [(s.start, s.end, s.category) for s in first] == [(s.start, s.end, s.category) for s in second]
    det.detect([page(0, "A different document.")])
    assert part.calls == 2


def test_fit_invalidates_the_detection_cache():
    part = FittedCountingDetector()
    det = EnsembleDetector([part])
    det.detect([page(0, "Notices to Alice Example.")])
    det.fit([])
    assert part.fits == 1
    det.detect([page(0, "Notices to Alice Example.")])
    assert part.calls == 2                             # recomputed after fit
