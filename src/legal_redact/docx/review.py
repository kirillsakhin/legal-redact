"""Review-page backend for .docx: rendered pages with mapped boxes, or text review.

Two modes, chosen automatically and fail-closed:

**rendered** — a Word or LibreOffice render of the .docx is served as page images
and every detector suggestion is drawn where the aligned text appears. The
rendered PDF is a *view only*: export always writes the .docx through the normal
writer + verifier. Rendered mode is used only when the alignment is trustworthy
for every suggestion (see ``align.py``); otherwise the whole document switches to
text review rather than show one box that might point at the wrong text.

**text** — suggestions are listed with a short in-memory context excerpt around
each span, next to accept/reject controls, pre-flight acknowledgements and the
export button. Excerpts are UI data only: they are never written to the decision
file, the report or the log.

Manual boxes exist only in rendered mode. A drawn box is mapped back to a
contiguous span in one .docx text container before it is stored, and rejected
with a clear message when that is not possible. The decision file keeps the same
format as `suggest`/`redact`: container number, character offsets and category,
never document text.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import webbrowser
from pathlib import Path

import pymupdf

from ..ocr_backends import CLOUD_OCR_ACK_LABEL, CLOUD_OCR_DISCLAIMER, EngineConfig
from ..pipeline import ExportRefused, save_decisions
from ..review import ReviewSession
from ..review_server import make_server
from . import images as image_tools
from .align import Alignment, align
from .extract import extract_text
from .package import DocxError, read_package
from .pipeline import export_docx
from .preflight import blocked, preflight_docx
from .render import detect_renderer, render_docx_to_pdf, renderer_label, renderer_name

CONTEXT_CHARS = 80
_CONTEXT_TOTAL = 260


def _container_labels(containers) -> list[str]:
    """Human labels for text containers ("main text", "header", "text box", ...)."""
    seen: set[str] = set()
    labels: list[str] = []
    for container in containers:
        part = container.part
        if part in seen:
            labels.append("text box")
            continue
        seen.add(part)
        if part.endswith("/document.xml"):
            labels.append("main text")
        elif part.endswith("/header1.xml") or "/header" in part:
            labels.append("header")
        elif "/footer" in part:
            labels.append("footer")
        elif part.endswith("/footnotes.xml"):
            labels.append("footnotes")
        elif part.endswith("/endnotes.xml"):
            labels.append("endnotes")
        elif part.endswith("/glossary/document.xml"):
            labels.append("auto-text")
        else:
            labels.append("text")
    return labels or ["text"]


class DocxReviewApp:
    """Same HTTP surface as the PDF `ReviewApp`, backed by the .docx pipeline."""

    def __init__(self, src: str, session: ReviewSession, decisions_path: str | None = None,
                 *, docx_text=None, rendered_pdf: str | Path | None = None,
                 alignment: Alignment | None = None, renderer_kind: str | None = None,
                 text_reason: str | None = None, image_readings=(),
                 engine_config: EngineConfig | None = None, ocr: bool = True):
        self.src = str(Path(src).resolve())
        self.session = session
        self.decisions_path = decisions_path
        self.lock = threading.Lock()
        self.docx_text = docx_text
        self.containers = docx_text.containers if docx_text is not None else []
        self.image_readings = list(image_readings)
        self.engine_config = engine_config
        self.ocr = ocr
        self._text_count = len(self.containers)
        labels = _container_labels(self.containers)
        for index, reading in enumerate(self.image_readings):
            labels.append(f"image {index + 1} ({reading.part.width}x{reading.part.height} px, "
                          f"{reading.words} word(s))")
        self._labels = labels or ["text"]
        self.ocr_state = None
        if engine_config is not None and self.image_readings:
            name, version, _available, _error = image_tools.engine_summary(engine_config)
            self.ocr_state = {"engine": engine_config.spec,
                              "name": name or engine_config.spec, "version": version,
                              "local": engine_config.local,
                              "disclaimer": (None if engine_config.local
                                             else CLOUD_OCR_DISCLAIMER)}
        self.renderer_kind = renderer_kind
        self.alignment = alignment
        self._tmpdir: Path | None = None
        self._pdf = None
        if rendered_pdf is not None and alignment is not None and alignment.ok:
            self._pdf = pymupdf.open(str(rendered_pdf))
            self.sizes = [(page.rect.width, page.rect.height) for page in self._pdf]
            self.mode = "rendered"
            self.note = (
                f"Rendered review from {renderer_name(renderer_kind)}. Boxes show where the "
                "text appears in that layout; export still writes and verifies the .docx.")
        else:
            self.sizes = []
            self.mode = "text"
            self.note = text_reason or (
                "Text review: no rendered page is available, so suggestions are listed as "
                "text. Export still writes and verifies the .docx.")
        self.png_cache: dict[tuple[int, int], bytes] = {}
        name = Path(self.src)
        self.out_default = str(name.with_name(name.stem + ".redacted.docx"))

    # -- construction --------------------------------------------------------
    @classmethod
    def prepare(cls, src: str, session: ReviewSession,
                decisions_path: str | None = None,
                renderer: str | None = None,
                engine_config: EngineConfig | None = None,
                ocr: bool = True) -> "DocxReviewApp":
        """Extract, read images, optionally render + align, and choose a mode."""
        docx_text = None
        image_readings = []
        try:
            package = read_package(src)
            docx_text = extract_text(package)
        except DocxError:
            docx_text = None
            package = None
        if docx_text is not None and ocr and package is not None and \
                package.layout.media_parts and engine_config is not None:
            readings, _errors = image_tools.read_images(package, engine_config,
                                                        page_offset=len(docx_text.containers))
            image_readings = readings
        findings = preflight_docx(src, ocr=ocr, engine_config=engine_config)
        rendered_pdf: Path | None = None
        alignment: Alignment | None = None
        renderer_kind: str | None = None
        tmpdir: Path | None = None
        reason: str | None = None

        if blocked(findings):
            reason = ("Pre-flight refuses this file (see the findings), so no rendered page is "
                      "shown. Fix the reported issue in Word first; export stays blocked.")
        elif docx_text is None:
            reason = "The document could not be read for a rendered page; text review only."
        else:
            renderer_kind = renderer if renderer is not None else detect_renderer()
            if renderer_kind is None:
                reason = ("No Microsoft Word or LibreOffice renderer was found, so this is a "
                          "text review. Install Word or LibreOffice, or use the text review.")
            else:
                tmpdir = Path(tempfile.mkdtemp(prefix="legal-redact-review-"))
                try:
                    rendered_pdf = render_docx_to_pdf(src, tmpdir / "rendered.pdf",
                                                      renderer=renderer_kind)
                    alignment = align(src, rendered_pdf)
                except Exception as exc:      # any renderer/alignment failure -> text mode
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    tmpdir = None
                    rendered_pdf = None
                    alignment = None
                    reason = (f"Rendered review is unavailable ({type(exc).__name__}), so "
                              "this is a text review.")
                else:
                    if not alignment.ok:
                        reason = (
                            "Text review: only "
                            f"{alignment.global_ratio:.0%} of the document text could be "
                            "located on the rendered pages, so no boxes are drawn.")
                    else:
                        text_count = len(docx_text.containers)
                        unplaced = sum(1 for decision in session.decisions
                                       if decision.page < text_count
                                       and alignment.map_decision(decision) is None)
                        if unplaced:
                            reason = (
                                f"Text review: {unplaced} suggestion(s) could not be located "
                                "on the rendered page with confidence. No boxes are shown "
                                "rather than a box that might point at the wrong text.")
                    if reason is not None:
                        shutil.rmtree(tmpdir, ignore_errors=True)
                        tmpdir = None
                        rendered_pdf = None
                        alignment = None
        app = cls(src, session, decisions_path, docx_text=docx_text,
                  rendered_pdf=rendered_pdf, alignment=alignment,
                  renderer_kind=renderer_kind if rendered_pdf is not None else None,
                  text_reason=reason, image_readings=image_readings,
                  engine_config=engine_config, ocr=ocr)
        app._tmpdir = tmpdir
        return app

    def close(self) -> None:
        if self._pdf is not None:
            try:
                self._pdf.close()
            except Exception:                 # pragma: no cover - defensive
                pass
            self._pdf = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    # -- state ---------------------------------------------------------------
    def state(self) -> dict:
        decisions = []
        rendered = self.mode == "rendered"
        text_count = len(self.containers)
        for index, decision in enumerate(self.session.decisions):
            item = {
                "i": index, "page": decision.page, "category": decision.category,
                "confidence": round(decision.confidence, 2), "reason": decision.reason,
                "detector": decision.detector, "proposed": decision.proposed,
                "status": decision.status, "source": decision.source, "rects": [],
            }
            is_image = decision.page >= text_count
            if rendered:
                span = self.alignment.map_decision(decision) if (
                    self.alignment and not is_image) else None
                if span is not None:
                    item["boxes"] = [{"page": box.page, "rect": [round(v, 2) for v in box.rect]}
                                     for box in span.boxes]
                    pages = sorted({box.page for box in span.boxes})
                    item["where"] = "p" + ", p".join(str(page + 1) for page in pages)
                else:
                    item["boxes"] = []
                    item["where"] = self._label(decision.page)
                    if is_image:
                        item["image"] = True
            else:
                item["boxes"] = []
                item["where"] = self._label(decision.page)
                item["context"] = self._context(decision)
                if is_image:
                    item["image"] = True
            decisions.append(item)
        return {
            "name": Path(self.src).name,
            "pages": [{"w": w, "h": h} for w, h in self.sizes],
            "review_mode": self.mode,
            "review_note": self.note,
            "renderer": renderer_label(self.renderer_kind) if rendered else "none",
            "manual_boxes": rendered,
            "repeats_available": False,
            "ocr": self.ocr_state,
            "decisions": decisions,
            "findings": [{"code": finding.code, "severity": finding.severity.value,
                          "message": finding.message, "page": finding.page,
                          "ack": finding.code in self.session.acknowledged,
                          "ack_label": (CLOUD_OCR_ACK_LABEL if finding.code == "PF019"
                                        else "I have looked at this")}
                         for finding in self.session.findings],
            "blockers": self.session.blockers(),
            "can_export": self.session.can_export(),
            "out_default": self.out_default,
        }

    def _label(self, page: int) -> str:
        if 0 <= page < len(self._labels):
            return self._labels[page]
        return "text"

    def _context(self, decision) -> dict:
        """Short excerpt around a suggestion; text mode only, never persisted."""
        text_count = len(self.containers)
        if 0 <= decision.page < text_count:
            text = self.containers[decision.page].text
        elif 0 <= decision.page - text_count < len(self.image_readings):
            text = self.image_readings[decision.page - text_count].page.text
        else:
            return {"pre": "", "text": "", "post": ""}
        start = max(0, min(decision.start, len(text)))
        end = max(start, min(decision.end, len(text)))
        if decision.source == "manual" and start == end == 0:
            return {"pre": "", "text": "", "post": ""}
        pre = " ".join(text[max(0, start - CONTEXT_CHARS):start].split())
        match = " ".join(text[start:end].split())
        post = " ".join(text[end:end + CONTEXT_CHARS].split())
        if len(match) > _CONTEXT_TOTAL:
            match = match[:_CONTEXT_TOTAL] + "..."
        return {"pre": pre, "text": match, "post": post}

    # -- actions (under the caller's lock) -----------------------------------
    def _save(self) -> None:
        if self.decisions_path:
            save_decisions(self.session, self.decisions_path)

    def decide(self, index: int, status: str) -> None:
        if status not in ("accepted", "rejected", "pending"):
            raise ValueError("bad status")
        self.session.decisions[index].status = status
        self._save()

    def add_box(self, page: int, rect) -> int:
        if self.mode != "rendered" or self.alignment is None:
            raise ValueError("manual boxes need a rendered page; this is a text review")
        container, start, end = self.alignment.reverse_rect(page, rect)
        index = self.session.add_manual(container, start, end)
        self._save()
        return index

    def remove_box(self, index: int) -> None:
        self.session.remove_manual(index)
        self._save()

    def ack(self, code: str, on: bool) -> None:
        if code not in {finding.code for finding in self.session.findings}:
            raise ValueError("unknown finding")
        self.session.acknowledge(code, on)
        self._save()

    def repeats(self) -> int:
        raise ValueError("find repeats is not available for .docx review")

    def bulk(self) -> int:
        number = self.session.accept_all_proposed_redactions()
        self._save()
        return number

    def page_png(self, page: int, dpi: int) -> bytes:
        if self._pdf is None:
            raise ValueError("no rendered page")
        dpi = max(50, min(200, dpi))
        key = (page, dpi)
        if key not in self.png_cache:
            self.png_cache[key] = self._pdf[page].get_pixmap(dpi=dpi).tobytes("png")
        return self.png_cache[key]

    def do_export(self, out: str) -> dict:
        out_path = Path(out).expanduser().resolve()
        if out_path.suffix.lower() != ".docx":
            return {"ok": False, "message": "output file must end in .docx"}
        if not out_path.parent.is_dir():
            return {"ok": False, "message": "output folder does not exist"}
        try:
            report = export_docx(self.src, str(out_path), self.session, ocr=self.ocr,
                                 engine_config=self.engine_config)
        except ExportRefused as exc:
            return {"ok": False, "message": str(exc),
                    "blockers": self.session.blockers(),
                    "report": exc.report.to_dict() if exc.report else None}
        return {"ok": True, "out": str(out_path), "report": report.to_dict()}


def serve_docx(src: str, session: ReviewSession, decisions_path: str | None,
               port: int = 0, open_browser: bool = True,
               renderer: str | None = None,
               engine_config: EngineConfig | None = None, ocr: bool = True) -> None:
    """Serve the .docx review page until Ctrl+C; always releases the render."""
    app = DocxReviewApp.prepare(src, session, decisions_path, renderer=renderer,
                                engine_config=engine_config, ocr=ocr)
    httpd, token = make_server(app, port)
    url = f"http://127.0.0.1:{httpd.server_port}/?t={token}"
    print(f"Review page (local only, this run only): {url}", flush=True)
    if app.mode == "rendered":
        print(f"Review mode: rendered page ({app.renderer_kind}); the .docx is the source of "
              "truth, the page is a view.", flush=True)
    else:
        print("Review mode: text review (no page image).", flush=True)
        print(app.note, flush=True)
    print("Press Ctrl+C to stop. Decisions are autosaved"
          + (f" to {decisions_path}" if decisions_path else "") + ".", flush=True)
    try:
        if open_browser:
            webbrowser.open(url)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        app.close()
