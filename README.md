# send-to-kindle

Send anything to your Kindle: a PDF as it is, a web article, a YouTube video's transcript, your own
notes, or something an AI assistant wrote for you. Conversion is done by programs only, the same
input always gives the same output, and every converted document is checked word by word against
its source before it goes out.

- **1:1**: a source goes to the Kindle unchanged (a PDF, EPUB or DOCX), or converted to EPUB when you
  ask. Nothing is rewritten, summarised or reordered.
- **Written content**: an assistant (for example Claude, through an MCP connector) reads a source's
  full text with `/read`, writes what you asked for in your own words (ultra brief, fun to read, the
  key points…) and sends it with `/send` as an EPUB.
- **Library** (optional): everything provided and sent, kept in one folder of a git repository, with
  a CSV ledger and an ID for every item.
- **Email inbox** (optional): mail a file or a link to your own address and it goes to the Kindle.

## How it works

```
assistant / script / you
        │  HTTPS + bearer token
        ▼
  send-to-kindle engine (Python, on any small server)
   ├─ fetch: links (requests), YouTube captions (yt-dlp), files from the library
   ├─ extract: PDF text (PyMuPDF, cross-checked by poppler's pdftotext), articles (trafilatura)
   ├─ convert: pandoc → EPUB 3, then the completeness check and an EPUB structure check
   ├─ send: SMTP (e.g. Gmail) → your Kindle's address, and nowhere else
   └─ library: LEDGER.csv, inbox/, user-provided/, sent/  (git commit + push)
        ▲
  email inbox (optional): Cloudflare Email Routing → inbox Worker → POST /inbound
```

Amazon converts EPUB into Kindle's own format on delivery; EPUB is the format it takes best. PDFs can
go as they are (every page exactly as printed) or as EPUB (reflowing text on the Kindle screen).

## What conversion keeps, and what it doesn't

- **The text, all of it.** After building the EPUB, the engine looks for every word of the source in
  it and reports the result, e.g. `100% of 83,210 words kept`, or exactly which words are missing.
- **PDFs** become one continuous text in reading order. A word or sentence cut by a page break is
  joined again. Left out on purpose: page numbers, and running headers and footers (a short line in
  the page margin that repeats on many pages); the report quotes what was removed. Pages with no
  text (scans) are listed; a PDF that is only scans is refused for EPUB, send it as PDF.
- **Not kept**: covers, tables of contents, page maps, page layout, images in converted PDFs and
  articles. If the layout matters, send the PDF as it is.
- **Articles**: the article text without menus, ads and comments (trafilatura, tuned to keep rather
  than drop). Pages that only render in a browser can't be read.
- **YouTube**: the full transcript from YouTube's own captions (made by people first, otherwise
  YouTube's speech recognition of the original audio). A video with no captions is refused.

## Setup, step by step

You need a small Linux server with a public IP (Ubuntu 24.04 is assumed), a domain name for HTTPS,
a mail account that can send by SMTP, and a Kindle.

1. **Mail account.** Gmail works: turn on 2-Step Verification, then create an App Password
   (Google Account → Security → App passwords). Note the 16 characters.
2. **Amazon.** In Manage Your Content and Devices → Preferences → Personal Document Settings, note
   your Kindle's email address and add your sending address to the *Approved Personal Document
   E-mail List* (the full address). Amazon drops mail from other senders without telling anyone.
3. **Install** on the server, as root, from a checkout of this repository:
   ```sh
   sudo git clone https://github.com/kaikisegfault/send-to-kindle.git /opt/send-to-kindle-src
   cd /opt/send-to-kindle-src && sudo ./deploy/install.sh
   ```
4. **Settings.** Fill in `/etc/send-to-kindle/settings` (table below), then
   `sudo systemctl restart send-to-kindle`. Make the token long and random, e.g.
   `openssl rand -base64 36`.
5. **HTTPS.** Point a host name at the server and put Caddy in front: see
   `deploy/Caddyfile.example`. Check: `curl https://kindle.example.com/health`.
6. **Library (optional).** Pick a repository and folder. On the server, make a deploy key for the
   service user and give it write access to that repository only:
   ```sh
   sudo -u kindle mkdir -p -m 700 /var/lib/send-to-kindle/.ssh
   sudo -u kindle ssh-keygen -t ed25519 -N "" -f /var/lib/send-to-kindle/.ssh/library
   sudo -u kindle sh -c 'ssh-keyscan github.com >> /var/lib/send-to-kindle/.ssh/known_hosts'
   ```
   Add the `.pub` key under the repository's Settings → Deploy keys with *Allow write access*.
7. **YouTube (optional).** YouTube often refuses data-centre addresses. Install Cloudflare WARP
   (pkg.cloudflareclient.com) and run it as a local proxy, so only the engine's YouTube requests use
   it and nothing else on the server changes. Set the mode before connecting:
   ```sh
   warp-cli --accept-tos registration new
   warp-cli --accept-tos mode proxy
   warp-cli --accept-tos proxy port 40000
   warp-cli --accept-tos connect
   ```
   Then set `STK_YOUTUBE_PROXY=socks5://127.0.0.1:40000` and `STK_YOUTUBE_WARP=yes`.
8. **Email inbox (optional).** With your domain on Cloudflare Email Routing: deploy
   `inbox-worker/` (`npx wrangler deploy --config inbox-worker/wrangler.jsonc`), give it its three
   secrets (`ENGINE_URL`, `STK_TOKEN`, `ALLOWED_SENDERS`), and route an address such as
   `kindle@your-domain` to the Worker. Set `STK_INBOX_SENDERS` to the same addresses.

**Updating** later: `cd /opt/send-to-kindle-src && sudo git pull && sudo ./deploy/install.sh`
(install the first time from that same place: `sudo git clone … /opt/send-to-kindle-src`).

## Settings

All settings are environment variables, in `/etc/send-to-kindle/settings` on a server.

| Name | Needed | What it is |
|---|---|---|
| `STK_KINDLE_EMAIL` | yes | Your Kindle's address (`…@kindle.com`) |
| `STK_SMTP_USER`, `STK_SMTP_PASSWORD` | yes | The sending account and its (app) password |
| `STK_SENDER` | | From address; defaults to `STK_SMTP_USER`. Must be on Amazon's approved list |
| `STK_SMTP_HOST`, `STK_SMTP_PORT` | | Default `smtp.gmail.com`, `587` (465 for implicit TLS) |
| `STK_TOKEN` | yes, to serve | The bearer token every request must carry |
| `STK_LISTEN`, `STK_PORT` | | Default `127.0.0.1`, `8735` |
| `STK_WORK_DIR` | | Default `/var/lib/send-to-kindle` |
| `STK_MAX_MB` | | Largest file to send, default `24` (Gmail's limit is 25 MB) |
| `STK_LIBRARY_REPO` | | Git URL of the library repository; empty = library on disk only |
| `STK_LIBRARY_BRANCH`, `STK_LIBRARY_FOLDER` | | Default `main`, `kindle-content` |
| `STK_LIBRARY_CHECKOUT` | | Where the repository is checked out (only the library folder) |
| `STK_GIT_SSH_KEY` | | The deploy key's private file |
| `STK_GIT_NAME`, `STK_GIT_EMAIL` | | Author of the library's commits |
| `STK_YOUTUBE_PROXY` | | Proxy for YouTube (and for sites that refuse the server) |
| `STK_YOUTUBE_WARP` | | `yes`: retry a refused YouTube request through a new WARP exit |
| `STK_INBOX_SENDERS` | | Comma-separated addresses the email inbox takes mail from |

## The library

```
kindle-content/
  LEDGER.csv      id, date, folder, file, mode, format, source, from, check, status, instructions
  inbox/          drop files here (any name); they get an ID when a request uses them
  user-provided/  what requests were given: files, and .md notes for links and text
  sent/           everything that went to the Kindle
```

Every item gets its own ID from one counter. A sent row's `from` names the items it was made from.
When a file goes to the Kindle unchanged, `sent/` holds the file and `user-provided/` only a short
note pointing at it, so nothing is stored twice. A failed send is a ledger row with its reason.

## API

Every request carries `Authorization: Bearer <STK_TOKEN>`. A *source* is one of
`{"url": "…"}`, `{"path": "inbox/file.pdf"}`, `{"id": 12}` (a library item) or
`{"text": "…", "title": "…"}`.

| Request | Body | Does |
|---|---|---|
| `POST /send` | `{"source": …, "format": "original"\|"epub", "title": "…"}` | 1:1. Without a format, files Kindle takes go as they are and everything else becomes EPUB |
| `POST /send` | `{"content": "markdown", "title": "…", "instructions": "…", "from": [ids]}` | Sends written content as EPUB |
| `POST /read` | `{"source": …, "offset": 0}` | The source's full text, up to 60,000 characters at a time; logs the source as user-provided |
| `GET /inbox` | | Files waiting in `inbox/`, and the last ledger rows |
| `GET /item/<id>` | | One ledger row, as `item` |
| `POST /inbound` | a raw email | The email inbox (from the Worker) |
| `GET /health` | | No token needed |

A request answers within about 50 seconds. A longer job carries on, replies `{"status": "working"}`,
and its result appears in the ledger.

## Command line

```sh
python -m send_to_kindle send https://example.com/article        # 1:1, EPUB
python -m send_to_kindle send inbox/report.pdf                   # the PDF as it is
python -m send_to_kindle send inbox/report.pdf --epub            # the PDF as reflowing text
python -m send_to_kindle read https://youtu.be/VIDEO_ID           # print the transcript
python -m send_to_kindle convert book.pdf book.epub               # build and check only, no mail
```

## Security

- The engine only ever mails your Kindle address (and, for the email inbox, short reports to the
  allowed senders). A stolen token can't turn it into a mail relay.
- The mail password lives only in the server's settings file (root and the service can read it).
- The email inbox takes a message only from an allowed address *and* with a valid DKIM signature
  from that address's own domain, so a forged From is refused.
- The service runs as its own user at the lowest CPU and I/O priority, with a memory cap, and can
  write only its own state directory.

## Tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

Needs `pandoc` and `pdftotext` (poppler-utils) installed.

## License

MIT
