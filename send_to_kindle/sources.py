"""Files and text into Documents.

The engine itself only downloads and recognises files by their first bytes (from_url, kind_of); opening
a file to read it (from_bytes, from_text, with_text) happens in the converter, a separate process with no
network (converter.py).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

from . import extract
from .config import Config
from .document import Document, sniff

UA = "send-to-kindle (+https://github.com/kaikisegfault/send-to-kindle)"
MAX_FETCH = 60 * 1024 * 1024
MAX_REDIRECTS = 5
WEB_PAGE = "html"


def _public(host: str, port: int) -> None:
    """Refuse a host that is, or resolves to, anything but the public internet (this server, a private
    network, a cloud's metadata address…), so a link can't reach inside."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"can't find {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"{host} is not on the public internet")


def download(url: str, cfg: Config) -> tuple[bytes, str, str]:
    """GET a file link, following redirects only to public addresses. Returns data, its type, the name."""
    import requests

    for _ in range(MAX_REDIRECTS + 1):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"not a web link: {url[:200]}")
        _public(u.hostname, u.port or (443 if u.scheme == "https" else 80))
        try:
            with requests.get(url, headers={"User-Agent": UA}, timeout=(15, 60), stream=True, allow_redirects=False) as r:
                if r.is_redirect and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                r.raise_for_status()
                chunks, size = [], 0
                for chunk in r.iter_content(256 * 1024):
                    size += len(chunk)
                    if size > MAX_FETCH:
                        raise ValueError("the file is over 60 MB")
                    chunks.append(chunk)
                name = urlparse(r.url).path.rsplit("/", 1)[-1]
                cd = re.search(r'filename="?([^";]+)"?', r.headers.get("Content-Disposition", ""))
                return b"".join(chunks), r.headers.get("Content-Type", ""), (cd.group(1) if cd else name) or "document"
        except requests.RequestException as e:
            raise RuntimeError(f"could not fetch {url[:200]}: {e}") from e
    raise ValueError("too many redirects")


def kind_of(data: bytes, name: str, content_type: str = "") -> str:
    """A file's format from its first bytes, type and name, without opening it."""
    return sniff(data, name, content_type)


def stem(name: str) -> str:
    return re.sub(r"\.[a-z0-9]{2,5}$", "", name, flags=re.I) or "document"


# ------------------------------------------------- in the converter only --

def from_bytes(data: bytes, *, name: str, origin: str, content_type: str = "") -> Document:
    fmt = sniff(data, name, content_type)
    base = stem(name)
    if fmt == "pdf":
        paragraphs, meta = extract.pdf_paragraphs(data)
        pdf_title = meta.get("pdf_title") or ""
        junk = not pdf_title or pdf_title.lower().startswith(("microsoft", "untitled")) or pdf_title.lower().endswith((".doc", ".docx", ".pdf"))
        meta["scan"] = extract.is_scan(meta)
        return Document("pdf", base if junk else pdf_title, origin, "pdf", data=data, body=extract.paragraphs_html(paragraphs),
                        text="\n\n".join(paragraphs), meta=meta)
    if fmt == "epub":
        return Document("epub", extract.epub_title(data) or base, origin, "epub", data=data, text=extract.epub_text(data))
    if fmt == "docx":
        return Document("docx", base, origin, "docx", data=data, text=extract.pandoc_text(data, "docx"))
    if fmt == WEB_PAGE:
        page = data.decode("utf-8", "replace")
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        body = re.sub(r"^.*?<body[^>]*>|</body>.*$", "", page, flags=re.S | re.I)
        body = re.sub(r"<(script|style|noscript)\b.*?</\1>", "", body, flags=re.S | re.I)
        return Document("html", extract.html_text(title.group(1)).strip() if title else base, origin, "html", data=data,
                        body=body, text=extract.html_text(body))
    if fmt in ("txt", "md"):
        text = data.decode("utf-8", "replace")
        heading = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), None) if fmt == "md" else None
        return from_text(text, title=heading or base, origin=origin, markdown=(fmt == "md"))
    raise ValueError(f"a {fmt} file can't be converted to EPUB: send it as it is")


def from_text(text: str, *, title: str, origin: str = "text", markdown: bool = True) -> Document:
    """Text as given. Markdown keeps its formatting; plain text becomes escaped paragraphs."""
    if markdown:
        plain = extract.pandoc_text(text.encode("utf-8"), "markdown")
        return Document("markdown", title, origin, "epub", body=text, text=plain)
    paragraphs = extract.text_paragraphs(text)
    return Document("text", title, origin, "epub", body=extract.paragraphs_html(paragraphs), text="\n\n".join(paragraphs))


def with_text(doc: Document) -> Document:
    """A scanned PDF gets its text by OCR, once, when it is converted."""
    if doc.kind == "pdf" and doc.meta.get("scan") and not doc.meta.get("ocr"):
        paragraphs, meta = extract.ocr_paragraphs(doc.data)
        doc.body, doc.text = extract.paragraphs_html(paragraphs), "\n\n".join(paragraphs)
        doc.meta.update(meta)
    return doc
