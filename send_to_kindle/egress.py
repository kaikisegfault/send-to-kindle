"""Where the engine may connect, by name: the library's git host and the mail service. Run as root by the
installer (and again after the settings change):  python3 -m send_to_kindle.egress

The firewall can only allow numeric addresses, so the engine gets its own DNS resolver (dnsmasq, on
127.0.0.2, send-to-kindle-dns.service): when the engine looks up one of the two names, the resolver puts the
answer's addresses into the firewall's allow sets in the same moment, before the engine connects. Nothing
polls and nothing goes stale. Every other name still resolves (mail signatures are checked by DNS), but its
addresses are never allowed.

Writes, from /etc/send-to-kindle/settings:
  dns.conf          the resolver's configuration: the two names and the sets they fill
  egress-ports.nft  the two ports (the git host's and the mail service's)
  resolv.conf       the engine's view of /etc/resolv.conf: the resolver above
Standard library only: it runs with the system's python3.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ETC = Path("/etc/send-to-kindle")
TABLE = ("inet", "send_to_kindle")
LISTEN = "127.0.0.2"
UPSTREAM = "127.0.0.53"


def settings(path: Path) -> dict[str, str]:
    """KEY=VALUE lines (systemd's EnvironmentFile format), without interpreting values."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


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


def destinations(env: dict[str, str]) -> dict[str, tuple[str, int]]:
    """{"git": (host, port), "mail": (host, port)} for this deployment."""
    out = {}
    git = git_host(env.get("STK_LIBRARY_REPO", ""))
    if git:
        out["git"] = git
    if env.get("STK_CF_EMAIL_TOKEN"):
        out["mail"] = ("api.cloudflare.com", 443)
    else:
        out["mail"] = (env.get("STK_SMTP_HOST") or "smtp.gmail.com", int(env.get("STK_SMTP_PORT") or 587))
    return out


def dns_conf(dest: dict[str, tuple[str, int]]) -> str:
    family, table = TABLE
    lines = [
        "# Written by python3 -m send_to_kindle.egress from the settings. Don't edit.",
        "# The engine's resolver: forwards every lookup, and puts the addresses of the allowed names into the",
        "# firewall's sets as it answers.",
        f"listen-address={LISTEN}",
        "port=53",
        "bind-interfaces",
        "no-resolv",
        "no-hosts",
        "no-poll",
        f"server={UPSTREAM}",
        "cache-size=0",  # every lookup goes upstream, so the sets always hold the current answer
        "pid-file=",
        "user=nobody",
    ]
    for name, (host, _) in dest.items():
        lines.append(f"nftset=/{host}/4#{family}#{table}#{name}4,6#{family}#{table}#{name}6")
    return "\n".join(lines) + "\n"


def ports_nft(dest: dict[str, tuple[str, int]]) -> str:
    family, table = TABLE
    lines = ["# Written by python3 -m send_to_kindle.egress from the settings. Don't edit."]
    for name in ("git", "mail"):
        if name in dest:
            lines.append(f"add element {family} {table} {name}_port {{ {dest[name][1]} }}")
    return "\n".join(lines) + "\n"


def main() -> int:
    dest = destinations(settings(ETC / "settings"))
    files = {"dns.conf": dns_conf(dest), "egress-ports.nft": ports_nft(dest), "resolv.conf": f"nameserver {LISTEN}\n"}
    for name, text in files.items():
        (ETC / name).write_text(text, encoding="utf-8")
        (ETC / name).chmod(0o644)
    print("egress: " + ", ".join(f"{h}:{p}" for h, p in dest.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
