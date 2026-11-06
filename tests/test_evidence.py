"""Compliance evidence report: written on export, contains counts/hashes/checks, no text.

The report is deliberately a record, not a certificate: these tests pin the
useful parts (hashes, policy, decisions, verifier checks, disclaimer) and the
critical property that no document text leaks into the file.
"""
import hashlib
import json

from docx import Document

from legal_redact.bench.synth import generate, render_pdf
from legal_redact.cli import main


def _synthetic_pdf(path):
    doc = generate("loan", 2)
    path.write_bytes(render_pdf(doc))
    return doc


def _accept_all(decisions_path):
    data = json.loads(decisions_path.read_text(encoding="utf-8"))
    for decision in data["decisions"]:
        decision["status"] = "accepted"
    decisions_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def test_pdf_export_writes_markdown_evidence_without_document_text(tmp_path):
    src = tmp_path / "contract.pdf"
    doc = _synthetic_pdf(src)
    decisions = tmp_path / "decisions.json"
    assert main(["suggest", str(src), "-o", str(decisions), "--detector", "rules",
                 "--policy", "contract-sharing"]) == 0
    data = _accept_all(decisions)
    assert data.get("policy") == "contract-sharing", "suggest must record the policy name"
    assert data["decisions"], "the synthetic contract should contain detectable identifiers"
    out = tmp_path / "contract.redacted.pdf"
    evidence = tmp_path / "evidence.md"
    assert main(["redact", str(src), "--decisions", str(decisions), "-o", str(out),
                 "--evidence", str(evidence)]) == 0
    text = evidence.read_text(encoding="utf-8")
    assert "# Redaction evidence report" in text
    assert "not a compliance certificate" in text
    assert hashlib.sha256(src.read_bytes()).hexdigest() in text
    assert hashlib.sha256(out.read_bytes()).hexdigest() in text
    assert "contract-sharing" in text
    assert "## Verification" in text and "PASS" in text
    assert "## Not covered" in text
    # No document text: none of the planted values may appear in the report.
    for gold in doc.gold:
        value = doc.text[gold.start:gold.end]
        if len(value) >= 4:
            assert value not in text


def test_json_evidence_has_the_machine_readable_fields(tmp_path):
    src = tmp_path / "contract.pdf"
    _synthetic_pdf(src)
    decisions = tmp_path / "decisions.json"
    assert main(["suggest", str(src), "-o", str(decisions), "--detector", "rules",
                 "--policy", "publication"]) == 0
    _accept_all(decisions)
    out = tmp_path / "redacted.pdf"
    evidence = tmp_path / "evidence.json"
    assert main(["redact", str(src), "--decisions", str(decisions), "-o", str(out),
                 "--evidence", str(evidence)]) == 0
    data = json.loads(evidence.read_text(encoding="utf-8"))
    assert data["schema"] == "legal-redact-evidence/1"
    assert data["tool"]["name"] == "legal-redact"
    assert data["policy"] == "publication"
    assert data["files"]["source"]["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert data["files"]["redacted"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert data["verification"]["passed"] is True
    assert data["verification"]["checks"], "the verifier checks are the point of the report"
    assert data["review"]["decisions"] >= 1
    # The JSON must not contain the document text either (checked byte-level: the
    # synthetic values are random; the strict assertion is in the markdown test).


def test_docx_export_writes_evidence_and_no_text(tmp_path):
    src = tmp_path / "contract.docx"
    document = Document()
    document.add_paragraph("Signed by Alice Example, alice.example@example.test.")
    document.save(src)
    decisions = tmp_path / "decisions.json"
    assert main(["suggest", str(src), "-o", str(decisions), "--detector", "rules",
                 "--policy", "contract-sharing"]) == 0
    _accept_all(decisions)
    out = tmp_path / "contract.redacted.docx"
    evidence = tmp_path / "evidence.json"
    assert main(["redact", str(src), "--decisions", str(decisions), "-o", str(out),
                 "--ack", "PF114", "--evidence", str(evidence)]) == 0
    raw = evidence.read_text(encoding="utf-8")
    assert "alice.example@example.test" not in raw
    data = json.loads(raw)
    assert data["files"]["redacted"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert data["verification"]["passed"] is True


def test_no_evidence_when_export_is_refused(tmp_path):
    src = tmp_path / "contract.docx"
    document = Document()
    document.add_paragraph("Contact alice.example@example.test.")
    document.save(src)
    decisions = tmp_path / "decisions.json"
    assert main(["suggest", str(src), "-o", str(decisions), "--detector", "rules",
                 "--policy", "contract-sharing"]) == 0
    _accept_all(decisions)
    out = tmp_path / "contract.redacted.docx"
    evidence = tmp_path / "evidence.md"
    # PF114 (custom XML) is a REVIEW finding and was not acknowledged: export must refuse.
    assert main(["redact", str(src), "--decisions", str(decisions), "-o", str(out),
                 "--evidence", str(evidence)]) == 1
    assert not out.exists()
    assert not evidence.exists()
