"""Command line.

    python -m send_to_kindle serve                       run the HTTP service
    python -m send_to_kindle send <link|file> [--epub] [--title T]
    python -m send_to_kindle read <link|file> [--offset N]
    python -m send_to_kindle convert <link|file> <out.epub>   no mail: just build and check the EPUB
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import check, convert, jobs, sources
from .config import Config


def _source(arg: str) -> dict:
    if arg.startswith(("http://", "https://")):
        return {"url": arg}
    if arg.startswith(("inbox/", "user-provided/", "sent/")):
        return {"path": arg}
    if arg.isdigit():
        return {"id": int(arg)}
    raise SystemExit(f"{arg}: give a link, a library path (inbox/…) or an item ID")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="send_to_kindle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    s = sub.add_parser("send")
    s.add_argument("source")
    s.add_argument("--epub", action="store_true", help="convert to EPUB even if it's a file Kindle takes")
    s.add_argument("--title")
    r = sub.add_parser("read")
    r.add_argument("source")
    r.add_argument("--offset", type=int, default=0)
    c = sub.add_parser("convert")
    c.add_argument("source")
    c.add_argument("out")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from .server import serve

        serve(Config.load(need_token=True))
    elif a.cmd == "send":
        res = jobs.send(Config.load(), {"source": _source(a.source), "format": "epub" if a.epub else None, "title": a.title})
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif a.cmd == "read":
        res = jobs.read(Config.load(need_mail=False), {"source": _source(a.source), "offset": a.offset})
        print(json.dumps({k: v for k, v in res.items() if k != "text"}, ensure_ascii=False, indent=2))
        print(res["text"])
    elif a.cmd == "convert":
        cfg = Config.load(need_mail=False)
        src = a.source
        doc = sources.from_url(src, cfg) if src.startswith(("http://", "https://")) else sources.from_path(Path(src), origin=src)
        data, ext, chk = convert.prepare(doc, "epub")
        Path(a.out).write_bytes(data)
        print(f"{a.out}: {len(data) // 1024} KB, {check.describe(chk) or 'no check'}", file=sys.stderr)


if __name__ == "__main__":
    main()
