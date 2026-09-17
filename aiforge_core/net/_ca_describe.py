"""Describing the certificates in the CA store and the gaps in the chain."""
from __future__ import annotations

from pathlib import Path


def _pkg():
    """``ca``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``ca``; patch any other
    name on this module."""
    import aiforge_core.net.ca as package
    return package


def _certificates(pem: str) -> list[str]:
    """The PEM blocks in ``pem``. Raises ValueError if there are none or one
    of them is not a certificate we can parse."""
    import re
    import ssl
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        pem, re.S)
    if not blocks:
        raise ValueError(
            "no certificate found — paste the PEM text, including the "
            "-----BEGIN CERTIFICATE----- line")
    for b in blocks:
        try:
            ssl.PEM_cert_to_DER_cert(b + "\n")
        except Exception as exc:  # noqa: BLE001  # the operator's paste
            raise ValueError(f"certificate could not be read: {exc}") from exc
    return blocks


def _is_ca(cert) -> bool:
    """True when basicConstraints says CA. A certificate without the extension
    is not one."""
    try:
        from cryptography.x509.oid import ExtensionOID
        bc = cert.extensions.get_extension_for_oid(
            ExtensionOID.BASIC_CONSTRAINTS).value
        return bool(bc.ca)
    except Exception:  # noqa: BLE001  # no extension means not a CA
        return False


def _add_x509_detail(item: dict, der: bytes) -> None:
    """Fill subject/issuer/expiry/kind in place. Never raises: the fingerprint
    alone is still worth showing, and cryptography is an indirect dependency.

    Self-issued CA = a root; a CA signed by someone else = an intermediate.
    Naming them on screen is what stops the usual mistake of installing the
    root alone and wondering why an internal host still fails.
    """
    try:
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der)
        item["subject"] = cert.subject.rfc4514_string()
        item["issuer"] = cert.issuer.rfc4514_string()
        item["not_after"] = cert.not_valid_after_utc.isoformat()
        item["is_ca"] = _is_ca(cert)
    except Exception:  # noqa: BLE001
        return
    if item["is_ca"]:
        item["kind"] = ("root" if item["subject"] == item["issuer"]
                        else "intermediate")
    else:
        item["kind"] = "not a CA"


def describe(pem: str | None = None) -> list[dict]:
    """Subject, issuer, expiry and SHA-256 for each certificate in the bundle.

    The point of showing this back is that a pasted certificate is unreadable
    to a human: the screen has to prove the right file landed.
    """
    import hashlib
    import ssl
    if pem is None:
        b = _pkg().bundle()
        try:
            pem = Path(b).read_text() if b else ""
        except OSError:
            return []
    out = []
    for block in _certificates(pem) if pem.strip() else []:
        der = ssl.PEM_cert_to_DER_cert(block + "\n")
        item = {"sha256": hashlib.sha256(der).hexdigest(),
                "subject": "", "issuer": "", "not_after": "",
                "kind": "certificate", "is_ca": False, "pem": block + "\n"}
        _add_x509_detail(item, der)
        out.append(item)
    return out


def gaps(certs: list[dict] | None = None) -> list[str]:
    """Problems worth telling the operator about, in their words.

    An intermediate whose issuer is not in the bundle still verifies IF the
    server sends the chain — most do — so this is a warning, never a refusal.
    The one we care about is the reverse of the usual advice: people paste the
    root, the server sends only its leaf, and nothing works.
    """
    certs = describe() if certs is None else certs
    subjects = {c["subject"] for c in certs if c["subject"]}
    out = []
    for c in certs:
        if c["kind"] == "intermediate" and c["issuer"] not in subjects:
            out.append(f"{c['subject']} is an intermediate and its issuer "
                       f"({c['issuer']}) is not in this list — add that root "
                       "too unless your servers send the full chain")
        if c["kind"] == "not a CA":
            out.append(f"{c['subject'] or c['sha256'][:16]} is a server "
                       "certificate, not a CA — trusting it covers that one "
                       "host only")
    return out
