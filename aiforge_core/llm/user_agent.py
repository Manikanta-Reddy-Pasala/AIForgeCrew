"""The User-Agent AIForge sends on model calls.

    aiforge/<version> (<username>)

Requested so calls are attributable at the gateway: which client, which build,
which person. The old value was a lie — every request went out as
``curl/8.5.0 (aiforge)``, chosen only because some proxies reject the stdlib
``Python-urllib`` agent. It identified nothing, and an operator reading gateway
logs could not tell one user's traffic from another's, or a stale build from a
current one.

Every field degrades on its own: an unknown version does not cost the username,
and an unknown username does not cost the version. A header that fails to build
must never fail the call, so the whole thing is wrapped and falls back.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

CLIENT = "aiforge"

# One token, no spaces, parens or control characters — those would break the
# header's own grammar (RFC 9110 product / comment syntax) and some gateways
# reject or truncate the whole field rather than the bad part.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _clean(value: str, fallback: str) -> str:
    out = _SAFE.sub("-", (value or "").strip()).strip("-")
    return out or fallback


@lru_cache(maxsize=1)
def version() -> str:
    """The installed package version, else the pyproject one, else 'dev'.

    Cached: ``importlib.metadata.version`` walks every sys.path entry, which is
    unbounded on a large or network-mounted site-packages, and this runs on the
    HTTP hot path. The username and the env override stay live.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version as _v
        try:
            return _clean(_v("aiforgecrew"), "dev")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001
        pass
    # Running from a checkout without an install (./run.sh does this): read the
    # single source of truth rather than duplicating the number in code.
    try:
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
        in_project = False
        for line in root.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_project = stripped == "[project]"
                continue
            # Anchored to [project] and to `version =`: "the first line that
            # starts with version" picked up `version_scheme = "guess"` from
            # any tool section that happened to sit higher in the file.
            if in_project and re.match(r"^version\s*=", stripped):
                return _clean(stripped.split("=", 1)[1].strip().strip('"\''), "dev")
    except Exception:  # noqa: BLE001
        pass
    return "dev"


def username() -> str:
    """The OS login name — macOS, Linux and Windows.

    getpass.getuser() reads LOGNAME/USER/LNAME/USERNAME then falls back to the
    password database, which covers all three; it RAISES on a container with no
    passwd entry and no env, so the call is guarded rather than trusted.
    """
    try:
        import getpass
        name = getpass.getuser()
    except Exception:  # noqa: BLE001
        name = (os.environ.get("USER") or os.environ.get("USERNAME")
                or os.environ.get("LOGNAME") or "")
    # Decompose accents first, so `José` reports as `Jose` rather than `Jos`
    # and `éric` as `eric` rather than `e-ric`. A name in a non-Latin script
    # still resolves to "unknown" — the honest outcome, since there is no
    # faithful ASCII for it and a mangled one attributes nothing.
    try:
        import unicodedata
        name = "".join(c for c in unicodedata.normalize("NFKD", name)
                       if not unicodedata.combining(c))
    except Exception:  # noqa: BLE001
        pass
    # Capped: gateways bound header size (nginx defaults to 8k buffers), and a
    # pathological login name should degrade this header, not the request.
    return _clean(name, "unknown")[:32]


# Values that mean "send no username". Without one of these there was no way
# to opt out at all: setting AIFORGE_LLM_USER_AGENT empty falls through to the
# default, so a user who read the release note and blanked the variable kept
# sending their login name to every third-party endpoint.
_OPT_OUT = {"off", "none", "no", "0", "false", "anon", "anonymous"}

# A header value may not carry control characters. urllib rejects a bare CRLF
# outright — as a ValueError the retry classifier treats as PERMANENT, so one
# typo in this variable kills every model call with an opaque "Invalid header
# value" — while CRLF+space (obs-fold, RFC 9110 §5.2, deprecated precisely
# because proxies disagree on it) went out on the wire intact. The one field a
# human types by hand was the only one not being sanitised.
_CTRL = re.compile(r"[\r\n\x00-\x1f\x7f]+")


def user_agent() -> str:
    """The full header value.

    ``AIFORGE_LLM_USER_AGENT`` overrides it — the escape hatch for a proxy or
    WAF that demands a specific string, which is the only reason the old
    curl-like default existed. Set it to ``off`` (or none/no/0/false/anon) to
    send the client and version WITHOUT the username.
    """
    override = _CTRL.sub("", os.environ.get("AIFORGE_LLM_USER_AGENT") or "").strip()
    if override.lower() in _OPT_OUT:
        return f"{CLIENT}/{version()}"
    if override:
        return override
    try:
        return f"{CLIENT}/{version()} ({username()})"
    except Exception:  # noqa: BLE001 — a header must never break a call
        return f"{CLIENT}/dev (unknown)"


__all__ = ["user_agent", "username", "version", "CLIENT"]
