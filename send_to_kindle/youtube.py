"""YouTube videos as their full transcript: YouTube's own captions, fetched with yt-dlp, never rewritten.

Servers in data centres are often refused by YouTube ("confirm you're not a bot"). Point STK_YOUTUBE_PROXY
at a proxy YouTube accepts; with STK_YOUTUBE_WARP=yes and Cloudflare WARP running in local proxy mode,
a refused request is retried through a fresh WARP exit address.
"""

from __future__ import annotations

import html
import re
import subprocess
import time
from urllib.parse import parse_qs, urlparse

from .config import Config
from .document import Document
from .extract import paragraphs_html

HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be")
TIMESTAMP = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s+-->")
TAG = re.compile(r"<[^>]+>")
PARAGRAPH_SECONDS = 60


def is_youtube(url: str) -> bool:
    return urlparse(url).netloc.lower() in HOSTS


def video_id(url: str) -> str | None:
    u = urlparse(url)
    if u.netloc.lower().endswith("youtu.be"):
        return u.path.strip("/").split("/")[0] or None
    if u.path == "/watch":
        return (parse_qs(u.query).get("v") or [None])[0]
    m = re.match(r"/(?:shorts|live|embed)/([\w-]{6,})", u.path)
    return m.group(1) if m else None


def pick_track(info: dict) -> tuple[str, dict] | None:
    """The caption track to use: made by people in the video's language first, then YouTube's own speech
    recognition of the original audio; English as the last resort."""
    lang = (info.get("language") or "").split("-")[0] or None
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}

    def vtt(tracks):
        return next((t for t in tracks or [] if t.get("ext") == "vtt"), None)

    candidates: list[tuple[dict, str]] = []
    if lang:
        candidates += [(manual, lang)] + [(manual, k) for k in manual if k.split("-")[0] == lang]
    candidates += [(manual, k) for k in manual if k.split("-")[0] == "en"]
    if lang:
        candidates += [(auto, f"{lang}-orig"), (auto, lang)]
    candidates += [(auto, k) for k in auto if k.endswith("-orig")]
    if len(manual) == 1:
        candidates.append((manual, next(iter(manual))))
    candidates.append((auto, "en"))
    for tracks, key in candidates:
        t = vtt(tracks.get(key))
        if t:
            return key, t
    return None


def vtt_paragraphs(vtt: str) -> list[str]:
    """A WebVTT file as running text: tags and timing out, the rolling repeats of auto-captions folded,
    a new paragraph about every minute."""
    paragraphs: list[str] = []
    current: list[str] = []
    recent: list[str] = []
    start = None
    seconds = 0.0
    lines = vtt.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = TIMESTAMP.match(lines[i])
        if not m:
            i += 1
            continue
        h, mnt, s, ms = m.groups()
        seconds = int(h or 0) * 3600 + int(mnt) * 60 + int(s) + int(ms) / 1000
        if current and start is not None and seconds - start >= PARAGRAPH_SECONDS:
            paragraphs.append(" ".join(current))
            current, start = [], None
        if start is None:
            start = seconds
        i += 1
        while i < len(lines) and lines[i].strip():
            text = html.unescape(TAG.sub("", lines[i])).strip()
            text = re.sub(r"\s+", " ", text)
            if text and text not in recent[-3:]:
                current.append(text)
                recent.append(text)
            i += 1
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def _warp_new_exit() -> None:
    subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True, timeout=30)
    time.sleep(2)
    subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True, timeout=30)
    for _ in range(15):
        r = subprocess.run(["warp-cli", "--accept-tos", "status"], capture_output=True, text=True, timeout=30)
        if "Connected" in r.stdout:
            return
        time.sleep(1)


def fetch(url: str, cfg: Config) -> Document:
    import yt_dlp

    opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True}
    if cfg.youtube_proxy:
        opts["proxy"] = cfg.youtube_proxy
    attempts = 4 if cfg.youtube_warp else 1
    for attempt in range(1, attempts + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                picked = pick_track(info)
                if not picked:
                    raise ValueError("this video has no captions, so there is no transcript to send")
                key, track = picked
                vtt = ydl.urlopen(track["url"]).read().decode("utf-8", "replace")
            break
        except yt_dlp.utils.DownloadError as e:
            if "not a bot" in str(e) and attempt < attempts:
                _warp_new_exit()
                continue
            raise RuntimeError(f"YouTube refused the request: {str(e).splitlines()[0][:200]}") from e
    paragraphs = vtt_paragraphs(vtt)
    if not paragraphs:
        raise ValueError("the captions of this video are empty")
    title = info.get("title") or video_id(url) or "YouTube video"
    channel = info.get("channel") or info.get("uploader") or ""
    duration = int(info.get("duration") or 0)
    link = info.get("webpage_url") or url
    head = [f"{channel} · {duration // 3600}:{duration % 3600 // 60:02d}:{duration % 60:02d}" if channel else "", link]
    body = paragraphs_html([h for h in head if h]) + "\n<hr/>\n" + paragraphs_html(paragraphs)
    return Document(
        kind="youtube",
        title=title,
        origin=link,
        ext="epub",
        body=body,
        text="\n\n".join(paragraphs),
        lang=(key.split("-")[0] if key else None),
        meta={
            "channel": channel,
            "duration": duration,
            "captions": "auto" if key in (info.get("automatic_captions") or {}) and key not in (info.get("subtitles") or {}) else "made by people",
            "caption_language": key,
        },
    )
