"""The converter: the only place a file is opened, in a process with no network.

On a server, systemd starts a fresh converter for each job (deploy/send-to-kindle-convert.socket and
send-to-kindle-convert@.service): its own throwaway user, no network at all, nothing on disk it can write
but its temporary files. A booby-trapped PDF meets a process that can reach nothing and keeps nothing.
The engine hands it the bytes over a Unix socket and gets the EPUB back.

    request   one JSON line {"kind": "file"|"markdown", "name": "x.pdf", "title": "…"|null, "size": N}, N bytes
    reply     one JSON line {"ok": true, "title": "…", "check": {…}, "size": M}, M bytes
              or            {"ok": false, "error": "…"}

Without STK_CONVERTER (tests, the command line) the engine converts in its own process.
"""

from __future__ import annotations

import json
import socket
import sys

from . import convert, sources
from .config import Config

MAX_INPUT = 64 * 1024 * 1024
MAX_HEADER = 64 * 1024


def build(kind: str, name: str, data: bytes, title: str | None) -> tuple[bytes, str, dict]:
    """The EPUB of a file or of Markdown, its title and its completeness check."""
    if kind == "markdown":
        doc = sources.from_text(data.decode("utf-8", "replace"), title=title or "Notes", markdown=True)
    else:
        doc = sources.from_bytes(data, name=name, origin=name)
    epub, _, chk = convert.prepare(doc, "epub", title)
    return epub, title or doc.title, chk


def _read_line(stream) -> dict:
    line = stream.readline(MAX_HEADER)
    if not line.endswith(b"\n"):
        raise ValueError("no request")
    return json.loads(line)


def serve_stdio() -> None:
    """One job on stdin/stdout (the socket systemd passed on)."""
    out = sys.stdout.buffer
    try:
        head = _read_line(sys.stdin.buffer)
        size = int(head["size"])
        if not 0 < size <= MAX_INPUT:
            raise ValueError("the file is empty or too large")
        data = sys.stdin.buffer.read(size)
        if len(data) != size:
            raise ValueError("the file arrived incomplete")
        epub, title, chk = build(str(head.get("kind")), str(head.get("name") or "document"), data, head.get("title"))
        out.write(json.dumps({"ok": True, "title": title, "check": chk, "size": len(epub)}, ensure_ascii=False).encode() + b"\n")
        out.write(epub)
    except Exception as e:  # noqa: BLE001 - every failure goes back as text
        out.write(json.dumps({"ok": False, "error": str(e) or type(e).__name__}, ensure_ascii=False).encode() + b"\n")
    out.flush()


def to_epub(cfg: Config, kind: str, name: str, data: bytes, title: str | None) -> tuple[bytes, str, dict]:
    """Convert through the converter process (or here, without STK_CONVERTER)."""
    if not cfg.converter:
        return build(kind, name, data, title)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(1800)
        s.connect(cfg.converter)
        head = {"kind": kind, "name": name, "title": title, "size": len(data)}
        s.sendall(json.dumps(head, ensure_ascii=False).encode() + b"\n" + data)
        s.shutdown(socket.SHUT_WR)
        reply = s.makefile("rb")
        head = _read_line(reply)
        if not head.get("ok"):
            raise ValueError(head.get("error") or "the converter failed")
        epub = reply.read(int(head["size"]))
    if len(epub) != int(head["size"]):
        raise RuntimeError("the converter's reply arrived incomplete")
    return epub, head["title"], head["check"]
