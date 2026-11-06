"""DOCX images: OCR, pixel-burn redaction, independent verification, review integration.

CI has no Tesseract, Word or LibreOffice. The fake command engine in
`tests/fixtures/fake_ocr_engine.py` is the reader: it returns a word only when
the image region the word claims is not black, so "after burning, the engine no
longer reads the word" is a meaningful property rather than a mock.

`ocr_available` is patched off in the main fixture so the verifier's preferred
independent reader (Tesseract) does not make results machine-dependent; the
same-engine path and its PF130 acknowledgement are then exercised on every
machine. One test covers real Tesseract when it is installed and skips otherwise.
"""
import json
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

import legal_redact.docx.images as image_tools
import legal_redact.docx.review as docx_review
import legal_redact.docx.write as docx_write
from legal_redact.cli import main
from legal_redact.detect.base import Category
from legal_redact.docx.pipeline import export_docx, load_decisions_docx, suggest_docx
from legal_redact.docx.preflight import blocked, preflight_docx
from legal_redact.ocr import ocr_available
from legal_redact.ocr_backends import (CLOUD_OCR_ACK_LABEL, CLOUD_OCR_DISCLAIMER,
                                       resolve_config)
from legal_redact.pipeline import ExportRefused, save_decisions
from legal_redact.review import ReviewSession
from legal_redact.policy import Policy

from test_docx import (FakeDetector, make_docx, para, picture, raw_parts, rewrite,
                       run)

HERE = Path(__file__).parent
FAKE = HERE / "fixtures" / "fake_ocr_engine.py"
REVIEW_FIX = HERE / "fixtures" / "docx_review"
PY = sys.executable
IMAGE_W, IMAGE_H = 400, 200
POLICY = Policy("test", "redact everything", {category: "redact"
                                             for category in Category.ALL})


# ---- fixtures --------------------------------------------------------------
def _planted_image(directory: Path):
    """A 400x200 image with real text, plus the word boxes a reader would return."""
    document = pymupdf.open()
    page = document.new_page(width=IMAGE_W, height=IMAGE_H)
    page.insert_text((20, 60), "Alice Example", fontsize=28)
    page.insert_text((20, 140), "alice@example.test", fontsize=22)
    words = [{"text": item[4],
              "box": [item[0] / IMAGE_W, item[1] / IMAGE_H,
                      item[2] / IMAGE_W, item[3] / IMAGE_H],
              "confidence": 0.99}
             for item in page.get_text("words")]
    png = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).tobytes("png")
    words_path = directory / "words.json"
    words_path.write_text(json.dumps(words), encoding="utf-8")
    return png, words_path, words


def command_spec(words_path: Path, *, mode: str = "valid", local: str = "true",
                 marker: Path | None = None) -> str:
    parts = [f'"{PY}"', f'"{FAKE}"', "--words", f'"{words_path}"',
             "--mode", mode, "--local", local]
    if marker is not None:
        parts += ["--marker", f'"{marker}"']
    return " ".join(parts)


def engine_config(words_path: Path, **kwargs):
    local = kwargs.get("local", "true") == "true"
    return resolve_config(engine="command:" + command_spec(words_path, **kwargs),
                          local=local, allow_cloud=not local)


def _image_docx(directory: Path, png: bytes, *, name: str = "doc.docx",
                body_extra: str = "") -> Path:
    src = make_docx(directory, name=name,
                    body=para(run("Borrower: Alice Example.")) + para(picture())
                    + body_extra,
                    media=True)
    rewrite(src, {"word/media/image1.png": png})
    return src


def _solid_png(width: int = 20, height: int = 10) -> bytes:
    """A real, dark image: the fake reader reports no words in a dark region."""
    document = pymupdf.open()
    page = document.new_page(width=width, height=height)
    page.draw_rect(page.rect, color=None, fill=(0.0, 0.0, 0.0))
    return page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).tobytes("png")


@pytest.fixture
def image_case(tmp_path, monkeypatch):
    """One decodable image with two lines of text + a same-engine command config."""
    monkeypatch.setattr(image_tools, "ocr_available", lambda: False)
    png, words_path, words = _planted_image(tmp_path)
    src = _image_docx(tmp_path, png)
    config = engine_config(words_path)
    return SimpleNamespace(src=src, png=png, words_path=words_path, words=words,
                           config=config, tmp=tmp_path)


def _accept_all(session: ReviewSession) -> None:
    for index in range(len(session.decisions)):
        session.accept(index)


def _ack_reviews(session: ReviewSession) -> None:
    for finding in session.findings:
        if finding.severity.value == "review":
            session.acknowledge(finding.code)


def _suggest_once(case, config=None, needles=None, ocr=True) -> ReviewSession:
    detector = FakeDetector(needles or [("Alice Example", Category.PERSON),
                                        ("alice@example.test", Category.EMAIL)])
    return suggest_docx(str(case.src), detector, POLICY, ocr=ocr,
                        engine_config=config or case.config)


def _ready_session(case, config=None) -> ReviewSession:
    session = _suggest_once(case, config=config)
    _accept_all(session)
    _ack_reviews(session)
    return session


# ---- discovery / decoding ---------------------------------------------------
def test_scan_reports_native_pixels_and_decodes(image_case):
    from legal_redact.docx.package import read_package
    parts, errors = image_tools.scan_images(read_package(str(image_case.src)))
    assert errors == []
    assert len(parts) == 1
    assert (parts[0].width, parts[0].height) == (IMAGE_W, IMAGE_H)
    assert parts[0].extension == "png"


def test_image_ocr_uses_a_300dpi_geometry_convention(image_case):
    from legal_redact.docx.package import read_package
    readings, errors = image_tools.read_images(
        read_package(str(image_case.src)), image_case.config, page_offset=1)
    assert errors == []
    page = readings[0].page
    assert (page.width, page.height) == (IMAGE_W, IMAGE_H)      # pixel coordinates
    assert page.page == 1
    assert page.ocr is True
    assert page.text.split() == ["Alice", "Example", "alice@example.test"]
    for word in page.words:
        assert 0 <= word.rect.x0 < word.rect.x1 <= IMAGE_W
        assert 0 <= word.rect.y0 < word.rect.y1 <= IMAGE_H


def test_undecodable_image_is_refused(image_case):
    rewrite(image_case.src, {"word/media/image1.png": b"this is not an image"})
    findings = preflight_docx(str(image_case.src), engine_config=image_case.config)
    assert "PF127" in {finding.code for finding in findings}
    assert blocked(findings)


def test_oversized_image_is_refused(image_case, monkeypatch):
    monkeypatch.setattr(image_tools, "MAX_IMAGE_PIXELS", 100)
    findings = preflight_docx(str(image_case.src), engine_config=image_case.config)
    assert "PF127" in {finding.code for finding in findings}
    assert blocked(findings)


def test_unsupported_image_format_is_refused(image_case, tmp_path):
    png = image_case.png
    src = make_docx(tmp_path, name="extra.docx",
                    body=para(run("Body.")) + para(picture()), media=True,
                    extra_parts={"word/media/image2.webp": b"RIFF....WEBP"})
    findings = preflight_docx(str(src), engine_config=image_case.config)
    assert "PF127" in {finding.code for finding in findings}
    assert blocked(findings)


def test_engine_failure_is_refused(image_case, tmp_path):
    broken = engine_config(image_case.words_path, mode="crash")
    findings = preflight_docx(str(image_case.src), engine_config=broken)
    assert "PF128" in {finding.code for finding in findings}
    assert blocked(findings)


def test_no_ocr_keeps_the_old_fail_closed_rule(image_case):
    findings = preflight_docx(str(image_case.src), ocr=False)
    assert "PF113" in {finding.code for finding in findings}
    assert "PF123" not in {finding.code for finding in findings}   # small image only
    large = make_docx(image_case.tmp, name="large.docx",
                      body=para(run("Body.")) + para(picture(cx=6700000, cy=4800000)),
                      media=True)
    large_findings = preflight_docx(str(large), ocr=False)
    assert "PF123" in {finding.code for finding in large_findings}
    assert blocked(large_findings)


# ---- pre-flight findings ----------------------------------------------------
def test_preflight_reports_ocr_use_without_quoting_text(image_case):
    findings = {finding.code: finding for finding in
                preflight_docx(str(image_case.src), engine_config=image_case.config)}
    assert "PF113" in findings and findings["PF113"].severity.value == "review"
    assert "PF129" in findings
    message = findings["PF129"].message
    assert "3 word(s) found in 1 of 1 image(s)" in message
    assert "Alice" not in message and "alice@example.test" not in message
    assert "PF130" in findings                     # same-engine check acknowledged
    assert "PF019" not in findings                 # local engine: no cloud disclaimer


def test_cloud_engine_is_acknowledged_before_export(image_case):
    cloud = engine_config(image_case.words_path, local="false")
    cloud.check_gate()                              # test config approved the run
    findings = {finding.code: finding for finding in
                preflight_docx(str(image_case.src), engine_config=cloud)}
    assert "PF019" in findings
    assert CLOUD_OCR_DISCLAIMER in findings["PF019"].message


# ---- suggest / export -------------------------------------------------------
def test_suggest_reaches_image_containers_and_pins_the_engine(image_case):
    from legal_redact.docx.extract import extract_text
    from legal_redact.docx.package import read_package
    session = _suggest_once(image_case)
    text_count = len(extract_text(read_package(str(image_case.src))).containers)
    image_decisions = [decision for decision in session.decisions
                       if decision.page >= text_count]
    assert image_decisions                                # detector read the pixels
    assert session.ocr_info and session.ocr_info["engine"].startswith("command:")
    assert session.ocr_info["local"] is True
    assert session.can_export() is False                  # REVIEW findings unacked


def test_export_burns_pixels_and_verifier_passes(image_case):
    session = _ready_session(image_case)
    out = image_case.tmp / "out.docx"
    report = export_docx(str(image_case.src), str(out), session,
                         engine_config=image_case.config)
    assert report.passed
    by_id = {check.id: check for check in report.checks}
    for check_id in ("V109", "V110", "V111"):
        assert by_id[check_id].status == "PASS"
    assert "SAME-ENGINE" in by_id["V110"].detail
    with zipfile.ZipFile(out) as archive:
        media = [name for name in archive.namelist()
                 if name.startswith("word/media/")]
        assert len(media) == 1 and media[0].startswith("word/media/redacted_")
        payload = archive.read(media[0])
    decoded = image_tools.decode_image(payload)
    assert (decoded.width, decoded.height) == (IMAGE_W, IMAGE_H)
    for word in image_case.words:
        rect = (word["box"][0] * IMAGE_W, word["box"][1] * IMAGE_H,
                word["box"][2] * IMAGE_W, word["box"][3] * IMAGE_H)
        assert image_tools.bright_fraction(decoded, rect) == 0.0
    # the original image bytes do not survive anywhere in the package
    source_payload = raw_parts(image_case.src)["word/media/image1.png"]
    for payload_part in raw_parts(out).values():
        assert source_payload not in payload_part
    # and the engine re-reading the output finds no words
    result = image_case.config.engine().analyze(
        payload, IMAGE_W * 72 / 300, IMAGE_H * 72 / 300, 300)
    assert [word.text for word in result.words] == []
    # report names the engine (configuration only, never document text)
    assert report.ocr_engine["configured"]["engine"].startswith("command:")
    assert report.ocr_engine["independent"] is False


def test_decisions_and_report_carry_no_document_text(image_case):
    session = _ready_session(image_case)
    decisions_path = image_case.tmp / "decisions.json"
    save_decisions(session, str(decisions_path))
    saved = decisions_path.read_text(encoding="utf-8")
    for text in ("Alice", "alice@example.test", "Example"):
        assert text not in saved
    assert '"ocr"' in saved and "command:" in saved
    out = image_case.tmp / "out.docx"
    report = export_docx(str(image_case.src), str(out), session,
                         engine_config=image_case.config)
    as_text = json.dumps(report.to_dict())
    assert "Alice" not in as_text and "alice@example.test" not in as_text


def _two_image_docx(directory: Path, first: bytes, second: bytes,
                    *, name: str = "two.docx") -> Path:
    """A .docx with two referenced, equal-display images (rId40/rId41)."""
    src = make_docx(directory, name=name,
                    body=para(run("Borrower: Alice Example.")) + para(picture()),
                    media=True)
    parts = raw_parts(src)
    document = parts["word/document.xml"].decode("utf-8")
    second_drawing = picture().replace("rId40", "rId41").replace('id="1"', 'id="2"')
    document = document.replace("</w:body>", f"<w:p>{second_drawing}</w:p></w:body>")
    rels = parts["word/_rels/document.xml.rels"].decode("utf-8")
    rels = rels.replace(
        "</Relationships>",
        '<Relationship Id="rId41" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/image2.png"/></Relationships>')
    parts["word/document.xml"] = document.encode("utf-8")
    parts["word/_rels/document.xml.rels"] = rels.encode("utf-8")
    parts["word/media/image1.png"] = first
    parts["word/media/image2.png"] = second
    with zipfile.ZipFile(src, "w") as archive:
        for part_name, payload in parts.items():
            archive.writestr(part_name, payload)
    return src


def test_untouched_image_is_reencoded_pixel_identical_and_metadata_free(image_case):
    second = _solid_png()
    src = _two_image_docx(image_case.tmp, image_case.png, second, name="two.docx")
    detector = FakeDetector([("Alice Example", Category.PERSON),
                             ("alice@example.test", Category.EMAIL)])
    session = suggest_docx(str(src), detector, POLICY, engine_config=image_case.config)
    _accept_all(session)
    _ack_reviews(session)
    out = image_case.tmp / "out.docx"
    report = export_docx(str(src), str(out), session, engine_config=image_case.config)
    assert report.passed
    source_payloads = {name: payload for name, payload in raw_parts(src).items()
                       if name.startswith("word/media/")}
    out_payloads = {name: payload for name, payload in raw_parts(out).items()
                    if name.startswith("word/media/")}
    assert sorted(out_payloads) == ["word/media/redacted_1.png", "word/media/redacted_2.png"]
    for payload in source_payloads.values():
        assert all(payload not in out_payload for out_payload in out_payloads.values())
    first_out = image_tools.decode_image(out_payloads["word/media/redacted_1.png"])
    second_out = image_tools.decode_image(out_payloads["word/media/redacted_2.png"])
    first_src = image_tools.decode_image(source_payloads["word/media/image1.png"])
    second_src = image_tools.decode_image(second)
    assert bytes(second_out.samples) == bytes(second_src.samples)   # pixels preserved
    assert (first_out.width, first_out.height) == (first_src.width, first_src.height)
    for original in source_payloads.values():
        assert all(original not in part for part in raw_parts(out).values())


def test_export_refuses_when_ocr_is_disabled_for_image_decisions(image_case):
    session = _ready_session(image_case)
    with pytest.raises(ExportRefused) as caught:
        export_docx(str(image_case.src), str(image_case.tmp / "out.docx"), session,
                    ocr=False, engine_config=image_case.config)
    assert "image OCR is disabled" in str(caught.value)


def test_export_refuses_a_different_engine_than_the_decision_file(image_case):
    session = _ready_session(image_case)
    other = engine_config(image_case.words_path, marker=image_case.tmp / "m")
    with pytest.raises(ExportRefused) as caught:
        export_docx(str(image_case.src), str(image_case.tmp / "out.docx"), session,
                    engine_config=other)
    assert "different reader" in str(caught.value)


def test_decision_file_without_an_engine_pin_is_refused(image_case):
    session = _suggest_once(image_case)
    data = {"decisions": [], "acknowledged": []}
    from dataclasses import asdict
    data["decisions"] = [asdict(decision) for decision in session.decisions]
    decisions_path = image_case.tmp / "unpinned.json"
    decisions_path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_decisions_docx(str(image_case.src), str(decisions_path),
                                 engine_config=image_case.config)
    assert "PF022" in {finding.code for finding in loaded.findings}
    assert blocked(loaded.findings)


# ---- verifier mutations (must withhold the file) --------------------------
def test_no_burn_mutation_is_withheld(image_case, monkeypatch):
    monkeypatch.setattr(docx_write, "reencode_image", lambda data, rects=(): data)
    session = _ready_session(image_case)
    with pytest.raises(ExportRefused) as caught:
        export_docx(str(image_case.src), str(image_case.tmp / "out.docx"), session,
                    engine_config=image_case.config)
    report = caught.value.report
    assert report is not None
    assert {"V109", "V110", "V111"} <= {check.id for check in report.failed()}


def test_metadata_mutation_is_withheld(image_case, monkeypatch):
    def burnt_with_metadata(data, rects=()):
        return image_tools.reencode_image(data, rects) + b"\nExif\x00\x00metadata"

    monkeypatch.setattr(docx_write, "reencode_image", burnt_with_metadata)
    session = _ready_session(image_case)
    with pytest.raises(ExportRefused) as caught:
        export_docx(str(image_case.src), str(image_case.tmp / "out.docx"), session,
                    engine_config=image_case.config)
    assert "V111" in {check.id for check in caught.value.report.failed()}


# ---- review integration ----------------------------------------------------
def test_text_review_lists_image_identity_and_in_memory_context(image_case, monkeypatch):
    monkeypatch.setattr(docx_review, "detect_renderer", lambda: None)
    session = _suggest_once(image_case)
    app = docx_review.DocxReviewApp.prepare(str(image_case.src), session,
                                            engine_config=image_case.config)
    try:
        state = app.state()
        assert state["review_mode"] == "text"
        image_items = [item for item in state["decisions"] if item.get("image")]
        assert image_items
        item = image_items[0]
        assert item["where"].startswith("image 1 (")
        assert item["boxes"] == []
        assert "Alice" in item["context"]["text"]
        # identity is the index and size, never the source part name
        serialised = json.dumps(state)
        assert "image1.png" not in serialised
        assert state["ocr"]["local"] is True
        assert state["can_export"] is False
    finally:
        app.close()


def test_rendered_review_keeps_image_suggestions_unmapped_but_listed(image_case,
                                                                     monkeypatch):
    """The rendered page has boxes for text; image suggestions are listed, not guessed."""
    src = image_case.tmp / "contract_image.docx"
    shutil.copyfile(REVIEW_FIX / "contract.docx", src)
    parts = raw_parts(src)
    content_types = parts["[Content_Types].xml"].decode("utf-8").replace(
        "</Types>", '<Default Extension="png" ContentType="image/png"/></Types>')
    parts["word/media/image1.png"] = image_case.png
    parts["[Content_Types].xml"] = content_types.encode("utf-8")
    with zipfile.ZipFile(src, "w") as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)

    def fake_render(src_arg, out_path, renderer=None):
        shutil.copyfile(REVIEW_FIX / "contract.pdf", out_path)
        return Path(out_path)

    monkeypatch.setattr(docx_review, "render_docx_to_pdf", fake_render)
    detector = FakeDetector([("Alice Example", Category.PERSON),
                             ("alice@example.test", Category.EMAIL)])
    session = suggest_docx(str(src), detector, POLICY, engine_config=image_case.config)
    app = docx_review.DocxReviewApp.prepare(str(src), session, renderer="word",
                                            engine_config=image_case.config)
    try:
        state = app.state()
        assert state["review_mode"] == "rendered"
        text_items = [item for item in state["decisions"] if not item.get("image")]
        image_items = [item for item in state["decisions"] if item.get("image")]
        assert image_items
        assert all(item["boxes"] == [] and item["where"].startswith("image 1")
                   for item in image_items)
        assert text_items and all(item["boxes"] for item in text_items)
        assert "Alice Example" not in json.dumps(state)    # rendered API is text-free
    finally:
        app.close()


def test_cloud_engine_review_state_carries_the_disclaimer_and_ack_label(image_case,
                                                                        monkeypatch):
    monkeypatch.setattr(docx_review, "detect_renderer", lambda: None)
    cloud = engine_config(image_case.words_path, local="false")
    session = _suggest_once(image_case, config=cloud)
    app = docx_review.DocxReviewApp.prepare(str(image_case.src), session,
                                            engine_config=cloud)
    try:
        state = app.state()
        assert state["ocr"]["local"] is False
        assert state["ocr"]["disclaimer"] == CLOUD_OCR_DISCLAIMER
        pf019 = next(item for item in state["findings"] if item["code"] == "PF019")
        assert pf019["ack_label"] == CLOUD_OCR_ACK_LABEL
        assert state["can_export"] is False
    finally:
        app.close()


# ---- CLI plumb-through -----------------------------------------------------
def test_cli_inspect_reports_the_image_ocr_plan(image_case, capsys):
    spec = image_case.config.spec
    assert main(["inspect", str(image_case.src), "--ocr-engine", spec,
                 "--ocr-local"]) == 0
    out = capsys.readouterr().out
    assert "images=1" in out
    assert "image_ocr=available" in out
    assert "PF113" in out


def test_cli_inspect_no_ocr_keeps_the_old_image_rule(image_case, capsys):
    assert main(["inspect", str(image_case.src), "--no-ocr"]) == 0
    out = capsys.readouterr().out
    assert "image_ocr=" not in out
    assert "not read" in out and "check every image yourself" in out


def test_cli_suggest_and_redact_burn_the_image(image_case, capsys):
    decisions = image_case.tmp / "cli.decisions.json"
    out = image_case.tmp / "cli.redacted.docx"
    spec = image_case.config.spec
    assert main(["suggest", str(image_case.src), "-o", str(decisions),
                 "--detector", "rules", "--ocr-engine", spec, "--ocr-local"]) == 0
    data = json.loads(decisions.read_text(encoding="utf-8"))
    assert data["decisions"], "the rules detector sees the planted e-mail"
    for decision in data["decisions"]:
        decision["status"] = "accepted"
    decisions.write_text(json.dumps(data), encoding="utf-8")
    findings = preflight_docx(str(image_case.src), engine_config=image_case.config)
    acks = ",".join(finding.code for finding in findings
                    if finding.severity.value == "review")
    assert main(["redact", str(image_case.src), "--decisions", str(decisions),
                 "-o", str(out), "--ocr-engine", spec, "--ocr-local",
                 "--ack", acks]) == 0
    capsys.readouterr()
    assert out.exists()
    payload = next(data for name, data in raw_parts(out).items()
                   if name.startswith("word/media/"))
    decoded = image_tools.decode_image(payload)
    email_word = next(word for word in image_case.words
                      if word["text"] == "alice@example.test")
    rect = (email_word["box"][0] * IMAGE_W, email_word["box"][1] * IMAGE_H,
            email_word["box"][2] * IMAGE_W, email_word["box"][3] * IMAGE_H)
    assert image_tools.bright_fraction(decoded, rect) == 0.0


# ---- the real Tesseract path (skipped where it is not installed) -----------
@pytest.mark.skipif(not ocr_available(), reason="Tesseract is not installed here")
def test_real_tesseract_reads_and_verifies_the_image(tmp_path):
    from legal_redact.docx.extract import extract_text
    from legal_redact.docx.package import read_package
    png, _words_path, _words = _planted_image(tmp_path)
    src = _image_docx(tmp_path, png)
    detector = FakeDetector([("Alice Example", Category.PERSON),
                             ("alice@example.test", Category.EMAIL)])
    session = suggest_docx(str(src), detector, POLICY)
    containers = extract_text(read_package(str(src))).containers
    text_count = len(containers)
    assert any(decision.page >= text_count for decision in session.decisions), \
        "Tesseract did not read the planted words"
    for index in range(len(session.decisions)):
        session.accept(index)
    for finding in session.findings:
        if finding.severity.value == "review":
            session.acknowledge(finding.code)
    report = export_docx(str(src), str(tmp_path / "out.docx"), session)
    assert report.passed
    checks = {check.id: check for check in report.checks}
    assert checks["V109"].status == "PASS"
    assert checks["V110"].status == "PASS"
    assert report.ocr_engine["independent"] is True
