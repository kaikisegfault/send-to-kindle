"""Delivery: one email with the document attached, to the Kindle address and nowhere else.

Amazon accepts personal documents only from senders on the Kindle account's "Approved Personal Document
E-mail List" and drops everything else without a bounce, so a clean send is not proof of arrival.
"""

from __future__ import annotations

import base64
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

import requests

from .config import Config

TYPES = {"epub": "application/epub+zip", "pdf": "application/pdf"}
CF_SEND = "https://api.cloudflare.com/client/v4/accounts/{}/email/sending/send"


def _send_cloudflare(cfg: Config, msg: EmailMessage) -> None:
    """Cloudflare Email Service's REST API: HTTPS on port 443, for hosts that block outgoing SMTP (Hetzner Cloud
    blocks 25 and 465, and Cloudflare's own SMTP takes only 465)."""
    body = msg.get_body(("plain",))
    payload = {
        "from": str(msg["From"]),
        "to": str(msg["To"]),
        "subject": str(msg["Subject"]),
        "text": body.get_content() if body else "",
    }
    attachments = [
        {"content": base64.b64encode(part.get_payload(decode=True)).decode("ascii"), "filename": part.get_filename(),
         "type": part.get_content_type(), "disposition": "attachment"}
        for part in msg.iter_attachments()
    ]
    if attachments:
        payload["attachments"] = attachments
    r = requests.post(CF_SEND.format(cfg.cf_account_id), json=payload,
                      headers={"Authorization": f"Bearer {cfg.cf_email_token}"}, timeout=120)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("success") or (data.get("result") or {}).get("permanent_bounces"):
        errors = "; ".join(e.get("message", "") for e in data.get("errors") or []) or r.text[:200]
        raise RuntimeError(f"Cloudflare Email Service refused the mail (HTTP {r.status_code}): {errors}")


def _send(cfg: Config, msg: EmailMessage) -> None:
    if cfg.cf_email_token:
        _send_cloudflare(cfg, msg)
        return
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
