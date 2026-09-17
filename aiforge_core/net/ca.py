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

from ._ca_describe import (  # noqa: F401  # re-exported
    _add_x509_detail,
    _certificates,
    _is_ca,
    describe,
    gaps,
)

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

#: What a corporate root actually arrives as. Windows hands out ``.cer``, an
#: internal PKI page hands out ``.crt``, openssl writes ``.pem`` — and an
#: operator who drops any of them into the ca/ folder means the same thing by
#: it. Only ``custom-ca.pem`` used to be read, so the other three were ignored
#: in silence and every https call still failed.
_CERT_SUFFIXES = (".pem", ".crt", ".cer", ".cert", ".der")
#: Where several dropped certificates are merged. An estate issues a root AND
#: intermediates, and a client needs the chain, not the first file we happened
#: to glob.
_MERGED_NAME = "bundle.pem"
#: Files in ca/ that WE generate. run.sh writes its system-merged bundle next
#: to the operator's certificate, so globbing the folder blindly folds every
#: public root back in as if they had dropped it — and the Settings panel then
#: lists ~150 of them as "added by you".
_GENERATED_NAMES = frozenset({_MERGED_NAME, "dropped-chain.pem",
                              "bundle-with-system.pem"})


def ca_dir(*, create: bool = False) -> Path:
    """The folder holding operator-supplied certificates."""
    from aiforge_core.config.secure_store import security_dir
    d = security_dir(create=create) / "ca"
    if create:
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError as exc:  # noqa: BLE001  # a mode we cannot set is a log
            log.warning("ca: could not chmod %s — %s", d, exc)
    return d


def _parses_as_certificate(pem: str) -> None:
    """Raise unless ``pem`` really is one or more X.509 certificates.

    ``ssl.DER_cert_to_PEM_cert`` does NOT validate — it base64-wraps whatever
    bytes it is handed, so a text file of notes renamed to .cer comes back as a
    perfectly-shaped BEGIN CERTIFICATE block full of nonsense. Loading it is
    the only check that parses the structure for real.
    """
    import ssl
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=pem)


def _pem_text(path: Path) -> str:
    """``path`` as VALIDATED PEM text, converting DER when that is what it holds.

    A ``.cer`` from a Windows export is usually DER — binary. Concatenating it
    into a bundle produces a file openssl silently reads as empty, which looks
    exactly like having installed nothing.
    """
    import ssl
    raw = path.read_bytes()
    pem = (raw.decode("utf-8", "replace") if b"-----BEGIN" in raw
           else ssl.DER_cert_to_PEM_cert(raw))
    _parses_as_certificate(pem)
    return pem


def dropped_certs() -> list[Path]:
    """Operator-supplied certificate files, newest name order, merged file
    excluded."""
    d = ca_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.name not in _GENERATED_NAMES
                  and p.suffix.lower() in _CERT_SUFFIXES)


def _merged_bundle(paths: list[Path]) -> Path | None:
    """One PEM holding every dropped certificate, rebuilt when an input changes.

    Returns None when nothing could be read — a bundle that silently lost a
    certificate is worse than no bundle, because verification would then fail
    with the operator believing their CA was installed.
    """
    out = ca_dir() / _MERGED_NAME
    try:
        newest = max(p.stat().st_mtime for p in paths)
        if out.is_file() and out.stat().st_mtime >= newest:
            return out
    except OSError:  # noqa: BLE001  # rebuild rather than trust a failed stat
        pass
    blocks: list[str] = []
    for p in paths:
        try:
            blocks.append(_pem_text(p).strip())
        except Exception:  # noqa: BLE001  # name the file that is wrong
            log.exception("ca: %s is not a certificate we can read — it is NOT "
                          "in the trust bundle", p.name)
    if not blocks:
        return None
    try:
        out.write_text("\n".join(blocks) + "\n", encoding="utf-8")
        out.chmod(0o600)
    except OSError:
        log.exception("ca: could not write %s", out)
        return None
    return out


def stored_path(*, create: bool = False) -> Path:
    """Where a certificate saved from the UI lives."""
    return ca_dir(create=create) / _STORE_NAME


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
    if saved.is_file():
        return str(saved)
    # Nothing from the UI — but the operator may simply have dropped their
    # corporate root into the folder, under whatever name their PKI gave it.
    dropped = dropped_certs()
    if not dropped:
        return None
    if len(dropped) == 1 and dropped[0].suffix.lower() in (".pem", ".crt"):
        try:
            if "-----BEGIN" in dropped[0].read_text(encoding="utf-8", errors="replace"):
                return str(dropped[0])       # already a usable PEM, use it as-is
        except OSError:  # noqa: BLE001  # fall through to the merge
            pass
    merged = _merged_bundle(dropped)
    return str(merged) if merged else None


def source() -> str:
    """Where the bundle in force came from: an env var name, "ui", or "".

    Saving from the UI PUBLISHES the path into SSL_CERT_FILE and friends so
    subprocesses see it — which are themselves resolution inputs. Reading the
    variable back naively therefore reported "an environment variable is set",
    which on screen means "you cannot change this here": the operator's own
    click would have locked them out of the button they had just used. So a
    value that IS our stored file is reported as what it is.
    """
    ours = {str(stored_path()), str(ca_dir() / _MERGED_NAME)}
    ours.update(str(p) for p in dropped_certs())
    for var in ENV_VARS:
        val = (os.environ.get(var) or "").strip()
        if val and val not in ours:
            return var
    if stored_path().is_file():
        return "ui"
    return "dropped" if dropped_certs() else ""


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


def own_certificates() -> list[dict]:
    """The certificates the OPERATOR supplied — pasted in, or dropped into the
    ca/ folder. Each carries ``origin`` so the screen knows which it can remove.

    Not the same as the certificates in force. The bundle actually used is
    merged with the platform's roots (``SSL_CERT_FILE`` REPLACES the trust
    store rather than adding to it), so describing the bundle listed ~150
    public roots and buried the one file the operator cared about.
    """
    out: list[dict] = []
    seen: set[str] = set()
    sources = [(stored_path(), "ui")] + [(p, "dropped") for p in dropped_certs()]
    for path, origin in sources:
        if not path.is_file():
            continue
        try:
            pem = _pem_text(path)
        except Exception:  # noqa: BLE001  # an unreadable file is reported by gaps()
            continue
        for c in describe(pem):
            if c["sha256"] in seen:
                continue                  # the same root dropped twice
            seen.add(c["sha256"])
            out.append({**c, "origin": origin, "file": path.name})
    return out


def status() -> dict:
    """Everything the Settings screen needs in one call."""
    b = bundle()
    mine = own_certificates()
    # How many are in the bundle in force. Shown as a count, never as rows:
    # the operator wants to see what THEY added, but hiding the difference
    # entirely would misrepresent what this box trusts.
    try:
        total = len(describe())
    except Exception:  # noqa: BLE001  # a bundle we cannot parse is still a bundle
        total = 0
    return {"configured": bool(b), "source": source(), "path": b or "",
            "readable": readable(b), "warnings": gaps(mine),
            "certificates": [{k: v for k, v in c.items() if k != "pem"}
                             for c in mine],
            "bundle_total": total,
            "others_in_bundle": max(0, total - len(mine)),
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
