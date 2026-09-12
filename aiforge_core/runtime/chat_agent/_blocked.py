"""What the model is told when a tool call is BLOCKED — so it changes approach
instead of retrying, or routing the same request around the block.

Most of the outside world is unreachable from an AIForge box on purpose: the
egress allowlist refuses the internet, the offline estate has only the internal
package index, policy denies some actions. A local model that hits one of those
walls tends to try the same thing again, then again with curl, then with a
notebook cell — burning steps on a request that is refused the same way every
time. Each kind of block gets a `next_step` that says plainly: this is policy,
it will not work, here is what IS available — and after repeated blocks in one
turn, a firmer instruction to stop reaching outside altogether.
"""
from __future__ import annotations

import json
import re

_NETWORK_FAIL_RE = re.compile(
    r"could not resolve host|temporary failure in name resolution|"
    r"name or service not known|nodename nor servname|network is unreachable|"
    r"no route to host|failed to establish a new connection|getaddrinfo|"
    r"\benotfound\b|\beai_again\b|connection timed out|connect timeout|"
    r"proxyerror|407 proxy|max retries exceeded with url",
    re.IGNORECASE)
_OUTSIDE_TOOLS = frozenset({"run_command", "run_shell", "bash", "shell",
                            "execute_ipython_cell", "web_fetch", "web_crawl",
                            "browse", "serve", "watch_until"})

_AVAILABLE = ("what is already here: the repo and files on disk, installed "
              "packages and their docs, pip/uv/npm installs (they go through "
              "the internal package index, which IS reachable), memory, and "
              "your own knowledge")

GUIDANCE = {
    "egress": (
        "BLOCKED BY POLICY: this host is outside the egress allowlist — not a "
        "glitch, and it will be refused the same way every time. Do NOT retry "
        "it, try another URL on it, or reach it another way (curl, wget, "
        "requests, a notebook cell, a browser). Change approach and use "
        + _AVAILABLE + ". If the task truly needs that outside content, finish "
        "everything else, then ask the user to paste it or allow the host in "
        "Settings → Egress."),
    "network": (
        "The outside network is not reachable from here (most outside hosts are "
        "blocked in this environment). Do NOT retry the same download or "
        "connection. Change approach and use " + _AVAILABLE + ". If something "
        "outside is truly required, say exactly what and ask the user."),
    "policy": (
        "BLOCKED BY POLICY: the same action will be refused again. Do NOT retry "
        "it or look for a workaround that does the same thing another way. Pick "
        "a different approach to the goal, or ask the user."),
}

_STOP_AFTER = 2


def classify(name: str, result) -> "str | None":
    """'egress' | 'policy' | 'network' for a blocked/unreachable call, else None."""
    if not isinstance(result, dict) or result.get("ok") is True:
        return None
    err = str(result.get("error") or "")
    if err in ("host_not_allowed", "egress_denied") or result.get("egress_denied"):
        return "egress"
    if result.get("blocked") or "denied by policy" in err:
        return "policy"
    if name in _OUTSIDE_TOOLS:
        try:
            text = json.dumps(result)[:6000]
        except (TypeError, ValueError):
            text = str(result)[:6000]
        if _NETWORK_FAIL_RE.search(text):
            return "network"
    return None


def for_model(st, name: str, result):
    """``result`` as the MODEL should see it: a blocked call gains a
    ``next_step``; the Nth block in one turn adds a firmer stop. The UI keeps
    the raw result. Counts blocks on ``st.blocked_hits``."""
    kind = classify(name, result)
    if kind is None:
        return result
    hits = int(getattr(st, "blocked_hits", 0) or 0) + 1
    try:
        st.blocked_hits = hits
    except AttributeError:
        pass
    step = GUIDANCE[kind]
    if hits >= _STOP_AFTER:
        step = (f"This is the {hits}th blocked outside attempt in this turn — "
                "STOP trying to reach outside. " + step)
    return {**result, "next_step": step}


__all__ = ["classify", "for_model", "GUIDANCE"]
