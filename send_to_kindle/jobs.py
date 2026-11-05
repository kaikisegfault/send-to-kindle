"""The requests: send a file to the Kindle as it is or converted to EPUB, send text as EPUB, one-time upload
addresses for files from elsewhere, remove items, list the inbox, look up one item.

The engine never opens a file: it recognises it by its first bytes, and a conversion happens in the
converter (converter.py), one at a time and outside the library lock. Each library step works on a fresh
copy of the library (library.py), one writer at a time.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import check, converter, mail, sources
from .config import Config
from .document import KINDLE_FORMATS, slug
from .library import Library, note

LOCK = threading.Lock()  # the library: one writer at a time
CONVERT = threading.Lock()  # one conversion at a time: the server comes second to everything else on it
AS_IS = {"pdf", "epub", "doc", "docx", "rtf", "png", "gif", "jpg", "jpeg", "bmp"}  # sent as they are unless EPUB is asked for
CONVERTIBLE = {"pdf", "epub", "docx", "html", "md", "txt"}
UPLOAD_SECONDS = 600
TOKEN = re.compile(r"[0-9a-f]{64}")


@dataclass
class Source:
    data: bytes
    name: str  # a file name, for the format and the title
    fmt: str  # pdf, epub, md, html…
    label: str  # for the ledger: the link, inbox/x.pdf, the item's file, "written in a chat", "uploaded"
    inbox: str | None = None  # the inbox file it came from, which leaves the inbox once sent
    item: dict | None = None  # the library item it is, when sent by ID


def _source(cfg: Config, src: dict) -> Source:
    if src.get("url"):
        url = src["url"].strip()
        data, ctype, name = sources.download(url, cfg)
        fmt = sources.kind_of(data, name, ctype)
        if fmt == "html":
            raise ValueError("that link is a web page, not a file: read the page and send its text as content")
        return Source(data, name, fmt, url)
    if src.get("path"):
        with LOCK, Library(cfg) as lib:
            p = lib.resolve(src["path"])
            data = p.read_bytes()
        return Source(data, p.name, sources.kind_of(data, p.name), f"inbox/{p.name}", inbox=f"inbox/{p.name}")
    if src.get("id") is not None:
        with LOCK, Library(cfg) as lib:
            row = lib.find(int(src["id"]))
            if not row:
                raise ValueError(f"there is no item {src['id']}")
            if row["status"].startswith("removed"):
                raise ValueError(f"item {row['id']} was removed")
            if not row["file"]:
                raise ValueError(f"item {row['id']} has no file")
            if row["file"].endswith(".md") and row["source"].startswith(("http://", "https://")):
                raise ValueError(f"item {row['id']} is a note about a link, not a document")
            p = lib.path(row)
            if not p.exists():
                raise ValueError(f"item {row['id']}'s file is missing")
            data = p.read_bytes()
        name = re.sub(r"^\d{4}-", "", p.name)
        return Source(data, name, sources.kind_of(data, name), row["file"], item=row)
    raise ValueError("a source needs one of: url (a file link), path (a file in inbox/) or id (a library item)")


def _wants_epub(fmt_asked: str | None, src: Source) -> bool:
    if fmt_asked not in (None, "original", "epub"):
        raise ValueError(f"unknown format {fmt_asked!r}: use original or epub")
    epub = fmt_asked == "epub" or (fmt_asked is None and src.fmt not in AS_IS)
    if epub and src.fmt not in CONVERTIBLE:
        raise ValueError(f"a {src.fmt} file can't be converted to EPUB: send it as it is")
    if not epub and src.fmt not in KINDLE_FORMATS:
        raise ValueError(f"Kindle can't take a {src.fmt} file as it is: send it as epub")
    return epub


def _deliver(cfg: Config, src: Source, epub: bool, title: str | None) -> dict:
    """Convert if asked, mail the Kindle, then file both what was given and what went out."""
    if epub:
        with CONVERT:
            kind = "markdown" if src.label == "written in a chat" else "file"  # a file's own heading names it
            out, title, chk = converter.to_epub(cfg, kind, src.name, src.data, title)
        ext, mode = "epub", "converted"
    else:
        out, chk, ext, mode = src.data, {}, {"jpeg": "jpg", "htm": "html"}.get(src.fmt, src.fmt), "as is"
        title = title or sources.stem(src.name)
    title = (title or "Document").strip()[:200]

    with LOCK, Library(cfg) as lib:
        from_ids = [src.item["id"]] if src.item else []
        # Send first: nothing is filed, and no inbox file moves, unless the mail went out.
        try:
            mail.to_kindle(cfg, out, f"{slug(title)}.{ext}", title)
        except Exception as e:
            lib.log("sent", mode=mode, format=ext, source=src.label, **{"from": " ".join(from_ids)},
                    check=check.describe(chk), status=f"failed: {e}")
            lib.commit(f"kindle: failed to send {title}")
            raise
        if src.item is None:
            common = dict(mode=mode, source=src.label, status="provided")
            if epub:  # the original, which the EPUB was made from
                given = lib.add("user-provided", title, src.fmt, data=src.data, format=src.fmt, **common)
            else:  # sent unchanged: sent/ has the file, this is only a note
                lines = [f"Source: {src.label}", f"Sent as it is, as item {lib.next_id() + 1}"]
                given = lib.add("user-provided", title, "md", data=note(title, lines), format="md", **common)
            from_ids = [given["id"]]
        row = lib.add("sent", title, ext, data=out, mode=mode, format=ext, source=src.label, check=check.describe(chk),
                      status="sent", **{"from": " ".join(from_ids)})
        if src.inbox:
            (lib.dir / src.inbox).unlink(missing_ok=True)
        lib.commit(f"kindle: {row['id']} sent {title}")
    return {
        "id": int(row["id"]),
        "file": row["file"],
        "title": title,
        "format": ext,
        "size_kb": round(len(out) / 1024),
        "mode": mode,
        "from": [int(i) for i in from_ids],
        "check": chk,
        "check_text": check.describe(chk),
    }


def send(cfg: Config, req: dict) -> dict:
    """A source (a file link, an inbox file, a library item) as it is or converted, or text as EPUB."""
    title = (req.get("title") or "").strip() or None
    if req.get("content"):
        if req.get("format") == "original":
            raise ValueError("text has no original file: it goes as EPUB")
        src = Source(req["content"].encode("utf-8"), "content.md", "md", "written in a chat")
        return _deliver(cfg, src, True, title or "Notes")
    src = _source(cfg, req.get("source") or {})
    return _deliver(cfg, src, _wants_epub(req.get("format"), src), title)


# ------------------------------------------------------------------ uploads --
# A one-time address for a file that can't come by link: one Claude made or was given in a chat, sent
# from Claude's own workspace with `curl -T file <url>`. The address is the credential: random, one
# use, 10 minutes. The request that makes it carries the engine's token.

def _slot_file(cfg: Config, token: str) -> Path:
    return cfg.work_dir / "uploads" / hashlib.sha256(token.encode()).hexdigest()


def upload_slot(cfg: Config, req: dict) -> dict:
    if not cfg.public_url:
        raise ValueError("uploads need STK_PUBLIC_URL")
    fmt = req.get("format")
    if fmt not in (None, "original", "epub"):
        raise ValueError(f"unknown format {fmt!r}: use original or epub")
    box = cfg.work_dir / "uploads"
    box.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time()
    for old in box.iterdir():  # expired, never used
        try:
            if json.loads(old.read_text())["expires"] < now:
                old.unlink()
        except (OSError, ValueError, KeyError):
            old.unlink(missing_ok=True)
    token = secrets.token_hex(32)
    slot = {"format": fmt, "title": (req.get("title") or "").strip() or None, "expires": now + UPLOAD_SECONDS}
    _slot_file(cfg, token).write_text(json.dumps(slot), encoding="utf-8")
    return {"url": f"{cfg.public_url}/upload/{token}/", "expires_in": UPLOAD_SECONDS}


def claim_upload(cfg: Config, token: str) -> dict:
    """The upload address's slot, used up now (one use, whatever happens next)."""
    if not TOKEN.fullmatch(token):
        raise PermissionError("not an upload address")
    f = _slot_file(cfg, token)
    try:
        slot = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PermissionError("this upload address was used already or has expired") from None
    f.unlink(missing_ok=True)
    if slot["expires"] < time.time():
        raise PermissionError("this upload address has expired")
    return slot


def upload(cfg: Config, slot: dict, filename: str, data: bytes) -> dict:
    """PUT /upload/<token>/<filename>: the file, sent the way its address was made for."""
    if not data:
        raise ValueError("the upload is empty")
    name = re.sub(r"[^\w.\- ]+", "", filename, flags=re.UNICODE).strip(". ")[:120] or "upload"
    src = Source(data, name, sources.kind_of(data, name), "uploaded")
    return _deliver(cfg, src, _wants_epub(slot.get("format"), src), slot.get("title"))


# ------------------------------------------------------------- the library --

def remove(cfg: Config, req: dict) -> dict:
    """Delete items' files from the library; their ledger rows stay, marked removed, so no ID comes back."""
    ids = [str(int(i)) for i in req.get("ids") or []]
    if not ids:
        raise ValueError("name the items to remove: ids")
    reason = (req.get("reason") or "").strip() or "on request"
    with LOCK, Library(cfg) as lib:
        rows = lib.rows()
        found = [r for r in rows if r["id"] in ids]
        missing = sorted(set(ids) - {r["id"] for r in found}, key=int)
        if missing:
            raise ValueError(f"there is no item {', '.join(missing)}")
        for r in found:
            if r["file"]:
                lib.path(r).unlink(missing_ok=True)
            r["status"] = f"removed: {reason}"
        lib.rewrite(rows)
        lib.commit(f"kindle: removed {', '.join(ids)} ({reason})")
    return {"removed": [int(i) for i in ids]}


def inbox(cfg: Config) -> dict:
    with LOCK, Library(cfg) as lib:
        files = [{"path": f"inbox/{p.name}", "kb": round(p.stat().st_size / 1024)} for p in lib.inbox()]
        recent = lib.rows()[-10:]
    return {"inbox": files, "recent": [{k: r[k] for k in ("id", "date", "folder", "file", "status")} for r in recent]}


def status(cfg: Config, item_id: int) -> dict:
    with LOCK, Library(cfg) as lib:
        row = lib.find(item_id)
    if not row:
        raise ValueError(f"there is no item {item_id}")
    return row
