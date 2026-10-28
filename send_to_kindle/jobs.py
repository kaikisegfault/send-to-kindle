"""The requests: send something to the Kindle, read a source's text, remove items, and mail in (inbound.py).

Fetching, OCR and conversion run outside the library lock, so a long job (a scanned book, a long video)
doesn't hold the others up; only the library's IDs, files and git history are one writer at a time.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from . import check, convert, mail, sources
from .config import Config
from .document import Document, slug
from .library import Library, note

LOCK = threading.Lock()
FAITHFUL_MIN = 98.0  # a source reformatted by an assistant must keep at least this share of its words
URL_CACHE_SECONDS = 24 * 3600


# ------------------------------------------------------------------ sources --

def _load(lib: Library, cfg: Config, src: dict) -> tuple[Document, str, Path | None, dict | None]:
    """The document a source points at, its label for the ledger, the inbox file it came from (if any)
    and the library row it already is (if any)."""
    if src.get("url"):
        return sources.from_url(src["url"].strip(), cfg), src["url"].strip(), None, None
    if src.get("path"):
        p = lib.resolve(src["path"])
        rel = f"{p.parent.name}/{p.name}"
        doc = sources.from_path(p, origin=rel)
        if p.parent.name == "inbox":
            return doc, rel, p, None
        row = next((r for r in lib.rows() if r["file"] == rel), None)
        return doc, rel, None, row
    if src.get("id") is not None:
        row = lib.find(int(src["id"]))
        if not row:
            raise ValueError(f"there is no item {src['id']}")
        if row["status"].startswith("removed"):
            raise ValueError(f"item {row['id']} was removed")
        p = lib.path(row)
        if p.suffix == ".md" and row["source"].startswith(("http://", "https://")):
            return sources.from_url(row["source"], cfg), row["source"], None, row
        return sources.from_path(p, origin=row["file"]), row["file"], None, row
    if src.get("text"):
        title = src.get("title") or "Text"
        return sources.from_text(src["text"], title=title, markdown=bool(src.get("markdown", True))), "text", None, None
    raise ValueError("a source needs one of: url, path (in the library), id (an item) or text")


def _provided(lib: Library, doc: Document, label: str, inbox_file: Path | None, *, keep_text: bool, sent_as: int | None = None,
              sent_unchanged: bool = False, instructions: str = "", mode: str = "1:1", title: str | None = None) -> dict:
    """Log what the request was given. Files keep their original unless the Kindle gets them unchanged
    (then sent/ has the file and this is a note); links and text become .md notes."""
    title = title or doc.title
    common = dict(mode=mode, source=label, status="provided", instructions=instructions)
    if inbox_file is not None and not sent_unchanged:
        return lib.add("user-provided", title, inbox_file.suffix.lstrip(".").lower() or doc.ext, move=inbox_file, format=doc.ext, **common)
    lines = [f"Source: {label}", f"Kind: {doc.kind}"]
    if doc.meta.get("channel"):
        lines.append(f"Channel: {doc.meta['channel']}")
    if sent_unchanged:
        lines.append(f"Sent 1:1, unchanged, as item {sent_as}")
    text = doc.text if keep_text and not doc.is_file else None
    if doc.kind in ("text", "markdown") and not doc.origin.startswith(("inbox", "user-provided", "sent")):
        text = doc.body if doc.kind == "markdown" else doc.text
    return lib.add("user-provided", title, "md", data=note(title, lines, text), format="md", **common)


# -------------------------------------------------------------- text cache --
# What a read extracted (a transcript, OCR text) is kept, so reading it in parts, or checking a
# reformatted version against it, doesn't fetch or OCR the source again.

def _cache_file(cfg: Config, key: str) -> Path:
    return cfg.work_dir / "text-cache" / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")


def _cache_get(cfg: Config, key: str | None) -> dict | None:
    if not key:
        return None
    f = _cache_file(cfg, key)
    if not f.exists():
        return None
    entry = json.loads(f.read_text(encoding="utf-8"))
    if key.startswith("url:") and time.time() - entry.get("time", 0) > URL_CACHE_SECONDS:
        return None
    return entry


def _cache_put(cfg: Config, keys: list[str | None], doc: Document, item_id: int) -> dict:
    entry = {"id": item_id, "title": doc.title, "kind": doc.kind, "text": doc.text or "", "time": time.time(),
             "meta": {k: v for k, v in doc.meta.items() if k != "pdf_title"}}
    for key in filter(None, keys):
        f = _cache_file(cfg, key)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    return entry


def _key(src: dict) -> str | None:
    for k in ("url", "path", "id"):
        if src.get(k) is not None and src.get(k) != "":
            return f"{k}:{str(src[k]).strip()}"
    return None


def _source_text(lib: Library, cfg: Config, item_id: int) -> str:
    """The text of an item, as read before (cache), as kept in its note, or extracted again."""
    cached = _cache_get(cfg, f"id:{item_id}")
    if cached:
        return cached["text"]
    row = lib.find(item_id)
    if not row:
        raise ValueError(f"there is no item {item_id}")
    p = lib.path(row)
    if p.suffix == ".md" and p.exists():
        kept = p.read_text(encoding="utf-8")
        if "\n---\n" in kept:
            return kept.split("\n---\n", 1)[1]
    doc, *_ = _load(lib, cfg, {"id": item_id})
    return sources.with_text(doc).text or ""


# ---------------------------------------------------------------- requests --

def send(cfg: Config, req: dict) -> dict:
    """1:1 (a source, converted only if asked), 1:1 formatted (a source reformatted by Claude, checked
    word by word against it) or instructions mode (content Claude wrote)."""
    lib = Library(cfg)
    title = (req.get("title") or "").strip() or None
    instructions = (req.get("instructions") or "").strip()
    with LOCK:
        lib.sync()

    existing = inbox_file = None
    if req.get("content"):
        doc = sources.from_text(req["content"], title=title or "Notes", markdown=True)
        data, ext, chk = convert.prepare(doc, "epub", title)
        from_ids = [str(i) for i in req.get("from") or []]
        if req.get("faithful"):
            if not from_ids:
                raise ValueError("a formatted 1:1 needs its source item in `from`")
            source_text = "\n\n".join(_source_text(lib, cfg, int(i)) for i in from_ids)
            chk = check.faithful(source_text, doc.text or "")
            if chk["kept_pct"] < FAITHFUL_MIN:
                raise ValueError(f"the formatted text leaves out source words ({check.describe(chk)}): "
                                 "fix it, or send the source 1:1 as it is")
            mode, label = "1:1 formatted", "formatted by Claude"
        else:
            mode, label = "instructions", "written by Claude"
        unchanged = False
    else:
        doc, label, inbox_file, existing = _load(lib, cfg, req.get("source") or {})
        data, ext, chk = convert.prepare(doc, req.get("format"), title)
        mode = "1:1"
        from_ids = [existing["id"]] if existing else []
        unchanged = doc.is_file and data is doc.data
    title = title or doc.title

    with LOCK:
        lib.sync()
        # Send first: nothing is filed, and no inbox file moves, unless the mail went out.
        try:
            mail.to_kindle(cfg, data, f"{slug(title)}.{ext}", title)
        except Exception as e:
            lib.log("sent", mode=mode, format=ext, source=label, **{"from": " ".join(from_ids)},
                    check=check.describe(chk), status=f"failed: {e}", instructions=instructions)
            lib.commit(f"kindle: failed to send {title}")
            raise
        if mode == "1:1" and existing is None:
            provided = _provided(lib, doc, label, inbox_file if (inbox_file and inbox_file.exists()) else None, keep_text=False,
                                 sent_as=lib.next_id() + 1, sent_unchanged=unchanged, title=title)
            from_ids = [provided["id"]]
        fields = dict(mode=mode, format=ext, source=label, check=check.describe(chk), status="sent", instructions=instructions)
        fields["from"] = " ".join(from_ids)
        if unchanged and inbox_file is not None and inbox_file.exists():
            row = lib.add("sent", title, ext, move=inbox_file, **fields)
        else:
            row = lib.add("sent", title, ext, data=data, **fields)
        lib.commit(f"kindle: {row['id']} sent {title}")
    return {
        "id": int(row["id"]),
        "file": row["file"],
        "title": title,
        "format": ext,
        "size_kb": round(len(data) / 1024),
        "mode": mode,
        "from": [int(i) for i in from_ids if i.isdigit()],
        "check": chk,
        "check_text": check.describe(chk),
    }


def read(cfg: Config, req: dict) -> dict:
    """A source's full text for Claude to work from, in parts; the source is logged as user-provided once."""
    offset = int(req.get("offset") or 0)
    limit = min(int(req.get("limit") or 60000), 120000)
    lib = Library(cfg)
    src = req.get("source") or {}
    key = _key(src)
    entry = _cache_get(cfg, key)
    if entry is None:
        with LOCK:
            lib.sync()
        doc, label, inbox_file, existing = _load(lib, cfg, src)
        sources.with_text(doc)  # a scan is read by OCR
        with LOCK:
            lib.sync()
            if existing is not None:
                item_id = int(existing["id"])
            else:
                known = lib.find_source(label) if label.startswith(("http://", "https://")) else None
                if known:
                    item_id = int(known["id"])
                else:
                    row = _provided(lib, doc, label, inbox_file, keep_text=True, mode="instructions",
                                    instructions=(req.get("instructions") or "").strip())
                    item_id = int(row["id"])
                    lib.commit(f"kindle: {item_id} provided {doc.title}")
        entry = _cache_put(cfg, [key, f"id:{item_id}"], doc, item_id)
    text = entry["text"]
    part = text[offset:offset + limit]
    end = offset + len(part)
    return {
        "id": entry["id"],
        "title": entry["title"],
        "kind": entry["kind"],
        "words": sum(check.words(text).values()),
        "chars": len(text),
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "meta": entry["meta"],
        "text": part,
    }


def remove(cfg: Config, req: dict) -> dict:
    """Delete items' files from the library; their ledger rows stay, marked removed, so no ID comes back."""
    ids = [str(int(i)) for i in req.get("ids") or []]
    if not ids:
        raise ValueError("name the items to remove: ids")
    reason = (req.get("reason") or "").strip() or "on request"
    with LOCK:
        lib = Library(cfg)
        lib.sync()
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
    with LOCK:
        lib = Library(cfg)
        lib.sync()
        files = [{"path": f"inbox/{p.name}", "kb": round(p.stat().st_size / 1024)} for p in lib.inbox()]
        recent = lib.rows()[-10:]
    return {"inbox": files, "recent": [{k: r[k] for k in ("id", "date", "folder", "file", "status")} for r in recent]}


def status(cfg: Config, item_id: int) -> dict:
    lib = Library(cfg)
    row = lib.find(item_id)
    if not row:
        raise ValueError(f"there is no item {item_id}")
    return row
