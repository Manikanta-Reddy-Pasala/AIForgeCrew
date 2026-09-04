"""Which hosts this box may talk to. Default DENY.

The rule the operator asked for: egress enforcement is ALWAYS on, the only
destinations allowed by default are the integrations they configured, and
anything else has to be added deliberately in Settings.

That is the inverse of how the allowlist started life. It began opt-in — an
empty list meant "no restriction" — because a list that defaulted to deny would
break every install the moment it shipped. It now defaults to deny anyway, and
the breakage is handled by DERIVING the base list from configuration that
already exists: if Jira is configured, the Jira host is allowed, and nobody has
to type it twice. A host that stops being configured stops being allowed, which
is the behaviour you want from a list you never maintain by hand.

Three sources, in order:
  1. derived   — the hosts of the configured integrations, the model endpoint
                 and the observability sink. Never stored; recomputed each call
                 so it cannot go stale against the config it mirrors.
  2. stored    — extras an operator added in Settings ($AIFORGE_CONFIG_DIR/
                 egress.json, written 0600 like every other config file).
  3. env       — AIFORGE_EGRESS_ALLOW_HOSTS, for headless/systemd deployments
                 that never open the UI.

LOOPBACK AND THE LAN ARE NOT IN THIS LIST and never need to be: they are not
egress, and `net.egress` decides them separately. Putting them here would let
an operator "tidy up" the list and lose their own dev server.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("aiforge.egress_hosts")

_FILE = "egress.json"


def _path(*, create: bool = False) -> Path:
    """``create`` only on WRITE. Reading used to mkdir, so on a config dir the
    process cannot write, ``stored_hosts()`` raised — and because it is the
    first operand of the union in ``allowed_hosts()``, the whole list died with
    it, including the AIFORGE_EGRESS_ALLOW_HOSTS fallback that exists for
    exactly that headless deployment. Fail-open on the file, never on the gate."""
    from aiforge_core.config.paths import config_dir
    d = Path(str(config_dir()))
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d / _FILE


def _host_of(value: str) -> str:
    """Host from a URL, or from a bare host[:port] string. Empty when neither."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "//" + raw
    try:
        return (urlsplit(raw).hostname or "").strip()
    except ValueError:
        return ""


# ── the probes ──────────────────────────────────────────────────────────────
# One function per configured destination, at module level rather than nested
# inside the collector: nine closures in one body is how that function reached a
# cognitive complexity of 19, and each of these is independently readable and
# independently testable. Every one returns RAW values (a URL, a host, maybe
# empty) — normalising to a hostname happens once, in the collector.


def _probe_ssl():
    """The service inventory net/ssl.py already keeps — model endpoint, embed
    and rerank sidecars, memory http, AIForge's own API, the MCP env list, the
    generic AIFORGE_*_BASE_URL sweep and per-role agent_config rows."""
    from aiforge_core.net.ssl import _configured_service_hosts
    return _configured_service_hosts()


def _probe_jira():
    from aiforge_core.runtime.tools.jira._core import _base
    return [_base()]


def _probe_confluence():
    from aiforge_core.runtime.tools.confluence._config import _base
    return [_base()]


def _probe_gitlab():
    from aiforge_core.runtime.tools.gitlab import _base
    return [_base()]


def _probe_mail():
    from aiforge_core.runtime.tools import email_tool
    return [(email_tool._smtp_conf() or {}).get("host"),
            (email_tool._imap_conf() or {}).get("host")]


def _probe_mcp():
    """As the CLIENT resolves them: env list AND the marketplace registry.
    Reading only the env var meant a one-click-installed server was refused on
    every call."""
    from aiforge_core.runtime.tools.mcp_client import _load_endpoints
    return list((_load_endpoints() or {}).values())


def _probe_registry():
    """The escalation chain lives in the model registry, which agent_config
    does not see."""
    from aiforge_core.config import model_registry
    return [row.get("base_url") for row in (model_registry.list_models() or [])
            if isinstance(row, dict)]


def _probe_admin():
    """May come from Settings rather than the environment."""
    from aiforge_core.memory.sync import role
    return [role.admin_url()]


def _probe_sinks():
    return [os.environ.get(var, "") for var in (
        "LANGFUSE_HOST", "AIFORGE_OTEL_ENDPOINT", "AIFORGE_PDS_API_BASE",
        "AIFORGE_EMBED_API_URL", "AIFORGE_CODEMEM_LM_URL",
        "AIFORGE_INTENT_LM_URL", "AIFORGE_PLANNER_LM_URL")]


_HOST_PROBES = (_probe_ssl, _probe_jira, _probe_confluence, _probe_gitlab,
                _probe_mail, _probe_mcp, _probe_registry, _probe_admin,
                _probe_sinks)


def _integration_hosts() -> set[str]:
    """Hosts of everything this install is configured to talk to.

    Derived from the CONSUMERS wherever one exists, not from a hand-kept list
    of env names. The first version of this function WAS such a list, and it
    was both wrong and short: it missed the MCP marketplace registry, the model
    registry's escalation rows, a Settings-saved sync admin, and half the
    sidecar env vars — each miss a feature that dies silently the moment the
    list defaults to deny. Worse, ``net/ssl.py`` already maintained the correct
    inventory (including a generic ``AIFORGE_*_BASE_URL`` sweep), so there were
    two copies of one rule — which is the drift this codebase has been bitten by
    before.

    Every probe is guarded SEPARATELY. One half-configured integration must not
    fail the box closed on everything at once, the model endpoint included.
    """
    out: set[str] = set()
    for probe in _HOST_PROBES:
        try:
            for value in probe() or ():
                host = _host_of(str(value or ""))
                if host:
                    out.add(host)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.debug("egress host probe %s failed: %s", probe.__name__, exc)
    return out


def stored_hosts() -> list[str]:
    """Extras added in Settings. Never raises: the env fallback has to survive
    an unreadable store."""
    try:
        p = _path()
        if not p.exists():
            return []
    except OSError as exc:
        log.warning("egress store unreadable (%s) — extras treated as empty", exc)
        return []
    try:
        data = json.loads(p.read_text()) or {}
    except Exception:  # noqa: BLE001 — a corrupt file must not open the gate
        log.warning("egress.json unreadable — treating the extras list as empty")
        return []
    hosts = data.get("extra_hosts")
    if not isinstance(hosts, list):
        return []
    return [h for h in (_host_of(str(x)) for x in hosts) if h]


def set_stored_hosts(hosts: list[str]) -> list[str]:
    """Replace the Settings extras. Returns what was saved (normalised to bare
    hostnames, deduped, order preserved)."""
    from aiforge_core.config import _atomic

    clean: list[str] = []
    for raw in hosts or []:
        h = _host_of(str(raw))
        if not h or h in clean:
            continue
        # Shape check. Suffix matching means one careless entry is the whole
        # internet: "com" allows every .com, and a public suffix like
        # "github.io" allows every user page on it. A single label is never a
        # host you meant to name.
        if h.count(".") < 1 or h.strip(".") != h:
            raise ValueError(
                f"{h!r} is not a specific enough host — give a full name like "
                "docs.python.org, not a bare label or a suffix")
        if any(c in h for c in "*?/ "):
            raise ValueError(f"{h!r} is not a hostname (wildcards are not supported)")
        if h.count(".") == 1:
            # Matching is host-or-subdomain, so a two-label entry covers
            # everything beneath it. That is right for "corp.example" and very
            # wrong for a public suffix like "github.io", where it grants every
            # user page. We cannot tell the two apart without a public-suffix
            # list, so say so rather than guess.
            log.warning("egress: %r also allows every subdomain — intended for "
                        "your own domain, not a public suffix like github.io", h)
        clean.append(h)
    _atomic.write_text(_path(create=True),
                       json.dumps({"extra_hosts": clean}, indent=2))
    _invalidate()
    return clean


def _env_hosts() -> set[str]:
    """Read-only extras named in the environment.

    Includes AIFORGE_BROWSER_ALLOWLIST: naming a host there is the same
    deliberate operator act as adding one in Settings, so it should grant
    READING. It must not grant writing, which is why these are here and not in
    the derived set — an operator listing github.com to look at a page has not
    asked for anything to be posted to it.
    """
    out: set[str] = set()
    for var in ("AIFORGE_EGRESS_ALLOW_HOSTS", "AIFORGE_BROWSER_ALLOWLIST"):
        raw = (os.environ.get(var) or "").strip()
        out.update(h for h in (_host_of(x) for x in raw.split(",")) if h)
    return out


_CACHE: dict[str, tuple[float, set[str]]] = {}
_CACHE_TTL_S = 5.0


def _invalidate() -> None:
    _CACHE.clear()


def _derived_cached() -> set[str]:
    """Derivation touches ~20 files and re-parses agent_config, and it runs on
    EVERY outbound decision — twice for a write, since the read list and the
    write list each derive. A few seconds of memoization keeps a Settings edit
    feeling immediate (and a save invalidates outright) while taking the cost
    off the hot path; measured 52 ms per call against a large agent_config."""
    now = time.monotonic()
    hit = _CACHE.get("derived")
    if hit is not None and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    hosts = _integration_hosts()
    _CACHE["derived"] = (now, hosts)
    return hosts


def allowed_hosts() -> set[str]:
    """Every host that may be REACHED: derived + stored + env.

    Each source is computed independently so one failing cannot take the others
    with it — the env fallback in particular has to survive a broken store."""
    return _derived_cached() | set(stored_hosts()) | _env_hosts()


def write_hosts() -> set[str]:
    """Every host that may be WRITTEN TO — the configured integrations only.

    The operator's Settings extras are for READING: a docs site, a spec, a
    changelog. Nothing about adding a host to that box says "and you may post
    our data there", and the two are very different permissions — reading pulls
    bytes in, writing pushes ours out. Keeping them apart means an operator can
    open up a doc site without also creating an exfiltration destination.

    What is on it, stated precisely rather than flatteringly: every host in the
    DERIVED set. That is the credentialed integrations (Jira / Confluence /
    GitLab / mail) AND the service endpoints this install cannot work without —
    the model, the embed and rerank sidecars, MCP servers, the sync admin, the
    observability sink. Those receive data by definition: an inference request
    IS the prompt, and refusing to write to the model would stop the product.

    So the honest boundary is not "credentialed" — it is "an endpoint the
    operator configured for this system to USE, rather than a host they added
    to read". Typing a model base_url in Settings does create a destination
    that receives prompts; that is what choosing a model means, and it is why
    the model endpoint belongs in .env / Settings review rather than in the
    read-only extras box.
    """
    return _integration_hosts()


def describe() -> dict:
    """For the Settings UI: what is allowed and WHERE each entry came from, so
    an operator can see they do not need to re-add their Jira host by hand."""
    derived = sorted(_derived_cached())
    stored = stored_hosts()
    env = sorted(_env_hosts())
    return {"derived": derived, "extra_hosts": stored, "env": env,
            "effective": sorted(set(derived) | set(stored) | set(env)),
            # The UI says this out loud: an added host is READ-ONLY, so nobody
            # adds one expecting the agent to be able to post to it.
            "writable": derived}


__all__ = ["allowed_hosts", "describe", "set_stored_hosts", "stored_hosts",
           "write_hosts"]
