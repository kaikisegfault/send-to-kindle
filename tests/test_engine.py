"""Engine tests that need no network and no mail: python -m unittest discover tests"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from send_to_kindle import check, convert, extract, jobs, sources, youtube
from send_to_kindle.config import Config
from send_to_kindle.library import Library

VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%
so<00:00:00.500><c> today</c><00:00:01.000><c> we</c>

00:00:02.000 --> 00:00:02.010 align:start position:0%
so today we

00:00:02.010 --> 00:00:04.000 align:start position:0%
so today we
talk<00:00:03.000><c> about</c><00:00:03.500><c> books</c>

00:01:05.000 --> 00:01:07.000
and &amp; that's all
"""


def make_pdf(pages: list[str]) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def cfg(tmp: str) -> Config:
    env = {"STK_WORK_DIR": tmp, "STK_LIBRARY_CHECKOUT": str(Path(tmp) / "lib"), "STK_KINDLE_EMAIL": "kindle@example.com",
           "STK_SMTP_USER": "me@example.com", "STK_SMTP_PASSWORD": "x", "STK_LIBRARY_REPO": ""}
    with mock.patch.dict(os.environ, env, clear=False):
        os.environ.pop("STK_LIBRARY_REPO", None)
        return Config.load()


class Text(unittest.TestCase):
    def test_join_lines_rejoins_broken_words(self):
        self.assertEqual(extract.join_lines(["an exam-", "ple of well-", "Known"]), "an example of well- Known")

    def test_vtt_folds_rolling_captions(self):
        paras = youtube.vtt_paragraphs(VTT)
        self.assertEqual(paras, ["so today we talk about books", "and & that's all"])

    def test_compare_counts_missing_words(self):
        r = check.compare("one two three three", "one three title page")
        self.assertEqual(r["source_words"], 4)
        self.assertEqual(r["kept_words"], 2)
        self.assertEqual(set(r["missing"]), {"two", "three"})

    def test_pick_track_prefers_human_captions_in_the_videos_language(self):
        info = {"language": "ru", "subtitles": {"ru": [{"ext": "vtt", "url": "m"}]},
                "automatic_captions": {"ru-orig": [{"ext": "vtt", "url": "a"}], "en": [{"ext": "vtt", "url": "t"}]}}
        self.assertEqual(youtube.pick_track(info)[0], "ru")
        info["subtitles"] = {}
        self.assertEqual(youtube.pick_track(info)[0], "ru-orig")

    def test_video_ids(self):
        self.assertEqual(youtube.video_id("https://youtu.be/abc123XYZ"), "abc123XYZ")
        self.assertEqual(youtube.video_id("https://www.youtube.com/watch?v=abc123XYZ&t=4"), "abc123XYZ")
        self.assertEqual(youtube.video_id("https://www.youtube.com/shorts/abc123XYZ"), "abc123XYZ")


class Pdf(unittest.TestCase):
    def test_pdf_to_epub_keeps_every_word_and_flags_empty_pages(self):
        body = "Chapter one. " + " ".join(f"word{i}" for i in range(300)) + " The <b>end</b> & *stars* #hash."
        data = make_pdf([body, "", "Second page text with a well-\nknown phrase.\n\n7"])
        doc = sources.from_bytes(data, name="Book.pdf", origin="test")
        self.assertEqual(doc.kind, "pdf")
        self.assertEqual(doc.meta["empty_pages"], [2])
        epub, ext, chk = convert.prepare(doc, "epub")
        self.assertEqual(ext, "epub")
        self.assertEqual(chk["kept_pct"], 100.0, chk)
        text = extract.epub_text(epub)
        self.assertIn("<b>end</b>", text)  # markup in a PDF stays text
        self.assertIn("*stars* #hash", text)
        self.assertEqual(chk["pages_without_text"], [2])

    def test_pdf_original_is_sent_untouched(self):
        data = make_pdf(["hello"])
        doc = sources.from_bytes(data, name="a.pdf", origin="t")
        out, ext, chk = convert.prepare(doc, None)
        self.assertIs(out, doc.data)
        self.assertEqual(ext, "pdf")


class Styling(unittest.TestCase):
    """What made Kindle draw boxes behind text and strand headings: colours in the CSS, split headings."""

    def build(self, markdown: str) -> dict:
        import io
        import zipfile

        doc = sources.from_text(markdown, title="Wishlist")
        epub, _, chk = convert.prepare(doc, "epub")
        z = zipfile.ZipFile(io.BytesIO(epub))
        css = "".join(z.read(n).decode() for n in z.namelist() if n.endswith(".css"))
        pages = {n: z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml")}
        return {"css": css, "pages": pages, "check": chk, "epub": epub}

    def test_css_sets_no_colours_backgrounds_or_fonts(self):
        css = self.build("# Tier 1\n\n## A book\n\nWhy it matters.\n")["css"]
        rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for banned in ("background", "color", "font-family: Georgia", "line-height"):
            self.assertNotIn(banned, rules, banned)
        self.assertIn(".keep", rules)

    def test_heading_is_kept_with_its_text(self):
        r = self.build("# Tier 1\n\n## Flowers for Algernon\n\nA short novel.\n\n## 1984\n\n- one\n- two\n")
        body = "".join(r["pages"].values())
        keep = r'<(?:div|section)[^>]*class="[^"]*\bkeep\b[^"]*"[^>]*>\s*'
        self.assertRegex(body, keep + r'<h2[^>]*>Flowers for Algernon</h2>\s*<p>A short novel.</p>\s*</(?:div|section)>')
        self.assertRegex(body, keep + r'<h2[^>]*>1984</h2>\s*<ul>')
        self.assertEqual(r["check"]["kept_pct"], 100.0)

    def test_headings_inside_wrappers_and_rebuilt_epubs_are_kept_too(self):
        if convert.pandoc_major() < 3:
            self.skipTest("pandoc 2.x trips over the keep filter on nested input (the engine then builds without it)")
        old = self.build("# Tier 1\n\n## Siddhartha\n\nA journey.\n\n## Fahrenheit 451\n\nBooks burn.\n")["epub"]
        doc = sources.from_bytes(old, name="old.epub", origin="t")
        rebuilt, _, chk = convert.prepare(doc, "epub")
        import io
        import zipfile

        z = zipfile.ZipFile(io.BytesIO(rebuilt))
        body = "".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml") and "nav" not in n)
        self.assertEqual(len(re.findall(r'class="[^"]*\bkeep\b', body)), 2, body)
        self.assertEqual(chk["kept_pct"], 100.0)
        page = "<div class='post'><div class='inner'><h2>Part one</h2><p>" + "words " * 40 + "</p><h3>Detail</h3><p>more text here</p></div></div>"
        from send_to_kindle.document import Document

        art = convert.to_epub(Document("article", "Wrapped", "t", "epub", body=page, text=extract.html_text(page)))
        z = zipfile.ZipFile(io.BytesIO(art))
        body = "".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml") and "nav" not in n)
        self.assertEqual(len(re.findall(r'class="[^"]*\bkeep\b', body)), 2, body)

    def test_no_title_page_where_pandoc_allows(self):
        if not convert._pandoc_has("--epub-title-page"):
            self.skipTest("this pandoc always writes a title page")
        self.assertFalse(any("title_page" in n for n in self.build("Text.\n")["pages"]))

    def test_language_from_script(self):
        self.assertEqual(convert.guess_lang("ეს არის ქართული ტექსტი წიგნების შესახებ"), "ka")
        self.assertEqual(convert.guess_lang("Это русский текст о книгах и чтении"), "ru")
        self.assertEqual(convert.guess_lang("This is about the books and reading"), "en")

    def test_epub_rebuilt_on_request_and_kept_as_is_otherwise(self):
        old = self.build("# Old\n\nSome text of an old book.\n")["epub"]
        doc = sources.from_bytes(old, name="old.epub", origin="t")
        same, ext, _ = convert.prepare(doc, None)
        self.assertIs(same, doc.data)
        rebuilt, ext, chk = convert.prepare(doc, "epub")
        self.assertEqual(ext, "epub")
        self.assertIsNot(rebuilt, doc.data)
        self.assertEqual(chk["kept_pct"], 100.0)


class Article(unittest.TestCase):
    def test_article_text_survives(self):
        paras = "".join(f"<p>Paragraph {i} says something about reading on a Kindle in bed, sentence number {i}.</p>" for i in range(12))
        page = f"<html lang='en'><head><title>Reading</title></head><body><nav>Home About Login</nav><article><h1>On reading</h1>{paras}</article><footer>Copyright</footer></body></html>"
        doc = sources.from_bytes(page.encode(), name="page.html", origin="https://example.com/a", content_type="text/html")
        self.assertEqual(doc.kind, "article")
        epub, ext, chk = convert.prepare(doc, None)
        self.assertEqual(ext, "epub")
        self.assertGreaterEqual(chk["kept_pct"], 99.0, chk)
        self.assertNotIn("Login", extract.epub_text(epub))


class Extractions(unittest.TestCase):
    def test_agree_superset_disagree(self):
        base = " ".join(f"w{i}" for i in range(500))
        self.assertFalse(sources.compare_extractions(base, base)["use_second"])
        fuller = base + " " + " ".join(f"extra{i}" for i in range(200))
        v = sources.compare_extractions(base, fuller)
        self.assertTrue(v["use_second"])
        other = " ".join(f"z{i}" for i in range(400))
        v = sources.compare_extractions(base, other)
        self.assertTrue(v["flag"])
        self.assertFalse(v["use_second"])


class Ocr(unittest.TestCase):
    def test_scanned_pdf_is_read_by_ocr(self):
        import shutil

        if not shutil.which("tesseract"):
            self.skipTest("tesseract is not installed here")
        import pymupdf

        text_pdf = pymupdf.open()
        page = text_pdf.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 400), "Reading on a Kindle in bed is calm and quiet.", fontsize=20)
        pix = page.get_pixmap(dpi=200)
        scan = pymupdf.open()
        scan.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=pix)
        doc = sources.from_bytes(scan.tobytes(), name="scan.pdf", origin="t")
        self.assertTrue(doc.meta["scan"])
        epub, ext, chk = convert.prepare(doc, "epub")
        self.assertIn("Kindle", extract.epub_text(epub))
        self.assertTrue(chk["ocr"])


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg(self.tmp.name)
        self.lib = Library(self.cfg)
        (self.lib.dir / "inbox").mkdir(parents=True)
        self.sent = []
        patcher = mock.patch("send_to_kindle.mail.to_kindle", side_effect=lambda c, d, f, t: self.sent.append((f, len(d))) or "<id>")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_inbox_pdf_sent_unchanged_is_stored_once(self):
        (self.lib.dir / "inbox" / "report.pdf").write_bytes(make_pdf(["quarterly report"]))
        res = jobs.send(self.cfg, {"source": {"path": "inbox/report.pdf"}})
        rows = self.lib.rows()
        self.assertEqual([r["id"] for r in rows], ["1", "2"])
        self.assertEqual(rows[0]["folder"], "user-provided")
        self.assertTrue(rows[0]["file"].endswith(".md"))
        self.assertIn("as item 2", self.lib.path(rows[0]).read_text())
        self.assertEqual(rows[1]["file"], "sent/0002-report.pdf")
        self.assertEqual(rows[1]["from"], "1")
        self.assertFalse((self.lib.dir / "inbox" / "report.pdf").exists())
        self.assertTrue(self.lib.path(rows[1]).read_bytes().startswith(b"%PDF"))
        self.assertEqual(res["id"], 2)

    def test_converted_inbox_pdf_keeps_the_original(self):
        (self.lib.dir / "inbox" / "book.pdf").write_bytes(make_pdf(["a book " * 50]))
        res = jobs.send(self.cfg, {"source": {"path": "inbox/book.pdf"}, "format": "epub"})
        rows = self.lib.rows()
        self.assertEqual(rows[0]["file"], "user-provided/0001-book.pdf")
        self.assertEqual(rows[1]["file"], "sent/0002-book.epub")
        self.assertEqual(res["check"]["kept_pct"], 100.0)

    def test_read_then_written_content(self):
        (self.lib.dir / "inbox" / "notes.md").write_text("# Notes\n\nSome **long** text to brief.\n")
        r = jobs.read(self.cfg, {"source": {"path": "inbox/notes.md"}})
        self.assertEqual(r["id"], 1)
        self.assertIn("long", r["text"])
        res = jobs.send(self.cfg, {"content": "# Brief\n\n- one point", "title": "Brief", "instructions": "ultra brief", "from": [r["id"]]})
        rows = self.lib.rows()
        self.assertEqual(rows[1]["mode"], "instructions")
        self.assertEqual(rows[1]["from"], "1")
        self.assertEqual(rows[1]["instructions"], "ultra brief")
        self.assertEqual(res["id"], 2)

    def test_formatted_one_to_one_is_checked_word_by_word(self):
        (self.lib.dir / "inbox" / "talk.md").write_text("# Talk\n\nso today we talk about books and how we read them\n")
        r = jobs.read(self.cfg, {"source": {"path": "inbox/talk.md"}})
        good = "# Talk\n\nSo today we talk about books, and how we read them."
        res = jobs.send(self.cfg, {"content": good, "title": "Talk", "faithful": True, "from": [r["id"]]})
        self.assertEqual(res["mode"], "1:1 formatted")
        self.assertEqual(res["check"]["kept_pct"], 100.0)
        dropped = "# Talk\n\nSo today we talk."
        with self.assertRaises(ValueError):
            jobs.send(self.cfg, {"content": dropped, "title": "Talk", "faithful": True, "from": [r["id"]]})

    def test_read_in_parts_extracts_once(self):
        (self.lib.dir / "inbox" / "long.md").write_text("# Long\n\n" + "word " * 30000)
        first = jobs.read(self.cfg, {"source": {"path": "inbox/long.md"}, "limit": 60000})
        self.assertIsNotNone(first["next_offset"])
        with mock.patch("send_to_kindle.jobs._load", side_effect=AssertionError("extracted again")):
            second = jobs.read(self.cfg, {"source": {"id": first["id"]}, "offset": first["next_offset"]})
        self.assertEqual(second["id"], first["id"])

    def test_remove_keeps_ids_unique(self):
        (self.lib.dir / "inbox" / "a.pdf").write_bytes(make_pdf(["a"]))
        jobs.send(self.cfg, {"source": {"path": "inbox/a.pdf"}})
        jobs.remove(self.cfg, {"ids": [1, 2], "reason": "test"})
        rows = self.lib.rows()
        self.assertTrue(all(r["status"] == "removed: test" for r in rows))
        self.assertFalse(self.lib.path(rows[1]).exists())
        self.assertEqual(self.lib.next_id(), 3)
        with self.assertRaises(ValueError):
            jobs.send(self.cfg, {"source": {"id": 2}})

    def test_failed_send_moves_nothing(self):
        (self.lib.dir / "inbox" / "x.pdf").write_bytes(make_pdf(["x"]))
        with mock.patch("send_to_kindle.mail.to_kindle", side_effect=RuntimeError("smtp down")):
            with self.assertRaises(RuntimeError):
                jobs.send(self.cfg, {"source": {"path": "inbox/x.pdf"}})
        rows = self.lib.rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["status"].startswith("failed"))
        self.assertTrue((self.lib.dir / "inbox" / "x.pdf").exists())


if __name__ == "__main__":
    unittest.main()
