# send-to-kindle

A public, reusable engine (MIT). README.md is the user documentation; keep it in step with the code.

- **No personal data in this repository, ever**: no real addresses, domains, server names, IPs,
  tokens or Kindle addresses. Examples use `example.com` and placeholders. Deployments keep their
  values in their own settings file (`/etc/send-to-kindle/settings`), never here.
- Its job is delivery: files as they are or converted, and text as EPUB. Reading web pages and videos is
  not its job (an assistant reads them and sends the text); don't add it back.
- Conversion stays programmatic: no model rewrites text inside the engine. Written content comes
  in ready-made through `/send` with `content`.
- Files are opened only in the converter (no network, a fresh process per file); the engine never parses
  a file. Links reach only public addresses.
- Every change to extraction or conversion: run the tests (`.venv/bin/python -m unittest discover -s
  tests`) and, where it matters, a real PDF (a scan too) through `python -m send_to_kindle convert`, and
  compare the completeness check before and after.
- Python 3.11+, standard library first; the few dependencies are in requirements.txt.
