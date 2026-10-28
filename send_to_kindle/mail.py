"""Delivery: one email with the document attached, to the Kindle address and nowhere else.

Amazon accepts personal documents only from senders on the Kindle account's "Approved Personal Document
E-mail List" and drops everything else without a bounce, so a clean send is not proof of arrival.
"""

from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from .config import Config

TYPES = {"epub": "application/epub+zip", "pdf": "application/pdf"}


def _send(cfg: Config, msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=120) as s:
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=120) as s:
            s.starttls(context=ctx)
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)


def to_kindle(cfg: Config, data: bytes, filename: str, title: str) -> str:
    """Mail one document to the Kindle. Returns the Message-ID."""
    if len(data) > cfg.max_mb * 1024 * 1024:
        raise ValueError(f"{len(data) / 1048576:.1f} MB is over the {cfg.max_mb} MB a mail can carry")
    msg = EmailMessage()
    msg["Subject"] = title[:200]
    msg["From"] = cfg.sender
    msg["To"] = cfg.kindle_email
    msg["Message-ID"] = make_msgid(domain=cfg.sender.split("@")[-1] or None)
    msg.set_content(f"Personal document: {title}")
    ext = filename.rsplit(".", 1)[-1].lower()
    ctype = TYPES.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    main, sub = ctype.split("/", 1)
    msg.add_attachment(data, maintype=main, subtype=sub, filename=filename)
    _send(cfg, msg)
    return msg["Message-ID"]


def notify(cfg: Config, to: str, subject: str, text: str) -> None:
    """A short report by mail, only ever to an address on the inbox allowlist."""
    if to.lower() not in cfg.inbox_senders:
        return
    msg = EmailMessage()
    msg["Subject"] = subject[:200]
    msg["From"] = cfg.sender
    msg["To"] = to
    msg.set_content(text)
    _send(cfg, msg)
