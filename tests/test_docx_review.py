"""Tests for the .docx review page: renderer detection, fail-closed alignment,
rendered review, manual-box reverse mapping and the text fallback.

The alignment tests use a committed, synthetic *pre-rendered* fixture
(`tests/fixtures/docx_review/`) so they run on CI, where neither Word nor
LibreOffice is installed. The review-server tests monkeypatch the renderer to
return that same PDF. Nothing here requires a renderer; one manual test in the
report exercises the real Word COM path on this machine.
"""
from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from legal_redact.docx.align import (Alignment, AlignmentError, ContainerTokens, DocxToken,
                                     PdfToken, align, tokenise)
from legal_redact.docx.extract import extract_text
from legal_redact.docx.package import read_package
from legal_redact.docx.pipeline import suggest_docx
from legal_redact.docx.render import (RenderError, RendererUnavailable, RenderTimeout,
                                      detect_renderer, render_docx_to_pdf, renderer_label,
                                      renderer_name)
from legal_redact.docx.review import DocxReviewApp
from legal_redact.detect.base import Category
from legal_redact.policy import Policy
from legal_redact.review_server import make_server

FIXTURE = Path(__file__).parent / "fixtures" / "docx_review"
DOCX = FIXTURE / "contract.docx"
PDF = FIXTURE / "contract.pdf"

NEEDLES = [
    ("Alice Example", Category.PERSON),
    ("alice@example.test", Category.EMAIL),
    ("+1 555 0100", Category.PHONE),
    ("GB53 PSCR 5001 9062 0132 78", Category.IBAN),
    ("12 May 1985", Category.DOB),
    ("82 Station Lane, Northvale", Category.ADDRESS),
    ("Bob Sample", Category.PERSON),
]


# ---- helpers ---------------------------------------------------------------
def _fake_detector():
    from test_docx import FakeDetector
    return FakeDetector


def _policy() -> Policy:
    return Policy("test", "redact everything", {category: "redact" for category in Category.ALL})


def fixture_session(*, accept: bool = False, ack: bool = False):
    session = suggest_docx(str(DOCX), _fake_detector()(NEEDLES), _policy())
    if accept:
        for decision in session.decisions:
            decision.status = "accepted"
    if ack:
        for finding in session.findings:
            session.acknowledge(finding.code)
    return session


@pytest.fixture(scope="module")
def contract():
    assert DOCX.is_file() and PDF.is_file(), "committed docx review fixture is missing"
    return DOCX, PDF


@pytest.fixture(scope="module")
def fixture_alignment(contract):
    return align(DOCX, PDF)


@pytest.fixture
def fake_renderer(monkeypatch, contract):
    """Stand in for Word/LibreOffice: the renderer 'produces' the committed PDF."""
    _, pdf = contract

    def fake_render(src, out, renderer=None):
        shutil.copyfile(pdf, out)
        return Path(out)

    monkeypatch.setattr("legal_redact.docx.review.render_docx_to_pdf", fake_render)
    return fake_render


@contextmanager
def review_server(app):
    httpd, token = make_server(app, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", token
    finally:
        httpd.shutdown()
        httpd.server_close()


def call(base, token, path, body=None):
    headers = {"X-Review-Token": token}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = exc.code
    try:
        return status, json.loads(payload or b"{}"), payload
    except ValueError:
        return status, {"bytes": len(payload)}, payload


def state(base, token):
    return call(base, token, "/api/state")[1]


def accept_and_ack(base, token):
    for decision in state(base, token)["decisions"]:
        if decision["status"] == "pending":
            assert call(base, token, "/api/decide",
                        {"i": decision["i"], "status": "accepted"})[0] == 200
    for finding in state(base, token)["findings"]:
        if finding["severity"] == "review" and not finding["ack"]:
            assert call(base, token, "/api/ack", {"code": finding["code"], "on": True})[0] == 200


def docx_text(path) -> str:
    """All decompressed bytes as one string (a crude leak check, like the auditor's)."""
    import zipfile
    with zipfile.ZipFile(path) as archive:
        return b"\n".join(archive.read(name) for name in archive.namelist()).decode(
            "utf-8", "replace")


# ---- renderer detection ----------------------------------------------------
def test_detect_renderer_prefers_word_then_libreoffice(monkeypatch):
    import legal_redact.docx.render as render
    monkeypatch.delenv("LEGAL_REDACT_RENDERER", raising=False)
    monkeypatch.setattr(render, "_word_available", lambda: True)
    monkeypatch.setattr(render, "_libreoffice_path", lambda: "/usr/bin/soffice")
    assert detect_renderer() == "word"
    monkeypatch.setattr(render, "_word_available", lambda: False)
    assert detect_renderer() == "libreoffice"
    monkeypatch.setattr(render, "_libreoffice_path", lambda: None)
    assert detect_renderer() is None
    monkeypatch.setattr(render, "_word_available", lambda: True)
    monkeypatch.setenv("LEGAL_REDACT_RENDERER", "none")
    assert detect_renderer() is None
    monkeypatch.setenv("LEGAL_REDACT_RENDERER", "libreoffice")
    assert detect_renderer() is None


def test_renderer_labels_and_names():
    assert renderer_label("word") == "rendered (word)"
    assert renderer_label("libreoffice") == "rendered (libreoffice)"
    assert renderer_label(None) == "text"
    assert renderer_name("word") == "Microsoft Word"
    assert renderer_name("libreoffice") == "LibreOffice"
    assert renderer_name(None) == "no renderer"


def test_libreoffice_render_uses_a_unique_profile_and_cleans_it(monkeypatch, tmp_path):
    import legal_redact.docx.render as render
    captured = {}

    def fake_run(cmd, timeout):
        captured["cmd"] = cmd
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / (Path(cmd[-1]).stem + ".pdf")).write_bytes(b"%PDF-1.4 fake")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(render, "_libreoffice_path", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(render, "_run_process", fake_run)
    monkeypatch.setattr(render, "_validate_pdf", lambda path: None)
    src = tmp_path / "in.docx"
    src.write_bytes(b"PK not really a zip")
    out = tmp_path / "out.pdf"
    result = render_docx_to_pdf(src, out, renderer="libreoffice")
    assert result == out.resolve()
    assert out.read_bytes() == b"%PDF-1.4 fake"
    command = captured["cmd"]
    profile_uri = next(part for part in command if part.startswith("-env:UserInstallation="))
    profile = Path(profile_uri.split("=file:///", 1)[1])
    assert "legal-redact-lo-profile" in str(profile)
    assert not profile.exists()                 # temporary profile removed


def test_libreoffice_failure_is_a_render_error(monkeypatch, tmp_path):
    import legal_redact.docx.render as render
    monkeypatch.setattr(render, "_libreoffice_path", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(render, "_run_process",
                        lambda cmd, timeout: SimpleNamespace(returncode=1, stdout="", stderr=""))
    src = tmp_path / "in.docx"
    src.write_bytes(b"x")
    with pytest.raises(RenderError):
        render_docx_to_pdf(src, tmp_path / "out.pdf", renderer="libreoffice")
    assert not (tmp_path / "out.pdf").exists()


def test_render_timeout_and_input_validation(monkeypatch, tmp_path):
    import legal_redact.docx.render as render

    def timeout(cmd, timeout):
        raise RenderTimeout("too slow")

    monkeypatch.setattr(render, "_libreoffice_path", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(render, "_run_process", timeout)
    src = tmp_path / "in.docx"
    src.write_bytes(b"x")
    with pytest.raises(RenderTimeout):
        render_docx_to_pdf(src, tmp_path / "out.pdf", renderer="libreoffice")
    with pytest.raises(RenderError):
        render_docx_to_pdf(tmp_path / "missing.docx", tmp_path / "out.pdf",
                           renderer="libreoffice")
    with pytest.raises(RenderError):
        render_docx_to_pdf(src, src, renderer="libreoffice")


def test_no_renderer_is_unavailable_not_an_error(monkeypatch, tmp_path):
    import legal_redact.docx.render as render
    monkeypatch.setattr(render, "detect_renderer", lambda: None)
    src = tmp_path / "in.docx"
    src.write_bytes(b"x")
    with pytest.raises(RendererUnavailable):
        render_docx_to_pdf(src, tmp_path / "out.pdf")


# ---- alignment -------------------------------------------------------------
def test_alignment_on_the_committed_render_is_trustworthy(contract, fixture_alignment):
    alignment = fixture_alignment
    assert alignment.ok and alignment.global_ratio >= 0.90
    assert alignment.page_count == 2
    containers = extract_text(read_package(DOCX)).containers
    for index, tokens in enumerate(alignment.container_tokens):
        assert len(tokens.mapping) == len(tokens.tokens), (index, containers[index].part)


def test_alignment_maps_suggestions_to_rendered_words(fixture_alignment):
    alignment = fixture_alignment
    containers = extract_text(read_package(DOCX)).containers
    first_page_height = alignment.sizes[0][1]
    assert [c.part for c in containers][0].endswith("document.xml")

    def find_spans(needle):
        found = []
        for index, container in enumerate(containers):
            start = container.text.find(needle)
            while start != -1:
                found.append((index, start, start + len(needle)))
                start = container.text.find(needle, start + 1)
        return found

    body_hits = find_spans("Alice Example")
    assert len(body_hits) >= 3                     # body, table and floating text box
    first = alignment.map_span(*body_hits[0])
    assert first is not None and first.boxes
    assert first.boxes[0].page == 0
    for hit in body_hits:
        assert alignment.map_span(*hit) is not None

    bob = find_spans("Bob Sample")[0]
    mapped = alignment.map_span(*bob)
    assert mapped is not None and mapped.boxes[0].page == 1      # signature is on page 2

    header_index = next(i for i, c in enumerate(containers) if c.part.endswith("header1.xml"))
    header_text = containers[header_index].text
    header = alignment.map_span(header_index, header_text.find("Harley"),
                                header_text.find("Harley") + len("Harley"))
    assert header is not None and header.boxes[0].page == 0
    assert header.boxes[0].rect[1] < first_page_height * 0.2    # header sits at the top

    footer_index = next(i for i, c in enumerate(containers) if c.part.endswith("footer1.xml"))
    footer_text = containers[footer_index].text
    footer = alignment.map_span(footer_index, footer_text.find("Harley"),
                                footer_text.find("Harley") + len("Harley"))
    assert footer is not None and footer.boxes[0].page == 0
    assert footer.boxes[0].rect[1] > first_page_height * 0.7    # footer sits at the bottom

    textbox_index = next(i for i, c in enumerate(containers)
                         if i > 0 and c.part.endswith("document.xml") and "Floating" in c.text)
    note = alignment.map_span(textbox_index, 0, len("Floating note"))
    assert note is not None and note.boxes

    # word identity: the box really covers the rendered e-mail token
    email_hit = find_spans("alice@example.test")[0]
    email_map = alignment.map_span(*email_hit)
    assert email_map is not None
    box = email_map.boxes[0]
    token = next(t for t in alignment.pdf_tokens
                 if t.page == box.page and t.norm == "alice@example.test")
    x0 = max(box.rect[0], token.rect[0])
    y0 = max(box.rect[1], token.rect[1])
    x1 = min(box.rect[2], token.rect[2])
    y1 = min(box.rect[3], token.rect[3])
    assert x1 > x0 and y1 > y0


def test_alignment_reverse_rect_round_trips(fixture_alignment):
    alignment = fixture_alignment
    containers = extract_text(read_package(DOCX)).containers
    text = containers[0].text
    start = text.find("Alice Example")
    forward = alignment.map_span(0, start, start + len("Alice Example"))
    assert forward is not None and forward.boxes
    box = forward.boxes[0]
    container, span_start, span_end = alignment.reverse_rect(box.page, box.rect)
    assert container == 0
    assert "Alice Example" in containers[container].text[span_start:span_end]
    again = alignment.map_span(container, span_start, span_end)
    assert again is not None and again.boxes


def test_alignment_rejects_partial_empty_and_unmapped_boxes(fixture_alignment):
    alignment = fixture_alignment
    containers = extract_text(read_package(DOCX)).containers
    start = containers[0].text.find("Alice Example")
    box = alignment.map_span(0, start, start + len("Alice Example")).boxes[0]
    x0, y0, x1, y1 = box.rect
    with pytest.raises(AlignmentError):
        alignment.reverse_rect(box.page, (x0 + (x1 - x0) * 0.3, y0, x1, y1))
    with pytest.raises(AlignmentError):
        alignment.reverse_rect(box.page, (2.0, 2.0, 30.0, 20.0))
    with pytest.raises(AlignmentError):
        alignment.reverse_rect(99, box.rect)
    with pytest.raises(AlignmentError):
        alignment.reverse_rect(box.page, (x1, y1, x0, y0))


def _fabricated_alignment() -> Alignment:
    pdf_tokens = [PdfToken(0, (0.0, 0.0, 10.0, 10.0), 0, "hello", "hello"),
                  PdfToken(0, (11.0, 0.0, 20.0, 10.0), 0, "42", "42")]
    containers = [SimpleNamespace(text="hello")]
    token_lists = [ContainerTokens(tokens=(DocxToken(0, 5, "hello"),), mapping={0: 0})]
    return Alignment([(100.0, 100.0)], pdf_tokens, containers, token_lists, [{0: 0}])


def test_alignment_rejects_a_box_over_rendered_only_text():
    alignment = _fabricated_alignment()
    with pytest.raises(AlignmentError) as caught:
        alignment.reverse_rect(0, (0.0, 0.0, 25.0, 12.0))
    assert "not part of the .docx text model" in str(caught.value)


def test_alignment_without_neighbour_context_fails_closed():
    pdf_tokens = [PdfToken(0, (0.0, 0.0, 10.0, 10.0), 0, "name", "name")]
    containers = [SimpleNamespace(text="alpha name omega")]
    tokens = (DocxToken(0, 5, "alpha"), DocxToken(6, 10, "name"), DocxToken(11, 16, "omega"))
    alignment = Alignment([(100.0, 100.0)], pdf_tokens, containers,
                          [ContainerTokens(tokens=tokens, mapping={1: 0})], [{1: 0}])
    assert alignment.map_span(0, 6, 10) is None
    assert not alignment.ok


def test_alignment_fails_closed_when_the_document_text_does_not_match(contract):
    from test_docx import make_docx, para, run
    import tempfile

    scratch = Path(tempfile.mkdtemp())
    try:
        other = make_docx(scratch, body=para(run("Completely different words appear here.")))
        alignment = align(other, PDF)
        assert not alignment.ok
        assert alignment.map_span(0, 0, len("Completely different words appear here.")) is None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_tokenise_keeps_offsets_and_normalises():
    text = "Alice  EXAMPLE,\n1985"
    tokens = tokenise(text)
    assert [text[t.start:t.end] for t in tokens] == ["Alice", "EXAMPLE,", "1985"]
    assert [t.norm for t in tokens] == ["alice", "example,", "1985"]


# ---- rendered review app ---------------------------------------------------
def test_rendered_state_has_pages_and_boxes_but_no_document_text(fake_renderer, tmp_path):
    app = DocxReviewApp.prepare(str(DOCX), fixture_session(), str(tmp_path / "dec.json"),
                                renderer="word")
    try:
        assert app.mode == "rendered"
        review_state = app.state()
        assert review_state["review_mode"] == "rendered"
        assert len(review_state["pages"]) == 2
        assert review_state["manual_boxes"] is True
        assert review_state["repeats_available"] is False
        assert all(decision["boxes"] for decision in review_state["decisions"])
        assert no_document_text(review_state, ["Alice Example", "alice@example.test",
                                               "+1 555 0100", "Bob Sample"])
        with review_server(app) as (base, token):
            live = state(base, token)
            assert no_document_text(live, ["Alice Example", "alice@example.test"])
            code, _, payload = call(base, token, "/page/0.png")
            assert code == 200 and payload[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        app.close()


def no_document_text(payload, needles) -> bool:
    rendered = json.dumps(payload)
    return all(needle not in rendered for needle in needles)


def test_rendered_review_round_trip_exports_a_verified_docx(fake_renderer, tmp_path):
    session = fixture_session()
    app = DocxReviewApp.prepare(str(DOCX), session, str(tmp_path / "dec.json"),
                                renderer="word")
    out = tmp_path / "out.docx"
    try:
        with review_server(app) as (base, token):
            accept_and_ack(base, token)
            assert state(base, token)["can_export"] is True
            code, body, _ = call(base, token, "/api/export", {"out": str(out)})
        assert code == 200 and body["ok"], body.get("message")
        report = body["report"]
        assert report["passed"]
        checks = {check["id"]: check["status"] for check in report["checks"]}
        for check_id in ("V101", "V102", "V103", "V104", "V105", "V106", "V107", "V108"):
            assert checks[check_id] == "PASS", (check_id, checks)
        assert out.exists()
        output = docx_text(out)
        for needle, _category in NEEDLES:
            assert needle not in output
        assert "\u2588" in output
        saved = json.loads((tmp_path / "dec.json").read_text(encoding="utf-8"))
        assert all(needle not in json.dumps(saved) for needle, _ in NEEDLES)
        rendered = docx_module().Document(str(out))
        assert any("\u2588" in paragraph.text for paragraph in rendered.paragraphs)
    finally:
        app.close()


def docx_module():
    return pytest.importorskip("docx")


def test_manual_box_reverse_maps_to_a_docx_span_and_exports(fake_renderer, tmp_path):
    app = DocxReviewApp.prepare(str(DOCX), fixture_session(), str(tmp_path / "dec.json"),
                                renderer="word")
    out = tmp_path / "out.docx"
    try:
        assert app.mode == "rendered"
        alignment = app.alignment
        containers = app.containers
        counts = Counter(token.norm for tokens in alignment.container_tokens
                         for token in tokens.tokens)
        assert counts["nova"] == 1                       # appears once in the whole document
        nova = next(token for token in alignment.pdf_tokens if token.norm == "nova")
        rect = (nova.rect[0] - 1.0, nova.rect[1] - 1.0, nova.rect[2] + 1.0, nova.rect[3] + 1.0)
        with review_server(app) as (base, token):
            code, body, _ = call(base, token, "/api/add",
                                 {"page": nova.page, "rect": list(rect)})
            assert code == 200
            manual = [d for d in body["state"]["decisions"] if d["source"] == "manual"]
            assert len(manual) == 1 and manual[0]["boxes"]
            # a box that cuts through a word is refused, not guessed
            partial = (nova.rect[0] + 3.0, nova.rect[1], nova.rect[2], nova.rect[3])
            code, body, _ = call(base, token, "/api/add",
                                 {"page": nova.page, "rect": list(partial)})
            assert code == 400 and "partly covers" in body["error"]
            code, body, _ = call(base, token, "/api/add",
                                 {"page": nova.page, "rect": [2.0, 2.0, 20.0, 20.0]})
            assert code == 400 and "does not cover any text" in body["error"]
            accept_and_ack(base, token)
            code, body, _ = call(base, token, "/api/export", {"out": str(out)})
        assert code == 200 and body["ok"], body.get("message")
        assert body["report"]["passed"]
        output = docx_text(out)
        assert "Nova" not in output and "\u2588" in output
    finally:
        app.close()


def test_rendered_review_blocks_export_until_findings_are_acknowledged(fake_renderer, tmp_path):
    out = tmp_path / "out.docx"
    app = DocxReviewApp.prepare(str(DOCX), fixture_session(accept=True),
                                str(tmp_path / "dec.json"), renderer="word")
    try:
        with review_server(app) as (base, token):
            code, body, _ = call(base, token, "/api/export", {"out": str(out)})
            assert code == 200 and body["ok"] is False and body["blockers"]
            assert not out.exists()
            assert call(base, token, "/api/repeats", {})[0] == 400
    finally:
        app.close()


def test_export_path_is_validated(fake_renderer, tmp_path):
    app = DocxReviewApp.prepare(str(DOCX), fixture_session(accept=True, ack=True),
                                str(tmp_path / "dec.json"), renderer="word")
    try:
        assert app.do_export(str(tmp_path / "out.pdf"))["ok"] is False
        assert app.do_export(str(tmp_path / "missing" / "out.docx"))["ok"] is False
    finally:
        app.close()


# ---- text fallback ---------------------------------------------------------
def test_text_fallback_without_a_renderer(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGAL_REDACT_RENDERER", "none")
    app = DocxReviewApp.prepare(str(DOCX), fixture_session(),
                                str(tmp_path / "dec.json"))
    out = tmp_path / "out.docx"
    try:
        assert app.mode == "text"
        assert "no microsoft word or libreoffice" in app.note.lower()
        review_state = app.state()
        assert review_state["review_mode"] == "text"
        assert review_state["pages"] == []
        assert review_state["manual_boxes"] is False
        assert all(decision["boxes"] == [] for decision in review_state["decisions"])
        contexts = [decision.get("context") for decision in review_state["decisions"]]
        assert any(context and context.get("text") for context in contexts)
        excerpt = next(context for context in contexts if context and context.get("text"))
        assert excerpt["text"] in json.dumps(review_state)      # UI only, in memory
        with review_server(app) as (base, token):
            assert call(base, token, "/api/add",
                        {"page": 0, "rect": [1, 1, 20, 20]})[0] == 400
            code, body, _ = call(base, token, "/api/export", {"out": str(out)})
            assert body["ok"] is False and body["blockers"]
            accept_and_ack(base, token)
            code, body, _ = call(base, token, "/api/export", {"out": str(out)})
        assert code == 200 and body["ok"], body.get("message")
        assert body["report"]["passed"]
        assert out.exists()
        assert "Alice Example" not in docx_text(out)
        saved = (tmp_path / "dec.json").read_text(encoding="utf-8")
        for needle, _ in NEEDLES:
            assert needle not in saved
        # the context excerpt is UI-only: it is not written into the decisions file
        assert "Consultant's contact details are" not in saved
    finally:
        app.close()


def test_text_mode_when_alignment_is_not_trustworthy(monkeypatch, tmp_path, fake_renderer):
    import legal_redact.docx.review as review
    monkeypatch.setattr(review, "align",
                        lambda src, pdf: Alignment([(600.0, 800.0)], [], [], [], [],
                                                   global_ratio=0.25))
    app = DocxReviewApp.prepare(str(DOCX), fixture_session(), str(tmp_path / "dec.json"),
                                renderer="word")
    try:
        assert app.mode == "text"
        assert "could be located" in app.note
        assert app._tmpdir is None                       # temporary render removed
    finally:
        app.close()


def test_text_mode_when_one_suggestion_cannot_be_placed(monkeypatch, tmp_path, fake_renderer):
    import legal_redact.docx.review as review

    class FakeAlignment:
        ok = True
        global_ratio = 1.0

        def map_decision(self, decision):
            return None

    monkeypatch.setattr(review, "align", lambda src, pdf: FakeAlignment())
    app = DocxReviewApp.prepare(str(DOCX), fixture_session(), str(tmp_path / "dec.json"),
                                renderer="word")
    try:
        assert app.mode == "text"
        assert "suggestion(s) could not be located" in app.note
    finally:
        app.close()


def test_blocked_preflight_skips_rendering(monkeypatch, tmp_path):
    from test_docx import make_docx, para, run
    src = make_docx(tmp_path, body='<w:p><w:ins w:id="1" w:author="A">'
                                    + run("Alice Example") + "</w:ins></w:p>")
    calls = []

    def must_not_render(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a refused file must not be rendered")

    import legal_redact.docx.review as review
    monkeypatch.setattr(review, "render_docx_to_pdf", must_not_render)
    session = suggest_docx(str(src), _fake_detector()([("Alice Example", Category.PERSON)]),
                           _policy())
    app = DocxReviewApp.prepare(str(src), session, str(tmp_path / "dec.json"), renderer="word")
    try:
        assert app.mode == "text"
        assert "pre-flight refuses" in app.note.lower()
        assert calls == []
    finally:
        app.close()


# ---- CLI wiring ------------------------------------------------------------
def test_cli_review_docx_dispatches_to_the_docx_reviewer(tmp_path, monkeypatch, capsys):
    from test_docx import make_docx, para, run
    from legal_redact.cli import main
    import legal_redact.docx.review as review

    src = make_docx(tmp_path, body=para(run("Email alice@example.test here.")))
    decisions = tmp_path / "doc.decisions.json"
    captured = {}

    def fake_serve(src_arg, session, decisions_path, port=0, open_browser=True, renderer=None,
                   engine_config=None, ocr=True):
        captured.update(src=str(src_arg), session=session, decisions=str(decisions_path),
                        port=port, open_browser=open_browser)

    monkeypatch.setattr(review, "serve_docx", fake_serve)
    assert main(["review", str(src), "--detector", "rules", "--no-browser",
                 "--decisions", str(decisions)]) == 0
    assert captured["src"] == str(Path(src).resolve()) or captured["src"] == str(src)
    assert captured["session"].decisions
    assert Path(captured["decisions"]).exists()
    saved = Path(captured["decisions"]).read_text(encoding="utf-8")
    assert "alice@example.test" not in saved


def test_cli_inspect_reports_review_page_mode(tmp_path, monkeypatch, capsys):
    from test_docx import make_docx
    from legal_redact.cli import main
    import legal_redact.docx.render as render

    src = make_docx(tmp_path, body="<w:p><w:r><w:t>Plain text.</w:t></w:r></w:p>")
    monkeypatch.setattr(render, "detect_renderer", lambda: "libreoffice")
    assert main(["inspect", str(src)]) == 0
    assert "review_page=rendered (libreoffice)" in capsys.readouterr().out
    monkeypatch.setattr(render, "detect_renderer", lambda: None)
    assert main(["inspect", str(src)]) == 0
    assert "review_page=text" in capsys.readouterr().out


def test_review_page_html_supports_both_modes():
    html = (Path(__file__).parents[1] / "src" / "legal_redact" / "web" / "review.html"
            ).read_text(encoding="utf-8")
    for marker in ("review_mode", "modebanner", "d.boxes", "d.context", "manual_boxes",
                   "repeats_available", "isTextMode", "V102"):
        assert marker in html
