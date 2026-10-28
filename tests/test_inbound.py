"""Email inbox checks without network: only a valid signature from the sender's own domain passes."""

from __future__ import annotations

import base64
import subprocess
import unittest

import dkim

from send_to_kindle import inbound

MESSAGE = (
    "From: Me <me@example.com>\r\nTo: kindle@example.com\r\nSubject: hi\r\nMessage-ID: <1@x>\r\n"
    "Date: Wed, 28 Oct 2026 10:00:00 +0000\r\nMIME-Version: 1.0\r\nContent-Type: text/plain\r\n\r\n"
    "https://example.org/article\r\n"
).encode()


def keypair() -> tuple[bytes, bytes]:
    pem = subprocess.run(["openssl", "genrsa", "2048"], capture_output=True, check=True).stdout
    der = subprocess.run(["openssl", "rsa", "-pubout", "-outform", "DER"], input=pem, capture_output=True, check=True).stdout
    return pem, der


class Dkim(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pem, der = keypair()
        cls.record = b"v=DKIM1; k=rsa; p=" + base64.b64encode(der)

    def sign(self, domain: str) -> tuple[bytes, callable]:
        sig = dkim.sign(MESSAGE, b"s1", domain.encode(), self.pem, include_headers=[b"from", b"to", b"subject", b"date", b"message-id"])
        name = f"s1._domainkey.{domain}.".encode()
        return sig + MESSAGE, (lambda q, timeout=5: self.record if q == name else None)

    def test_own_domain_passes(self):
        raw, dns = self.sign("example.com")
        self.assertTrue(inbound._dkim_ok(raw, "me@example.com", dns))

    def test_another_domains_signature_fails(self):
        raw, dns = self.sign("evil.test")
        self.assertFalse(inbound._dkim_ok(raw, "me@example.com", dns))

    def test_changed_body_fails(self):
        raw, dns = self.sign("example.com")
        self.assertFalse(inbound._dkim_ok(raw.replace(b"article", b"other!!"), "me@example.com", dns))

    def test_unsigned_fails(self):
        self.assertFalse(inbound._dkim_ok(MESSAGE, "me@example.com", lambda q, timeout=5: None))


if __name__ == "__main__":
    unittest.main()
