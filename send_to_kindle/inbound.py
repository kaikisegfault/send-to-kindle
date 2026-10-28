"""The email inbox: mail a file or a link to your inbox address and it goes to the Kindle 1:1.

A Cloudflare Email Worker passes each message on unchanged (see inbox-worker/). A message is taken only
when its From address is on STK_INBOX_SENDERS and it carries a valid DKIM signature from that address's
own domain, so nobody can send in your name. Subject "inbox": the files only go to the library's inbox/,
for a later request.
"""

from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import urlparse

from . import jobs, mail
from .config import Config
from .document import KINDLE_FORMATS, slug
from .library import Library

LINK = re.compile(r"https?://[^\s<>\")\]]+")
# Links that mail providers put in their own footers, not something to read.
FOOTER_HOSTS = ("proton.me", "pm.me", "protonmail.com", "google.com", "gmail.com", "accounts.google.com")


def _dkim_ok(raw: bytes, sender: str) -> bool:
    import dkim

    domain = sender.rsplit("@", 1)[-1].lower()
    msg = BytesParser(policy=policy.compat32).parsebytes(raw)
    sigs = msg.get_all("DKIM-Signature") or []
    for i, sig in enumerate(sigs):
        tags = dict(t.strip().split("=", 1) for t in re.sub(r"\s+", " ", str(sig)).split(";") if "=" in t)
        d = tags.get("d", "").strip().lower()
        if d == domain or domain.endswith("." + d):
            try:
                if dkim.verify(raw, index=i):
                    return True
            except Exception:
                continue
    return False


def _parts(msg: EmailMessage) -> tuple[list[tuple[str, bytes]], list[str]]:
    files, links = [], []
    for part in msg.iter_attachments():
        name = part.get_filename() or "attachment"
        data = part.get_payload(decode=True) or b""
        if data:
            files.append((name, data))
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        text = body.get_content()
        for url in LINK.findall(text):
            url = url.rstrip(".,;:!?'")
            host = urlparse(url).netloc.lower()
            if any(host == h or host.endswith("." + h) for h in FOOTER_HOSTS):
                continue
            if url not in links:
                links.append(url)
    return files, links


def _keep_refused(cfg: Config, raw: bytes) -> None:
    """The last few refused mails, for diagnosis (only in the service's own state directory)."""
    import time

    box = cfg.work_dir / "refused-mail"
    box.mkdir(parents=True, exist_ok=True)
    (box / f"{int(time.time())}.eml").write_bytes(raw)
    for old in sorted(box.glob("*.eml"))[:-5]:
        old.unlink(missing_ok=True)


def handle(cfg: Config, raw: bytes) -> dict:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    if sender not in cfg.inbox_senders:
        return {"taken": False, "reason": f"{sender or 'unknown sender'} is not allowed"}
    if not _dkim_ok(raw, sender):
        _keep_refused(cfg, raw)
        return {"taken": False, "reason": f"no valid DKIM signature from {sender.split('@')[-1]}"}
    subject = str(msg.get("Subject", "")).strip()
    files, links = _parts(msg)
    to_inbox = subject.lower().startswith("inbox")
    results: list[str] = []

    if to_inbox:
        with jobs.LOCK:
            lib = Library(cfg)
            lib.sync()
            box = lib.dir / "inbox"
            box.mkdir(parents=True, exist_ok=True)
            for name, data in files:
                stem, _, ext = name.rpartition(".")
                target = box / f"{slug(stem or name)}.{ext.lower() or 'bin'}"
                n = 2
                while target.exists():
                    target = box / f"{slug(stem or name)}-{n}.{ext.lower() or 'bin'}"
                    n += 1
                target.write_bytes(data)
                results.append(f"inbox: {target.name}")
            if links:
                listing = "\n".join(f"- {u}" for u in links)
                (box / f"links-{slug(subject[5:].strip() or 'from-mail')}.md").write_text(f"# Links from mail\n\n{listing}\n", encoding="utf-8")
                results.append(f"inbox: {len(links)} link(s) in a note")
            lib.commit(f"kindle: inbox from mail ({len(files)} files, {len(links)} links)")
    else:
        for name, data in files:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            try:
                res = _send_file(cfg, name, data, ext)
                results.append(f"sent {res['id']}: {res['title']} ({res['format']}{', ' + res['check_text'] if res['check_text'] else ''})")
            except Exception as e:
                results.append(f"failed: {name}: {e}")
        for url in links:
            try:
                res = jobs.send(cfg, {"source": {"url": url}})
                results.append(f"sent {res['id']}: {res['title']} ({res['format']}{', ' + res['check_text'] if res['check_text'] else ''})")
            except Exception as e:
                results.append(f"failed: {url}: {e}")
    if not results:
        results.append("nothing to send: no attachments and no links")
    try:
        mail.notify(cfg, sender, f"Kindle: {subject or 'your mail'}", "\n".join(results))
    except Exception:
        pass
    return {"taken": True, "results": results}


def _send_file(cfg: Config, name: str, data: bytes, ext: str) -> dict:
    """An attachment: into the inbox first, then sent from there (1:1, as it is when Kindle takes it)."""
    with jobs.LOCK:
        lib = Library(cfg)
        lib.sync()
        box = lib.dir / "inbox"
        box.mkdir(parents=True, exist_ok=True)
        stem = name.rsplit(".", 1)[0] if "." in name else name
        target = box / f"mail-{slug(stem)}.{ext or 'bin'}"
        target.write_bytes(data)
    fmt = None if ext in KINDLE_FORMATS else "epub"
    return jobs.send(cfg, {"source": {"path": f"inbox/{target.name}"}, "format": fmt, "title": stem})
