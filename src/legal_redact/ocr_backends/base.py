"""Pluggable OCR engines: the shared contract.

The default engine is the local Tesseract pipeline in `legal_redact.ocr`
(evaluated, measured, deterministic). A user who needs a different reader -- a
local model, or a paid third-party service through their own wrapper -- can
configure the `command` engine instead: an external program is run once per
page and must return one JSON object with word boxes in the upright page frame
(see `docs/OCR_BACKENDS.md` for the full contract and examples).

Design rule, same as everywhere else: an OCR result is a GUESS, so the engine
layer never trusts one. Words are validated (finite numbers, boxes inside the
page, confidence in 0..1, non-empty text), failures are errors rather than
"no text", and the page frame of every word is explicit (0..1, top-left) so a
buggy engine cannot quietly shift every redaction box.

Third-party/cloud engines are doubly gated (see `docs/OCR_BACKENDS.md`): they
must be approved for the run (`--allow-cloud-ocr`) and every file must have the
pre-flight REVIEW finding acknowledged before export. The disclaimer below is
used verbatim in the finding, the CLI warning, the review page and the docs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from ..ocr import OcrError

# Engine specification forms: "tesseract" (default) or "command:<cmdline>".
DEFAULT_ENGINE = "tesseract"
COMMAND_PREFIX = "command:"
ENV_ENGINE = "LEGAL_REDACT_OCR_ENGINE"
ENV_ALLOW_CLOUD = "LEGAL_REDACT_ALLOW_CLOUD_OCR"
ENV_LOCAL = "LEGAL_REDACT_OCR_LOCAL"

# Hard bounds for the command contract. A page of dense A4 text is well under
# 5,000 words; anything beyond these numbers is not a document OCR result and is
# refused rather than parsed.
MAX_WORDS = 20_000
MAX_WORD_CHARS = 500
MAX_STDOUT_BYTES = 32 * 1024 * 1024
ENGINE_TIMEOUT_S = 180.0

# Verbatim disclaimer: shown to whoever selects a third-party (cloud) engine.
CLOUD_OCR_DISCLAIMER = (
    "Third-party (cloud) OCR is selected. Page images of this document will be sent to an "
    "external OCR service configured by you, outside this machine. That processing can result "
    "in the document being compromised: third-party access, retention and logging, data "
    "location and onward transfers, interception, or a provider breach. Do not use cloud OCR "
    "for privileged, confidential or client documents unless your firm has approved the "
    "service, the transfer is lawful (for example a suitable data processing agreement is in "
    "place) and you are authorised to share the document. The local Tesseract default does "
    "not carry this risk."
)

# The per-file approval the lawyer must tick for a non-local engine.
CLOUD_OCR_ACK_LABEL = (
    "I approve sending this document's page images to the third-party OCR service "
    "(cloud processing may compromise the document)"
)


class EngineConfigError(RuntimeError):
    """The requested OCR engine configuration is invalid or not available."""


class CloudOcrNotApproved(EngineConfigError):
    """A non-local engine was configured without --allow-cloud-ocr."""


@dataclass(frozen=True)
class OcrWord:
    """One recognised word. Box is [x0, y0, x1, y1] in a 0..1 top-left page frame."""
    text: str
    box: tuple[float, float, float, float]
    confidence: float


@dataclass(frozen=True)
class OcrResult:
    """One page read by an engine. None of these fields may carry document text
    except `words`; `meta` is for engine diagnostics and is never put in a report."""
    words: tuple[OcrWord, ...]
    engine: str
    version: str | None
    local: bool
    rotation: int | None          # degrees of orientation correction the engine applied
    meta: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


class OcrEngine(Protocol):
    """What the pipeline needs from any OCR reader.

    `analyze` receives the page rendered as a PNG (what the review page would
    show: the page's display orientation, no rotation baked in beyond the PDF's
    own), its size in PDF points, and the render dpi. It must return words whose
    boxes are relative to that same page frame, reading order, with confidence
    0..1. It raises OcrError for any page it cannot read -- never returns an
    empty result to mean "unreadable" (an empty result means a blank page).
    """

    name: str

    def available(self) -> bool: ...

    def version(self) -> str | None: ...

    def local(self) -> bool: ...

    def analyze(self, image_png: bytes, page_width_pt: float, page_height_pt: float,
                dpi: int) -> OcrResult: ...


def _is_number(value: Any) -> bool:
    """True for a finite int/float but not for bool (bool is an int subclass)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def validate_result_payload(payload: Any) -> OcrResult:
    """Strictly validate one command-engine JSON object into an OcrResult.

    Raises OcrError on anything invalid. Error messages name the field/count and
    never include recognised text.
    """
    if not isinstance(payload, dict):
        raise OcrError("OCR engine output must be a JSON object")
    words_raw = payload.get("words")
    if not isinstance(words_raw, list):
        raise OcrError("OCR engine output has no 'words' array")
    if len(words_raw) > MAX_WORDS:
        raise OcrError(f"OCR engine returned {len(words_raw)} words (limit {MAX_WORDS})")
    local = payload.get("local")
    if not isinstance(local, bool):
        raise OcrError("OCR engine output must state 'local' as a boolean (true/false)")
    rotation = payload.get("rotation")
    if rotation is not None:
        if isinstance(rotation, bool) or not isinstance(rotation, int) \
                or rotation not in (0, 90, 180, 270):
            raise OcrError("OCR engine output 'rotation' must be 0, 90, 180, 270 or null")
    engine = payload.get("engine", "command")
    version = payload.get("version")
    if not isinstance(engine, str) or not engine.strip():
        raise OcrError("OCR engine output 'engine' must be a non-empty string")
    if version is not None and not isinstance(version, str):
        raise OcrError("OCR engine output 'version' must be a string or null")
    words: list[OcrWord] = []
    for i, raw in enumerate(words_raw):
        if not isinstance(raw, dict):
            raise OcrError(f"OCR engine word {i} is not an object")
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise OcrError(f"OCR engine word {i} has no non-empty text")
        if len(text) > MAX_WORD_CHARS:
            raise OcrError(f"OCR engine word {i} is longer than {MAX_WORD_CHARS} characters")
        box = raw.get("box")
        if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_is_number(v) for v in box):
            raise OcrError(f"OCR engine word {i} 'box' must be four finite numbers")
        x0, y0, x1, y1 = (float(v) for v in box)
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise OcrError(f"OCR engine word {i} 'box' is outside the page frame "
                           "(expected 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1)")
        confidence = raw.get("confidence")
        if not _is_number(confidence) or not 0.0 <= float(confidence) <= 1.0:
            raise OcrError(f"OCR engine word {i} 'confidence' must be a number 0..1")
        words.append(OcrWord(text, (x0, y0, x1, y1), float(confidence)))
    meta_raw = payload.get("meta")
    meta: dict[str, Any] = {}
    if isinstance(meta_raw, Mapping):
        for key, value in list(meta_raw.items())[:32]:
            if isinstance(key, str) and (isinstance(value, (str, int, float, bool)) or value is None):
                meta[key[:64]] = value if not isinstance(value, str) else value[:200]
    return OcrResult(tuple(words), engine.strip(), version, local, rotation, meta)
