"""The completeness check: every word of the source has to be in the finished EPUB."""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter

WORD = re.compile(r"\w+", re.UNICODE)
LINE_BREAK_HYPHEN = re.compile(r"(?<=\w)-[ \t]*\n\s*(?=\w)")


def words(text: str) -> Counter:
    """Words of a text, lower case. A word broken by a hyphen at a line or paragraph end counts as one."""
    return Counter(w.lower() for w in WORD.findall(LINE_BREAK_HYPHEN.sub("", text)))


def compare(source_text: str, epub_plain: str) -> dict:
    """How many of the source's words the EPUB keeps (extra words, like the title page, don't count)."""
    src, out = words(source_text), words(epub_plain)
    total = sum(src.values())
    kept = sum(min(n, out[w]) for w, n in src.items())
    missing = Counter({w: n - out[w] for w, n in src.items() if n > out[w]})
    return {
        "source_words": total,
        "kept_words": kept,
        "kept_pct": round(100 * kept / total, 2) if total else 100.0,
        "missing": [w for w, _ in missing.most_common(15)],
    }


def describe(check: dict) -> str:
    if not check:
        return ""
    line = f"{check['kept_pct']:g}% of {check['source_words']:,} words kept"
    if check.get("missing"):
        line += " (missing: " + ", ".join(check["missing"][:8]) + ")"
    return line


def validate_epub(data: bytes) -> None:
    """The structure Amazon needs: mimetype first and stored, a container, an OPF and readable XHTML."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        first = z.infolist()[0]
        if first.filename != "mimetype" or z.read("mimetype") != b"application/epub+zip":
            raise ValueError("EPUB: mimetype is not the first entry")
        if first.compress_type != zipfile.ZIP_STORED:
            raise ValueError("EPUB: mimetype is compressed")
        ET.fromstring(z.read("META-INF/container.xml"))
        if not any(n.endswith(".opf") for n in z.namelist()):
            raise ValueError("EPUB: no package (OPF) file")
        for n in z.namelist():
            if n.endswith((".xhtml", ".opf", ".ncx")):
                try:
                    ET.fromstring(z.read(n))
                except ET.ParseError as e:
                    raise ValueError(f"EPUB: {n} is not well-formed ({e})") from e
