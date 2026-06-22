"""TLS context resolution for the LLM HTTP client — scoped to model calls.

Self-hosted OpenAI-compatible endpoints (vLLM / LiteLLM / Ollama / TGI)
are often fronted by an internal or self-signed certificate. The stdlib
``urllib.request.urlopen`` default verifies against the system trust
store, so such an endpoint fails with ``CERTIFICATE_VERIFY_FAILED``.

This module builds the ``ssl.SSLContext`` passed to *only* the LLM
call sites (``client._post``, ``health._probe``,
``openai_compatible.probe``). It never touches the process-global
default, so unrelated HTTPS traffic keeps full verification.

Env knobs (highest priority first):

* ``AIFORGE_LLM_CA_BUNDLE`` — path to a PEM CA bundle / cert. When set,
  verification stays ON but trusts this CA. Also honours the standard
  ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` if AIForge's var is unset.
* ``AIFORGE_LLM_SSL_VERIFY`` — ``true`` (default) verifies normally;
  ``false`` / ``0`` / ``no`` / ``off`` disables verification for the
  model endpoint *only* (use for an internal box with a self-signed
  cert you control). Ignored when a CA bundle is supplied.

``context_for(base_url)`` returns ``None`` for non-HTTPS URLs (plain
``http://`` local endpoints) so behaviour there is unchanged.
"""
from __future__ import annotations

import os
import ssl

_FALSEY = {"0", "false", "no", "off", ""}


def _verify_enabled() -> bool:
    raw = os.environ.get("AIFORGE_LLM_SSL_VERIFY")
    if raw is None:
        return True  # secure by default
    return raw.strip().lower() not in _FALSEY


def _ca_bundle() -> str | None:
    for var in ("AIFORGE_LLM_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()
    return None


def context_for(base_url: str | None) -> ssl.SSLContext | None:
    """Return the SSL context to pass to ``urlopen`` for ``base_url``.

    ``None`` for plain ``http://`` (and any non-https) URLs — urllib
    ignores the context there anyway, but returning ``None`` keeps the
    code path identical to the pre-existing behaviour.

    For ``https://``:
      * custom CA bundle set → verifying context trusting that CA;
      * verify disabled       → unverified context (CERT_NONE);
      * otherwise             → default verifying context.
    """
    if not base_url or not str(base_url).lower().startswith("https://"):
        return None

    ca = _ca_bundle()
    if ca:
        # Verification stays ON, anchored to the supplied CA bundle.
        return ssl.create_default_context(cafile=ca)

    if not _verify_enabled():
        # Scoped opt-out for a trusted self-hosted endpoint.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    return ssl.create_default_context()
