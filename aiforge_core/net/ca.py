"""ONE CA bundle, honoured by everything AIForge talks to.

A corporate estate does not hand out one certificate per service: it runs an
internal CA (often an inspecting appliance) and expects clients to trust that
root. Before this module the answer was scattered — the model client read
``AIFORGE_LLM_CA_BUNDLE``, the Jira/Confluence/GitLab helpers read
``{PREFIX}_CA_BUNDLE`` or ``AIFORGE_CA_BUNDLE``, and git, curl, gh and npm
running as subprocesses read NONE of them, so a clone from an internal GitLab
failed with ``SSL certificate problem`` while the same host worked over REST.

So: set ``AIFORGE_CA_BUNDLE`` to your root CA and every path verifies against
it — the model endpoint, the integrations, AIForge's own HTTP, and every
subprocess that inherits the environment.

Resolution order (first one that exists wins)::

    AIFORGE_CA_BUNDLE  →  SSL_CERT_FILE  →  REQUESTS_CA_BUNDLE  →  the
    certificate saved from the UI ($AIFORGE_CONFIG_DIR/security/ca/custom-ca.pem)

The last entry is why this module exists rather than a docs line: an operator
who has a corporate root certificate should be able to paste it into Settings
and have the whole product trust it, without editing a unit file, exporting a
variable and restarting. Saving one re-publishes the subprocess variables
immediately, so the very next git clone in the same process picks it up.

``AIFORGE_LLM_CA_BUNDLE`` still overrides for the model endpoint ALONE (see
``net.ssl``), because pointing the model at a different CA from everything else
is a real deployment and the narrower var should keep winning.

**A CA bundle beats trust-on-first-use.** ``net.trust`` pins a self-signed
endpoint's own certificate when there is no other way to verify it; a CA that
issued the certificate is that other way, so every caller checks the bundle
first and TOFU never runs. Verification is never turned OFF by anything here.

**A bad path fails loudly, on purpose.** If the file cannot be read we still
export it and log an error: git then says so in one line, whereas silently
dropping it would leave the operator believing their CA was in use.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("aiforge.ca")

#: Read in order; the first non-empty value is the bundle.
ENV_VARS = ("AIFORGE_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")

#: Every variable a subprocess might read for the same answer. Git and curl
#: each have their own name, node has a third, and the two requests/openssl
#: names are what Python tooling inside a shell command reads.
SUBPROCESS_VARS = (
    "GIT_SSL_CAINFO",       # git over https
    "CURL_CA_BUNDLE",       # curl, and libcurl users
    "SSL_CERT_FILE",        # openssl / python stdlib
    "REQUESTS_CA_BUNDLE",   # requests, pip, awscli
    "NODE_EXTRA_CA_CERTS",  # node, npm, the web build
)


#: Filename under ``$AIFORGE_CONFIG_DIR/security/ca`` for the UI-saved bundle.
_STORE_NAME = "custom-ca.pem"


def stored_path(*, create: bool = False) -> Path:
    """Where a certificate saved from the UI lives."""
    from aiforge_core.config.secure_store import security_dir
    d = security_dir(create=create) / "ca"
    if create:
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError as exc:  # noqa: BLE001 — a mode we cannot set is a log
            log.warning("ca: could not chmod %s — %s", d, exc)
    return d / _STORE_NAME


def bundle() -> str | None:
    """The CA bundle path in force, or None when there is none.

    An environment variable wins over the saved certificate, so a deployment
    that sets one keeps control; the UI is the answer for everyone else.
    """
    for var in ENV_VARS:
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    saved = stored_path()
    return str(saved) if saved.is_file() else None


def source() -> str:
    """Where the bundle in force came from: an env var name, "ui", or "".

    Saving from the UI PUBLISHES the path into SSL_CERT_FILE and friends so
    subprocesses see it — which are themselves resolution inputs. Reading the
    variable back naively therefore reported "an environment variable is set",
    which on screen means "you cannot change this here": the operator's own
    click would have locked them out of the button they had just used. So a
    value that IS our stored file is reported as what it is.
    """
    ours = str(stored_path())
    for var in ENV_VARS:
        val = (os.environ.get(var) or "").strip()
        if val and val != ours:
            return var
    return "ui" if stored_path().is_file() else ""


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
        except Exception as exc:  # noqa: BLE001 — the operator's paste
            raise ValueError(f"certificate could not be read: {exc}") from exc
    return blocks


def describe(pem: str | None = None) -> list[dict]:
    """Subject, issuer, expiry and SHA-256 for each certificate in the bundle.

    The point of showing this back is that a pasted certificate is unreadable
    to a human: the screen has to prove the right file landed.
    """
    import hashlib
    import ssl
    if pem is None:
        b = bundle()
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
        try:    # cryptography ships with the http stack; never fail on it
            from cryptography import x509
            from cryptography.x509.oid import ExtensionOID
            cert = x509.load_der_x509_certificate(der)
            item["subject"] = cert.subject.rfc4514_string()
            item["issuer"] = cert.issuer.rfc4514_string()
            item["not_after"] = cert.not_valid_after_utc.isoformat()
            try:
                bc = cert.extensions.get_extension_for_oid(
                    ExtensionOID.BASIC_CONSTRAINTS).value
                item["is_ca"] = bool(bc.ca)
            except Exception:  # noqa: BLE001 — no extension means not a CA
                item["is_ca"] = False
            # Self-issued CA = a root; a CA signed by someone else = an
            # intermediate. Naming them on screen is what stops the usual
            # mistake of installing the root alone and wondering why an
            # internal host still fails.
            if item["is_ca"]:
                item["kind"] = ("root" if item["subject"] == item["issuer"]
                                else "intermediate")
            else:
                item["kind"] = "not a CA"
        except Exception:  # noqa: BLE001 — the fingerprint alone still helps
            pass
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


def save(pem: str) -> list[dict]:
    """Store a pasted CA certificate and put it into force immediately.

    Returns what was stored, described. Raises ValueError on anything that is
    not a readable certificate — silently accepting a bad paste would leave
    the operator believing TLS was fixed.
    """
    certs = describe(pem)          # validates, and raises on a bad paste
    _write("".join(c["pem"] for c in certs))
    log.info("ca: saved %d certificate(s) to %s", len(certs), stored_path())
    return certs


def add(pem: str) -> list[dict]:
    """Append certificates to the bundle, keeping what is already trusted.

    An estate hands you a root AND one or two intermediates, often as separate
    files. ``save`` replaces, which quietly loses the first file the moment the
    second is added — the failure then looks like "it forgot my certificate".
    So the screen adds, and duplicates (same fingerprint) are ignored rather
    than stacked.
    """
    incoming = describe(pem)          # validates, and raises on a bad paste
    have = describe()
    seen = {c["sha256"] for c in have}
    merged = have + [c for c in incoming if c["sha256"] not in seen]
    _write("".join(c["pem"] for c in merged))
    log.info("ca: bundle now holds %d certificate(s)", len(merged))
    return merged


def remove(sha256: str) -> bool:
    """Drop one certificate from the bundle by fingerprint."""
    have = describe()
    keep = [c for c in have if c["sha256"] != sha256]
    if len(keep) == len(have):
        return False
    if keep:
        _write("".join(c["pem"] for c in keep))
    else:
        clear()
    return True


def _write(pem: str) -> None:
    """Put ``pem`` in the store, 0600, and publish it to subprocesses."""
    path = stored_path(create=True)
    path.write_text(pem if pem.endswith("\n") else pem + "\n")
    try:
        path.chmod(0o600)
    except OSError as exc:  # noqa: BLE001
        log.warning("ca: could not chmod %s — %s", path, exc)
    apply_to_process_env(force=True)


def clear() -> bool:
    """Forget the saved certificate. True when one was removed."""
    path = stored_path()
    if not path.is_file():
        return False
    path.unlink()
    for var in SUBPROCESS_VARS:
        if (os.environ.get(var) or "").strip() == str(path):
            del os.environ[var]
    log.info("ca: removed the saved certificate")
    return True


def status() -> dict:
    """Everything the Settings screen needs in one call."""
    b = bundle()
    certs = describe()
    return {"configured": bool(b), "source": source(), "path": b or "",
            "readable": readable(b), "warnings": gaps(certs),
            "certificates": [{k: v for k, v in c.items() if k != "pem"}
                             for c in certs],
            "applies_to": ["the model endpoint", "Jira, Confluence, GitLab",
                           "AIForge's own HTTP", "git, curl, npm and the "
                           "agent's shell"]}


def readable(path: str | None = None) -> bool:
    """Whether the bundle exists and can be read. Never raises."""
    p = path or bundle()
    if not p:
        return False
    try:
        return Path(p).is_file() and os.access(p, os.R_OK)
    except OSError:
        return False


def context():
    """A VERIFYING ``ssl.SSLContext`` anchored to the bundle, or None.

    None means "no bundle configured", never "verification off" — the caller
    then uses its own default verifying context.
    """
    import ssl
    b = bundle()
    if not b:
        return None
    try:
        return ssl.create_default_context(cafile=b)
    except OSError as exc:      # ssl.SSLError derives from OSError
        raise ValueError(f"CA bundle {b!r} could not be loaded: {exc}") from exc


def subprocess_env(env: dict | None = None) -> dict:
    """``env`` (default a copy of ``os.environ``) with the CA vars filled in.

    An operator who has already set one of these keeps it: this fills gaps, it
    does not overrule a deliberate choice.
    """
    out = dict(os.environ if env is None else env)
    b = bundle()
    if not b:
        return out
    for var in SUBPROCESS_VARS:
        if not (out.get(var) or "").strip():
            out[var] = b
    return out


def apply_to_process_env(*, force: bool = False) -> str | None:
    """Publish the bundle into this process's own environment.

    Called once at startup so that everything spawned afterwards — git, gh,
    curl, npm, an MCP stdio server, whatever the agent runs in its shell —
    verifies against the same CA without each call site remembering to ask,
    and again with ``force`` after the UI saves one, so the change takes hold
    without a restart. ``force`` still leaves a value the OPERATOR set alone;
    it only replaces one this function put there.
    Returns the bundle it applied, or None.
    """
    b = bundle()
    if not b:
        return None
    if not readable(b):
        log.error("CA bundle %s is not readable — git, curl and the model "
                  "client will fail loudly rather than fall back to the "
                  "system store", b)
    ours = str(stored_path())
    for var in SUBPROCESS_VARS:
        current = (os.environ.get(var) or "").strip()
        if not current or (force and current == ours):
            os.environ[var] = b
    log.info("CA bundle %s applied to %s", b, ", ".join(SUBPROCESS_VARS))
    return b
