"""A pinned certificate for the TLS tests, without a network.

``net.trust`` pins a host's certificate on first use and verifies against it,
which is what replaced every CERT_NONE path. A test cannot reach a host to
fetch one, so it stubs the fetch with a real self-signed certificate — real,
because ``create_default_context(cadata=…)`` parses it and a fake string would
only prove the stub was called.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

_CACHE: dict[str, str] = {}


def self_signed_pem() -> str:
    """One self-signed certificate for the whole session (openssl is already a
    dependency of the existing CA-bundle test)."""
    if "pem" not in _CACHE:
        with tempfile.TemporaryDirectory() as d:
            crt, key = Path(d) / "c.pem", Path(d) / "k.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048",
                 "-keyout", str(key), "-out", str(crt), "-days", "1",
                 "-nodes", "-subj", "/CN=pinned-test-cert"],
                check=True, capture_output=True)
            _CACHE["pem"] = crt.read_text()
    return _CACHE["pem"]


def another_self_signed_pem() -> str:
    """A SECOND, genuinely different certificate — for the rotation case, where
    a hand-edited blob would not parse and would prove nothing."""
    if "pem2" not in _CACHE:
        with tempfile.TemporaryDirectory() as d:
            crt, key = Path(d) / "c2.pem", Path(d) / "k2.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048",
                 "-keyout", str(key), "-out", str(crt), "-days", "1",
                 "-nodes", "-subj", "/CN=pinned-test-cert-2"],
                check=True, capture_output=True)
            _CACHE["pem2"] = crt.read_text()
    return _CACHE["pem2"]


def stub_pin(monkeypatch) -> str:
    """Make every host resolve to the same pinned certificate. Returns the PEM."""
    pem = self_signed_pem()
    monkeypatch.setattr("aiforge_core.net.trust.ensure_pinned",
                        lambda host, port=443: pem)
    return pem


def no_pin(monkeypatch) -> None:
    """Nothing pinned and nothing fetchable — the fallback path."""
    monkeypatch.setattr("aiforge_core.net.trust.ensure_pinned",
                        lambda host, port=443: "")


def trusts_the_pin(ctx) -> bool:
    """Whether ``ctx`` verifies against the stubbed certificate."""
    for cert in ctx.get_ca_certs():
        for rdn in cert.get("subject", ()):
            for key, value in rdn:
                if key == "commonName" and value == "pinned-test-cert":
                    return True
    return False
