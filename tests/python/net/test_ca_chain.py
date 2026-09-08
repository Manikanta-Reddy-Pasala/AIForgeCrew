"""A root AND its intermediates, which is how a real estate hands them out.

The user's case: "I have ca, intermediate cert to trust for all like models
jira, confluence git and other places". Two properties have to hold, and only
one of them is about parsing:

1. adding the second file must not drop the first — a replace-on-save store
   loses the root the moment the intermediate is uploaded, and the failure
   reads as "it forgot my certificate";
2. the bundle must actually VERIFY a server whose certificate was issued by
   the intermediate. That is a TLS handshake, not a string comparison, so
   this module performs one against a real socket.
"""
from __future__ import annotations

import ssl
import threading

import pytest

from aiforge_core.net import ca

pytest.importorskip("cryptography")

from cryptography import x509                                    # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa         # noqa: E402
from cryptography.x509.oid import NameOID                         # noqa: E402
from datetime import datetime, timedelta, timezone                # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for var in set(ca.ENV_VARS) | set(ca.SUBPROCESS_VARS):
        monkeypatch.delenv(var, raising=False)


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert(subject, issuer_name, issuer_key, pub, *, ca_cert, days=365):
    now = datetime.now(timezone.utc)
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                     subject)]))
         .issuer_name(issuer_name)
         .public_key(pub)
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - timedelta(days=1))
         .not_valid_after(now + timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=ca_cert, path_length=None),
                        critical=True))
    if not ca_cert:
        b = b.add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False)
    return b.sign(issuer_key, hashes.SHA256())


@pytest.fixture
def chain(tmp_path):
    """root → intermediate → leaf(localhost), plus the leaf's private key."""
    rk, ik, lk = _key(), _key(), _key()
    root = _cert("Acme Root CA", x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Acme Root CA")]),
        rk, rk.public_key(), ca_cert=True, days=730)
    inter = _cert("Acme Issuing CA", root.subject, rk, ik.public_key(),
                  ca_cert=True, days=730)
    leaf = _cert("localhost", inter.subject, ik, lk.public_key(),
                 ca_cert=False)
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM).decode()
    leaf_file = tmp_path / "leaf.pem"
    leaf_file.write_text(
        pem(leaf) + lk.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()).decode())
    return {"root": pem(root), "intermediate": pem(inter),
            "leaf_file": str(leaf_file)}


def _serve(leaf_file: str):
    """A TLS socket presenting ONLY the leaf — no chain, as plenty of
    internal appliances are configured."""
    import socket
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(leaf_file)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def run():
        try:
            conn, _ = sock.accept()
            with ctx.wrap_socket(conn, server_side=True) as s:
                s.recv(16)
        except Exception:      # noqa: BLE001 — a refused handshake is the test
            pass
        finally:
            sock.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return port


def _handshake(port: int) -> None:
    """Connect with the CA bundle in force. Raises on a verification failure."""
    import socket
    ctx = ca.context()
    assert ctx is not None, "no bundle configured"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname="localhost") as s:
            s.send(b"hi")


# ── the two files, kept together ────────────────────────────────────────────

def test_adding_the_intermediate_keeps_the_root(chain):
    ca.add(chain["root"])
    certs = ca.add(chain["intermediate"])
    subjects = sorted(c["subject"] for c in certs)
    assert len(certs) == 2, "the root was dropped when the intermediate landed"
    assert any("Acme Root CA" in s for s in subjects)
    assert any("Acme Issuing CA" in s for s in subjects)


def test_each_one_is_named_for_what_it_is(chain):
    certs = ca.add(chain["root"] + chain["intermediate"])
    kinds = {c["subject"].split("=")[-1]: c["kind"] for c in certs}
    assert kinds["Acme Root CA"] == "root"
    assert kinds["Acme Issuing CA"] == "intermediate"


def test_the_same_file_twice_is_not_stacked(chain):
    ca.add(chain["root"])
    assert len(ca.add(chain["root"])) == 1


def test_one_can_be_removed_without_losing_the_other(chain):
    certs = ca.add(chain["root"] + chain["intermediate"])
    inter = next(c for c in certs if c["kind"] == "intermediate")
    assert ca.remove(inter["sha256"]) is True
    left = ca.describe()
    assert len(left) == 1
    assert left[0]["kind"] == "root"


def test_a_lone_intermediate_is_flagged(chain):
    ca.add(chain["intermediate"])
    assert any("issuer" in w for w in ca.gaps())


def test_no_warning_once_the_root_is_there_too(chain):
    ca.add(chain["root"] + chain["intermediate"])
    assert ca.gaps() == []


# ── the handshake, which is the whole point ────────────────────────────────

def test_the_root_alone_cannot_verify_a_leaf_the_server_sends_bare(chain):
    """This IS the reported error, reproduced: the server offers no chain, so
    the root alone leaves a hole where the intermediate should be."""
    ca.add(chain["root"])
    port = _serve(chain["leaf_file"])
    with pytest.raises(ssl.SSLCertVerificationError) as exc:
        _handshake(port)
    assert "unable to get local issuer certificate" in str(exc.value)


def test_root_plus_intermediate_verifies_the_same_server(chain):
    ca.add(chain["root"])
    ca.add(chain["intermediate"])
    _handshake(_serve(chain["leaf_file"]))     # no exception = verified


def test_the_integration_path_uses_that_same_bundle(chain):
    from aiforge_core.runtime.tools import _http_integration as hi
    ca.add(chain["root"] + chain["intermediate"])
    conf = hi.integration_conf("jira", "JIRA")
    assert conf["ca_bundle"] == str(ca.stored_path())
    assert conf["insecure_tls"] is False
    port = _serve(chain["leaf_file"])
    ctx = hi.ssl_context(insecure_tls=False, ca_bundle=conf["ca_bundle"],
                         url=f"https://localhost:{port}")
    import socket
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname="localhost") as s:
            s.send(b"hi")                       # Jira over the internal CA


# ── the pin has to work as an anchor too ────────────────────────────────────
# Reported live: an operator ticked "skip TLS verify" for a self-hosted model
# endpoint and got the CA-bundle error back out of the pinning path —
#   probe -> url=https://chat.ai.internal/api/v1/models insecure_flag=True
#            tls=pinned(self-signed)
#   probe FAILED ... [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify
#            failed: unable to get local issuer certificate
# Two separate defects produced that one line, and each gets a handshake here.

def _pin(host: str, leaf_file: str) -> None:
    """Pin exactly what ``trust.fetch`` would record: the leaf, alone."""
    from aiforge_core.net import trust
    with open(leaf_file) as fh:
        leaf = fh.read().split("-----BEGIN PRIVATE KEY")[0]
    leaf = leaf.split("-----BEGIN RSA PRIVATE KEY")[0]
    trust.store(host, leaf)


def _connect(ctx, port: int) -> None:
    import socket
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname="localhost") as s:
            s.send(b"hi")


def test_a_pinned_leaf_verifies_the_server_that_presented_it(chain):
    """The defect: a leaf is not self-issued, so without PARTIAL_CHAIN OpenSSL
    walks PAST the pin looking for the internal CA that signed it, does not
    find it, and reports the operator's exact error — from the very path whose
    whole purpose is to make a self-signed endpoint reachable."""
    from aiforge_core.net import trust
    _pin("localhost", chain["leaf_file"])
    ctx = trust.context_for_pin("localhost")
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname
    _connect(ctx, _serve(chain["leaf_file"]))      # no exception = verified


def test_a_pin_is_still_a_pin_and_refuses_a_different_certificate(chain,
                                                                  tmp_path):
    """PARTIAL_CHAIN must not become "trust anything the host offers"."""
    from aiforge_core.net import trust
    other_k = _key()
    other = _cert("localhost", x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]),
        other_k, other_k.public_key(), ca_cert=False)
    other_file = tmp_path / "other.pem"
    other_file.write_text(
        other.public_bytes(serialization.Encoding.PEM).decode()
        + other_k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()).decode())
    _pin("localhost", chain["leaf_file"])          # pinned: the REAL leaf
    ctx = trust.context_for_pin("localhost")
    port = _serve(str(other_file))                 # served: a substitute
    with pytest.raises(ssl.SSLError):
        _connect(ctx, port)


def test_the_skip_verify_path_uses_the_operators_ca_when_there_is_one(chain):
    """"A CA bundle beats trust-on-first-use on every path" — insecure_context
    was the path where it did not, so uploading a root + intermediate and then
    ticking "skip TLS verify" discarded the bundle and pinned the leaf."""
    from aiforge_core.net.ssl import insecure_context
    ca.add(chain["root"] + chain["intermediate"])
    port = _serve(chain["leaf_file"])
    _connect(insecure_context(f"https://localhost:{port}"), port)


def test_the_probe_label_names_the_ca_bundle_when_one_is_in_force(chain,
                                                                  monkeypatch):
    """The log line has to name what actually anchored the handshake."""
    from aiforge_core.llm.providers.openai_compatible import _probe_tls_plan
    url = "https://chat.ai.internal/api/v1/models"
    assert _probe_tls_plan(url, True)[1] == "pinned(self-signed)"
    ca.add(chain["root"])
    assert _probe_tls_plan(url, True)[1] == "ca-bundle"
