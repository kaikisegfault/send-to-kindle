"""The library: one folder, in a git repository or on disk, that keeps everything provided and sent.

    <folder>/LEDGER.csv      one row per item, every item with its own ID
    <folder>/inbox/          files dropped in by hand; they get an ID when a request uses them
    <folder>/user-provided/  what a request was given: files, and .md notes for links and text
    <folder>/sent/           what went to the Kindle: the complete library of delivered documents

IDs come from one counter for all items. When a file goes to the Kindle exactly as it was given, sent/
holds the file and user-provided/ only a short .md note pointing at it, so nothing is stored twice.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .document import slug

FIELDS = ["id", "date", "folder", "file", "mode", "format", "source", "from", "check", "status", "instructions"]


class Library:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.git = bool(cfg.library_repo)
        self.root = cfg.library_checkout
        self.dir = self.root / cfg.library_folder if self.git else cfg.library_checkout
        self.ledger = self.dir / "LEDGER.csv"

    # ------------------------------------------------------------------ git --

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if self.cfg.git_ssh_key:
            env["GIT_SSH_COMMAND"] = f"ssh -i {self.cfg.git_ssh_key} -o IdentitiesOnly=yes -o BatchMode=yes"
        return subprocess.run(["git", "-C", str(self.root), *args], env=env, capture_output=True, text=True, check=check, timeout=600)

    def sync(self) -> None:
        """Bring the folder up to date (clone it the first time, only this folder's files)."""
        if not self.git:
            self.dir.mkdir(parents=True, exist_ok=True)
            return
        if not (self.root / ".git").exists():
            self.root.parent.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            if self.cfg.git_ssh_key:
                env["GIT_SSH_COMMAND"] = f"ssh -i {self.cfg.git_ssh_key} -o IdentitiesOnly=yes -o BatchMode=yes"
            subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--sparse", "--branch", self.cfg.library_branch,
                            self.cfg.library_repo, str(self.root)], env=env, check=True, capture_output=True, timeout=900)
            self._git("sparse-checkout", "set", self.cfg.library_folder)
        self._git("pull", "--quiet", "--rebase")
        self.dir.mkdir(parents=True, exist_ok=True)

    def commit(self, message: str) -> None:
        if not self.git:
            return
        self._git("add", "-A", "--", self.cfg.library_folder)
        if not self._git("diff", "--cached", "--quiet", check=False).returncode:
            return
        self._git("-c", f"user.name={self.cfg.git_name}", "-c", f"user.email={self.cfg.git_email}", "commit", "--quiet", "-m", message)
        for _ in range(3):
            if self._git("push", "--quiet", check=False).returncode == 0:
                return
            self._git("pull", "--quiet", "--rebase")
        self._git("push", "--quiet")

    # --------------------------------------------------------------- ledger --

    def rows(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        with self.ledger.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def next_id(self) -> int:
        return max((int(r["id"]) for r in self.rows() if r["id"].isdigit()), default=0) + 1

    def rewrite(self, rows: list[dict]) -> None:
        """The whole ledger written again (to mark items removed); rows keep their IDs and order."""
        with self.ledger.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in FIELDS})

    def _append(self, row: dict) -> None:
        new = not self.ledger.exists()
        with self.ledger.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in FIELDS})

    def find(self, item_id: int) -> dict | None:
        return next((r for r in self.rows() if r["id"] == str(item_id)), None)

    def find_source(self, source: str) -> dict | None:
        """The user-provided item already made for this link, if any (and not removed)."""
        return next((r for r in self.rows() if r["folder"] == "user-provided" and r["source"] == source
                     and not r["status"].startswith("removed")), None)

    # ---------------------------------------------------------------- items --

    def add(self, folder: str, title: str, ext: str, *, data: bytes | None = None, move: Path | None = None, **fields) -> dict:
        """Store one item under the next ID and log it. Returns its ledger row."""
        item_id = self.next_id()
        rel = f"{folder}/{item_id:04d}-{slug(title)}.{ext}"
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if move is not None:
            shutil.move(str(move), path)
        else:
            path.write_bytes(data or b"")
        row = {"id": str(item_id), "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "folder": folder, "file": rel, **fields}
        self._append(row)
        return row

    def log(self, folder: str, **fields) -> dict:
        """A ledger row with no file, e.g. a send that failed."""
        row = {"id": str(self.next_id()), "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "folder": folder, "file": "", **fields}
        self._append(row)
        return row

    def path(self, row: dict) -> Path:
        return self.dir / row["file"]

    def inbox(self) -> list[Path]:
        box = self.dir / "inbox"
        return sorted(p for p in box.glob("*") if p.is_file() and not p.name.startswith(".")) if box.exists() else []

    def resolve(self, rel: str) -> Path:
        """A path inside the folder, given as e.g. 'inbox/report.pdf'; never outside it."""
        p = (self.dir / rel).resolve()
        if self.dir.resolve() not in p.parents:
            raise ValueError(f"{rel} is outside the library")
        if not p.is_file():
            raise ValueError(f"{rel} is not in the library")
        return p


def note(title: str, lines: list[str], text: str | None = None) -> bytes:
    """A user-provided .md note: what was given, where it came from, and (when kept) its text."""
    out = io.StringIO()
    out.write(f"# {title}\n\n")
    for line in lines:
        out.write(f"- {line}\n")
    if text:
        out.write("\n---\n\n" + text.strip() + "\n")
    return out.getvalue().encode("utf-8")
