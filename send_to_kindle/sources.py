"""Files and text into Documents.

The engine itself only recognises files by their first bytes (kind_of); opening a file to read it
(from_bytes, from_text, with_text) happens in the converter, a separate process with no network
(converter.py). The engine never downloads anything: files and text are handed to it.
"""

from __future__ import annotations

import re

from . import extract
from .document import Document, sniff

WEB_PAGE = "html"


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
