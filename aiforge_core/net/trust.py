"""Trusting a self-hosted endpoint WITHOUT turning verification off.

The problem this replaces: a Jira, a Confluence, a GitLab or a model endpoint
on an internal host, fronted by a self-signed certificate. The old answer was
``check_hostname = False; verify_mode = CERT_NONE`` for that one endpoint —
scoped, deliberate, documented, and still "no verification at all", which means
anything on the path can impersonate the host and the client will never notice.

The right answer for a self-signed certificate is not to stop verifying: it is
to trust THAT certificate, as its own authority. ``ssl`` supports it directly —
``create_default_context(cadata=pem)`` verifies the chain against the pinned
certificate and keeps hostname checking on. A different certificate, from a
machine-in-the-middle or a re-issue, fails; which is the entire point.

**Where the trust comes from.** The operator marking an endpoint "self-signed"
IS the consent, so the first connection to such a host fetches its certificate
and records it (trust on first use). Every later connection verifies against
that recorded certificate, so the window is one connection on a network the
operator chose, rather than every connection forever. The fingerprint is
logged when it is pinned and again if it ever changes, because a silent re-pin
would give back exactly what this removed.

Pins live in ``$AIFORGE_CONFIG_DIR/security/trusted_certs/<host>.pem`` —
alongside the credentials, 0600, one file per host so an operator can read,
diff or delete one by hand.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
from pathlib import Path

log = logging.getLogger("aiforge.trust")

_DIR = "trusted_certs"
# A hostname, and nothing that could climb out of the directory. The host comes
# from configuration, but a filename built from anything network-adjacent is
# worth pinning down rather than trusting.
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_FETCH_TIMEOUT_S = 8


def _dir(*, create: bool = False) -> Path:
    from aiforge_core.config.secure_store import security_dir
    d = security_dir(create=create) / _DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError as exc:  # noqa: BLE001 — a mode we cannot set is a log
            log.warning("trust: could not chmod %s — %s", d, exc)
    return d


def pin_path(host: str) -> Path | None:
    """Where ``host``'s pinned certificate lives, or None if the name is not
    one we will build a path from."""
    h = (host or "").strip().lower()
    if not h or not _SAFE_HOST_RE.match(h):
        return None
    return _dir() / f"{h}.pem"


def fingerprint(pem: str) -> str:
    """SHA-256 of the certificate, in the form every tool prints it."""
    try:
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (ValueError, TypeError):
        return ""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def pinned_pem(host: str) -> str:
    """The certificate pinned for ``host``, or "" when there is none."""
    p = pin_path(host)
    if p is None:
        return ""
    try:
        return p.read_text() if p.is_file() else ""
    except OSError:
        return ""


def store(host: str, pem: str) -> str:
    """Record ``pem`` as the certificate for ``host``. Returns its fingerprint.

    A CHANGED certificate is logged loudly and then written: an internal CA
    re-issues, and refusing the new one would strand the operator with no way
    back except deleting a file they do not know about. The log line is what
    makes the change visible — it is the same event as a warning from ssh about
    a changed host key, and it deserves the same volume.
    """
    p = pin_path(host)
    if p is None or not (pem or "").strip():
        return ""
    fp = fingerprint(pem)
    old = pinned_pem(host)
    try:
        _dir(create=True)
        p.write_text(pem)
        p.chmod(0o600)
    except OSError as exc:  # noqa: BLE001 — never break a connection over this
        log.warning("trust: could not pin %s — %s", host, exc)
        return fp
    if old and old.strip() != pem.strip():
        log.warning("trust: the certificate for %s CHANGED — now %s. If you did "
                    "not re-issue it, stop and investigate.", host, fp)
    else:
        log.info("trust: pinned %s (%s)", host, fp)
    return fp


def fetch(host: str, port: int = 443) -> str:
    """The certificate ``host`` presents, as PEM, or "".

    Fetching a self-signed certificate cannot itself verify one — that is the
    definition of the problem. ``ssl.get_server_certificate`` is the stdlib
    helper for exactly this, so the handshake happens inside CPython rather
    than through a hand-built non-verifying context of ours: nothing in this
    codebase sets CERT_NONE, and the one unverified handshake in the system is
    a certificate READ that sends nothing and reads no body.
    """
    if not host:
        return ""
    try:
        return ssl.get_server_certificate((host, int(port)),
                                          timeout=_FETCH_TIMEOUT_S) or ""
    except (OSError, ValueError) as exc:
        log.info("trust: could not fetch a certificate from %s:%s — %s",
                 host, port, exc)
        return ""


def ensure_pinned(host: str, port: int = 443) -> str:
    """The pinned certificate for ``host``, fetching and recording it if this
    is the first time. "" when the host cannot be reached."""
    pem = pinned_pem(host)
    if pem:
        return pem
    if not tofu_enabled():
        log.warning("trust: no pinned certificate for %s and trust-on-first-use "
                    "is off (AIFORGE_TLS_NO_TOFU) — pin it by hand or supply a "
                    "CA bundle", host)
        return ""
    pem = fetch(host, port)
    if pem:
        store(host, pem)
    return pem


def context_for_pin(host: str, port: int = 443) -> ssl.SSLContext | None:
    """A VERIFYING context anchored to ``host``'s pinned certificate.

    None when there is nothing pinned and none could be fetched — the caller
    then falls back to ordinary verification, which is the right failure: a
    connection refused for a certificate reason, with a message naming the fix.
    """
    pem = ensure_pinned(host, port)
    if not pem:
        return None
    try:
        return ssl.create_default_context(cadata=pem)
    except (ssl.SSLError, ValueError) as exc:
        log.warning("trust: pinned certificate for %s is unusable — %s",
                    host, exc)
        return None


def forget(host: str) -> bool:
    """Drop a pin. True when a file was removed."""
    p = pin_path(host)
    try:
        if p is not None and p.is_file():
            p.unlink()
            return True
    except OSError as exc:  # noqa: BLE001
        log.warning("trust: could not remove the pin for %s — %s", host, exc)
    return False


def listing() -> list[dict]:
    """Every pinned host and its fingerprint — the answer to "what do we
    trust", which nobody could ask while the answer was "whatever answers"."""
    out: list[dict] = []
    d = _dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.pem")):
        try:
            out.append({"host": f.stem, "fingerprint": fingerprint(f.read_text()),
                        "path": str(f)})
        except OSError:
            continue
    return out


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in (
        "1", "true", "yes", "on")


def tofu_enabled() -> bool:
    """Trust-on-first-use may be switched off for a deployment that pins its
    certificates by hand (or ships a CA bundle) and wants a fetch to be an
    error rather than a silent trust decision."""
    return not _env_flag("AIFORGE_TLS_NO_TOFU")


__all__ = ["context_for_pin", "ensure_pinned", "fetch", "fingerprint",
           "forget", "listing", "pin_path", "pinned_pem", "store",
           "tofu_enabled"]
