"""Trusting a local CA from the SCREEN, not only from a unit file.

The report that drove this: "urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: unable to get local issuer certificate (bearer
auth)" against an internal Jira, from someone with no shell on the box. The
only fix at the time was an env var and a restart, so the UI had to grow a
way to say "trust this certificate", and it has to take hold immediately.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aiforge_core.net import ca

# A throwaway self-signed certificate, generated in the fixture below.
pytest.importorskip("cryptography")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for var in set(ca.ENV_VARS) | set(ca.SUBPROCESS_VARS):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def pem() -> str:
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Acme Root CA")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                           critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
def client():
    from aiforge_core.api.api import app
    return TestClient(app)


# ── the store ───────────────────────────────────────────────────────────────

def test_nothing_configured_by_default():
    st = ca.status()
    assert st["configured"] is False
    assert st["source"] == ""


def test_saving_puts_it_in_force_at_once(pem):
    ca.save(pem)
    assert ca.bundle() == str(ca.stored_path())
    assert ca.source() == "ui"
    # …and the next subprocess sees it without a restart, which is the point.
    import os
    assert os.environ["GIT_SSL_CAINFO"] == str(ca.stored_path())


def test_saved_file_is_not_world_readable(pem):
    ca.save(pem)
    assert oct(ca.stored_path().stat().st_mode)[-3:] == "600"


def test_the_screen_can_show_what_landed(pem):
    certs = ca.save(pem)
    assert certs
    assert "Acme Root CA" in certs[0]["subject"]
    assert len(certs[0]["sha256"]) == 64


def test_a_bad_paste_is_refused_rather_than_stored():
    with pytest.raises(ValueError):
        ca.save("this is not a certificate")
    assert not ca.stored_path().is_file()


def test_an_env_bundle_still_wins(pem, monkeypatch, tmp_path):
    ca.save(pem)
    other = tmp_path / "estate.pem"
    other.write_text(pem)
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", str(other))
    assert ca.bundle() == str(other)
    assert ca.source() == "AIFORGE_CA_BUNDLE"


def test_clearing_forgets_it(pem):
    ca.save(pem)
    assert ca.clear() is True
    assert ca.bundle() is None
    import os
    assert "GIT_SSL_CAINFO" not in os.environ


# ── the routes the screen calls ─────────────────────────────────────────────

def test_get_reports_nothing_configured(client):
    body = client.get("/api/runtime/ca").json()
    assert body["configured"] is False
    assert "the model endpoint" in body["applies_to"]


def test_put_then_get_round_trips(client, pem):
    put = client.put("/api/runtime/ca", json={"pem": pem})
    assert put.status_code == 200
    assert put.json()["saved"] == 1
    body = client.get("/api/runtime/ca").json()
    assert body["configured"] is True
    assert body["source"] == "ui"
    assert "Acme Root CA" in body["certificates"][0]["subject"]


def test_put_rejects_junk_with_a_reason(client):
    r = client.put("/api/runtime/ca", json={"pem": "nope"})
    assert r.status_code == 400
    assert "certificate" in r.json()["detail"].lower()


def test_delete_removes_it(client, pem):
    client.put("/api/runtime/ca", json={"pem": pem})
    r = client.delete("/api/runtime/ca")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert client.get("/api/runtime/ca").json()["configured"] is False


def test_an_integration_uses_the_saved_certificate(pem, monkeypatch):
    """The actual bug: a Jira call verified against the system store only."""
    from aiforge_core.runtime.tools import _http_integration as hi
    ca.save(pem)
    conf = hi.integration_conf("jira", "JIRA")
    assert conf["ca_bundle"] == str(ca.stored_path())
    assert conf["insecure_tls"] is False
    ctx = hi.ssl_context(insecure_tls=False, ca_bundle=conf["ca_bundle"],
                         url="https://jira.internal")
    assert ctx is not None            # verification ON, anchored to our CA
