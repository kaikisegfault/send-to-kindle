# send-to-kindle

A public, reusable engine (MIT). README.md is the user documentation; keep it in step with the code.

- **No personal data in this repository, ever**: no real addresses, domains, server names, IPs,
  tokens or Kindle addresses. Examples use `example.com` and placeholders. Deployments keep their
  values in their own settings file (`/etc/send-to-kindle/settings`), never here.
- Conversion stays programmatic: no model rewrites text inside the engine. Written content comes
  in ready-made through `/send` with `content`.
- Every change to extraction or conversion: run the tests (`.venv/bin/python -m unittest discover -s
  tests`) and, where it matters, a real PDF, article and video through `python -m send_to_kindle
  convert`, and compare the completeness check before and after.
- Python 3.11+, standard library first; the few dependencies are in requirements.txt.
