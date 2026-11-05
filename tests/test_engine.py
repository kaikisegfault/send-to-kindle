"""Engine tests that need no network and no mail: python -m unittest discover tests"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from send_to_kindle import check, convert, converter, extract, jobs, sources
from send_to_kindle.config import Config
from send_to_kindle.library import Library



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

    def test_compare_counts_missing_words(self):
        r = check.compare("one two three three", "one three title page")
        self.assertEqual(r["source_words"], 4)
        self.assertEqual(r["kept_words"], 2)
        self.assertEqual(set(r["missing"]), {"two", "three"})


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
    """Requests against a library on disk; mail is captured, not sent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg(self.tmp.name)
        self.lib = Library(self.cfg)
        (self.lib.dir / "inbox").mkdir(parents=True)
        self.sent = []
        patcher = mock.patch("send_to_kindle.mail.to_kindle", side_effect=lambda c, d, f, t: self.sent.append((f, d)) or "<id>")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_inbox_pdf_sent_as_is_is_stored_once_and_never_opened(self):
        (self.lib.dir / "inbox" / "report.pdf").write_bytes(make_pdf(["quarterly report"]))
        with mock.patch("send_to_kindle.converter.build", side_effect=AssertionError("opened")):
            res = jobs.send(self.cfg, {"source": {"path": "inbox/report.pdf"}})
        rows = self.lib.rows()
        self.assertEqual([r["id"] for r in rows], ["1", "2"])
        self.assertEqual(rows[0]["folder"], "user-provided")
        self.assertIn("as item 2", (self.lib.dir / rows[0]["file"]).read_text())
        self.assertEqual(rows[1]["file"], "sent/0002-report.pdf")
        self.assertEqual((rows[1]["from"], rows[1]["mode"]), ("1", "as is"))
        self.assertFalse((self.lib.dir / "inbox" / "report.pdf").exists())
        self.assertEqual(res["id"], 2)

    def test_converted_inbox_pdf_keeps_the_original(self):
        (self.lib.dir / "inbox" / "book.pdf").write_bytes(make_pdf(["a book " * 50]))
        res = jobs.send(self.cfg, {"source": {"path": "inbox/book.pdf"}, "format": "epub"})
        rows = self.lib.rows()
        self.assertEqual(rows[0]["file"], "user-provided/0001-book.pdf")
        self.assertEqual(rows[1]["file"], "sent/0002-book.epub")
        self.assertEqual(res["check"]["kept_pct"], 100.0)

    def test_text_from_a_chat_goes_as_epub(self):
        res = jobs.send(self.cfg, {"content": "# Brief\n\n- one point\n- another", "title": "Brief"})
        rows = self.lib.rows()
        self.assertEqual(rows[0]["file"], "user-provided/0001-brief.md")
        self.assertEqual((rows[1]["file"], rows[1]["source"]), ("sent/0002-brief.epub", "written in a chat"))
        self.assertEqual(res["check"]["kept_pct"], 100.0)
        with self.assertRaises(ValueError):
            jobs.send(self.cfg, {"content": "x", "format": "original"})

    def test_markdown_file_takes_its_heading_as_title(self):
        (self.lib.dir / "inbox" / "t.md").write_text("# A Talk\n\nso today we talk about books\n")
        res = jobs.send(self.cfg, {"source": {"path": "inbox/t.md"}})
        self.assertEqual((res["title"], res["format"]), ("A Talk", "epub"))

    def test_the_engine_takes_no_links(self):
        with self.assertRaisesRegex(ValueError, "takes no links"):
            jobs.send(self.cfg, {"source": {"url": "https://example.com/paper.pdf"}})
        self.assertFalse(hasattr(sources, "download"))

    def test_upload_address_works_once_and_expires(self):
        self.cfg = dataclasses.replace(self.cfg, public_url="https://kindle.example.com")
        slot = jobs.upload_slot(self.cfg, {"format": "original", "title": "Made in a chat"})
        token = slot["url"].rstrip("/").rsplit("/", 1)[-1]
        self.assertTrue(slot["url"].startswith("https://kindle.example.com/upload/"))
        res = jobs.upload(self.cfg, jobs.claim_upload(self.cfg, token), "styled.pdf", make_pdf(["styled"]))
        self.assertEqual((res["title"], res["format"], res["mode"]), ("Made in a chat", "pdf", "as is"))
        with self.assertRaises(PermissionError):
            jobs.claim_upload(self.cfg, token)
        late = jobs.upload_slot(self.cfg, {})["url"].rstrip("/").rsplit("/", 1)[-1]
        with mock.patch("send_to_kindle.jobs.time.time", return_value=time.time() + 601):
            with self.assertRaises(PermissionError):
                jobs.claim_upload(self.cfg, late)
        with self.assertRaises(PermissionError):
            jobs.claim_upload(self.cfg, "../../etc/passwd")

    def test_remove_keeps_ids_unique(self):
        (self.lib.dir / "inbox" / "a.pdf").write_bytes(make_pdf(["a"]))
        jobs.send(self.cfg, {"source": {"path": "inbox/a.pdf"}})
        jobs.remove(self.cfg, {"ids": [1, 2], "reason": "test"})
        rows = self.lib.rows()
        self.assertTrue(all(r["status"] == "removed: test" for r in rows))
        self.assertFalse((self.lib.dir / rows[1]["file"]).exists())
        self.assertEqual(self.lib.next_id(), 3)
        with self.assertRaises(ValueError):
            jobs.send(self.cfg, {"source": {"id": 2}})

    def test_failed_send_moves_nothing(self):
        (self.lib.dir / "inbox" / "x.pdf").write_bytes(make_pdf(["x"]))
        with mock.patch("send_to_kindle.mail.to_kindle", side_effect=RuntimeError("mail down")):
            with self.assertRaises(RuntimeError):
                jobs.send(self.cfg, {"source": {"path": "inbox/x.pdf"}})
        rows = self.lib.rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["status"].startswith("failed"))
        self.assertTrue((self.lib.dir / "inbox" / "x.pdf").exists())

    def test_mail_in_takes_files_not_links(self):
        from email.message import EmailMessage
        from send_to_kindle import inbound

        self.cfg = dataclasses.replace(self.cfg, inbox_senders=["me@example.com"])
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "me@example.com", "kindle@example.com", "read this"
        msg.set_content("and this page: https://example.com/article")
        msg.add_attachment(make_pdf(["mailed"]), maintype="application", subtype="pdf", filename="mailed.pdf")
        with mock.patch("send_to_kindle.inbound._dkim_ok", return_value=True), mock.patch("send_to_kindle.mail.notify"):
            r = inbound.handle(self.cfg, msg.as_bytes())
        self.assertTrue(r["results"][0].startswith("sent 2: mailed"), r)
        self.assertIn("not taken", r["results"][1])


class Addresses(unittest.TestCase):
    """The firewall's allowed addresses: the library's git host and the mail service, nothing else."""

    def test_git_hosts(self):
        from send_to_kindle.addresses import git_host

        self.assertEqual(git_host("git@github.com:me/lib.git"), ("github.com", 22))
        self.assertEqual(git_host("ssh://git@git.example.com:2222/me/lib.git"), ("git.example.com", 2222))
        self.assertEqual(git_host("https://git.example.com/me/lib.git"), ("git.example.com", 443))
        self.assertIsNone(git_host(""))

    def test_sets_are_filled_from_the_published_lists(self):
        from send_to_kindle import addresses

        meta = json.dumps({"git": ["140.82.112.0/20", "20.201.28.151/32", "2606:50c0::/32"]}).encode()
        pages = {"https://api.github.com/meta": meta}
        api = ["104.19.192.174", "2606:4700:300a::6813:c0ae"]
        with mock.patch("send_to_kindle.addresses._get", side_effect=lambda u: pages[u]), \
             mock.patch("send_to_kindle.addresses._resolve", side_effect=lambda h: api if h == "api.cloudflare.com" else []):
            text = addresses.render(addresses.lists({"STK_LIBRARY_REPO": "git@github.com:me/lib.git", "STK_CF_EMAIL_TOKEN": "t"}))
        self.assertIn("add element inet send_to_kindle git4 { 140.82.112.0/20, 20.201.28.151/32 }", text)
        self.assertIn("add element inet send_to_kindle git6 { 2606:50c0::/32 }", text)
        self.assertIn("add element inet send_to_kindle git_port { 22 }", text)
        self.assertIn("add element inet send_to_kindle mail4 { 104.19.192.174/32 }", text)
        self.assertIn("add element inet send_to_kindle mail6 { 2606:4700:300a::6813:c0ae/128 }", text)
        self.assertIn("add element inet send_to_kindle mail_port { 443 }", text)
        self.assertEqual(text.count("flush set"), 6)


class Converter(unittest.TestCase):
    """The converter as systemd runs it: a fresh process per connection, the socket as its stdin/stdout."""

    def test_conversion_through_the_socket(self):
        import socket
        import subprocess
        import sys
        import threading

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "convert.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(2)

        def accept(n):
            for _ in range(n):
                conn, _ = server.accept()
                subprocess.run([sys.executable, "-m", "send_to_kindle", "convert-stdio"], stdin=conn, stdout=conn,
                               cwd=Path(__file__).resolve().parents[1], timeout=120)
                conn.close()

        threading.Thread(target=accept, args=(2,), daemon=True).start()
        c = dataclasses.replace(cfg(tmp), converter=path)
        epub, title, chk = converter.to_epub(c, "file", "Book.pdf", make_pdf(["Chapter one. " + "word " * 100]), None)
        self.assertTrue(epub.startswith(b"PK"))
        self.assertEqual((title, chk["kept_pct"]), ("Book", 100.0))
        with self.assertRaisesRegex(ValueError, "can't be converted"):
            converter.to_epub(c, "file", "photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100, None)
        server.close()


class GitLibrary(unittest.TestCase):
    """A library in a repository: a fresh copy for each job, nothing left on disk, files at the root."""

    def test_job_pushes_and_leaves_nothing(self):
        import subprocess

        tmp = tempfile.mkdtemp()
        bare, seed = os.path.join(tmp, "lib.git"), os.path.join(tmp, "seed")
        run = lambda *a, cwd=None: subprocess.run(a, cwd=cwd, check=True, capture_output=True)
        run("git", "init", "--bare", "-q", "-b", "main", bare)
        run("git", "-C", bare, "config", "uploadpack.allowFilter", "true")
        run("git", "init", "-q", "-b", "main", seed)
        os.makedirs(os.path.join(seed, "inbox"))
        Path(seed, "inbox", "report.pdf").write_bytes(make_pdf(["quarterly"]))
        Path(seed, "LEDGER.csv").write_text(",".join(["id", "date", "folder", "file", "mode", "format", "source", "from", "check", "status", "instructions"]) + "\n")
        for a in (("add", "-A"), ("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "seed"), ("push", "-q", bare, "main")):
            run("git", *a, cwd=seed)
        work = os.path.join(tmp, "work")
        env = {"STK_WORK_DIR": work, "STK_LIBRARY_REPO": f"file://{bare}", "STK_KINDLE_EMAIL": "k@example.com",
               "STK_SMTP_USER": "me@example.com", "STK_SMTP_PASSWORD": "x"}
        with mock.patch.dict(os.environ, env):
            c = Config.load()
        with mock.patch("send_to_kindle.mail.to_kindle", return_value="<id>"):
            res = jobs.send(c, {"source": {"path": "inbox/report.pdf"}})
        self.assertEqual(res["file"], "sent/0002-report.pdf")
        self.assertEqual([p for p in os.listdir(work) if p.startswith("library-")], [])
        files = run("git", "-C", bare, "ls-tree", "-r", "--name-only", "main").stdout.decode().split()
        self.assertIn("sent/0002-report.pdf", files)
        self.assertIn("user-provided/0001-report.md", files)
        self.assertNotIn("inbox/report.pdf", files)
        self.assertEqual(jobs.status(c, 2)["file"], "sent/0002-report.pdf")

class CloudflareMail(unittest.TestCase):
    ENV = {"STK_KINDLE_EMAIL": "reader@kindle.com", "STK_SENDER": "kindle@example.com",
           "STK_CF_ACCOUNT_ID": "acct", "STK_CF_EMAIL_TOKEN": "tok"}

    def test_send_goes_to_the_rest_api_without_smtp_settings(self):
        from send_to_kindle import mail
        with mock.patch.dict(os.environ, self.ENV, clear=True):
            cfg = Config.load()
        reply = mock.Mock(status_code=200)
        reply.json.return_value = {"success": True, "result": {"delivered": ["reader@kindle.com"], "permanent_bounces": []}}
        with mock.patch("send_to_kindle.mail.requests.post", return_value=reply) as post, \
             mock.patch("send_to_kindle.mail.smtplib.SMTP") as smtp, mock.patch("send_to_kindle.mail.smtplib.SMTP_SSL") as ssl_smtp:
            mail.to_kindle(cfg, b"PK\x03\x04 epub bytes", "book.epub", "A Book")
        smtp.assert_not_called()
        ssl_smtp.assert_not_called()
        url, = post.call_args.args
        self.assertEqual(url, "https://api.cloudflare.com/client/v4/accounts/acct/email/sending/send")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer tok"})
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["from"], body["to"], body["subject"]), ("kindle@example.com", "reader@kindle.com", "A Book"))
        self.assertEqual(body["text"].strip(), "Personal document: A Book")
        att, = body["attachments"]
        self.assertEqual((att["filename"], att["type"], att["disposition"]), ("book.epub", "application/epub+zip", "attachment"))
        self.assertEqual(base64.b64decode(att["content"]), b"PK\x03\x04 epub bytes")

    def test_a_refusal_raises(self):
        from send_to_kindle import mail
        with mock.patch.dict(os.environ, self.ENV, clear=True):
            cfg = Config.load()
        reply = mock.Mock(status_code=403, text="")
        reply.json.return_value = {"success": False, "errors": [{"message": "sender domain not onboarded"}]}
        with mock.patch("send_to_kindle.mail.requests.post", return_value=reply):
            with self.assertRaisesRegex(RuntimeError, "not onboarded"):
                mail.notify(cfg, "kindle@example.com", "s", "t")  # not on the inbox allowlist: nothing sent
                mail.to_kindle(cfg, b"x", "a.pdf", "A")


if __name__ == "__main__":
    unittest.main()
