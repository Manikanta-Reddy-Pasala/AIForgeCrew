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

Resolution order (first non-empty wins)::

    AIFORGE_CA_BUNDLE   →   SSL_CERT_FILE   →   REQUESTS_CA_BUNDLE

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


def bundle() -> str | None:
    """The configured CA bundle path, or None when there is none."""
    for var in ENV_VARS:
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    return None


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


def apply_to_process_env() -> str | None:
    """Publish the bundle into this process's own environment.

    Called once at startup so that everything spawned afterwards — git, gh,
    curl, npm, an MCP stdio server, whatever the agent runs in its shell —
    verifies against the same CA without each call site remembering to ask.
    Returns the bundle it applied, or None.
    """
    b = bundle()
    if not b:
        return None
    if not readable(b):
        log.error("CA bundle %s is not readable — git, curl and the model "
                  "client will fail loudly rather than fall back to the "
                  "system store", b)
    for var in SUBPROCESS_VARS:
        if not (os.environ.get(var) or "").strip():
            os.environ[var] = b
    log.info("CA bundle %s applied to %s", b, ", ".join(SUBPROCESS_VARS))
    return b
