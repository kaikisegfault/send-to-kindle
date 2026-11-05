"""Settings, all from the environment (README, "Settings"). Nothing personal lives in the code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def _need(name: str) -> str:
    value = _get(name)
    if not value:
        raise SystemExit(f"send-to-kindle: {name} is not set (README, \"Settings\")")
    return value


def _list(name: str) -> list[str]:
    return [s.strip().lower() for s in (_get(name) or "").split(",") if s.strip()]


@dataclass(frozen=True)
class Config:
    # Delivery
    kindle_email: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    sender: str
    max_mb: int
    # Cloudflare Email Service's REST API instead of SMTP (optional): the account and a send-only token
    cf_account_id: str | None
    cf_email_token: str | None
    # HTTP service
    token: str | None
    listen: str
    port: int
    # Working files
    work_dir: Path
    # Library (optional): a git repository (its root, or a folder of it) that keeps everything provided and sent
    library_repo: str | None
    library_branch: str
    library_folder: str
    library_checkout: Path
    git_name: str
    git_email: str
    git_ssh_key: str | None
    # One-time upload addresses (POST /upload-slot) are made on this public base, e.g. https://kindle.example.com
    public_url: str | None
    # Files are opened by the converter: a separate process with no network, reached on this Unix socket.
    # Empty: converted in this process (tests, the command line).
    converter: str | None
    # Email inbox (optional)
    inbox_senders: list[str]

    @classmethod
    def load(cls, *, need_mail: bool = True, need_token: bool = False) -> "Config":
        mail = _need if need_mail else _get
        cloudflare = bool(_get("STK_CF_EMAIL_TOKEN"))
        smtp = _get if cloudflare else mail  # with Cloudflare's API, no SMTP account is needed
        work = Path(_get("STK_WORK_DIR", "/var/lib/send-to-kindle"))
        return cls(
            kindle_email=mail("STK_KINDLE_EMAIL") or "",
            smtp_host=_get("STK_SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(_get("STK_SMTP_PORT", "587")),
            smtp_user=smtp("STK_SMTP_USER") or "",
            smtp_password=smtp("STK_SMTP_PASSWORD") or "",
            sender=(mail("STK_SENDER") if cloudflare else _get("STK_SENDER") or _get("STK_SMTP_USER")) or "",
            # Amazon takes up to 50 MB; Gmail and Cloudflare stop at 25 MB per message, attachments included.
            max_mb=int(_get("STK_MAX_MB", "24")),
            cf_account_id=mail("STK_CF_ACCOUNT_ID") if cloudflare else None,
            cf_email_token=_get("STK_CF_EMAIL_TOKEN"),
            token=_need("STK_TOKEN") if need_token else _get("STK_TOKEN"),
            listen=_get("STK_LISTEN", "127.0.0.1"),
            port=int(_get("STK_PORT", "8735")),
            work_dir=work,
            library_repo=_get("STK_LIBRARY_REPO"),
            library_branch=_get("STK_LIBRARY_BRANCH", "main"),
            library_folder=(_get("STK_LIBRARY_FOLDER", "") or "").strip("/"),
            library_checkout=Path(_get("STK_LIBRARY_CHECKOUT", str(work / "library"))),
            git_name=_get("STK_GIT_NAME", "send-to-kindle"),
            git_email=_get("STK_GIT_EMAIL", "send-to-kindle@localhost"),
            git_ssh_key=_get("STK_GIT_SSH_KEY"),
            public_url=(_get("STK_PUBLIC_URL") or "").rstrip("/") or None,
            converter=_get("STK_CONVERTER"),
            inbox_senders=_list("STK_INBOX_SENDERS"),
        )
