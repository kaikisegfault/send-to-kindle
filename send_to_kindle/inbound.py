"""The email inbox: mail a file to your inbox address and it goes to the Kindle (links are not taken).

A Cloudflare Email Worker passes each message on unchanged (see inbox-worker/). A message is taken only
when its From address is on STK_INBOX_SENDERS and it carries a valid DKIM signature from that address's
own domain, so nobody can send in your name. The subject decides:
    "convert" in it   every file converted to EPUB (still every word, checked)
    starts "inbox"    the files only go to the library's inbox/, for a later request
    anything else     files Kindle takes go as they are; others become EPUB
"""

from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import urlparse

from . import jobs, mail, sources
from .config import Config
from .document import slug
from .library import Library

LINK = re.compile(r"https?://[^\s<>\")\]]+")
# Links that mail providers put in their own footers: not counted as links the sender meant.
FOOTER_HOSTS = ("proton.me", "pm.me", "protonmail.com", "google.com", "gmail.com", "accounts.google.com")


def _dkim_ok(raw: bytes, sender: str, dnsfunc=None) -> bool:
    """A valid DKIM signature whose domain is the sender's own (or its parent). dnsfunc: for tests."""
    import dkim

    lookup = {"dnsfunc": dnsfunc} if dnsfunc else {}
    domain = sender.rsplit("@", 1)[-1].lower()
    msg = BytesParser(policy=policy.compat32).parsebytes(raw)
    sigs = msg.get_all("DKIM-Signature") or []
    for i, sig in enumerate(sigs):
        tags = dict(t.strip().split("=", 1) for t in re.sub(r"\s+", " ", str(sig)).split(";") if "=" in t)
        d = tags.get("d", "").strip().lower()
        if d == domain or domain.endswith("." + d):
            try:
                if dkim.DKIM(raw).verify(idx=i, **lookup):
                    return True
            except dkim.DKIMException:
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


def _store(lib: Library, name: str, data: bytes) -> str:
    """A file into inbox/ under a free name; returns the name."""
    box = lib.dir / "inbox"
    box.mkdir(parents=True, exist_ok=True)
    stem, _, ext = name.rpartition(".")
    stem, ext = slug(stem or name), (ext.lower() if stem else "") or "bin"
    target, n = box / f"{stem}.{ext}", 2
    while target.exists():
        target, n = box / f"{stem}-{n}.{ext}", n + 1
    target.write_bytes(data)
    return target.name


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
    results: list[str] = []

    convert = bool(re.search(r"\bconvert\b", subject, re.IGNORECASE))
    if subject.lower().startswith("inbox") and not convert:
        with jobs.LOCK, Library(cfg) as lib:
            for name, data in files:
                results.append(f"inbox: {_store(lib, name, data)}")
            lib.commit(f"kindle: inbox from mail ({len(files)} files)")
    else:
        for name, data in files:
            try:
                src = jobs.Source(data, name, sources.kind_of(data, name), f"mail from {sender}")
                res = jobs._deliver(cfg, src, jobs._wants_epub("epub" if convert else None, src), None)
                results.append(f"sent {res['id']}: {res['title']} ({res['format']}{', ' + res['check_text'] if res['check_text'] else ''})")
            except Exception as e:
                with jobs.LOCK, Library(cfg) as lib:  # not lost: kept in the inbox
                    kept = _store(lib, name, data)
                    lib.commit(f"kindle: inbox from mail (a send that failed: {kept})")
                results.append(f"failed: {name}: {e} (kept as inbox/{kept})")
    if links:
        results.append(f"{len(links)} link(s) not taken: this address takes files only. Ask Claude to read a page and send it.")
    if not results:
        results.append("nothing to send: no attachments")
    try:
        mail.notify(cfg, sender, f"Kindle: {subject or 'your mail'}", "\n".join(results))
    except Exception:
        pass
    return {"taken": True, "results": results}
