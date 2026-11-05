"""Command line.

    python -m send_to_kindle serve                       run the HTTP service
    python -m send_to_kindle convert-stdio               one conversion on stdin/stdout (the converter service)
    python -m send_to_kindle send <file link|inbox/…|item ID> [--epub|--original] [--title T]
    python -m send_to_kindle convert <file> <out.epub>   no mail: just build and check the EPUB
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import check, converter, jobs
from .config import Config


def _source(arg: str) -> dict:
    if arg.startswith(("http://", "https://")):
        return {"url": arg}
    if arg.startswith("inbox/"):
        return {"path": arg}
    if arg.isdigit():
        return {"id": int(arg)}
    raise SystemExit(f"{arg}: give a file link, an inbox path (inbox/…) or an item ID")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="send_to_kindle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("convert-stdio")
    s = sub.add_parser("send")
    s.add_argument("source")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--epub", action="store_true", help="convert to EPUB")
    g.add_argument("--original", action="store_true", help="the file exactly as it is")
    s.add_argument("--title")
    c = sub.add_parser("convert")
    c.add_argument("source")
    c.add_argument("out")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from .server import serve

        serve(Config.load(need_token=True))
    elif a.cmd == "convert-stdio":
        converter.serve_stdio()
    elif a.cmd == "send":
        fmt = "epub" if a.epub else "original" if a.original else None
        res = jobs.send(Config.load(), {"source": _source(a.source), "format": fmt, "title": a.title})
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif a.cmd == "convert":
        src = Path(a.source)
        epub, title, chk = converter.build("file", src.name, src.read_bytes(), None)
        Path(a.out).write_bytes(epub)
        print(f"{a.out}: {title}, {len(epub) // 1024} KB, {check.describe(chk) or 'no check'}", file=sys.stderr)


if __name__ == "__main__":
    main()
