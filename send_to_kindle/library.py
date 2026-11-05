"""The library: everything provided and sent, in a git repository (or a folder on disk).

    LEDGER.csv      one row per item, every item with its own ID
    inbox/          files dropped in by hand or mailed in; they get an ID when a request uses them
    user-provided/  what a request was given
    sent/           what went to the Kindle: the complete library of delivered documents

With a repository, nothing stays on the server: each job takes a fresh copy (no file contents but the
ledger and the inbox), files what it made, pushes, and deletes the copy. STK_LIBRARY_FOLDER puts the
library in a folder of the repository instead of its root.

IDs come from one counter for all items. When a file goes to the Kindle exactly as it was given, sent/
holds the file and user-provided/ only a short .md note pointing at it, so nothing is stored twice.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .document import slug

FIELDS = ["id", "date", "folder", "file", "mode", "format", "source", "from", "check", "status", "instructions"]


class Library:
    """Use as `with Library(cfg) as lib:`: a fresh copy in, pushed and deleted out (with a repository)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.git = bool(cfg.library_repo)
        self.root: Path = cfg.library_checkout
        self.dir: Path = self.root
        self.ledger = self.dir / "LEDGER.csv"

    def __enter__(self) -> "Library":
        self.sync()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ git --

    def _env(self) -> dict:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if self.cfg.git_ssh_key:
            env["GIT_SSH_COMMAND"] = f"ssh -i {self.cfg.git_ssh_key} -o IdentitiesOnly=yes -o BatchMode=yes"
        return env

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], env=self._env(), capture_output=True, text=True, check=check, timeout=600)

    def _pattern(self, rel: str) -> str:
        return "/" + "/".join(p for p in (self.cfg.library_folder, rel) if p)

    def sync(self) -> None:
        """A fresh copy of the library: the ledger, the README and the inbox; other files on demand."""
        if not self.git:
            self.dir.mkdir(parents=True, exist_ok=True)
            return
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="library-", dir=self.cfg.work_dir))
        self.dir = self.root / self.cfg.library_folder if self.cfg.library_folder else self.root
        self.ledger = self.dir / "LEDGER.csv"
        subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--sparse", "--branch", self.cfg.library_branch,
                        self.cfg.library_repo, str(self.root)], env=self._env(), check=True, capture_output=True, timeout=900)
        self._git("sparse-checkout", "set", "--no-cone", self._pattern("LEDGER.csv"), self._pattern("README.md"), self._pattern("inbox/"))
        self.dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        if self.git and self.root != self.cfg.library_checkout:
            shutil.rmtree(self.root, ignore_errors=True)

    def fetch(self, rel: str) -> Path:
        """An item's file, brought into the copy (a repository's copy starts without them)."""
        p = self.dir / rel
        if self.git and not p.exists():
            self._git("sparse-checkout", "add", self._pattern(rel))
        return p

    def commit(self, message: str) -> None:
        if not self.git:
            return
        self._git("add", "-A", "--sparse", "--", self.cfg.library_folder or ".")
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

    # ---------------------------------------------------------------- items --

    def add(self, folder: str, title: str, ext: str, *, data: bytes, **fields) -> dict:
        """Store one item under the next ID and log it. Returns its ledger row."""
        item_id = self.next_id()
        rel = f"{folder}/{item_id:04d}-{slug(title)}.{ext}"
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        row = {"id": str(item_id), "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "folder": folder, "file": rel, **fields}
        self._append(row)
        return row

    def log(self, folder: str, **fields) -> dict:
        """A ledger row with no file, e.g. a send that failed."""
        row = {"id": str(self.next_id()), "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "folder": folder, "file": "", **fields}
        self._append(row)
        return row

    def path(self, row: dict) -> Path:
        return self.fetch(row["file"])

    def inbox(self) -> list[Path]:
        box = self.dir / "inbox"
        return sorted(p for p in box.glob("*") if p.is_file() and not p.name.startswith(".")) if box.exists() else []

    def resolve(self, rel: str) -> Path:
        """A file in the inbox, given as e.g. 'inbox/report.pdf'; never anywhere else."""
        p = (self.dir / rel).resolve()
        if p.parent != (self.dir / "inbox").resolve():
            raise ValueError(f"{rel} is not a file in inbox/ (library items are sent by their ID)")
        if not p.is_file():
            raise ValueError(f"{rel} is not in the inbox")
        return p


def note(title: str, lines: list[str]) -> bytes:
    """A user-provided .md note: what was given and where it came from."""
    out = io.StringIO()
    out.write(f"# {title}\n\n")
    for line in lines:
        out.write(f"- {line}\n")
    return out.getvalue().encode("utf-8")
