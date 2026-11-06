"""Regression tests for the v0.5 independent-audit findings F1-F5 (image path).

Fixtures I03/I06/I18/I19/I20/I21/I22/I23 are copied from the auditor's synthetic
corpus (`_legalredact_publish/docx_audit_v05/fixtures`, generated with Pillow,
synthetic canaries only). Tests that need OCR use the shipped fake command engine
so CI needs no Tesseract, Word or LibreOffice.

Covered:
- F2/F5: multi-frame GIF / APNG / animated WebP / multi-page TIFF are refused before
  any decode-and-copy; a single-frame GIF is accepted and normalised to PNG.
- F3: zero-word large images are refused; zero-word small and low-resolution images
  require an explicit acknowledgement; a dishonest engine whose word boxes sit on
  blank pixels is refused; an accepted image passage readable in another image is
  refused at export (source cross-check) and by V110 (all output images).
- F1: two same-size redacted images pair by the writer's canonical rename order and
  export PASS; a tampered output image count fails closed.
- F4: every image is re-encoded metadata-free; untouched images keep their pixels.
"""
import json
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

import pymupdf
import pytest

import legal_redact.docx.images as image_tools
from legal_redact.cli import main
from legal_redact.detect.base import Category
from legal_redact.docx.extract import extract_text
from legal_redact.docx.package import read_package
from legal_redact.docx.pipeline import export_docx, suggest_docx
from legal_redact.docx.preflight import blocked, preflight_docx
from legal_redact.docx.verify import verify_docx
from legal_redact.pipeline import ExportRefused
from legal_redact.review import ReviewSession
from test_docx import FakeDetector, make_docx, para, picture, raw_parts, rewrite, run
from test_docx_images import (_solid_png, _two_image_docx, engine_config)

HERE = Path(__file__).parent
AUDIT = HERE / "fixtures" / "docx_audit"
POLICY = __import__("legal_redact.policy", fromlist=["Policy"]).Policy(
    "test", "redact everything", {category: "redact" for category in Category.ALL})
PY = sys.executable
FAKE = HERE / "fixtures" / "fake_ocr_engine.py"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ---- helpers ---------------------------------------------------------------
def _marked_image(fill, directory: Path, *, name: str = "words.json"):
    """A 400x200 image with two text lines and a 20x20 colour marker in the corner."""
    document = pymupdf.open()
    page = document.new_page(width=400, height=200)
    page.insert_text((20, 60), "Alice Example", fontsize=28)
    page.insert_text((20, 140), "alice@example.test", fontsize=22)
    page.draw_rect(pymupdf.Rect(0, 0, 20, 20), color=None, fill=fill)
    words = [{"text": item[4],
              "box": [item[0] / 400, item[1] / 200, item[2] / 400, item[3] / 200],
              "confidence": 0.99}
             for item in page.get_text("words")]
    png = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).tobytes("png")
    words_path = directory / name
    words_path.write_text(json.dumps(words), encoding="utf-8")
    return png, words_path, words


def _blank_image(width: int = 400, height: int = 300) -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=width, height=height)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    return page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).tobytes("png")


def _png_with_chunk(png: bytes, chunk_type: bytes, payload: bytes) -> bytes:
    """Insert one PNG chunk before the first IDAT (CRC-correct)."""
    body = bytearray(PNG_SIGNATURE)
    position = 8
    inserted = False
    while position + 8 <= len(png):
        length = int.from_bytes(png[position:position + 4], "big")
        kind = png[position + 4:position + 8]
        end = position + 8 + length + 4
        chunk = png[position:end]
        if kind == b"IDAT" and not inserted:
            crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
            body += len(payload).to_bytes(4, "big") + chunk_type + payload + crc.to_bytes(4, "big")
            inserted = True
        body += chunk
        position = end
        if kind == b"IEND":
            break
    return bytes(body)


def _fake_engine(words: Path | None, *, mode: str = "valid"):
    if words is None:
        words = Path(tempfile.mkdtemp()) / "empty.json"
    if not words.exists():
        words.write_text("[]", encoding="utf-8")
    spec = " ".join([f'"{PY}"', f'"{FAKE}"', "--words", f'"{words}"',
                     "--mode", mode, "--local", "true"])
    from legal_redact.ocr_backends import resolve_config
    return resolve_config(engine="command:" + spec, local=True)


def _accept_all(session) -> None:
    for index in range(len(session.decisions)):
        session.accept(index)


def _ack_reviews(session) -> None:
    for finding in session.findings:
        if finding.severity.value == "review":
            session.acknowledge(finding.code)


def _session_for_export(src: Path, findings, config) -> ReviewSession:
    session = ReviewSession([], findings)
    session.ocr_info = {"engine": config.spec, "version": "1.0", "local": True}
    return session


# ---- F2 / F5: multi-frame containers --------------------------------------
def _gif_bytes(frames: int) -> bytes:
    data = bytearray(b"GIF89a")
    data += (10).to_bytes(2, "little") + (10).to_bytes(2, "little")
    data += bytes([0x80, 0x00, 0x00])                  # global colour table, 2 colours
    data += bytes([0, 0, 0, 255, 255, 255])
    for _ in range(frames):
        data += b"\x2c"                                # image descriptor
        data += (0).to_bytes(2, "little") + (0).to_bytes(2, "little")
        data += (10).to_bytes(2, "little") + (10).to_bytes(2, "little")
        data += bytes([0x00])                          # no local colour table
        data += b"\x02\x01\x00\x00"                    # LZW min + one-byte sub-block + terminator
    data += b"\x3b"                                    # trailer
    return bytes(data)


def test_gif_frame_parser_accepts_single_and_refuses_multiple():
    assert image_tools.frame_count_issue(_gif_bytes(1), "gif") is None
    reason = image_tools.frame_count_issue(_gif_bytes(2), "gif")
    assert reason and "animated" in reason
    with pytest.raises(image_tools.ImageError):
        image_tools.frame_count_issue(b"GIF89a\x01", "gif")


def test_apng_chunk_is_refused_and_plain_png_accepted():
    plain = _blank_image(40, 20)
    assert image_tools.frame_count_issue(plain, "png") is None
    apng = _png_with_chunk(plain, b"acTL", b"\x00\x00\x00\x02\x00\x00\x00\x00")
    reason = image_tools.frame_count_issue(apng, "png")
    assert reason and "animated PNG" in reason
    with pytest.raises(image_tools.ImageError):
        image_tools.frame_count_issue(b"\x89PNG\r\n\x1a\nbroken", "png")


def test_animated_webp_is_refused_and_malformed_webp_fails_closed():
    animated = (b"RIFF" + (30).to_bytes(4, "little") + b"WEBP"
                + b"ANIM" + (6).to_bytes(4, "little") + b"\x00" * 6)
    reason = image_tools.frame_count_issue(animated, "webp")
    assert reason and "WebP is animated" in reason
    with pytest.raises(image_tools.ImageError):
        image_tools.frame_count_issue(b"RIFF\x00\x00WEBP", "webp")


@pytest.mark.parametrize("name", ["I06_animated_gif.docx", "I18_animated_gif_pii_frame2.docx",
                                  "I20_animated_png_pii_frame2.docx",
                                  "I23_gif_pii_first_frame.docx"])
def test_audit_multiframe_fixtures_are_refused_with_no_output(name, tmp_path):
    src = AUDIT / name
    findings = preflight_docx(str(src))
    assert "PF127" in {finding.code for finding in findings}
    assert blocked(findings)
    session = _session_for_export(src, findings, _fake_engine(None))
    out = tmp_path / "out.docx"
    with pytest.raises(ExportRefused):
        export_docx(str(src), str(out), session, engine_config=_fake_engine(None))
    assert not out.exists()
    assert main(["inspect", str(src)]) == 1


def test_a_single_frame_gif_is_accepted_and_can_be_exported():
    src = AUDIT / "I19_single_frame_gif.docx"
    parts, errors = image_tools.scan_images(read_package(str(src)))
    assert errors == [] and len(parts) == 1 and parts[0].extension == "gif"
    config = _fake_engine(None)                       # zero words: small image -> review ack
    findings = preflight_docx(str(src), engine_config=config)
    assert "PF127" not in {finding.code for finding in findings}
    assert "PF131" in {finding.code for finding in findings}


# ---- F3: unread images, ink evidence, cross-image leaks -------------------
def test_zero_word_large_image_is_refused(tmp_path):
    src = make_docx(tmp_path, name="large_blank.docx",
                    body=para(run("Body.")) + para(picture(cx=6700000, cy=4800000)),
                    media=True)
    rewrite(src, {"word/media/image1.png": _blank_image()})
    findings = preflight_docx(str(src), engine_config=_fake_engine(None))
    assert "PF132" in {finding.code for finding in findings}
    assert blocked(findings)


def test_zero_word_small_image_needs_acknowledgement_then_exports(tmp_path):
    src = make_docx(tmp_path, name="small_blank.docx",
                    body=para(run("Borrower: Alice Example.")) + para(picture()), media=True)
    rewrite(src, {"word/media/image1.png": _blank_image()})
    config = _fake_engine(None)
    findings = preflight_docx(str(src), engine_config=config)
    review_codes = {finding.code for finding in findings
                    if finding.severity.value == "review"}
    assert "PF131" in review_codes
    assert not blocked(findings)
    session = suggest_docx(str(src), FakeDetector([("Alice Example", Category.PERSON)]),
                           POLICY, engine_config=config)
    _accept_all(session)
    with pytest.raises(ExportRefused):                # PF131/PF113 not acknowledged yet
        export_docx(str(src), str(tmp_path / "unacked.docx"), session, engine_config=config)
    _ack_reviews(session)
    out = tmp_path / "acked.docx"
    report = export_docx(str(src), str(out), session, engine_config=config)
    assert report.passed
    assert out.exists()
    checks = {check.id: check for check in report.checks}
    assert checks["V112"].status == "WARN"
    assert "image 1 (" in checks["V112"].detail
    assert "NOT verified" in checks["V112"].detail


def test_low_effective_resolution_needs_acknowledgement(tmp_path):
    src = make_docx(tmp_path, name="lowres.docx",
                    body=para(run("Body.")) + para(picture(cx=3657600, cy=914400)),
                    media=True)
    rewrite(src, {"word/media/image1.png": _solid_png(100, 25)})
    findings = preflight_docx(str(src), engine_config=_fake_engine(None))
    codes = {finding.code: finding for finding in findings}
    assert "PF133" in codes and codes["PF133"].severity.value == "review"


def test_dishonest_engine_words_on_blank_pixels_are_refused(tmp_path):
    src = make_docx(tmp_path, name="blank.docx", body=para(run("Body.")) + para(picture()),
                    media=True)
    rewrite(src, {"word/media/image1.png": _blank_image()})
    words = tmp_path / "fake_words.json"
    words.write_text(json.dumps([{"text": "Alice Example",
                                  "box": [0.05, 0.2, 0.4, 0.35],
                                  "confidence": 0.99}]), encoding="utf-8")
    findings = preflight_docx(str(src), engine_config=_fake_engine(words, mode="force"))
    codes = {finding.code for finding in findings}
    assert "PF128" in codes
    assert blocked(findings)
    session = suggest_docx(str(src), FakeDetector([("Alice Example", Category.PERSON)]),
                           POLICY, engine_config=_fake_engine(words, mode="force"))
    assert blocked(session.findings)


def test_same_size_images_accepted_on_one_and_not_the_other_are_refused(tmp_path):
    first, words_path, _words = _marked_image((1, 0, 0), tmp_path)
    second, _path2, _words2 = _marked_image((0, 0, 1), tmp_path, name="words2.json")
    src = _two_image_docx(tmp_path, first, second, name="two_images.docx")
    config = engine_config(words_path)
    detector = FakeDetector([("Alice Example", Category.PERSON),
                             ("alice@example.test", Category.EMAIL)])
    session = suggest_docx(str(src), detector, POLICY, engine_config=config)
    text_count = len(extract_text(read_package(str(src))).containers)
    first_image_page = min(decision.page for decision in session.decisions
                           if decision.page >= text_count)
    for index, decision in enumerate(session.decisions):
        if decision.page < text_count or decision.page == first_image_page:
            session.accept(index)
        else:
            session.reject(index)
    _ack_reviews(session)
    out = tmp_path / "cross.docx"
    with pytest.raises(ExportRefused) as caught:
        export_docx(str(src), str(out), session, engine_config=config)
    assert "still readable in another embedded image" in str(caught.value)
    assert not out.exists()


def test_v110_catches_an_accepted_needle_in_an_untouched_exported_image(tmp_path, monkeypatch):
    first, words_path, _words = _marked_image((1, 0, 0), tmp_path)
    second, _path2, _words2 = _marked_image((0, 0, 1), tmp_path, name="words2.json")
    src = _two_image_docx(tmp_path, first, second, name="two_leak.docx")
    config = engine_config(words_path)
    detector = FakeDetector([("Alice Example", Category.PERSON)])
    session = suggest_docx(str(src), detector, POLICY, engine_config=config)
    text_count = len(extract_text(read_package(str(src))).containers)
    kept = False
    for index, decision in enumerate(session.decisions):
        if decision.page >= text_count:
            if not kept:
                session.accept(index)
                kept = True
            else:
                session.reject(index)
        else:
            session.accept(index)
    _ack_reviews(session)
    # Disable the source-level cross-image check so only the output verifier can catch it.
    monkeypatch.setattr(image_tools, "image_text_has_needle", lambda *args: False)
    out = tmp_path / "leak.docx"
    with pytest.raises(ExportRefused) as caught:
        export_docx(str(src), str(out), session, engine_config=config)
    assert caught.value.report is not None
    assert "V110" in {check.id for check in caught.value.report.failed()}
    assert not out.exists()


# ---- F1: canonical image pairing ------------------------------------------
def test_two_same_size_images_pair_canonically_and_export(tmp_path):
    first, words_path, words = _marked_image((1, 0, 0), tmp_path)
    second, _path2, _words2 = _marked_image((0, 0, 1), tmp_path, name="words2.json")
    src = _two_image_docx(tmp_path, first, second, name="two_same_size.docx")
    config = engine_config(words_path)
    session = suggest_docx(str(src), FakeDetector([("Alice Example", Category.PERSON),
                                                   ("alice@example.test", Category.EMAIL)]),
                           POLICY, engine_config=config)
    _accept_all(session)
    _ack_reviews(session)
    out = tmp_path / "paired.docx"
    report = export_docx(str(src), str(out), session, engine_config=config)
    assert report.passed
    media = {name: data for name, data in raw_parts(out).items()
             if name.startswith("word/media/")}
    assert sorted(media) == ["word/media/redacted_1.png", "word/media/redacted_2.png"]
    first_pix = image_tools.decode_image(media["word/media/redacted_1.png"])
    second_pix = image_tools.decode_image(media["word/media/redacted_2.png"])
    assert list(first_pix.samples[0:3]) == [255, 0, 0]     # red marker stayed with image 1
    assert list(second_pix.samples[0:3]) == [0, 0, 255]    # blue marker stayed with image 2
    for word in words:
        rect = (word["box"][0] * 400, word["box"][1] * 200,
                word["box"][2] * 400, word["box"][3] * 200)
        assert image_tools.bright_fraction(first_pix, rect) == 0.0
        assert image_tools.bright_fraction(second_pix, rect) == 0.0


def test_image_count_tamper_fails_closed(tmp_path):
    first, words_path, _words = _marked_image((1, 0, 0), tmp_path)
    second, _path2, _words2 = _marked_image((0, 0, 1), tmp_path, name="words2.json")
    src = _two_image_docx(tmp_path, first, second, name="tamper_src.docx")
    config = engine_config(words_path)
    session = suggest_docx(str(src), FakeDetector([("Alice Example", Category.PERSON),
                                                   ("alice@example.test", Category.EMAIL)]),
                           POLICY, engine_config=config)
    _accept_all(session)
    _ack_reviews(session)
    out = tmp_path / "tamper_out.docx"
    assert export_docx(str(src), str(out), session, engine_config=config).passed
    parts = raw_parts(out)
    parts.pop("word/media/redacted_2.png")
    tampered = tmp_path / "tampered.docx"
    with zipfile.ZipFile(tampered, "w") as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    tampered_report = verify_docx(str(src), str(tampered),
                                  [("PERSON", "Alice Example")],
                                  engine_config=config)
    assert not tampered_report.passed
    assert "V109" in {check.id for check in tampered_report.failed()}


# ---- F4: every image re-encoded metadata-free ----------------------------
def test_untouched_metadata_image_is_reencoded_and_pixels_preserved(tmp_path):
    src = tmp_path / "I03.docx"
    src.write_bytes((AUDIT / "I03_metadata_only_image.docx").read_bytes())
    config = _fake_engine(None)
    findings = preflight_docx(str(src), engine_config=config)
    assert not blocked(findings)
    session = suggest_docx(str(src), FakeDetector([("body.contact@example.test",
                                                    Category.EMAIL)]),
                           POLICY, engine_config=config)
    assert session.decisions
    _accept_all(session)
    _ack_reviews(session)
    out = tmp_path / "I03_out.docx"
    report = export_docx(str(src), str(out), session, engine_config=config)
    assert report.passed
    source_media = {name: data for name, data in raw_parts(src).items()
                    if name.startswith("word/media/")}
    out_media = {name: data for name, data in raw_parts(out).items()
                 if name.startswith("word/media/")}
    assert sorted(out_media) == ["word/media/redacted_1.png"]
    payload = out_media["word/media/redacted_1.png"]
    for marker in (b"EXIFCANARY", b"XMPCANARY", b"ICCCANARY", b"Exif", b"pHYs", b"iCCP"):
        assert marker not in payload
    source_pix = image_tools.decode_image(next(iter(source_media.values())))
    out_pix = image_tools.decode_image(payload)
    assert (out_pix.width, out_pix.height) == (source_pix.width, source_pix.height)
    assert bytes(out_pix.samples) == bytes(source_pix.samples)
    for original in source_media.values():
        assert all(original not in part for part in raw_parts(out).values())


def test_embedded_png_metadata_is_stripped_from_an_untouched_image(tmp_path):
    png, _words_path, _words = _marked_image((0.5, 0.5, 0.5), tmp_path)
    text_chunk = _png_with_chunk(png, b"tEXt", b"Comment\x00EXIFCANARY Alice Example")
    phys_chunk = b"\x00\x00\x0b\x13\x00\x00\x0b\x13\x01"
    with_metadata = _png_with_chunk(text_chunk, b"pHYs", phys_chunk)
    src = make_docx(tmp_path, name="metadata.docx",
                    body=para(run("Borrower: Alice Example.")) + para(picture()), media=True)
    rewrite(src, {"word/media/image1.png": with_metadata})
    config = _fake_engine(None)
    session = suggest_docx(str(src), FakeDetector([("Alice Example", Category.PERSON)]),
                           POLICY, engine_config=config)
    _accept_all(session)
    _ack_reviews(session)
    out = tmp_path / "metadata_out.docx"
    report = export_docx(str(src), str(out), session, engine_config=config)
    assert report.passed
    payload = next(data for name, data in raw_parts(out).items()
                   if name.startswith("word/media/"))
    assert b"EXIFCANARY" not in payload and b"pHYs" not in payload
    source_pix = image_tools.decode_image(with_metadata)
    out_pix = image_tools.decode_image(payload)
    assert bytes(out_pix.samples) == bytes(source_pix.samples)


def test_both_edited_and_untouched_images_are_reencoded(tmp_path):
    first, words_path, words = _marked_image((1, 0, 0), tmp_path)
    second = _solid_png(400, 200)                     # dark, no readable text
    src = _two_image_docx(tmp_path, first, second, name="mixed.docx")
    config = engine_config(words_path)
    detector = FakeDetector([("Alice Example", Category.PERSON),
                             ("alice@example.test", Category.EMAIL)])
    session = suggest_docx(str(src), detector, POLICY, engine_config=config)
    text_count = len(extract_text(read_package(str(src))).containers)
    for index, decision in enumerate(session.decisions):
        if decision.page >= text_count:
            session.accept(index)                     # image 1 text is redacted
        else:
            session.accept(index)
    _ack_reviews(session)
    out = tmp_path / "mixed_out.docx"
    report = export_docx(str(src), str(out), session, engine_config=config)
    assert report.passed
    source_media = {name: data for name, data in raw_parts(src).items()
                    if name.startswith("word/media/")}
    out_media = {name: data for name, data in raw_parts(out).items()
                 if name.startswith("word/media/")}
    for payload in source_media.values():
        assert all(payload not in out_payload for out_payload in out_media.values())
    edited_pix = image_tools.decode_image(out_media["word/media/redacted_1.png"])
    untouched_pix = image_tools.decode_image(out_media["word/media/redacted_2.png"])
    for word in words:
        rect = (word["box"][0] * 400, word["box"][1] * 200,
                word["box"][2] * 400, word["box"][3] * 200)
        assert image_tools.bright_fraction(edited_pix, rect) == 0.0
    source_second = image_tools.decode_image(second)
    assert bytes(untouched_pix.samples) == bytes(source_second.samples)
    details = {check.id: check.detail for check in report.checks}
    assert "untouched images are pixel-identical" in details["V109"]


# ---- R1/R2 after the second audit round: disclosure + contrast-based ink -----
I10_WORDS = [
    {"text": "Alice", "box": [0.014, 0.0692, 0.088, 0.1654], "confidence": 0.9},
    {"text": "Example", "box": [0.101, 0.0692, 0.23, 0.1923], "confidence": 0.9},
    {"text": "alice@example.test", "box": [0.015, 0.2385, 0.312, 0.3615],
     "confidence": 0.9},
    {"text": "IBAN:", "box": [0.017, 0.4077, 0.101, 0.5038], "confidence": 0.9},
    {"text": "DE89", "box": [0.115, 0.4077, 0.196, 0.5038], "confidence": 0.9},
    {"text": "3704", "box": [0.209, 0.4077, 0.281, 0.5038], "confidence": 0.9},
    {"text": "0044", "box": [0.293, 0.4077, 0.366, 0.5038], "confidence": 0.9},
    {"text": "0532", "box": [0.378, 0.4077, 0.451, 0.5038], "confidence": 0.9},
    {"text": "0130", "box": [0.463, 0.4077, 0.536, 0.5038], "confidence": 0.9},
    {"text": "00", "box": [0.548, 0.4077, 0.583, 0.5038], "confidence": 0.9},
    {"text": "Phone:", "box": [0.017, 0.5769, 0.12, 0.6731], "confidence": 0.9},
    {"text": "+44", "box": [0.133, 0.5769, 0.187, 0.6731], "confidence": 0.9},
    {"text": "20", "box": [0.199, 0.5769, 0.234, 0.6731], "confidence": 0.9},
    {"text": "7946", "box": [0.247, 0.5769, 0.319, 0.6731], "confidence": 0.9},
    {"text": "0958", "box": [0.331, 0.5769, 0.404, 0.6731], "confidence": 0.9},
]

I22_WORDS = [
    {"text": "ice", "box": [0.0078, 0.028, 0.0189, 0.042], "confidence": 0.9},
    {"text": "Example", "box": [0.0211, 0.028, 0.0406, 0.046], "confidence": 0.9},
    {"text": "aiceGexampe", "box": [0.0078, 0.058, 0.0428, 0.076], "confidence": 0.9},
    {"text": "test", "box": [0.0444, 0.06, 0.0533, 0.072], "confidence": 0.9},
    {"text": "BAN:", "box": [0.0083, 0.088, 0.0194, 0.102], "confidence": 0.9},
    {"text": "DEB9", "box": [0.0239, 0.064, 0.0361, 0.12], "confidence": 0.9},
    {"text": "3704", "box": [0.0383, 0.064, 0.0489, 0.12], "confidence": 0.9},
    {"text": "0044", "box": [0.0511, 0.064, 0.0622, 0.12], "confidence": 0.9},
    {"text": "0532", "box": [0.0644, 0.064, 0.075, 0.12], "confidence": 0.9},
    {"text": "0120", "box": [0.0772, 0.064, 0.0867, 0.12], "confidence": 0.9},
    {"text": "00,", "box": [0.09, 0.064, 0.0972, 0.12], "confidence": 0.9},
    {"text": "Prone!", "box": [0.0061, 0.094, 0.0244, 0.15], "confidence": 0.9},
    {"text": "0958", "box": [0.0556, 0.118, 0.0667, 0.132], "confidence": 0.9},
]


def _fixture(tmp_path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes((AUDIT / name).read_bytes())
    return path


def _words_file(tmp_path, words, name: str = "words.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(words), encoding="utf-8")
    return path


def test_i10_low_contrast_text_is_not_refused_and_exports(tmp_path):
    """R2: valid low-contrast glyphs count as ink; the E2-style blank refusal stays."""
    src = _fixture(tmp_path, "I10_low_contrast_text.docx")
    config = engine_config(_words_file(tmp_path, I10_WORDS))
    findings = preflight_docx(str(src), engine_config=config)
    assert "PF128" not in {finding.code for finding in findings}
    assert not blocked(findings)
    session = suggest_docx(str(src), FakeDetector([("Alice Example", Category.PERSON),
                                                   ("alice@example.test", Category.EMAIL)]),
                           POLICY, engine_config=config)
    assert session.decisions                             # the image text was read
    _accept_all(session)
    _ack_reviews(session)
    out = tmp_path / "I10_out.docx"
    report = export_docx(str(src), str(out), session, engine_config=config)
    assert report.passed
    checks = {check.id: check for check in report.checks}
    assert checks["V110"].status == "PASS"
    assert checks["V112"].status == "PASS"               # words were read, not tiny
    assert "had their text read" in checks["V112"].detail


def test_i22_tiny_text_goes_through_pf133_acknowledgement(tmp_path):
    """R1b: tiny text is flagged by PF133 even when the engine returned (misread) words."""
    src = _fixture(tmp_path, "I22_tiny_text.docx")
    config = engine_config(_words_file(tmp_path, I22_WORDS))
    findings = preflight_docx(str(src), engine_config=config)
    pf133 = next(finding for finding in findings if finding.code == "PF133")
    assert pf133.severity.value == "review"
    assert "very small text" in pf133.message
    assert "PF131" not in {finding.code for finding in findings}   # words were returned
    assert not blocked(findings)
    session = suggest_docx(str(src), FakeDetector([("body.contact@example.test",
                                                    Category.EMAIL)]),
                           POLICY, engine_config=config)
    _accept_all(session)
    with pytest.raises(ExportRefused):                    # PF133 unacknowledged
        export_docx(str(src), str(tmp_path / "unacked.docx"), session, engine_config=config)
    _ack_reviews(session)
    out = tmp_path / "I22_out.docx"
    report = export_docx(str(src), str(out), session, engine_config=config)
    assert report.passed
    v112 = {check.id: check for check in report.checks}["V112"]
    assert v112.status == "WARN"                          # tiny text: unverified pixels
    assert "image 1 (1800x500 px)" in v112.detail
    assert "text was very small" in v112.detail
    assert "NOT verified for hidden text" in v112.detail
    # The same WARN row must reach the evidence report (built from report.checks).
    from legal_redact.evidence import build_evidence, write_evidence
    evidence = build_evidence(str(src), str(out), session, report)
    evidence_checks = {check["id"]: check for check in evidence["verification"]["checks"]}
    assert evidence_checks["V112"]["status"] == "WARN"
    assert "text was very small" in evidence_checks["V112"]["detail"]
    evidence_path = tmp_path / "I22.evidence.md"
    write_evidence(evidence_path, evidence)
    assert "| V112 " in evidence_path.read_text(encoding="utf-8")
    assert "WARN" in evidence_path.read_text(encoding="utf-8")


def test_pf131_message_says_pixels_were_not_checked(tmp_path):
    src = make_docx(tmp_path, name="blank.docx",
                    body=para(run("Body.")) + para(picture()), media=True)
    rewrite(src, {"word/media/image1.png": _blank_image()})
    findings = preflight_docx(str(src), engine_config=_fake_engine(None))
    pf131 = next(finding for finding in findings if finding.code == "PF131")
    assert "NOT checked" in pf131.message
    assert "personal data" in pf131.message
    assert "Open every such image yourself" in pf131.message


def test_v112_warns_when_ocr_is_not_used_at_all(tmp_path):
    src = make_docx(tmp_path, name="noocr.docx",
                    body=para(run("Borrower: Alice Example.")) + para(picture()), media=True)
    rewrite(src, {"word/media/image1.png": _blank_image()})
    session = suggest_docx(str(src), FakeDetector([("Alice Example", Category.PERSON)]),
                           POLICY, ocr=False)
    _accept_all(session)
    _ack_reviews(session)
    out = tmp_path / "noocr_out.docx"
    report = export_docx(str(src), str(out), session, ocr=False)
    assert report.passed
    v112 = {check.id: check for check in report.checks}["V112"]
    assert v112.status == "WARN"
    assert "image 1 (" in v112.detail
    assert "OCR was not used" in v112.detail


def test_ink_contrast_accepts_low_contrast_glyphs_and_rejects_uniform_boxes():
    document = pymupdf.open()
    page = document.new_page(width=200, height=60)
    page.insert_text((10, 42), "Alice", fontsize=30, color=(0.9, 0.9, 0.9))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False)
    words = page.get_text("words")
    rect = (words[0][0], words[0][1], words[0][2], words[0][3])
    assert image_tools.ink_contrast(pix, rect) > image_tools._INK_MIN_SHARE
    blank_page = pymupdf.open()
    blank = blank_page.new_page(width=200, height=60)
    blank.draw_rect(blank.rect, color=None, fill=(1, 1, 1))
    blank_pix = blank.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False)
    assert image_tools.ink_contrast(blank_pix, rect) == 0.0
