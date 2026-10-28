"""Text out of files and web pages, by programs only: the same input always gives the same text."""

from __future__ import annotations

import html
import io
import re
import subprocess
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path

PAGE_NUMBER = re.compile(r"^\s*(?:page\s+)?\d{1,4}\s*$", re.IGNORECASE)


def join_lines(lines: list[str]) -> str:
    """Lines of one paragraph as one line: a word broken by a hyphen at a line end is joined again."""
    out = ""
    for line in (l.strip() for l in lines):
        if not line:
            continue
        if out.endswith("-") and len(out) > 1 and out[-2].isalpha() and line[:1].islower():
            out = out[:-1] + line
        else:
            out = f"{out} {line}" if out else line
    return out


MARGIN = 0.12  # top and bottom share of a page where running headers and footers sit


def _repeat_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "", text)).strip().lower()


def pdf_paragraphs(data: bytes) -> tuple[list[str], dict]:
    """Every text block of every page, in the PDF's reading order, as paragraphs.

    Nothing is summarised or reordered. Left out: blocks that are only a page number, and running
    headers and footers (a short line in the top or bottom margin that repeats on many pages, like the
    book's title with the page number). Pages with no text at all (scans) are listed in the result.
    """
    import pymupdf  # PyMuPDF

    pages_blocks: list[list[tuple[str, bool]]] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = doc.page_count
        title = (doc.metadata or {}).get("title") or ""
        for page in doc:
            height = page.rect.height or 1
            blocks = []
            for block in page.get_text("blocks"):
                if block[6] != 0:  # 0 = text, 1 = image
                    continue
                text = join_lines(block[4].splitlines())
                if text:
                    in_margin = block[3] <= height * MARGIN or block[1] >= height * (1 - MARGIN)
                    blocks.append((text, in_margin))
            pages_blocks.append(blocks)

    seen: dict[str, set[int]] = {}
    for number, blocks in enumerate(pages_blocks):
        for text, in_margin in blocks:
            key = _repeat_key(text)
            if in_margin and key and len(text) <= 100:
                seen.setdefault(key, set()).add(number)
    running = {k for k, on in seen.items() if len(on) >= max(3, pages // 5)}

    paragraphs: list[str] = []
    empty: list[int] = []
    removed: dict[str, str] = {}
    for number, blocks in enumerate(pages_blocks, start=1):
        if not blocks:
            empty.append(number)
        for text, in_margin in blocks:
            if PAGE_NUMBER.match(text):
                continue
            key = _repeat_key(text)
            if in_margin and key in running:
                removed.setdefault(key, text)
                continue
            paragraphs.append(text)
    meta = {"pages": pages, "empty_pages": empty, "pdf_title": title.strip()}
    if removed:
        meta["running_headers_removed"] = list(removed.values())[:5]
    return mend_breaks(paragraphs), meta


def mend_breaks(paragraphs: list[str]) -> list[str]:
    """Join a paragraph to the next when a column or page break cut it: a word split by a hyphen, or a
    sentence that goes on in lower case."""
    out: list[str] = []
    for p in paragraphs:
        if out and p[:1].islower():
            prev = out[-1]
            if prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha():
                out[-1] = prev[:-1] + p
                continue
            if prev[-1:].isalpha() or prev[-1:] in ",;":
                out[-1] = f"{prev} {p}"
                continue
        out.append(p)
    return out


SCAN_SHARE = 0.8  # a PDF with no text on at least this share of its pages is a scan
OCR_LANGS = ("eng", "rus", "kat")  # tried on the first page; the rest of the scan uses what it finds


def is_scan(meta: dict) -> bool:
    pages = meta.get("pages") or 0
    return bool(pages) and len(meta.get("empty_pages") or []) >= pages * SCAN_SHARE


def _tesseract(png: bytes, langs: str) -> str:
    import os

    env = dict(os.environ, OMP_THREAD_LIMIT="1")  # one core: the server's other work comes first
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "page.png"
        img.write_bytes(png)
        r = subprocess.run(["tesseract", str(img), "stdout", "-l", langs, "--psm", "3"], capture_output=True, env=env, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"tesseract failed: {r.stderr.decode()[:300]}")
    return r.stdout.decode("utf-8", "replace")


def _installed_langs() -> set[str]:
    try:
        r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30)
    except OSError:
        return set()
    return {l.strip() for l in r.stdout.splitlines()[1:] if l.strip()}


def ocr_paragraphs(data: bytes) -> tuple[list[str], dict]:
    """A scanned PDF's text by OCR (Tesseract, on this machine; nothing leaves it). Page by page, in order;
    page numbers and running headers left out as for text PDFs. OCR can misread a word."""
    import pymupdf
    from .convert import guess_lang

    have = _installed_langs()
    if not have:
        raise ValueError("this PDF is a scan and OCR (tesseract) isn't installed: send it as the original PDF")
    langs = "+".join(l for l in OCR_LANGS if l in have) or "eng"
    pages_text: list[list[str]] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for number, page in enumerate(doc):
            png = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY).tobytes("png")
            text = _tesseract(png, langs)
            if number == 0:
                lang = guess_lang(text)
                langs = {"ka": "kat", "ru": "rus", "en": "eng"}.get(lang or "", langs)
                if langs not in have:
                    langs = "+".join(l for l in OCR_LANGS if l in have) or "eng"
            pages_text.append(text_paragraphs(text))
    # Running headers: a short first or last paragraph that repeats (digits aside) on many pages.
    count: dict[str, int] = {}
    for paras in pages_text:
        for p in {paras[0], paras[-1]} if paras else set():
            if len(p) <= 100:
                count[_repeat_key(p)] = count.get(_repeat_key(p), 0) + 1
    running = {k for k, n in count.items() if k and n >= max(3, len(pages_text) // 5)}
    paragraphs = [p for paras in pages_text for p in paras if not PAGE_NUMBER.match(p) and _repeat_key(p) not in running]
    return mend_breaks(paragraphs), {"ocr": True, "ocr_languages": langs}


def second_extraction(page: str) -> str:
    """The article text by a second, independent extractor (Mozilla's Readability), to check the first."""
    from readability import Document as Readable

    return html_text(Readable(page).summary(html_partial=True))


def pdftotext_words(data: bytes) -> int | None:
    """Word count by a second, independent extractor (poppler's pdftotext), to cross-check PyMuPDF."""
    try:
        r = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", "-", "-"], input=data, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    from .check import words

    text = r.stdout.decode("utf-8", "replace")
    text = "\n".join(l for l in text.splitlines() if not PAGE_NUMBER.match(l))
    return sum(words(text).values())


class _Text(HTMLParser):
    """Visible text of an XHTML/HTML document, one block per line."""

    BLOCKS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(markup: str) -> str:
    p = _Text()
    p.feed(markup)
    lines = (re.sub(r"\s+", " ", l).strip() for l in "".join(p.parts).splitlines())
    return "\n".join(l for l in lines if l)


def epub_text(data: bytes) -> str:
    """All readable text of an EPUB: its content files in spine order, without the table of contents."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        order = _spine(z) or sorted(n for n in names if n.endswith((".xhtml", ".html", ".htm")) and "nav" not in n)
        return "\n".join(html_text(z.read(n).decode("utf-8", "replace")) for n in order if n in names)


def _opf(z: zipfile.ZipFile):
    import xml.etree.ElementTree as ET

    try:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
        return path, ET.fromstring(z.read(path))
    except (KeyError, StopIteration, ET.ParseError):
        return None, None


def _spine(z: zipfile.ZipFile) -> list[str]:
    opf_path, opf = _opf(z)
    if opf is None:
        return []
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    items = {e.get("id"): e for e in opf.iter() if e.tag.endswith("}item")}
    order = []
    for ref in (e for e in opf.iter() if e.tag.endswith("}itemref")):
        item = items.get(ref.get("idref"))
        if item is not None and "nav" not in (item.get("properties") or "").split():
            order.append(base + item.get("href"))
    return order


def epub_title(data: bytes) -> str:
    """The EPUB's own title (dc:title), or "" if it has none."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        _, opf = _opf(z)
    if opf is None:
        return ""
    return next(((e.text or "").strip() for e in opf.iter() if e.tag.endswith("}title")), "")


def pandoc_text(data: bytes, fmt: str) -> str:
    """Plain text of a document pandoc reads (docx, html, markdown)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"in.{fmt}"
        src.write_bytes(data)
        r = subprocess.run(["pandoc", "-f", fmt, "-t", "plain", "--wrap=none", str(src)], capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"pandoc could not read the {fmt}: {r.stderr.decode()[:300]}")
        return r.stdout.decode("utf-8", "replace")


def paragraphs_html(paragraphs: list[str]) -> str:
    """Paragraphs as HTML, every character escaped: nothing in the text can turn into markup."""
    return "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)


def text_paragraphs(text: str) -> list[str]:
    """Plain text split into paragraphs at blank lines; lines inside a paragraph are joined."""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    return [p for p in (join_lines(b.splitlines()) for b in blocks) if p]


def article(page: str, url: str) -> dict | None:
    """The article in a web page (no menus, ads or comments), as HTML and as plain text."""
    import trafilatura

    options = dict(url=url, favor_recall=True, include_comments=False, include_tables=True, include_images=False, include_links=False, deduplicate=False)
    body = trafilatura.extract(page, output_format="html", include_formatting=True, **options)
    text = trafilatura.extract(page, output_format="txt", **options)
    if not body or not text:
        return None
    meta = trafilatura.extract_metadata(page, default_url=url)
    lang = None
    m = re.search(r"<html[^>]*\blang=[\"']?([a-zA-Z-]{2,10})", page[:5000])
    if m:
        lang = m.group(1)
    # trafilatura wraps the article in <html><body><main>…; pandoc reads the inner markup alone.
    body = re.sub(r"^.*?<body[^>]*>|</body>.*$", "", body, flags=re.S)
    return {
        "title": (meta.title if meta and meta.title else "").strip(),
        "author": (meta.author if meta and meta.author else "") or "",
        "site": (meta.sitename if meta and meta.sitename else "") or "",
        "date": (meta.date if meta and meta.date else "") or "",
        "body": body,
        "text": text,
        "lang": lang,
    }
