"""Documents to Kindle files: EPUB built by pandoc from the extracted content, then checked."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from . import check, extract
from .document import KINDLE_FORMATS, Document


def to_epub(doc: Document, title: str | None = None) -> bytes:
    """One EPUB of the document's content. No cover, no table of contents: the text, start to end."""
    title = title or doc.title
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.epub"
        if doc.kind == "docx":
            src, fmt = Path(tmp) / "in.docx", "docx"
            src.write_bytes(doc.data)
        elif doc.kind == "markdown":
            src, fmt = Path(tmp) / "in.md", "markdown"
            src.write_text(doc.body, encoding="utf-8")
        else:
            if doc.body is None:
                raise ValueError(f"nothing to convert in this {doc.kind}")
            src, fmt = Path(tmp) / "in.html", "html"
            src.write_text(f"<html><body>{doc.body}</body></html>", encoding="utf-8")
        cmd = ["pandoc", "-f", fmt, "-t", "epub3", "-o", str(out), "--metadata", f"title={title}"]
        if doc.lang:
            cmd += ["--metadata", f"lang={doc.lang}"]
        author = doc.meta.get("author") or doc.meta.get("channel") or doc.meta.get("site")
        if author:
            cmd += ["--metadata", f"author={author}"]
        cmd.append(str(src))
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"pandoc failed: {r.stderr.decode()[:400]}")
        data = out.read_bytes()
    check.validate_epub(data)
    return data


def prepare(doc: Document, fmt: str | None, title: str | None = None) -> tuple[bytes, str, dict]:
    """The file to send and its completeness check.

    fmt "original": the file itself, untouched (files in a Kindle format only).
    fmt "epub": converted, then every word of the source looked for in the result.
    No fmt: files stay as they are, everything else becomes EPUB.
    """
    if fmt not in (None, "original", "epub"):
        raise ValueError(f"unknown format {fmt!r}: use original or epub")
    if fmt is None:
        fmt = "original" if doc.is_file and doc.ext in KINDLE_FORMATS else "epub"
    if fmt == "original":
        if not doc.is_file or doc.ext not in KINDLE_FORMATS:
            raise ValueError(f"a {doc.kind} has no original file to send: use epub")
        return doc.data, doc.ext, {}
    if doc.kind == "epub":
        return doc.data, "epub", {}
    if doc.kind == "pdf" and not (doc.text or "").strip():
        raise ValueError(f"this PDF is a scan ({doc.meta.get('pages')} pages of images, no text): send it as the original PDF")
    data = to_epub(doc, title)
    result = check.compare(doc.text or "", extract.epub_text(data))
    if doc.kind == "pdf":
        result["pages"] = doc.meta.get("pages")
        if doc.meta.get("empty_pages"):
            result["pages_without_text"] = doc.meta["empty_pages"]
        if doc.meta.get("running_headers_removed"):
            result["running_headers_removed"] = doc.meta["running_headers_removed"]
        second = extract.pdftotext_words(doc.data)
        if second is not None:
            result["pdftotext_words"] = second
    return data, "epub", result
