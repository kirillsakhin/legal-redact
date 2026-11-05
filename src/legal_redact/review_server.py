"""Local review server: the lawyer's accept / reject / add interface.

Security model (it serves a confidential document, so this matters):
- binds to 127.0.0.1 only; no external network calls, no CDN, no telemetry;
- every request needs a random per-run token (URL for the page, header for the API),
  so another web page open in the same browser cannot drive it;
- the Host header must be this server's own address (DNS-rebinding defence);
- strict CSP and no-store caching; page images are rendered on demand, not written to disk;
- the API never returns document text, only boxes, categories, reasons and counts.
  (The page image of course shows the document: it is the lawyer's own screen.)
Decisions are autosaved to a text-free JSON file after every change.
"""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pymupdf

from .pipeline import ExportRefused, add_repeats, export, save_decisions
from .review import ReviewSession

MAX_BODY = 64 * 1024


class ReviewApp:
    def __init__(self, src: str, session: ReviewSession, decisions_path: str | None = None):
        self.src = str(Path(src).resolve())
        self.session = session
        self.decisions_path = decisions_path
        self.lock = threading.Lock()
        self.doc = pymupdf.open(self.src)
        self.sizes = [(p.rect.width, p.rect.height) for p in self.doc]
        self.png_cache: dict[tuple[int, int], bytes] = {}
        p = Path(self.src)
        self.out_default = str(p.with_name(p.stem + ".redacted.pdf"))

    # -- state ---------------------------------------------------------------
    def state(self) -> dict:
        s = self.session
        return {
            "name": Path(self.src).name,
            "pages": [{"w": w, "h": h} for w, h in self.sizes],
            "decisions": [
                {"i": i, "page": d.page, "category": d.category, "confidence": round(d.confidence, 2),
                 "reason": d.reason, "detector": d.detector, "proposed": d.proposed,
                 "status": d.status, "source": d.source, "rects": [list(r) for r in d.rects]}
                for i, d in enumerate(s.decisions)],
            "findings": [{"code": f.code, "severity": f.severity.value, "message": f.message,
                          "page": f.page, "ack": f.code in s.acknowledged} for f in s.findings],
            "blockers": s.blockers(),
            "can_export": s.can_export(),
            "out_default": self.out_default,
        }

    def _save(self) -> None:
        if self.decisions_path:
            save_decisions(self.session, self.decisions_path)

    # -- actions (all under the lock) -------------------------------------------
    def decide(self, i: int, status: str) -> None:
        if status not in ("accepted", "rejected", "pending"):
            raise ValueError("bad status")
        self.session.decisions[i].status = status
        self._save()

    def add_box(self, page: int, rect) -> None:
        w, h = self.sizes[page]
        x0, y0, x1, y1 = (float(v) for v in rect)
        x0, x1 = sorted((max(0, x0), min(w, x1)))
        y0, y1 = sorted((max(0, y0), min(h, y1)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise ValueError("box too small")
        self.session.add_manual_rect(page, (x0, y0, x1, y1))
        self._save()

    def remove_box(self, i: int) -> None:
        self.session.remove_manual(i)
        self._save()

    def ack(self, code: str, on: bool) -> None:
        if code not in {f.code for f in self.session.findings}:
            raise ValueError("unknown finding")
        self.session.acknowledge(code, on)
        self._save()

    def repeats(self) -> int:
        n = add_repeats(self.src, self.session)       # proposed as undecided, never auto-accepted
        self._save()
        return n

    def bulk(self) -> int:
        n = self.session.accept_all_proposed_redactions()
        self._save()
        return n

    def do_export(self, out: str) -> dict:
        out_p = Path(out).expanduser().resolve()
        if out_p.suffix.lower() != ".pdf":
            return {"ok": False, "message": "output file must end in .pdf"}
        if not out_p.parent.is_dir():
            return {"ok": False, "message": "output folder does not exist"}
        try:
            rep = export(self.src, str(out_p), self.session)
        except ExportRefused as exc:
            return {"ok": False, "message": str(exc), "blockers": self.session.blockers(),
                    "report": exc.report.to_dict() if exc.report else None}
        return {"ok": True, "out": str(out_p), "report": rep.to_dict()}

    def page_png(self, n: int, dpi: int) -> bytes:
        dpi = max(50, min(200, dpi))
        key = (n, dpi)
        if key not in self.png_cache:
            self.png_cache[key] = self.doc[n].get_pixmap(dpi=dpi).tobytes("png")
        return self.png_cache[key]


def make_server(app: ReviewApp, port: int = 0):
    token = secrets.token_urlsafe(24)
    html = resources.files("legal_redact.web").joinpath("review.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "legal-redact-review"

        def log_message(self, *a):          # URLs carry the token; keep logs silent
            pass

        # -- guards ----------------------------------------------------------------
        def _guard(self, query) -> bool:
            host = (self.headers.get("Host") or "").lower()
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if host not in allowed:
                self._send(403, b"bad host", "text/plain")
                return False
            given = self.headers.get("X-Review-Token") or (query.get("t") or [""])[0]
            if not secrets.compare_digest(given, token):
                self._send(403, b"bad token", "text/plain")
                return False
            return True

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        # -- routes ----------------------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._guard(q):
                return
            if u.path == "/":
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path == "/api/state":
                with app.lock:
                    return self._json(app.state())
            if u.path.startswith("/page/") and u.path.endswith(".png"):
                try:
                    n = int(u.path[6:-4])
                    dpi = int((q.get("dpi") or ["110"])[0])
                    with app.lock:
                        data = app.page_png(n, dpi)
                except (ValueError, IndexError):
                    return self._send(404, b"no such page", "text/plain")
                return self._send(200, data, "image/png")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            if not self._guard(parse_qs(u.query)):
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > MAX_BODY:
                    return self._send(413, b"too large", "text/plain")
                body = json.loads(self.rfile.read(n) or b"{}")
                with app.lock:
                    if u.path == "/api/decide":
                        app.decide(int(body["i"]), str(body["status"]))
                    elif u.path == "/api/add":
                        app.add_box(int(body["page"]), body["rect"])
                    elif u.path == "/api/remove":
                        app.remove_box(int(body["i"]))
                    elif u.path == "/api/ack":
                        app.ack(str(body["code"]), bool(body["on"]))
                    elif u.path == "/api/repeats":
                        return self._json({"added": app.repeats(), "state": app.state()})
                    elif u.path == "/api/bulk":
                        return self._json({"accepted": app.bulk(), "state": app.state()})
                    elif u.path == "/api/export":
                        res = app.do_export(str(body.get("out") or app.out_default))
                        res["state"] = app.state()
                        return self._json(res)
                    else:
                        return self._send(404, b"not found", "text/plain")
                    return self._json({"state": app.state()})
            except (KeyError, ValueError, IndexError, TypeError, json.JSONDecodeError) as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd, token


def serve(src: str, session: ReviewSession, decisions_path: str | None, port: int = 0,
          open_browser: bool = True) -> None:
    app = ReviewApp(src, session, decisions_path)
    httpd, token = make_server(app, port)
    url = f"http://127.0.0.1:{httpd.server_port}/?t={token}"
    print(f"Review page (local only, this run only): {url}", flush=True)
    print("Press Ctrl+C to stop. Decisions are autosaved"
          + (f" to {decisions_path}" if decisions_path else "") + ".", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
