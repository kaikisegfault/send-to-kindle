"""The HTTP service: a small JSON API behind a bearer token, for a connector, a script or the email inbox.

    POST /send     {"source": {...}, "format": "original"|"epub", "title": "..."}   1:1
                   {"content": "markdown", "title": "...", "instructions": "...", "from": [ids]}   written content
                   {"content": "markdown", "faithful": true, "from": [id]}   a source reformatted, checked word by word
    POST /read     {"source": {...}, "offset": 0}        the source's full text, in parts (OCR for scans)
    POST /remove   {"ids": [ids], "reason": "..."}       delete items' files; their rows stay, marked removed
    GET  /inbox                                          files waiting in the library's inbox/
    GET  /item/<id>                                      one ledger row, as "item"
    POST /inbound  raw email (message/rfc822)            the email inbox; answers at once, works after
    GET  /health

A source is {"url": "..."}, {"path": "inbox/file.pdf"}, {"id": 12} or {"text": "...", "title": "..."}.
Long jobs answer within ~50 s; if one takes longer the reply says it is still working, the ledger shows
the result when it is done, and a short mail tells the sender address how it ended.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import inbound, jobs
from .config import Config

log = logging.getLogger("send-to-kindle")
MAX_BODY = 40 * 1024 * 1024
WAIT = 50


def _late_report(cfg: Config, what: str, box: dict) -> None:
    """A job that outlasted its request: tell the owner by mail how it ended."""
    from . import check, mail

    if "error" in box:
        subject, text = f"Kindle: {what} failed", box["error"]
    else:
        r = box["result"]
        subject = f"Kindle: {what} done"
        text = f"Item {r.get('id')}: {r.get('title')} ({r.get('format', '')}). {check.describe(r.get('check') or {})}".strip()
    try:
        mail.notify(cfg, cfg.sender, subject, text)
    except Exception:  # noqa: BLE001
        log.error("late report failed\n%s", traceback.format_exc())


def run_with_deadline(cfg: Config, what: str, fn, *args) -> dict:
    box: dict = {}
    guard = threading.Lock()

    def work():
        try:
            box["result"] = fn(*args)
        except Exception as e:  # noqa: BLE001 - reported to the caller as text
            log.error("job failed: %s\n%s", e, traceback.format_exc())
            box["error"] = str(e)
        with guard:
            late = box.get("late")
        if late:
            _late_report(cfg, what, box)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(WAIT)
    with guard:
        if "result" not in box and "error" not in box:
            box["late"] = True
            return {"status": "working", "note": "still working; it carries on, the ledger shows the result and a mail reports it"}
    if "error" in box:
        return {"status": "error", "error": box["error"]}
    return {"status": "ok", **box["result"]}


def make_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "send-to-kindle"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.command, self.path.split("?")[0])

        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            return bool(cfg.token) and hmac.compare_digest(given.encode(), cfg.token.encode())

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request too large")
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            if self.path == "/health":
                return self._reply(200, {"status": "ok"})
            if not self._authorized():
                return self._reply(401, {"status": "error", "error": "unauthorized"})
            try:
                if self.path == "/inbox":
                    return self._reply(200, {"status": "ok", **jobs.inbox(cfg)})
                if self.path.startswith("/item/"):
                    return self._reply(200, {"status": "ok", "item": jobs.status(cfg, int(self.path.rsplit("/", 1)[-1]))})
            except Exception as e:  # noqa: BLE001
                return self._reply(400, {"status": "error", "error": str(e)})
            return self._reply(404, {"status": "error", "error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._reply(401, {"status": "error", "error": "unauthorized"})
            try:
                raw = self._body()
            except ValueError as e:
                return self._reply(413, {"status": "error", "error": str(e)})
            if self.path == "/inbound":
                threading.Thread(target=self._inbound, args=(raw,), daemon=True).start()
                return self._reply(202, {"status": "accepted"})
            try:
                req = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._reply(400, {"status": "error", "error": "body is not JSON"})
            if self.path == "/send":
                result = run_with_deadline(cfg, "send", jobs.send, cfg, req)
            elif self.path == "/read":
                result = run_with_deadline(cfg, "read", jobs.read, cfg, req)
            elif self.path == "/remove":
                result = run_with_deadline(cfg, "remove", jobs.remove, cfg, req)
            else:
                return self._reply(404, {"status": "error", "error": "not found"})
            return self._reply(200 if result["status"] != "error" else 422, result)

        @staticmethod
        def _inbound(raw: bytes) -> None:
            try:
                log.info("inbound: %s", inbound.handle(cfg, raw))
            except Exception:  # noqa: BLE001
                log.error("inbound failed\n%s", traceback.format_exc())

    return Handler


def serve(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    httpd = ThreadingHTTPServer((cfg.listen, cfg.port), make_handler(cfg))
    log.info("send-to-kindle listening on %s:%s", cfg.listen, cfg.port)
    httpd.serve_forever()
