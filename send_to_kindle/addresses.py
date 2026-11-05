"""Where the engine may connect, for its firewall table: the library's git host and the mail service.
Run as root, hourly (send-to-kindle-addresses.timer) and by the installer:  python3 -m send_to_kindle.addresses

- GitHub (the library over SSH, port 22): the "git" list GitHub publishes at api.github.com/meta (those
  addresses serve GitHub's git and nothing else).
- Cloudflare's Email Sending API (port 443): the addresses of api.cloudflare.com itself, not Cloudflare's
  whole network, which also serves countless websites.
- Another git host or an SMTP server: its current DNS addresses.

The sets go to /etc/send-to-kindle/addresses.nft, which egress.nft includes. If a list can't be fetched,
the last good file stays and nothing changes. Standard library only: it runs with the system's python3.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

OUT = Path("/etc/send-to-kindle/addresses.nft")
TABLE = "inet send_to_kindle"
UA = {"User-Agent": "send-to-kindle (firewall address lists)"}


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return r.read()


def _resolve(host: str) -> list[str]:
    """A host's addresses; asked three times, as DNS may hand out a share of them at a time."""
    found: set[str] = set()
    for _ in range(3):
        found |= {i[4][0] for i in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    return sorted(found)


def git_host(repo: str) -> tuple[str, int] | None:
    """The library repository's host and port: git@host:path, ssh://[user@]host[:port]/path, https://host/path."""
    if not repo:
        return None
    m = re.match(r"^[\w.-]+@([\w.-]+):", repo)
    if m:
        return m.group(1).lower(), 22
    m = re.match(r"^(ssh|https)://(?:[^@/]+@)?([\w.-]+)(?::(\d+))?/", repo)
    if m:
        return m.group(2).lower(), int(m.group(3) or (22 if m.group(1) == "ssh" else 443))
    return None


def lists(env: dict) -> dict[str, tuple[list[str], int]]:
    """{"git": (networks, port), "mail": (networks, port)} for this deployment."""
    out: dict[str, tuple[list[str], int]] = {}
    host = git_host(env.get("STK_LIBRARY_REPO", ""))
    if host:
        name, port = host
        nets = json.loads(_get("https://api.github.com/meta"))["git"] if name == "github.com" else _resolve(name)
        out["git"] = (nets, port)
    if env.get("STK_CF_EMAIL_TOKEN"):
        out["mail"] = (_resolve("api.cloudflare.com"), 443)
    else:
        out["mail"] = (_resolve(env.get("STK_SMTP_HOST") or "smtp.gmail.com"), int(env.get("STK_SMTP_PORT") or 587))
    return out


def render(found: dict[str, tuple[list[str], int]]) -> str:
    """The nft commands that fill the table's sets (the table itself is in egress.nft)."""
    lines = ["# Written by python3 -m send_to_kindle.addresses: where the engine may connect. Don't edit."]
    for name in ("git", "mail"):
        nets, port = found.get(name, ([], 0))
        v4 = [str(ipaddress.ip_network(n, strict=False)) for n in nets if ipaddress.ip_network(n, strict=False).version == 4]
        v6 = [str(ipaddress.ip_network(n, strict=False)) for n in nets if ipaddress.ip_network(n, strict=False).version == 6]
        for set_name, items in ((f"{name}4", v4), (f"{name}6", v6), (f"{name}_port", [str(port)] if port else [])):
            lines.append(f"flush set {TABLE} {set_name}")
            if items:
                lines.append(f"add element {TABLE} {set_name} {{ {', '.join(items)} }}")
    if not any(found.get(n, ([], 0))[0] for n in ("git", "mail")):
        raise ValueError("no addresses at all")
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        text = render(lists(dict(os.environ)))
    except Exception as e:  # noqa: BLE001 - keep the last good file
        print(f"addresses: not refreshed ({e}); the last list stays", file=sys.stderr)
        return 0 if OUT.exists() else 1
    if OUT.exists() and OUT.read_text() == text:
        return 0
    with tempfile.NamedTemporaryFile("w", dir=OUT.parent, delete=False, prefix=".addresses-") as f:
        f.write(text)
        tmp = f.name
    os.chmod(tmp, 0o644)
    # The table with its sets is loaded: apply now, in one transaction (refused whole if invalid). Otherwise
    # (first install, or an older table without the sets) the firewall loads the file when it (re)starts.
    loaded = subprocess.run(["nft", "list", "set", *TABLE.split(), "git4"], capture_output=True)
    if loaded.returncode == 0:
        r = subprocess.run(["nft", "-f", tmp], capture_output=True, text=True)
        if r.returncode != 0:
            os.unlink(tmp)
            print(f"addresses: nft refused the new list ({r.stderr.strip()[:300]}); the last list stays", file=sys.stderr)
            return 1
    os.replace(tmp, OUT)
    print(f"addresses: {OUT} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
