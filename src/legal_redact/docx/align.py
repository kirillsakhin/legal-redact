"""Map .docx text offsets to the rendered PDF, and rendered boxes back to text.

The review page must never show a box that might be the wrong text. This module
is therefore deliberately conservative:

- it aligns *tokens*, not characters (whitespace-normalised, NFKC, casefolded);
- it aligns each .docx text container separately, because the rendered page
  interleaves headers, footers, footnotes and text boxes in an order the OOXML
  text model does not have;
- it anchors on tokens that occur exactly once on both sides (longest increasing
  subsequence), then fills the gaps with a local sequence match. The result is
  monotone: a .docx token can map to at most one rendered token and vice versa;
- a suggestion maps only when **every** token it touches is aligned and one
  neighbour token is aligned nearby as well — an isolated, context-free match is
  treated as unreliable. Anything else returns ``None`` and the review page
  switches to text mode rather than drawing a possibly-wrong box;
- a manual rendered box only maps back when it fully contains whole words, the
  words are contiguous in one .docx container, and the resulting span maps
  forward again. Partial coverage, mixed parts, gaps and unmapped
  (renderer-generated) text are rejected with a clear message.

Determinism: all matching is deterministic; no randomness, no model, no clock.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf

from .extract import extract_text
from .package import read_package

MIN_GLOBAL_MATCH_RATIO = 0.90     # share of .docx tokens that must align to trust the page
MAX_SAME_PAGE_GAP = 8             # rendered-only tokens tolerated between two aligned tokens
MAX_GAP_MATCH = 500_000           # a gap bigger than this is left unmatched (fail closed)
_CONTEXT_GAP = 3                  # a mapped token needs an aligned neighbour this close
_CONTAINMENT_TOLERANCE = 1.0      # pt of slack when a drawn box is checked against glyphs
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
_WORD_RE = re.compile(r"\S+")


class AlignmentError(ValueError):
    """A manual box or a mapping request cannot be answered reliably.

    Subclasses ``ValueError`` so the review server's existing input-error path
    returns the message to the page as a 400 instead of closing the connection.
    """


@dataclass(frozen=True)
class Box:
    page: int
    rect: tuple[float, float, float, float]


@dataclass(frozen=True)
class PdfToken:
    page: int
    rect: tuple[float, float, float, float]
    line: int
    text: str
    norm: str


@dataclass(frozen=True)
class DocxToken:
    start: int
    end: int
    norm: str


@dataclass(frozen=True)
class SpanMap:
    """Where one accepted .docx span appears on the rendered pages."""
    container: int
    start: int
    end: int
    boxes: tuple[Box, ...]
    matched: int
    total: int

    @property
    def confidence(self) -> float:
        return self.matched / self.total if self.total else 0.0


@dataclass(frozen=True)
class ContainerTokens:
    tokens: tuple[DocxToken, ...]
    mapping: dict[int, int]        # .docx token index -> rendered token index


def _normalise_token(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).casefold()


def tokenise(text: str) -> list[DocxToken]:
    """Split a container's text into tokens with their character offsets."""
    return [DocxToken(match.start(), match.end(), _normalise_token(match.group()))
            for match in _WORD_RE.finditer(text)]


# ---- token sequence alignment ---------------------------------------------
def _longest_increasing(anchors: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest subsequence with both coordinates increasing (patience sorting)."""
    if not anchors:
        return []
    from bisect import bisect_left
    tails: list[int] = []             # smallest tail value of a run of each length
    tails_at: list[int] = []          # index into anchors
    previous: list[int] = [-1] * len(anchors)
    for index, (_, j) in enumerate(anchors):
        at = bisect_left(tails, j)
        if at == len(tails):
            tails.append(j)
            tails_at.append(index)
        else:
            tails[at] = j
            tails_at[at] = index
        previous[index] = tails_at[at - 1] if at else -1
    ordered: list[tuple[int, int]] = []
    cursor = tails_at[-1]
    while cursor != -1:
        ordered.append(anchors[cursor])
        cursor = previous[cursor]
    ordered.reverse()
    return ordered


def _fill_gap(a: list[str], b: list[str], offset_a: int, offset_b: int) -> list[tuple[int, int]]:
    if not a or not b:
        return []
    if len(a) * len(b) > MAX_GAP_MATCH:
        return []                     # cannot align this gap confidently: leave it unmatched
    pairs: list[tuple[int, int]] = []
    for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for step in range(block.size):
            pairs.append((offset_a + block.a + step, offset_b + block.b + step))
    return pairs


def align_sequences(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Monotone token alignment; deterministic and fail-closed on huge gaps."""
    count_a, count_b = Counter(a), Counter(b)
    positions_b: dict[str, int] = {}
    for index, token in enumerate(b):
        if count_b[token] == 1:
            positions_b[token] = index
    anchors = [(i, positions_b[token]) for i, token in enumerate(a)
               if count_a[token] == 1 and token in positions_b]
    anchors = _longest_increasing(anchors)
    pairs: list[tuple[int, int]] = []
    previous_a = previous_b = -1
    for index_a, index_b in anchors + [(len(a), len(b))]:
        pairs += _fill_gap(a[previous_a + 1:index_a], b[previous_b + 1:index_b],
                           previous_a + 1, previous_b + 1)
        if index_a < len(a):
            pairs.append((index_a, index_b))
        previous_a, previous_b = index_a, index_b
    return pairs


# ---- the mapping -----------------------------------------------------------
class Alignment:
    """A monotone mapping between one .docx extraction and one rendered PDF."""

    def __init__(self, sizes, pdf_tokens, containers, token_lists, mappings,
                 *, global_ratio: float | None = None):
        self.sizes = list(sizes)
        self.pdf_tokens = list(pdf_tokens)
        self.containers = list(containers)
        self.container_tokens = list(token_lists)
        self.mappings = list(mappings)          # [{docx_idx: pdf_idx}] per container
        self.reverse: dict[int, tuple[int, int]] = {}
        for container_index, mapping in enumerate(self.mappings):
            for docx_index, pdf_index in mapping.items():
                self.reverse[pdf_index] = (container_index, docx_index)
        if global_ratio is None:
            matched = sum(len(mapping) for mapping in self.mappings)
            total = sum(len(tokens.tokens) for tokens in self.container_tokens)
            global_ratio = matched / total if total else 0.0
        self.global_ratio = global_ratio

    @property
    def page_count(self) -> int:
        return len(self.sizes)

    @property
    def ok(self) -> bool:
        return bool(self.pdf_tokens) and self.global_ratio >= MIN_GLOBAL_MATCH_RATIO

    # -- forward -------------------------------------------------------------
    def map_span(self, container: int, start: int, end: int) -> SpanMap | None:
        """Rendered boxes for a .docx span, or None when the mapping is not reliable."""
        if not (0 <= container < len(self.container_tokens)):
            return None
        text = self.containers[container].text
        if not (0 <= start < end <= len(text)):
            return None
        tokens = self.container_tokens[container].tokens
        mapping = self.mappings[container]
        touched = [(index, token) for index, token in enumerate(tokens)
                   if token.end > start and token.start < end]
        if not touched:
            return None
        pdf_indexes: list[int] = []
        for docx_index, _token in touched:
            pdf_index = mapping.get(docx_index)
            if pdf_index is None:
                return None                   # a word of this span was not rendered
            pdf_indexes.append(pdf_index)
        if any(right <= left for left, right in zip(pdf_indexes, pdf_indexes[1:])):
            return None                       # not monotone: contradictory mapping
        for left, right in zip(pdf_indexes, pdf_indexes[1:]):
            first, second = self.pdf_tokens[left], self.pdf_tokens[right]
            if second.page < first.page:
                return None
            if second.page == first.page and right - left > MAX_SAME_PAGE_GAP:
                return None                   # far-apart words: likely a different occurrence
        first_ti, last_ti = touched[0][0], touched[-1][0]
        if not self._neighbour_supports(container, first_ti, last_ti, pdf_indexes):
            return None                       # isolated match with no aligned context
        boxes = self._boxes(pdf_indexes)
        if not boxes:
            return None
        return SpanMap(container, start, end, boxes, len(touched), len(touched))

    def _neighbour_supports(self, container: int, first_ti: int, last_ti: int,
                            pdf_indexes: list[int]) -> bool:
        """Require one aligned token just outside the span, close to it.

        A span that makes up its whole container is fine (nothing to compare).
        A span surrounded by .docx tokens that are *not* aligned is an isolated
        match with no context: refused rather than drawn.
        """
        mapping = self.mappings[container]
        tokens = self.container_tokens[container].tokens
        before = first_ti - 1 if first_ti > 0 else None
        after = last_ti + 1 if last_ti + 1 < len(tokens) else None
        checks = []
        if before is not None and before in mapping:
            checks.append((mapping[before], pdf_indexes[0]))
        if after is not None and after in mapping:
            checks.append((pdf_indexes[-1], mapping[after]))
        if before is None and after is None:
            return True                       # span is the whole container
        if not checks:
            return False                      # neighbours exist but none is aligned
        for left, right in checks:
            first, second = self.pdf_tokens[left], self.pdf_tokens[right]
            if second.page == first.page and right - left <= _CONTEXT_GAP:
                return True
            if second.page == first.page + 1 and abs(right - left) <= 10:
                return True                   # a page break between the two words
        return False

    def _boxes(self, pdf_indexes: list[int]) -> tuple[Box, ...]:
        boxes: list[Box] = []
        current_page = None
        current_line = None
        x0 = y0 = x1 = y1 = 0.0
        for index in pdf_indexes:
            token = self.pdf_tokens[index]
            if token.page != current_page or token.line != current_line:
                if current_page is not None:
                    boxes.append(Box(current_page, (x0, y0, x1, y1)))
                current_page, current_line = token.page, token.line
                x0, y0, x1, y1 = token.rect
            else:
                x0, y0 = min(x0, token.rect[0]), min(y0, token.rect[1])
                x1, y1 = max(x1, token.rect[2]), max(y1, token.rect[3])
        if current_page is not None:
            boxes.append(Box(current_page, (x0, y0, x1, y1)))
        return tuple(boxes)

    # -- backward ------------------------------------------------------------
    def reverse_rect(self, page: int, rect) -> tuple[int, int, int]:
        """Map a drawn rendered-page box back to (container, start, end)."""
        if not (0 <= page < len(self.sizes)):
            raise AlignmentError("that page does not exist")
        try:
            x0, y0, x1, y1 = (float(value) for value in rect)
        except (TypeError, ValueError):
            raise AlignmentError("the box coordinates are not numbers") from None
        if x1 <= x0 or y1 <= y0:
            raise AlignmentError("the box has no area")
        covered: list[int] = []
        for index, token in enumerate(self.pdf_tokens):
            if token.page != page:
                continue
            tx0, ty0, tx1, ty1 = token.rect
            if tx1 <= x0 or tx0 >= x1 or ty1 <= y0 or ty0 >= y1:
                continue                                  # no overlap
            contained = (tx0 >= x0 - _CONTAINMENT_TOLERANCE
                         and ty0 >= y0 - _CONTAINMENT_TOLERANCE
                         and tx1 <= x1 + _CONTAINMENT_TOLERANCE
                         and ty1 <= y1 + _CONTAINMENT_TOLERANCE)
            if not contained:
                raise AlignmentError("the box only partly covers a word; draw it around whole words")
            covered.append(index)
        if not covered:
            raise AlignmentError("the box does not cover any text")
        targets: list[tuple[int, int]] = []
        for index in covered:
            target = self.reverse.get(index)
            if target is None:
                raise AlignmentError(
                    "the box covers text that is not part of the .docx text model (for "
                    "example a page number or a field result); it cannot be redacted from "
                    "the .docx — convert the page to PDF if it matters")
            targets.append(target)
        containers = {container for container, _ in targets}
        if len(containers) != 1:
            raise AlignmentError("the box covers text from more than one document part; "
                                 "draw one box per passage")
        container = containers.pop()
        token_indexes = [index for _, index in targets]
        if token_indexes != sorted(token_indexes):
            raise AlignmentError("the words in the box are not in document order")
        for left, right in zip(token_indexes, token_indexes[1:]):
            if right != left + 1:
                raise AlignmentError("the box covers a non-contiguous phrase; draw it around "
                                     "one continuous phrase")
        tokens = self.container_tokens[container].tokens
        start = min(tokens[index].start for index in token_indexes)
        end = max(tokens[index].end for index in token_indexes)
        if self.map_span(container, start, end) is None:
            raise AlignmentError("the box could not be mapped back to the .docx with confidence")
        return container, start, end

    # -- decisions -----------------------------------------------------------
    def map_decision(self, decision) -> SpanMap | None:
        """Rendered boxes for a reviewer decision, or None if it cannot be placed."""
        if decision.source == "manual" and decision.start == decision.end == 0:
            return None
        return self.map_span(decision.page, decision.start, decision.end)


def _pdf_tokens(pdf_path) -> tuple[list[tuple[float, float]], list[PdfToken]]:
    sizes: list[tuple[float, float]] = []
    tokens: list[PdfToken] = []
    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document):
            sizes.append((page.rect.width, page.rect.height))
            line_ids: dict[tuple[int, int], int] = {}
            for x0, y0, x1, y1, text, block, line, _word in page.get_text("words", sort=True):
                key = (block, line)
                line_id = line_ids.setdefault(key, len(line_ids))
                tokens.append(PdfToken(page_number, (float(x0), float(y0), float(x1), float(y1)),
                                       line_id, text, _normalise_token(text)))
    return sizes, tokens


def align(src: str | Path, pdf_path: str | Path) -> Alignment:
    """Build the mapping between a .docx extraction and its rendered PDF."""
    package = read_package(src)
    docx_text = extract_text(package)
    containers = docx_text.containers
    sizes, pdf_tokens = _pdf_tokens(pdf_path)
    pdf_norms = [token.norm for token in pdf_tokens]
    used = [False] * len(pdf_tokens)
    token_lists: list[ContainerTokens] = []
    mappings: list[dict[int, int]] = []
    matched_total = 0
    eligible_total = 0
    for container in containers:
        tokens = tokenise(container.text)
        if container.part.endswith("glossary/document.xml"):
            # AutoText is not rendered by Word; it cannot be placed on the page.
            token_lists.append(ContainerTokens(tuple(tokens), {}))
            mappings.append({})
            continue
        eligible_total += len(tokens)
        masked = [norm if not used[index] else f"\x00{index}"
                  for index, norm in enumerate(pdf_norms)]
        pairs = align_sequences([token.norm for token in tokens], masked)
        mapping: dict[int, int] = {}
        for docx_index, pdf_index in pairs:
            if pdf_index >= len(pdf_tokens) or used[pdf_index]:
                continue
            if tokens[docx_index].norm != pdf_norms[pdf_index]:
                continue
            mapping[docx_index] = pdf_index
            used[pdf_index] = True
        matched_total += len(mapping)
        token_lists.append(ContainerTokens(tuple(tokens), mapping))
        mappings.append(mapping)
    global_ratio = matched_total / eligible_total if eligible_total else 0.0
    return Alignment(sizes, pdf_tokens, containers, token_lists, mappings,
                     global_ratio=global_ratio)
