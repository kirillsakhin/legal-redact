import json
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
    res = json.loads(call(base, token, "/api/export", {"out": str(out)})[1])
    assert res["ok"] and out.exists() and res["report"]["passed"]
    saved = json.loads((tmp / "dec.json").read_text())          # autosave happened, text-free
    assert len(saved["decisions"]) == len(s["decisions"])
    for g in d.gold:
        assert d.text[g.start:g.end] not in json.dumps(saved)


def test_export_is_refused_while_decisions_are_open_and_validates_output_path(server):
    base, token, _, tmp = server
    res = json.loads(call(base, token, "/api/export", {"out": str(tmp / "o.pdf")})[1])
    assert not res["ok"] and res["blockers"] and not (tmp / "o.pdf").exists()
    assert not json.loads(call(base, token, "/api/export", {"out": str(tmp / "o.txt")})[1])["ok"]
    assert not json.loads(call(base, token, "/api/export", {"out": str(tmp / "nodir" / "o.pdf")})[1])["ok"]


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
