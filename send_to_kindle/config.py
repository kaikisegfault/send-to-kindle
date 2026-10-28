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
    # HTTP service
    token: str | None
    listen: str
    port: int
    # Working files
    work_dir: Path
    # Library (optional): a folder in a git repository that keeps everything provided and sent
    library_repo: str | None
    library_branch: str
    library_folder: str
    library_checkout: Path
    git_name: str
    git_email: str
    git_ssh_key: str | None
    # YouTube
    youtube_proxy: str | None
    youtube_warp: bool
    # Email inbox (optional)
    inbox_senders: list[str]

    @classmethod
    def load(cls, *, need_mail: bool = True, need_token: bool = False) -> "Config":
        mail = _need if need_mail else _get
        work = Path(_get("STK_WORK_DIR", "/var/lib/send-to-kindle"))
        return cls(
            kindle_email=mail("STK_KINDLE_EMAIL") or "",
            smtp_host=_get("STK_SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(_get("STK_SMTP_PORT", "587")),
            smtp_user=mail("STK_SMTP_USER") or "",
            smtp_password=mail("STK_SMTP_PASSWORD") or "",
            sender=_get("STK_SENDER") or _get("STK_SMTP_USER") or "",
            # Amazon takes up to 50 MB; Gmail stops at 25 MB per message, attachments included.
            max_mb=int(_get("STK_MAX_MB", "24")),
            token=_need("STK_TOKEN") if need_token else _get("STK_TOKEN"),
            listen=_get("STK_LISTEN", "127.0.0.1"),
            port=int(_get("STK_PORT", "8735")),
            work_dir=work,
            library_repo=_get("STK_LIBRARY_REPO"),
            library_branch=_get("STK_LIBRARY_BRANCH", "main"),
            library_folder=_get("STK_LIBRARY_FOLDER", "kindle-content").strip("/"),
            library_checkout=Path(_get("STK_LIBRARY_CHECKOUT", str(work / "library"))),
            git_name=_get("STK_GIT_NAME", "send-to-kindle"),
            git_email=_get("STK_GIT_EMAIL", "send-to-kindle@localhost"),
            git_ssh_key=_get("STK_GIT_SSH_KEY"),
            youtube_proxy=_get("STK_YOUTUBE_PROXY"),
            youtube_warp=(_get("STK_YOUTUBE_WARP", "no") or "no").lower() in ("1", "yes", "true"),
            inbox_senders=_list("STK_INBOX_SENDERS"),
        )
