import json
from pathlib import Path

ATTEST = ["special_categories", "indirect_identifiers", "authorised"]

import threading
import urllib.error
import urllib.request

import pytest

from legal_redact.bench.synth import generate, render_pdf
from legal_redact.detect.rules import RulesDetector
from legal_redact.pipeline import suggest
from legal_redact.policy import load_policy
from legal_redact.review_server import ReviewApp, make_server


@pytest.fixture
def server(tmp_path):
    d = generate("loan", 2)
    src = tmp_path / "in.pdf"
    src.write_bytes(render_pdf(d))
    session = suggest(str(src), RulesDetector(names=False), load_policy("counterparty"))
    app = ReviewApp(str(src), session, str(tmp_path / "dec.json"))
    httpd, token = make_server(app, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    yield base, token, d, tmp_path
    httpd.shutdown()
    httpd.server_close()


def call(base, token, path, body=None, headers=None, host=None):
    h = {"X-Review-Token": token, **(headers or {})}
    if host:
        h["Host"] = host
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=h,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def state(base, token):
    return json.loads(call(base, token, "/api/state")[1])


def test_requests_without_the_token_or_with_a_foreign_host_are_refused(server):
    base, token, *_ = server
    assert call(base, "wrong", "/api/state")[0] == 403
    assert call(base, token, "/api/state", host="evil.example:80")[0] == 403
    assert call(base, token, "/api/decide", {"i": 0, "status": "accepted"}, host="evil.example")[0] == 403
    assert call(base, token, "/api/state")[0] == 200


def test_page_image_and_security_headers(server):
    base, token, *_ = server
    code, body, hdr = call(base, token, "/page/0.png?dpi=80")
    assert code == 200 and body[:8] == b"\x89PNG\r\n\x1a\n"
    assert "no-store" in hdr["Cache-Control"] and "frame-ancestors 'none'" in hdr["Content-Security-Policy"]
    assert call(base, token, "/page/9.png")[0] == 404


def test_state_never_contains_document_text(server):
    base, token, d, _ = server
    raw = call(base, token, "/api/state")[1].decode()
    for g in d.gold:
        assert d.text[g.start:g.end] not in raw


def test_decide_add_ack_and_export_flow(server):
    base, token, d, tmp = server
    s = state(base, token)
    assert not s["can_export"] and s["blockers"]
    for dec in s["decisions"]:
        call(base, token, "/api/decide", {"i": dec["i"], "status": "accepted"})
    for f in s["findings"]:
        if f["severity"] == "review":
            call(base, token, "/api/ack", {"code": f["code"], "on": True})
    code, body, _ = call(base, token, "/api/add", {"page": 0, "rect": [10, 10, 80, 22]})
    assert code == 200
    s = json.loads(body)["state"]
    assert s["can_export"] and any(x["source"] == "manual" for x in s["decisions"])
    out = tmp / "out.pdf"
    res = json.loads(call(base, token, "/api/export", {"out": str(out), "attest": ATTEST})[1])
    assert res["ok"] and out.exists() and res["report"]["passed"]
    saved = json.loads((tmp / "dec.json").read_text())          # autosave happened, text-free
    assert len(saved["decisions"]) == len(s["decisions"])
    for g in d.gold:
        assert d.text[g.start:g.end] not in json.dumps(saved)


def test_export_is_refused_while_decisions_are_open_and_validates_output_path(server):
    base, token, _, tmp = server
    res = json.loads(call(base, token, "/api/export", {"out": str(tmp / "o.pdf"), "attest": ATTEST})[1])
    assert not res["ok"] and res["blockers"] and not (tmp / "o.pdf").exists()
    assert not json.loads(call(base, token, "/api/export", {"out": str(tmp / "o.txt"), "attest": ATTEST})[1])["ok"]
    assert not json.loads(call(base, token, "/api/export", {"out": str(tmp / "nodir" / "o.pdf"), "attest": ATTEST})[1])["ok"]



def test_export_requires_attestations_and_writes_evidence(server):
    base, token, _, tmp = server
    for dec in state(base, token)["decisions"]:
        call(base, token, "/api/decide", {"i": dec["i"], "status": "accepted"})
    for f in state(base, token)["findings"]:
        if f["severity"] == "review":
            call(base, token, "/api/ack", {"code": f["code"], "on": True})
    out = tmp / "out.pdf"
    # No attestations: the server refuses and writes nothing.
    res = json.loads(call(base, token, "/api/export", {"out": str(out)})[1])
    assert res["ok"] is False and "attestation" in res["message"]
    assert not out.exists()
    # One missing: still refused, the missing id is reported.
    res = json.loads(call(base, token, "/api/export",
                          {"out": str(out), "attest": ATTEST[:2]})[1])
    assert res["ok"] is False and res["missing_attestations"] == ["authorised"]
    # Complete: export succeeds and the evidence report is written next to the output.
    res = json.loads(call(base, token, "/api/export",
                          {"out": str(out), "attest": ATTEST})[1])
    assert res["ok"] and res["report"]["passed"]
    evidence = Path(res["evidence"])
    assert evidence.exists()
    text = evidence.read_text(encoding="utf-8")
    assert "Reviewer attestations" in text and "Residual risk" in text
    assert "Inputs for your DPIA / ROPA" in text
    assert "not a compliance certificate" in text
    assert "special_categories" in json.dumps(res["state"]["attestation"])


def test_bad_input_is_a_400_not_a_crash(server):
    base, token, *_ = server
    assert call(base, token, "/api/decide", {"i": 999, "status": "accepted"})[0] == 400
    assert call(base, token, "/api/decide", {"i": 0, "status": "bogus"})[0] == 400
    assert call(base, token, "/api/add", {"page": 0, "rect": [1, 1, 2, 2]})[0] == 400   # too small
    assert call(base, token, "/api/ack", {"code": "PF999", "on": True})[0] == 400
    assert call(base, token, "/api/decide", {})[0] == 400                      # missing fields


def test_only_manual_boxes_can_be_removed(server):
    base, token, *_ = server
    s = state(base, token)
    first = s["decisions"][0]["i"]
    assert call(base, token, "/api/remove", {"i": first})[0] == 400
    r = json.loads(call(base, token, "/api/add", {"page": 0, "rect": [10, 10, 80, 22]})[1])["state"]
    manual = [x["i"] for x in r["decisions"] if x["source"] == "manual"][0]
    r = json.loads(call(base, token, "/api/remove", {"i": manual})[1])["state"]
    assert next(x for x in r["decisions"] if x["i"] == manual)["status"] == "rejected"


def test_repeats_endpoint_proposes_but_never_accepts(server):
    base, token, d, _ = server
    s = state(base, token)
    for dec in s["decisions"]:
        call(base, token, "/api/decide", {"i": dec["i"], "status": "accepted"})
    assert call(base, "wrong", "/api/repeats", {})[0] == 403
    res = json.loads(call(base, token, "/api/repeats", {})[1])
    assert res["added"] == len([x for x in res["state"]["decisions"] if x["source"] == "repeat"])
    assert all(x["status"] == "pending" for x in res["state"]["decisions"] if x["source"] == "repeat")
    again = json.loads(call(base, token, "/api/repeats", {})[1])
    assert again["added"] == res["added"] or again["added"] == 0     # a second press adds no duplicates
