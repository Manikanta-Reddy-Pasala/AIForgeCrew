"""One place that decides what may leave this box.

Why a module and not a flag read at each call site: the switches were read in
five places and honoured in two, so ``AIFORGE_WEB_FETCH_DISABLE=1`` closed
``web_fetch`` while ``fetch_url``, ``http_get``, ``web_read``, ``web_crawl``
and ``browse`` sailed straight past it. An operator locking down a box was
told the door was shut and it was not. Every outbound page read now asks the
same two questions here.

The switches:
  AIFORGE_ALLOW_WEB_FETCH   may this box read a page at all (default OFF)
  AIFORGE_WEB_FETCH_DISABLE hard-off, wins over the above
  AIFORGE_WEB_SEARCH_DISABLE  legacy name for the hard-off, still honoured so a
                            machine already locked down does not reopen when
                            the web-search code was deleted

Beyond pages, the same module decides for every DECLARED destination — a host
the operator configured, not one the model composed:

  integration  Jira / Confluence / GitLab (one shared HTTP entry point)
  email        SMTP / IMAP
  telemetry    Langfuse and anything else that mirrors prompts off-box
  mcp          remote MCP servers

  AIFORGE_EGRESS_OFF          hard-off for ALL of the above at once
  AIFORGE_EGRESS_ALLOW_HOSTS  CSV; when set, a declared destination must match
  AIFORGE_<CLASS>_DISABLE     per class, e.g. AIFORGE_TELEMETRY_DISABLE
  AIFORGE_UNATTENDED_WRITES   allow WRITE verbs with no human watching (off)
  AIFORGE_UPLOAD_DISABLE      refuse file uploads outright

What this is NOT — and the docs must not claim otherwise:

  * Not a guarantee that no model-composed text can leave once fetching is ON.
    A URL carries a path and a query string, and the agent writes the URL.
    ``looks_like_search`` refuses the obvious search endpoints; it is a speed
    bump for an agent taking a shortcut, not a boundary against one determined
    to smuggle bytes out. The boundary is the switch.
  * Not a control over ARBITRARY CODE. ``run_command``, ``execute_ipython_cell``
    and anything they spawn can open a socket directly; `curl` in a shell does
    not pass through here and cannot be made to. Only an OS-level egress
    firewall or a network namespace closes that, and this module deliberately
    does not pretend to.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlsplit

_log = logging.getLogger("aiforge.egress")

_TRUE = ("1", "true", "yes", "on")

# Query-bearing endpoints of the engines an agent reaches for by reflex once it
# is told it has no search tool. Host-suffix matched, never substring-matched
# over the whole URL (``evil.example/#google.com`` must not match).
# Hosts that exist to answer queries: any query string on them is a search.
_SEARCH_HOSTS = (
    "duckduckgo.com", "bing.com", "search.brave.com", "api.tavily.com",
    "serpapi.com", "startpage.com", "ecosia.org", "mojeek.com",
    "marginalia.nu", "searx.be", "perplexity.ai", "kagi.com", "phind.com",
)
# Hosts that ALSO serve documentation, buckets and mailing lists. Refusing
# every query string on these would block ordinary reading —
# storage.googleapis.com/bucket/spec.html?v=2 is not a search — so they need a
# search-shaped PATH as well.
_MIXED_HOSTS = ("google.com", "googleapis.com", "yahoo.com", "yandex.com",
                "baidu.com", "you.com")
_SEARCH_PATHS = ("/search", "/html", "/lite", "/web", "/results", "/s")
# Fronts for the same thing: a cache view, a reader proxy, a self-hosted searx.
_SEARCH_PREFIXES = ("webcache.google", "r.jina.ai", "searx")
# google.co.uk / google.de / yahoo.co.jp — the ccTLD estates, matched by shape
# rather than by listing 190 domains.
_CCTLD_RE = re.compile(
    r"(^|\.)(google|yahoo|bing|yandex)\.[a-z]{2,3}(\.[a-z]{2})?$")


# Destination classes with a DECLARED host. "web" is handled separately: its
# host comes from the model, so it has its own, stricter default (off).
_CLASSES = ("integration", "email", "telemetry", "mcp", "sync")

# Classes where a WRITE means "this agent changed something in a system other
# people use", which is what the human-approval rule exists for. Telemetry and
# MCP are excluded on purpose: a trace POST is observability, and refusing it
# in unattended runs would blind the pipeline — exactly the runs whose traces
# matter most. Their control is the class switch, not attendance.
# NOT "sync": the memory hub is an unattended background cycle by design —
# requiring a human for it would simply switch fleet sync off.
_ATTENDED_WRITE_CLASSES = ("integration", "email")

# HTTP verbs that change something at the far end. A read pulls data toward us;
# a write pushes our content out, which is the direction this module cares
# about.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_local_host(host: str) -> bool:
    """This machine or the LAN. Browsing or fetching these is not egress, so no
    allowlist entry is ever needed for a dev server — an operator who locks the
    box down must not lose their own app.

    NOT link-local: 169.254.169.254 is the cloud metadata service, i.e. the
    target the guards exist to keep out. "On my network" and "the thing that
    hands out credentials" must not share a branch.
    """
    import ipaddress

    if not host:
        return False
    host = host.strip("[]").lower()
    if host in ("localhost", "localhost.localdomain", "host.docker.internal",
                "host.containers.internal"):
        return True
    # `.localhost` only. `.local`, `.lan` and `.internal` were here and were a
    # HOLE, not a convenience: `metadata.google.internal` is the canonical name
    # of the cloud metadata service this function's own comment says it exists
    # to keep out, and `vault.internal` / `*.svc.cluster.local` are the two
    # things most worth exfiltrating to. A SUFFIX cannot tell you an address is
    # on your machine; only an IP literal or the reserved `localhost` name can.
    # A LAN dev server reached by NAME now needs an allowlist entry, which is a
    # fair price for not shipping a metadata bypass.
    if host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_link_local:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_unspecified)


def _env_true(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in _TRUE


def _host_of(value: str) -> str:
    """Hostname from a URL *or* a bare ``host[:port]``.

    Callers legitimately hold one or the other: an SMTP/IMAP config has a host,
    an HTTP client has a URL. Without this they invented pseudo-schemes to get a
    host through — which reads as an insecure-protocol finding — or passed the
    bare host and had every check refuse it, because ``urlsplit("h.example")``
    puts it in ``path`` and leaves ``hostname`` None.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "//" + raw
    try:
        return (urlsplit(raw).hostname or "").strip().lower()
    except ValueError:
        return ""


def _host_is_writable(url: str) -> bool:
    """May we PUSH data to this host? Only the configured integrations, plus
    this machine/LAN (a local dev server is not an exfiltration destination)."""
    host = _host_of(url)
    if not host:
        return False
    if is_local_host(host):
        return True
    try:
        from aiforge_core.config.egress_hosts import write_hosts
        allow = write_hosts()
    except Exception as exc:  # noqa: BLE001
        # Fail CLOSED, as with the read list.
        _log.warning("egress write-list unavailable — refusing %s (%s)",
                     host, exc)
        return False
    return any(host == h or host.endswith("." + h) for h in allow)


def _is_blocked_address(url: str) -> bool:
    """Link-local, multicast and reserved literals — the SSRF targets. Kept
    separate from ``is_local_host`` so "my LAN" and "the thing that hands out
    cloud credentials" can never share a branch."""
    import ipaddress

    try:
        host = (urlsplit(url).hostname or "").strip("[]").lower()
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_link_local or ip.is_multicast or ip.is_reserved)


def host_allowed(url: str) -> bool:
    """Whether ``url``'s host may be reached. DEFAULT DENY.

    The allowlist is not optional and cannot be emptied into "allow all": it is
    seeded from the integrations the operator already configured (see
    config/egress_hosts.py), extended in Settings, and anything else is refused.
    An unparseable host is refused too — we cannot match what we cannot read.

    Loopback and the LAN bypass the list entirely; they are not egress.
    """
    host = _host_of(url)
    if not host:
        return False
    if is_local_host(host):
        return True
    try:
        from aiforge_core.config.egress_hosts import allowed_hosts
        allow = allowed_hosts()
    except Exception as exc:  # noqa: BLE001
        # Fail CLOSED. Everywhere else a broken probe fails open so a turn is
        # never lost, but this one decides whether bytes leave the machine, and
        # "the allowlist would not load" is not a reason to send them.
        _log.warning("egress allowlist unavailable — refusing %s (%s)",
                     host, exc)
        return False
    return any(host == h or host.endswith("." + h) for h in allow)


def class_off(kind: str) -> bool:
    """Is this destination class switched off — by its own name or the master."""
    if _env_true("AIFORGE_EGRESS_OFF"):
        return True
    return _env_true(f"AIFORGE_{kind.upper()}_DISABLE")


def attended() -> bool:
    """Is a human watching this run?

    An interactive chat has a session id and an approver attached; the ticket
    pipeline and scheduled jobs do not. Writes and uploads are the operations
    where that difference matters — approval is what makes them safe, and
    approval is exactly what an unattended run does not have.
    """
    try:
        from aiforge_core.runtime import chat_cancel
        return chat_cancel.active() is not None
    except Exception:  # noqa: BLE001 — never break a call over this
        return False


def allow(kind: str, url: str = "", *, method: str = "GET",
          upload: bool = False) -> dict | None:
    """``None`` when this may go out, else the refusal to hand back.

    ``kind`` is one of :data:`_CLASSES`. ``method`` decides read vs write;
    ``upload`` marks a call that carries a FILE rather than a sentence.
    """
    if kind not in _CLASSES:
        raise ValueError(f"unknown egress class: {kind!r}")
    if class_off(kind):
        return {"ok": False, "error": f"{kind}_egress_disabled",
                "hint": (f"{kind} traffic is switched off on this install "
                         f"(AIFORGE_EGRESS_OFF / AIFORGE_{kind.upper()}_DISABLE)."
                         " Tell the user what you would have sent.")}
    if url and not host_allowed(url):
        return {"ok": False, "error": "host_not_allowed",
                "hint": ("this host is not on the operator's egress allowlist "
                         "(AIFORGE_EGRESS_ALLOW_HOSTS).")}
    if upload and _env_true("AIFORGE_UPLOAD_DISABLE"):
        return {"ok": False, "error": "upload_disabled",
                "hint": ("file uploads are switched off on this install "
                         "(AIFORGE_UPLOAD_DISABLE).")}
    is_write = upload or method.upper() in _WRITE_METHODS
    # A host the operator ADDED in Settings is readable, never writable. Adding
    # a docs site to the allowlist must not also create somewhere to post our
    # data — reading pulls bytes in, writing pushes ours out, and only the
    # second one is exfiltration. Writable hosts come from integration config,
    # which carries a credential and a deliberate setup step.
    if is_write and url and not _host_is_writable(url):
        return {"ok": False, "error": "host_not_writable",
                "hint": ("this host is allowed for READING only. Hosts added "
                         "in Settings cannot be written to; a destination that "
                         "receives data has to be configured as an integration."
                         )}
    if (is_write and kind in _ATTENDED_WRITE_CLASSES and not attended()
            and not _env_true("AIFORGE_UNATTENDED_WRITES")):
        # The gap this closes: approval is honoured in interactive chat, but an
        # autonomous run has no approver, so tool_gate degrades ASK to allow and
        # a pipeline could post to Jira or send mail with nobody watching.
        return {"ok": False, "error": "unattended_write_refused",
                "hint": ("this run has no human attached, so writing to an "
                         "external system is refused. Report what you would "
                         "have written; an operator can set "
                         "AIFORGE_UNATTENDED_WRITES=1 to allow it.")}
    return None


def hard_off() -> bool:
    """The operator's kill switch, under either name."""
    return (_env_true("AIFORGE_WEB_FETCH_DISABLE")
            or _env_true("AIFORGE_WEB_SEARCH_DISABLE"))


def fetch_allowed() -> bool:
    """True only when page fetching is switched on AND not hard-off."""
    if hard_off():
        return False
    return str(os.environ.get("AIFORGE_ALLOW_WEB_FETCH", "0")).strip().lower() \
        in _TRUE


def looks_like_search(url: str) -> bool:
    """A query against a known search engine. Web SEARCH was removed as a
    capability, so reaching one through a page-fetch tool is the same egress by
    another name — refuse it and say so, rather than let the removal be undone
    by a URL. A bare engine homepage (no query) is not refused: it carries no
    payload."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False

    def _on(hosts) -> bool:
        return any(host == h or host.endswith("." + h) for h in hosts)

    if not (parts.query or parts.fragment):
        return False            # a homepage carries no payload
    if _on(_SEARCH_HOSTS) or host.startswith(_SEARCH_PREFIXES):
        return True
    path = (parts.path or "/").rstrip("/").lower()
    search_path = any(path == p or path.startswith(p + "/")
                      for p in _SEARCH_PATHS)
    if _on(_MIXED_HOSTS) and search_path:
        return True
    return bool(_CCTLD_RE.search(host)) and search_path


def check(url: str = "") -> dict | None:
    """``None`` when the fetch may proceed, else the refusal to hand back to
    the model — shaped like every other tool result so no caller has to invent
    an error string."""
    if hard_off():
        return {"ok": False, "error": "web_fetch_disabled",
                "hint": ("web access is switched off on this install "
                         "(AIFORGE_WEB_FETCH_DISABLE). Ask the user to paste "
                         "the content you need.")}
    if not fetch_allowed():
        return {"ok": False,
                "error": "web fetch disabled (set AIFORGE_ALLOW_WEB_FETCH=1)"}
    if url and looks_like_search(url):
        return {"ok": False, "error": "web_search_removed",
                "hint": ("this install has no web search — fetching a search "
                         "engine's result page is the same thing. Ask the user "
                         "for a direct URL, or say what you could not verify.")}
    # A blocked ADDRESS is refused with its real reason. Reaching this via the
    # allowlist ("not on your list") would send the reader after the wrong
    # problem entirely — 169.254.169.254 is not a host you forgot to add.
    if url and _is_blocked_address(url):
        return {"ok": False, "error": "blocked (ssrf): non-public address",
                "hint": ("link-local / metadata addresses are refused "
                         "regardless of the allowlist.")}
    # The allowlist applies to PAGES too, not only to declared destinations.
    # Otherwise "only the integrations are reachable" would be true of Jira and
    # false of web_fetch, which is the wider hole of the two: the integration
    # host is fixed config, while a page URL is written by the model.
    if url and not host_allowed(url):
        return {"ok": False, "error": "host_not_allowed",
                "hint": ("this host is not on the egress allowlist. Allowed by "
                         "default: the configured integrations, the model "
                         "endpoint, and this machine/LAN. An operator can add "
                         "a host in Settings -> Egress. Ask the user to add it "
                         "or to paste the content.")}
    return None


__all__ = ["allow", "attended", "check", "class_off", "fetch_allowed",
           "hard_off", "host_allowed", "looks_like_search"]
