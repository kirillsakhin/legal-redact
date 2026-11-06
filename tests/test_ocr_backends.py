"""Pluggable OCR engines: contract validation, cloud gates, pinning, cross-check.

The fake command engine in tests/fixtures/fake_ocr_engine.py is honest enough to
stand in for a real reader: it returns a word only when the image region over
that word is not black, so redaction tests are meaningful without any OCR
library or network call.

Covers: command/JSON validation (fail closed on every malformed answer),
spec/env resolution, the double cloud gate (engine-level approval then per-file
acknowledgement, no page rendered before approval), decision-file engine
pinning, the Tesseract-preferred V15 cross-check with its same-engine label,
orientation fallback, the review page's cloud state, and a regression pin for
the Tesseract backend's word boxes.
"""
import json
import sys
import time
from pathlib import Path

import pymupdf
import pytest

from legal_redact import pipeline
from legal_redact.bench.synth import generate, render_pdf
from legal_redact.cli import main
from legal_redact.detect.rules import RulesDetector
from legal_redact.extract import extract_pages
from legal_redact.models import Severity
from legal_redact.ocr import ocr_available
from legal_redact.ocr_backends import (CLOUD_OCR_ACK_LABEL, CLOUD_OCR_DISCLAIMER,
                                       CloudOcrNotApproved, CommandEngine, EngineConfigError,
                                       TesseractEngine, canonical_spec, default_config,
                                       resolve_config, split_command, validate_result_payload)
from legal_redact.pipeline import (ExportRefused, export, load_decisions, save_decisions,
                                   suggest)
from legal_redact.policy import load_policy
from legal_redact.preflight import preflight
from legal_redact.redact import write_redacted

HERE = Path(__file__).parent
FAKE = HERE / "fixtures" / "fake_ocr_engine.py"
EXPECTED = HERE / "fixtures" / "tesseract_words_expected.json"
PY = sys.executable
SCAN_DPI = 300


# ---- fixtures --------------------------------------------------------------
def _build_scan_and_words(directory: Path):
    doc = generate("loan", 2)
    src = pymupdf.open("pdf", render_pdf(doc))
    page_text = extract_pages(src)[0]
    words = [{"text": w.text,
              "box": [w.rect.x0 / page_text.width, w.rect.y0 / page_text.height,
                      w.rect.x1 / page_text.width, w.rect.y1 / page_text.height],
              "confidence": 0.99} for w in page_text.words]
    words_path = directory / "words.json"
    words_path.write_text(json.dumps(words), encoding="utf-8")
    pix = src[0].get_pixmap(dpi=SCAN_DPI)
    scan = pymupdf.open()
    page = scan.new_page(width=src[0].rect.width, height=src[0].rect.height)
    page.insert_image(page.rect, pixmap=pix)
    scan_path = directory / "scan.pdf"
    scan.save(scan_path)
    src.close()
    scan.close()
    return str(scan_path), str(words_path), doc


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    return _build_scan_and_words(tmp_path_factory.mktemp("ocr-backends"))


def command_spec(words_path, *, local="true", mode="valid", marker=None, extra=""):
    parts = [f'"{PY}"', f'"{FAKE}"', "--words", f'"{words_path}"', "--mode", mode,
             "--local", local]
    if marker:
        parts += ["--marker", f'"{marker}"']
    if extra:
        parts.append(extra)
    return " ".join(parts)


def local_config(words_path, **kw):
    return resolve_config(engine="command:" + command_spec(words_path, local="true", **kw),
                          local=True)


def cloud_config(words_path, **kw):
    return resolve_config(engine="command:" + command_spec(words_path, local="false", **kw),
                          allow_cloud=True)


def accept_all(session):
    for i in range(len(session.decisions)):
        session.accept(i)
    for finding in session.findings:
        session.acknowledge(finding.code)


def _png_of_scan(scan_path: str) -> bytes:
    doc = pymupdf.open(scan_path)
    try:
        return doc[0].get_pixmap(dpi=150).tobytes("png")
    finally:
        doc.close()


# ---- specification parsing / configuration ---------------------------------
def test_engine_specs_are_canonicalised_and_bad_ones_refused():
    assert canonical_spec("tesseract") == "tesseract"
    assert canonical_spec("  TESSERACT  ") == "tesseract"
    assert canonical_spec(None) == "tesseract"
    assert canonical_spec("command: my-wrapper --flag") == "command:my-wrapper --flag"
    with pytest.raises(EngineConfigError):
        canonical_spec("mystery-engine")
    with pytest.raises(EngineConfigError):
        canonical_spec("command:")
    with pytest.raises(EngineConfigError):
        canonical_spec("command")


def test_split_command_handles_quoted_windows_paths():
    argv = split_command('"C:\\Program Files\\Py\\python.exe" "wrap me.py" --flag x')
    assert argv == ["C:\\Program Files\\Py\\python.exe", "wrap me.py", "--flag", "x"]


def test_cloud_gate_requires_explicit_approval_and_names_the_docs():
    config = resolve_config(command="my-wrapper", allow_cloud=False)
    assert config.local is False and config.allow_cloud is False
    with pytest.raises(CloudOcrNotApproved) as exc:
        config.check_gate()
    assert "docs/OCR_BACKENDS.md" in str(exc.value) and "--allow-cloud-ocr" in str(exc.value)
    approved = resolve_config(command="my-wrapper", allow_cloud=True)
    approved.check_gate()                       # no raise
    local = resolve_config(command="my-wrapper", local=True)
    local.check_gate()                          # declared local: no cloud gate


def test_env_fallback_selects_the_engine(monkeypatch):
    env = {"LEGAL_REDACT_OCR_ENGINE": "command:my-wrapper --x",
           "LEGAL_REDACT_OCR_LOCAL": "1"}
    config = resolve_config(env=env)
    assert config.spec == "command:my-wrapper --x" and config.local is True
    env["LEGAL_REDACT_OCR_ENGINE"] = "tesseract"
    assert resolve_config(env=env).spec == "tesseract"
    cloud_env = {"LEGAL_REDACT_OCR_ENGINE": "command:my-wrapper",
                 "LEGAL_REDACT_ALLOW_CLOUD_OCR": "1"}
    cloud = resolve_config(env=cloud_env)
    assert cloud.local is False and cloud.allow_cloud is True


def test_unavailable_command_engine_is_refused(fixture, capsys):
    scan, _, _ = fixture
    rc = main(["inspect", scan, "--ocr-engine", "command:definitely-not-a-real-program-xyz",
               "--ocr-local"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "PF020" in captured.out and "unavailable or invalid" in captured.out
    assert not captured.err.count("Traceback")


def test_ocr_command_flag_is_equivalent_to_command_spec(fixture, capsys):
    scan, words, _ = fixture
    rc = main(["inspect", scan, "--ocr-command", command_spec(words, local="true"),
               "--ocr-local"])
    assert rc == 0
    assert "engine=command" in capsys.readouterr().out


# ---- command engine: strict output validation ------------------------------
def test_valid_fake_engine_returns_words(fixture):
    scan, words, _ = fixture
    engine = CommandEngine([PY, str(FAKE), "--words", words], local=True)
    result = engine.analyze(_png_of_scan(scan), 595.0, 842.0, 150)
    assert result.local is True and result.engine == "fake" and result.version == "1.0"
    assert len(result.words) > 50
    assert all(0.0 <= w.box[0] < w.box[2] <= 1.0 for w in result.words)


def test_invalid_json_is_refused(fixture):
    scan, words, _ = fixture
    engine = CommandEngine([PY, str(FAKE), "--words", words, "--mode", "invalid-json"],
                           local=True)
    with pytest.raises(Exception, match="not valid JSON"):
        engine.analyze(_png_of_scan(scan), 595.0, 842.0, 150)


@pytest.mark.parametrize("mode,fragment", [
    ("out-of-bounds", "outside the page frame"),
    ("bad-confidence", "confidence"),
    ("empty-word", "no non-empty text"),
    ("no-local", "local"),
    ("bad-rotation", "rotation"),
])
def test_invalid_payloads_are_refused(fixture, mode, fragment):
    scan, words, _ = fixture
    engine = CommandEngine([PY, str(FAKE), "--words", words, "--mode", mode], local=True)
    with pytest.raises(Exception) as exc:
        engine.analyze(_png_of_scan(scan), 595.0, 842.0, 150)
    assert fragment in str(exc.value)


def test_payload_validation_rejects_bool_and_nonfinite_numbers():
    base = {"local": True, "words": [{"text": "x", "box": [0, 0, 1, 1], "confidence": 0.5}]}
    for bad in ({"local": True, "words": [{"text": "x", "box": [0, 0, 1, 1],
                                          "confidence": True}]},
                {"local": True, "words": [{"text": "x", "box": [0, 0, 1, float("nan")],
                                          "confidence": 0.5}]},
                {"local": True, "words": [{"text": "x", "box": [0, 0, 1, 1]}]},
                {"local": "yes", "words": []}):
        with pytest.raises(Exception):
            validate_result_payload(bad)
    # A blank page is a legitimate empty result, not an error.
    assert validate_result_payload({"local": True, "words": []}).words == ()


def test_crash_and_timeout_are_refused(fixture):
    scan, words, _ = fixture
    crash = CommandEngine([PY, str(FAKE), "--words", words, "--mode", "crash"], local=True)
    with pytest.raises(Exception, match="failed"):
        crash.analyze(_png_of_scan(scan), 595.0, 842.0, 150)
    slow = CommandEngine([PY, str(FAKE), "--words", words, "--mode", "timeout"],
                         local=True, timeout=1.0)
    start = time.perf_counter()
    with pytest.raises(Exception, match="timed out"):
        slow.analyze(_png_of_scan(scan), 595.0, 842.0, 150)
    # The engine timeout is 1 s; the generous bound only proves the call does not
    # hang forever (a loaded CI runner can take many seconds to spawn Python).
    assert time.perf_counter() - start < 60


def test_locality_contradiction_is_refused(fixture):
    scan, words, _ = fixture
    engine = CommandEngine([PY, str(FAKE), "--words", words, "--mode", "wrong-local"],
                           local=True)
    with pytest.raises(Exception, match="contradicts its configuration"):
        engine.analyze(_png_of_scan(scan), 595.0, 842.0, 150)


def test_available_reflects_the_executable(tmp_path):
    assert CommandEngine([PY, str(FAKE)], local=True).available() is True
    assert CommandEngine(["definitely-not-a-real-program-xyz"], local=True).available() is False


# ---- CLI: cloud gates, engine display, env ---------------------------------
def test_cloud_engine_refused_without_approval_before_any_ocr(fixture, capsys, tmp_path):
    scan, words, _ = fixture
    marker = tmp_path / "invoked.txt"
    spec = "command:" + command_spec(words, local="false", marker=marker)
    rc = main(["inspect", scan, "--ocr-engine", spec])
    captured = capsys.readouterr()
    assert rc == 2
    assert not marker.exists(), "the engine was invoked before cloud approval"
    assert CLOUD_OCR_DISCLAIMER in captured.err
    assert "docs/OCR_BACKENDS.md" in captured.err
    assert "--allow-cloud-ocr" in captured.err


def test_cloud_engine_with_approval_prints_disclaimer_and_uses_engine(fixture, capsys, tmp_path):
    scan, words, _ = fixture
    marker = tmp_path / "invoked.txt"
    spec = "command:" + command_spec(words, local="false", marker=marker)
    rc = main(["inspect", scan, "--ocr-engine", spec, "--allow-cloud-ocr"])
    captured = capsys.readouterr()
    assert rc == 0 and marker.exists()
    assert "engine=command" in captured.out and "local=no" in captured.out
    assert "PF019" in captured.out and CLOUD_OCR_DISCLAIMER in captured.out
    assert CLOUD_OCR_DISCLAIMER in captured.err


def test_local_declaration_skips_the_cloud_gate(fixture, capsys):
    scan, words, _ = fixture
    spec = "command:" + command_spec(words, local="true")
    rc = main(["inspect", scan, "--ocr-engine", spec, "--ocr-local"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "engine=command" in captured.out and "local=yes" in captured.out
    assert "PF019" not in captured.out
    assert CLOUD_OCR_DISCLAIMER not in captured.err


def test_env_selects_engine_and_locality(fixture, monkeypatch, capsys):
    scan, words, _ = fixture
    monkeypatch.setenv("LEGAL_REDACT_OCR_ENGINE",
                       "command:" + command_spec(words, local="true"))
    monkeypatch.setenv("LEGAL_REDACT_OCR_LOCAL", "1")
    assert main(["inspect", scan]) == 0
    assert "engine=command" in capsys.readouterr().out


def test_unknown_engine_spec_is_a_clean_refusal(fixture, capsys):
    scan, _, _ = fixture
    assert main(["inspect", scan, "--ocr-engine", "mystery"]) == 2
    captured = capsys.readouterr()
    assert "expected 'tesseract' or 'command:<cmdline>'" in captured.err
    assert "docs/OCR_BACKENDS.md" in captured.err


def test_inspect_reports_default_engine(fixture, capsys):
    scan, _, _ = fixture
    rc = main(["inspect", scan])
    text = capsys.readouterr().out
    assert "engine=tesseract" in text and "local=yes" in text
    if ocr_available():
        assert rc == 0
        assert "version=tesseract" in text
        assert "ocr=available" in text
    else:
        # CI runners have no Tesseract: inspect must still name the default engine,
        # report it as unavailable, and refuse the scan instead of pretending.
        assert rc == 1
        assert "ocr=unavailable" in text
        assert "PF003" in text


# ---- end-to-end with a custom engine ---------------------------------------
def _no_tesseract(monkeypatch):
    import legal_redact.extract as extract_mod
    import legal_redact.ocr as ocr_mod
    import legal_redact.preflight as preflight_mod
    monkeypatch.setattr(ocr_mod, "ocr_available", lambda: False)
    monkeypatch.setattr(preflight_mod, "ocr_available", lambda: False)
    monkeypatch.setattr(extract_mod, "ocr_available", lambda: False)


def test_custom_engine_end_to_end_without_tesseract(fixture, monkeypatch, tmp_path):
    """No Tesseract at all: the custom engine reads, exports and checks its own output."""
    scan, words, _ = fixture
    _no_tesseract(monkeypatch)
    config = local_config(words)
    session = suggest(scan, RulesDetector(names=False), load_policy("counterparty"),
                      engine_config=config)
    codes = {f.code: f for f in session.findings}
    assert session.decisions
    assert codes["PF021"].severity is Severity.REVIEW          # no independent cross-check
    assert codes["PF023"].severity is Severity.REVIEW          # no OSD orientation check
    assert "PF019" not in codes                                # declared local
    accept_all(session)
    out = str(tmp_path / "custom.redacted.pdf")
    report = export(scan, out, session, engine_config=config)
    assert report.passed
    assert report.ocr_engine["independent"] is False
    assert "SAME-ENGINE" in report.ocr_engine["verification_reader"]
    v15 = next(c for c in report.checks if c.id == "V15")
    assert v15.status == "PASS" and "SAME-ENGINE" in v15.detail
    result = pymupdf.open(out)
    try:
        assert result[0].get_text().strip() == ""
        assert len(result[0].get_images(full=True)) == 1
    finally:
        result.close()


def test_same_engine_check_still_fails_a_leak(fixture, monkeypatch, tmp_path):
    """Never turn a FAIL into a PASS: a leak stays a leak under same-engine checking."""
    scan, words, _ = fixture
    _no_tesseract(monkeypatch)
    config = local_config(words)
    session = suggest(scan, RulesDetector(names=False), load_policy("counterparty"),
                      engine_config=config)
    accept_all(session)

    def remove_nothing(src_, out_, boxes, raster_pages=(), raster_dpi=300):
        import shutil
        shutil.copyfile(src_, out_)

    monkeypatch.setattr(pipeline, "write_redacted", remove_nothing)
    out = str(tmp_path / "leak.pdf")
    with pytest.raises(ExportRefused) as exc:
        export(scan, out, session, engine_config=config)
    failed = {c.id for c in exc.value.report.failed()}
    assert {"V14", "V15"} <= failed
    assert not Path(out).exists()


@pytest.mark.skipif(not ocr_available(), reason="Tesseract is not installed")
def test_tesseract_is_preferred_as_the_independent_reader(fixture, tmp_path):
    scan, words, _ = fixture
    config = local_config(words)
    session = suggest(scan, RulesDetector(names=False), load_policy("counterparty"),
                      engine_config=config)
    accept_all(session)
    out = str(tmp_path / "independent.pdf")
    report = export(scan, out, session, engine_config=config)
    assert report.passed
    assert report.ocr_engine["independent"] is True
    assert "Tesseract" in report.ocr_engine["verification_reader"]
    v15 = next(c for c in report.checks if c.id == "V15")
    assert "Tesseract (independent reader)" in v15.detail


# ---- cloud: per-file acknowledgement and decision-file pinning -------------
def test_cloud_engine_needs_the_acknowledgement_before_export(fixture, tmp_path):
    scan, words, _ = fixture
    config = cloud_config(words)
    session = suggest(scan, RulesDetector(names=False), load_policy("counterparty"),
                      engine_config=config)
    cloud = [f for f in session.findings if f.code == "PF019"]
    assert len(cloud) == 1 and cloud[0].message == CLOUD_OCR_DISCLAIMER
    assert cloud[0].severity is Severity.REVIEW
    accept_all(session)
    session.acknowledged.discard("PF019")               # un-tick the approval
    out = str(tmp_path / "cloud.pdf")
    with pytest.raises(ExportRefused) as exc:
        export(scan, out, session, engine_config=config)
    assert "PF019" in str(exc.value)
    assert not Path(out).exists()
    session.acknowledge("PF019")
    report = export(scan, out, session, engine_config=config)
    assert report.passed
    assert report.ocr_engine["configured"]["local"] is False


def test_cloud_approval_is_required_on_export_even_with_a_pinned_file(fixture, tmp_path):
    scan, words, _ = fixture
    approved = cloud_config(words)
    session = suggest(scan, RulesDetector(names=False), load_policy("counterparty"),
                      engine_config=approved)
    accept_all(session)
    out = str(tmp_path / "cloud-unapproved.pdf")
    unapproved = resolve_config(
        engine="command:" + command_spec(words, local="false"), allow_cloud=False)
    with pytest.raises(CloudOcrNotApproved):
        export(scan, out, session, engine_config=unapproved)
    assert not Path(out).exists()
    # The recorded pin also carries the cloud flag, so a local-claiming config mismatches.
    local_claim = resolve_config(
        engine="command:" + command_spec(words, local="true"), local=True)
    with pytest.raises(ExportRefused) as exc:
        export(scan, out, session, engine_config=local_claim)
    assert "re-run" in str(exc.value)
    assert not Path(out).exists()


def test_engine_change_between_suggest_and_export_is_refused(fixture, tmp_path):
    scan, words, _ = fixture
    default = default_config()
    session = suggest(scan, RulesDetector(names=False), load_policy("counterparty"),
                      engine_config=default)
    accept_all(session)
    other = local_config(words)
    out = str(tmp_path / "mismatch.pdf")
    with pytest.raises(ExportRefused) as exc:
        export(scan, out, session, engine_config=other)
    assert "re-run" in str(exc.value) and "OCR engine" in str(exc.value)
    assert not Path(out).exists()
    loaded = load_decisions(scan, _write_decisions(session, tmp_path), engine_config=other)
    assert "PF022" in {f.code for f in loaded.findings}
    assert any(f.severity is Severity.REFUSE for f in loaded.findings if f.code == "PF022")


def _write_decisions(session, tmp_path) -> str:
    path = tmp_path / "decisions.json"
    save_decisions(session, str(path))
    return str(path)


def test_old_decision_file_without_engine_pin_is_refused(fixture, tmp_path):
    scan, _, _ = fixture
    session = suggest(scan, RulesDetector(names=False), load_policy("counterparty"))
    accept_all(session)
    path = Path(_write_decisions(session, tmp_path))
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("ocr", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_decisions(scan, str(path))
    assert any(f.code == "PF022" and "re-run" in f.message for f in loaded.findings)
    out = str(tmp_path / "old.pdf")
    with pytest.raises(ExportRefused) as exc:
        export(scan, out, loaded)
    assert "no OCR engine record" in str(exc.value) and "re-run" in str(exc.value)
    assert not Path(out).exists()


def test_decision_file_records_the_engine_pin(fixture, tmp_path):
    scan, words, _ = fixture
    session = suggest(scan, RulesDetector(names=False), load_policy("counterparty"),
                      engine_config=local_config(words))
    data = json.loads(Path(_write_decisions(session, tmp_path)).read_text(encoding="utf-8"))
    assert data["ocr"]["engine"].startswith("command:")
    assert data["ocr"]["local"] is True and data["ocr"]["cloud"] is False


# ---- review page: cloud state and explicit approval label ------------------
def test_review_page_state_exposes_cloud_state_and_approval_label(fixture, tmp_path):
    from legal_redact.review_server import ReviewApp
    scan, words, _ = fixture
    config = cloud_config(words)
    session = suggest(scan, RulesDetector(names=False), load_policy("counterparty"),
                      engine_config=config)
    app = ReviewApp(scan, session, str(tmp_path / "dec.json"), engine_config=config)
    state = app.state()
    assert state["ocr"]["local"] is False
    assert state["ocr"]["disclaimer"] == CLOUD_OCR_DISCLAIMER
    assert state["ocr"]["ack_label"] == CLOUD_OCR_ACK_LABEL
    cloud = next(f for f in state["findings"] if f["code"] == "PF019")
    assert cloud["ack_label"] == CLOUD_OCR_ACK_LABEL
    other = next(f for f in state["findings"] if f["code"] == "PF016")
    assert other["ack_label"] == "I have looked at this"
    html = (HERE.parent / "src" / "legal_redact" / "web" / "review.html").read_text(
        encoding="utf-8")
    assert 'id="cloudwarning"' in html and "cloud-warning" in html


# ---- orientation fallback --------------------------------------------------
def test_orientation_fallback_when_tesseract_is_missing(fixture, monkeypatch):
    import legal_redact.preflight as preflight_mod
    scan, words, _ = fixture
    monkeypatch.setattr(preflight_mod, "ocr_available", lambda: False)
    config = local_config(words)
    codes = {f.code: f for f in preflight(pymupdf.open(scan), engine_config=config)}
    assert codes["PF023"].severity is Severity.REVIEW
    assert "Tesseract OSD" in codes["PF023"].message
    assert "PF017" not in codes


@pytest.mark.skipif(not ocr_available(), reason="Tesseract is not installed")
def test_orientation_still_checked_when_tesseract_exists(fixture, monkeypatch):
    import legal_redact.preflight as preflight_mod
    scan, words, _ = fixture
    monkeypatch.setattr(preflight_mod, "ocr_orientation", lambda page, dpi=200: (90, 10.0))
    config = local_config(words)
    codes = {f.code: f for f in preflight(pymupdf.open(scan), engine_config=config)}
    assert codes["PF017"].severity is Severity.REVIEW
    assert "PF023" not in codes


# ---- Tesseract backend regression pin --------------------------------------
TESSERACT_BOX_TOLERANCE = 0.004


@pytest.mark.skipif(not ocr_available(), reason="Tesseract is not installed")
def test_tesseract_backend_word_boxes_match_the_pinned_fixture():
    """Guard against refactor drift: the default engine must still return the same
    words and boxes (within 0.4% of the page) as the pre-pluggable-engine code."""
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    doc = generate("loan", 2)
    src = pymupdf.open("pdf", render_pdf(doc))
    pix = src[0].get_pixmap(dpi=expected["dpi"])
    scan = pymupdf.open()
    page = scan.new_page(width=src[0].rect.width, height=src[0].rect.height)
    page.insert_image(page.rect, pixmap=pix)
    png = page.get_pixmap(dpi=expected["dpi"]).tobytes("png")
    src.close()
    scan.close()
    result = TesseractEngine().analyze(png, expected["page"][0], expected["page"][1],
                                       expected["dpi"])
    expected_words = expected["words"]
    got_texts = [w.text for w in result.words]
    want_texts = [w["text"] for w in expected_words]
    assert got_texts == want_texts
    tolerance = (TESSERACT_BOX_TOLERANCE
                 if result.version == expected.get("tesseract_version") else 0.015)
    for got, want in zip(result.words, expected_words):
        for axis in range(4):
            assert abs(got.box[axis] - want["box"][axis]) <= tolerance, (got.text, axis)
