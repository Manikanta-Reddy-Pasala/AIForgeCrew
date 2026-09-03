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
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("aiforge.egress_hosts")

_FILE = "egress.json"


def _path() -> Path:
    from aiforge_core.config.paths import config_dir
    d = Path(str(config_dir()))
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


def _integration_hosts() -> set[str]:
    """Hosts of the configured integrations. Each probe is guarded: a broken or
    half-configured integration must not take the whole allowlist with it —
    that would fail CLOSED on everything at once, including the model."""
    out: set[str] = set()

    def _add(fn) -> None:
        try:
            out.add(_host_of(fn() or ""))
        except Exception as exc:  # noqa: BLE001
            log.debug("egress host probe failed: %s", exc)

    try:
        from aiforge_core.runtime.tools.jira._core import _base as _jira
        _add(_jira)
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime.tools.confluence._config import _base as _conf
        _add(_conf)
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime.tools.gitlab import _base as _gl
        _add(_gl)
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime.tools import email_tool
        conf = email_tool._smtp_conf()
        out.add(_host_of(str(conf.get("host") or "")))
        imap = getattr(email_tool, "_imap_conf", None)
        if imap is not None:
            out.add(_host_of(str((imap() or {}).get("host") or "")))
    except Exception:  # noqa: BLE001
        pass
    for var in ("LANGFUSE_HOST", "AIFORGE_LM_BASE_URL", "AIFORGE_EMBED_BASE_URL",
                "AIFORGE_RERANK_BASE_URL", "AIFORGE_ADMIN_URL"):
        out.add(_host_of(os.environ.get(var, "")))
    # Remote MCP endpoints: "name=url,name=url"
    for entry in (os.environ.get("AIFORGE_MCP_ENDPOINTS") or "").split(","):
        if "=" in entry:
            out.add(_host_of(entry.split("=", 1)[1]))
    # Per-role model endpoints the operator set in the UI.
    try:
        from aiforge_core.config.agent_config._resolve import load_all
        for row in (load_all() or {}).values():
            if isinstance(row, dict):
                out.add(_host_of(str(row.get("base_url") or "")))
    except Exception:  # noqa: BLE001
        pass
    return {h for h in out if h}


def stored_hosts() -> list[str]:
    """Extras added in Settings."""
    p = _path()
    if not p.exists():
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
        if h and h not in clean:
            clean.append(h)
    _atomic.write_text(_path(), json.dumps({"extra_hosts": clean}, indent=2))
    return clean


def _env_hosts() -> set[str]:
    raw = (os.environ.get("AIFORGE_EGRESS_ALLOW_HOSTS") or "").strip()
    return {h for h in (_host_of(x) for x in raw.split(",")) if h}


def allowed_hosts() -> set[str]:
    """Every host that may be REACHED: derived + stored + env."""
    return _integration_hosts() | set(stored_hosts()) | _env_hosts()


def write_hosts() -> set[str]:
    """Every host that may be WRITTEN TO — the configured integrations only.

    The operator's Settings extras are for READING: a docs site, a spec, a
    changelog. Nothing about adding a host to that box says "and you may post
    our data there", and the two are very different permissions — reading pulls
    bytes in, writing pushes ours out. Keeping them apart means an operator can
    open up a doc site without also creating an exfiltration destination.

    A destination that genuinely needs writes is an INTEGRATION: it is
    configured with a base URL and a credential, which is a deliberate act with
    a review attached, and its host is derived from that config.
    """
    return _integration_hosts()


def describe() -> dict:
    """For the Settings UI: what is allowed and WHERE each entry came from, so
    an operator can see they do not need to re-add their Jira host by hand."""
    derived = sorted(_integration_hosts())
    stored = stored_hosts()
    env = sorted(_env_hosts())
    return {"derived": derived, "extra_hosts": stored, "env": env,
            "effective": sorted(set(derived) | set(stored) | set(env)),
            # The UI says this out loud: an added host is READ-ONLY, so nobody
            # adds one expecting the agent to be able to post to it.
            "writable": derived}


__all__ = ["allowed_hosts", "describe", "set_stored_hosts", "stored_hosts",
           "write_hosts"]
