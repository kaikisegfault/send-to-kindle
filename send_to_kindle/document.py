"""The one thing every step passes along: a fetched source, as a file to send or as text to convert."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# What Amazon's Send to Kindle takes by email, as files. Anything else is converted to EPUB.
KINDLE_FORMATS = {"pdf", "epub", "doc", "docx", "txt", "rtf", "htm", "html", "png", "gif", "jpg", "jpeg", "bmp"}


@dataclass
class Document:
    """A source, fetched and understood.

    kind: pdf | epub | docx | file (another Kindle format) | article | youtube | text | markdown
    data: the file itself, for kinds that are files.
    body: the readable content as HTML (article, youtube, text) or Markdown (markdown), for EPUB.
    text: the plain text a reader gets: what Claude reads and what the completeness check counts.
    """

    kind: str
    title: str
    origin: str
    ext: str
    data: bytes | None = None
    body: str | None = None
    text: str | None = None
    lang: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def is_file(self) -> bool:
        return self.data is not None


def slug(title: str, limit: int = 60) -> str:
    """A file-name-safe form of a title; letters of any script stay."""
    s = unicodedata.normalize("NFKC", title).lower()
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE).strip("-_")
    return (s[:limit].rstrip("-_") or "untitled")


def sniff(data: bytes, name: str = "", content_type: str = "") -> str:
    """The format of a file from its first bytes, then its type header, then its name."""
    head = data[:2048]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        if b"mimetypeapplication/epub+zip" in head:
            return "epub"
        if b"word/" in data[:65536] or name.lower().endswith(".docx"):
            return "docx"
    ct = content_type.split(";")[0].strip().lower()
    by_type = {
        "application/pdf": "pdf",
        "application/epub+zip": "epub",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/msword": "doc",
        "application/rtf": "rtf",
        "text/rtf": "rtf",
        "text/html": "html",
        "application/xhtml+xml": "html",
        "text/plain": "txt",
        "text/markdown": "md",
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/bmp": "bmp",
    }
    if ct in by_type:
        return by_type[ct]
    m = re.search(r"\.([a-z0-9]{2,5})$", name.lower())
    if m:
        return {"jpeg": "jpg", "htm": "html", "markdown": "md"}.get(m.group(1), m.group(1))
    if head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    return "bin"
