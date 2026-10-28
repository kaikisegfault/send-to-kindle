"""Turn a request's source into a Document: a link, a file in the library, or text."""

from __future__ import annotations

import re
from pathlib import Path

from . import extract, youtube
from .config import Config
from .document import KINDLE_FORMATS, Document, sniff

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
MAX_FETCH = 60 * 1024 * 1024
BLOCKED = {401, 403, 429, 503}


def from_url(url: str, cfg: Config) -> Document:
    if not re.match(r"^https?://", url):
        raise ValueError(f"not a web link: {url}")
    if youtube.is_youtube(url):
        return youtube.fetch(url, cfg)
    data, ctype, final = _download(url, cfg)
    name = final.rsplit("/", 1)[-1].split("?")[0]
    return from_bytes(data, name=name, origin=url, content_type=ctype)


def _download(url: str, cfg: Config) -> tuple[bytes, str, str]:
    """GET the link; a site that refuses this server is asked again through the YouTube proxy, if any."""
    import requests

    proxies = [None]
    if cfg.youtube_proxy:
        proxies.append({"http": cfg.youtube_proxy, "https": cfg.youtube_proxy})
    last = None
    for proxy in proxies:
        try:
            with requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=(15, 60), stream=True, proxies=proxy) as r:
                if r.status_code in BLOCKED and proxy is None and len(proxies) > 1:
                    last = f"HTTP {r.status_code}"
                    continue
                r.raise_for_status()
                chunks, size = [], 0
                for chunk in r.iter_content(256 * 1024):
                    size += len(chunk)
                    if size > MAX_FETCH:
                        raise ValueError("the file is over 60 MB")
                    chunks.append(chunk)
                return b"".join(chunks), r.headers.get("Content-Type", ""), r.url
        except requests.RequestException as e:
            last = str(e)
    raise RuntimeError(f"could not fetch {url}: {last}")


def from_bytes(data: bytes, *, name: str, origin: str, content_type: str = "") -> Document:
    fmt = sniff(data, name, content_type)
    stem = re.sub(r"\.[a-z0-9]{2,5}$", "", name, flags=re.I) or "document"
    if fmt == "pdf":
        paragraphs, meta = extract.pdf_paragraphs(data)
        pdf_title = meta.get("pdf_title") or ""
        junk = not pdf_title or pdf_title.lower().startswith(("microsoft", "untitled")) or pdf_title.lower().endswith((".doc", ".docx", ".pdf"))
        title = stem if junk else pdf_title
        meta["scan"] = extract.is_scan(meta)
        return Document("pdf", title, origin, "pdf", data=data, body=extract.paragraphs_html(paragraphs), text="\n\n".join(paragraphs), meta=meta)
    if fmt == "epub":
        return Document("epub", extract.epub_title(data) or stem, origin, "epub", data=data, text=extract.epub_text(data))
    if fmt == "docx":
        return Document("docx", stem, origin, "docx", data=data, text=extract.pandoc_text(data, "docx"))
    if fmt == "html":
        page = data.decode("utf-8", "replace")
        art = extract.article(page, origin)
        if not art:
            raise ValueError("no article text found on this page (it may need a browser to show its content)")
        meta = {k: art[k] for k in ("author", "site", "date") if art[k]}
        body, text = art["body"], art["text"]
        try:
            second = extract.second_extraction(page)
        except Exception:  # noqa: BLE001 - the second opinion is optional
            second = ""
        verdict = compare_extractions(text, second)
        meta["second_extractor"] = verdict["note"]
        if verdict["use_second"]:
            # The second extractor holds everything the first found and more: nothing is lost by using it.
            body, text = extract.paragraphs_html(extract.text_paragraphs(second.replace("\n", "\n\n"))), second
        if verdict["flag"]:
            meta["check_page"] = verdict["note"]
        return Document("article", art["title"] or stem, origin, "epub", body=body, text=text, lang=art["lang"], meta=meta)
    if fmt in ("txt", "md"):
        return from_text(data.decode("utf-8", "replace"), title=stem, origin=origin, markdown=(fmt == "md"))
    if fmt in KINDLE_FORMATS:
        return Document("file", stem, origin, fmt, data=data)
    raise ValueError(f"Kindle can't take this file type ({fmt})")


def compare_extractions(first: str, second: str) -> dict:
    """Two independent extractions of one article: agree, second is a fuller superset (use it), or they
    disagree (flag it, so the page is looked at)."""
    from .check import words

    a, b = words(first), words(second)
    na, nb = sum(a.values()), sum(b.values())
    if not nb:
        return {"use_second": False, "flag": False, "note": "second extractor found nothing"}
    only_second = sum(n - a[w] for w, n in b.items() if n > a[w])
    only_first = sum(n - b[w] for w, n in a.items() if n > b[w])
    if only_second <= max(60, 0.03 * nb):
        return {"use_second": False, "flag": False, "note": f"agrees ({nb:,} words)"}
    if only_first <= max(30, 0.03 * na) and nb <= 2 * na:
        return {"use_second": True, "flag": False, "note": f"found {only_second:,} more words; used the fuller text"}
    return {"use_second": False, "flag": True,
            "note": f"disagrees: {only_second:,} words only in the second extraction, {only_first:,} only in the first"}


def from_text(text: str, *, title: str, origin: str = "text", markdown: bool = True) -> Document:
    """Text as given. Markdown keeps its formatting; plain text becomes escaped paragraphs."""
    if markdown:
        plain = extract.pandoc_text(text.encode("utf-8"), "markdown")
        return Document("markdown", title, origin, "epub", body=text, text=plain)
    paragraphs = extract.text_paragraphs(text)
    return Document("text", title, origin, "epub", body=extract.paragraphs_html(paragraphs), text="\n\n".join(paragraphs))


def with_text(doc: Document) -> Document:
    """A scanned PDF gets its text by OCR, once, when its text is needed (to read or to convert)."""
    if doc.kind == "pdf" and doc.meta.get("scan") and not doc.meta.get("ocr"):
        paragraphs, meta = extract.ocr_paragraphs(doc.data)
        doc.body, doc.text = extract.paragraphs_html(paragraphs), "\n\n".join(paragraphs)
        doc.meta.update(meta)
    return doc


def from_path(path: Path, origin: str) -> Document:
    data = path.read_bytes()
    if path.suffix.lower() == ".md":
        text = data.decode("utf-8", "replace")
        title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), path.stem)
        return from_text(text, title=title, origin=origin, markdown=True)
    return from_bytes(data, name=path.name, origin=origin)
