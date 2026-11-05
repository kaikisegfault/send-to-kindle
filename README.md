# send-to-kindle

Send files and text to your Kindle: a PDF, EPUB or DOCX as it is, the same converted to a clean EPUB,
or text an AI assistant wrote or read for you. Conversion is done by programs only, the same input
always gives the same output, and every converted document is checked word by word against its source
before it goes out.

- **As it is**: a file goes to the Kindle unchanged.
- **Converted**: a PDF (scans included, through OCR), EPUB, DOCX, HTML, Markdown or text file, or text
  sent with the request, becomes an EPUB that reflows on the Kindle screen.
- **Handed to it**: a file in the library's inbox, a library item, a file uploaded to a one-time
  address (for example from an assistant's own workspace), mail, or text. It downloads nothing.
- **Library** (optional): everything provided and sent, kept in a git repository, with a CSV ledger and
  an ID for every item. Nothing stays on the server.

Reading web pages and videos is not its job, and it opens no links: an assistant reads those and sends
the text or the file.

## How it works

```
assistant / script / you
        │  HTTPS + bearer token (or a one-time upload address)
        ▼
  send-to-kindle engine (Python, on any small server)
   ├─ take: the library's inbox and items, one-time uploads, mail, text (no links)
   ├─ convert, in a separate process with no network: PDF text (PyMuPDF, cross-checked by poppler's
   │  pdftotext; Tesseract OCR for scans), EPUB, DOCX, HTML, Markdown → pandoc → EPUB 3, then the
   │  completeness check and an EPUB structure check
   ├─ send: Cloudflare Email Service or SMTP → your Kindle's address, and nowhere else
   └─ library: LEDGER.csv, inbox/, user-provided/, sent/ (git commit + push)
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
  text are listed.
- **Scanned PDFs** (pages that are pictures): converted through OCR (Tesseract, on the server; nothing
  leaves it). The scan's language is the one Tesseract reads the first page with most confidence
  (English, Russian, Georgian installed). The report gives the mean word confidence; under 60% the scan
  is refused for EPUB as too poor to trust: send it as the PDF.
- **Not kept**: covers, tables of contents, page maps, page layout, images in converted PDFs and HTML.
  If the layout matters, send the file as it is.
- **Styling**: none that fights the Kindle. The EPUB's stylesheet (`send_to_kindle/kindle.css`) sets
  no colours, backgrounds, fonts or line heights, so the reader's theme (including dark mode), font
  and spacing apply, and no boxes appear behind the text. There is no title page; each top-level
  heading starts a chapter, and every other heading is kept on the same page as the text after it
  (`keep.lua`). The language is set from the text's script, for the Kindle's dictionary and
  hyphenation. An EPUB sent with `format: "epub"` is rebuilt this way; without it, it goes as it is.

## Setup, step by step

You need a small Linux server with a public IP (Ubuntu 24.04 is assumed), a domain name for HTTPS,
a way to send mail (Cloudflare Email Service, or an account that can send by SMTP), and a Kindle.

1. **Mail.** Either Cloudflare Email Service (a domain onboarded for Email Sending and an API token with
   only *Email Sending: Edit*), or an SMTP account: Gmail works with 2-Step Verification and an App
   Password (Google Account → Security → App passwords).
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
6. **Library (optional).** Pick a repository. On the server, make a deploy key for the service user and
   give it write access to that repository only:
   ```sh
   sudo -u kindle mkdir -p -m 700 /var/lib/send-to-kindle/.ssh
   sudo -u kindle ssh-keygen -t ed25519 -N "" -f /var/lib/send-to-kindle/.ssh/library
   sudo -u kindle sh -c 'ssh-keyscan github.com >> /var/lib/send-to-kindle/.ssh/known_hosts'
   ```
   Add the `.pub` key under the repository's Settings → Deploy keys with *Allow write access*.
7. **Email inbox (optional).** With your domain on Cloudflare Email Routing: deploy
   `inbox-worker/` (`npx wrangler deploy --config inbox-worker/wrangler.jsonc`), give it its three
   secrets (`ENGINE_URL`, `STK_TOKEN`, `ALLOWED_SENDERS`), and route an address such as
   `kindle@your-domain` to the Worker. Set `STK_INBOX_SENDERS` to the same addresses.

**Updating** later: `cd /opt/send-to-kindle-src && sudo git pull && sudo ./deploy/install.sh`. Versions
before 2.0 also read web articles and YouTube videos and kept a copy of the library and a text cache
in `/var/lib/send-to-kindle` (`library/`, `text-cache/`); those can be deleted.

## Settings

All settings are environment variables, in `/etc/send-to-kindle/settings` on a server.

| Name | Needed | What it is |
|---|---|---|
| `STK_KINDLE_EMAIL` | yes | Your Kindle's address (`…@kindle.com`) |
| `STK_SMTP_USER`, `STK_SMTP_PASSWORD` | yes, without Cloudflare | The sending account and its (app) password |
| `STK_SENDER` | yes, with Cloudflare | From address; defaults to `STK_SMTP_USER`. Must be on Amazon's approved list |
| `STK_SMTP_HOST`, `STK_SMTP_PORT` | | Default `smtp.gmail.com`, `587` (465 for implicit TLS) |
| `STK_CF_ACCOUNT_ID`, `STK_CF_EMAIL_TOKEN` | | Send through Cloudflare Email Service's REST API (HTTPS) instead of SMTP: the account ID and an API token with only *Email Sending: Edit*. For hosts that block outgoing SMTP (Hetzner Cloud blocks ports 25 and 465; Cloudflare's SMTP takes only 465). `STK_SENDER` must be on a domain onboarded for Email Sending; set `STK_MAX_MB=18` (25 MiB per message after base64) |
| `STK_TOKEN` | yes, to serve | The bearer token every request must carry |
| `STK_PUBLIC_URL` | for uploads | The engine's public address, e.g. `https://kindle.example.com`: one-time upload addresses are made on it |
| `STK_CONVERTER` | | The converter's socket, `/run/send-to-kindle/convert.sock` (set by the installer). Empty: files are converted inside the engine |
| `STK_LISTEN`, `STK_PORT` | | Default `127.0.0.1`, `8735` |
| `STK_WORK_DIR` | | Default `/var/lib/send-to-kindle` |
| `STK_MAX_MB` | | Largest file to send, default `24` (Gmail's limit is 25 MB) |
| `STK_LIBRARY_REPO` | | Git URL of the library repository; empty = library on disk only |
| `STK_LIBRARY_BRANCH` | | Default `main` |
| `STK_LIBRARY_FOLDER` | | A folder of the repository to keep the library in; empty (default) = its root |
| `STK_LIBRARY_CHECKOUT` | | The library's folder when there is no repository |
| `STK_GIT_SSH_KEY` | | The deploy key's private file |
| `STK_GIT_NAME`, `STK_GIT_EMAIL` | | Author of the library's commits |
| `STK_INBOX_SENDERS` | | Comma-separated addresses the email inbox takes mail from |

## The library

```
LEDGER.csv      id, date, folder, file, mode, format, source, from, check, status, instructions
inbox/          drop files here (any name); they get an ID when a request uses them
user-provided/  what requests were given
sent/           everything that went to the Kindle
```

Every item gets its own ID from one counter; dates are UTC. A sent row's `from` names the items it was
made from. When a file goes to the Kindle unchanged, `sent/` holds the file and `user-provided/` only a
short note pointing at it, so nothing is stored twice. A failed send is a ledger row with its reason.
`instructions` holds the request's words for items from before 2.0.

With a repository, each job takes a fresh copy (the ledger, the README and the inbox; other files only
when a job needs them), files what it made, pushes and deletes the copy: the repository is the only
library.

## API

Every request carries `Authorization: Bearer <STK_TOKEN>`, except an upload to a one-time address. A
*source* is `{"path": "inbox/file.pdf"}` or `{"id": 12}` (a library item); the engine takes no links.

| Request | Body | Does |
|---|---|---|
| `POST /send` | `{"source": …, "format": "original"\|"epub", "title": "…"}` | A file. Without a format, PDF, EPUB, DOCX and the other files Kindle takes go as they are; HTML, Markdown and text become EPUB |
| `POST /send` | `{"content": "markdown", "title": "…"}` | Text, as EPUB |
| `POST /upload-slot` | `{"format": "original"\|"epub", "title": "…"}` | A one-time upload address (`url`), for 10 minutes |
| `PUT <url><file name>` | the file | Sends it the way the address was made for (e.g. `curl -T report.pdf <url>`) and answers with the report |
| `POST /remove` | `{"ids": [ids], "reason": "…"}` | Deletes items' files; their ledger rows stay, marked removed, so no ID comes back |
| `GET /inbox` | | Files waiting in `inbox/`, and the last ledger rows |
| `GET /item/<id>` | | One ledger row, as `item` |
| `POST /inbound` | a raw email | The email inbox (from the Worker) |
| `GET /health` | | No token needed |

A request answers within about 50 seconds. A longer job (a long scan, say) carries on, replies
`{"status": "working"}`, appears in the ledger when done, and a short mail to the sender address
says how it ended.

## Email inbox

Mail files to your inbox address and they go to the Kindle: files Kindle takes as they are, others as
EPUB. The word "convert" in the subject converts every file to EPUB (every word kept and checked); a
subject starting with "inbox" only stores them in `inbox/`. Links in a mail are not taken. The sender gets
a short reply.

## Command line

```sh
python -m send_to_kindle send inbox/report.pdf                    # the PDF as it is
python -m send_to_kindle send inbox/report.pdf --epub              # the PDF as reflowing text
python -m send_to_kindle convert book.pdf book.epub                # build and check only, no mail
```

## Security

- **Files are opened only by the converter**: a fresh process for each file, started by systemd with
  its own throwaway user, no network at all and nothing it can write but its private `/tmp`. The engine
  itself recognises files by their first bytes and never parses them.
- **No links, and a network allowed by name**: the engine downloads nothing. Its firewall table
  (`deploy/egress.nft`) lets its user reach only the library's git host and the mail service. The engine
  has its own resolver (dnsmasq on 127.0.0.2, `send-to-kindle-dns.service`), the only one it may use: when
  it looks up one of those two names, the resolver puts the answer's addresses into the firewall's allow
  sets in the same moment. No other website, nothing on the server, no private address. After changing the
  library repository or the mail settings, run the installer again.
- **Upload addresses** are random, work once and expire after 10 minutes; only a request with the
  token can make one.
- The engine only ever mails your Kindle address (and short reports to the email inbox's allowed
  senders). A stolen token can't turn it into a mail relay.
- The mail password or token lives only in the server's settings file (root and the service can read it).
- The email inbox takes a message only from an allowed address *and* with a valid DKIM signature
  from that address's own domain, so a forged From is refused.
- The engine runs as its own user at the lowest CPU and I/O priority, with a memory cap, and can
  write only its own state directory.

## Tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

Needs `pandoc` and `pdftotext` (poppler-utils) installed.

## License

MIT
