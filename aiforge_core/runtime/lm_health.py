"""LM Studio liveness probe + opportunistic SSH-tunnel restart.

The Doer's primary provider is qwen-coder-next over LM Studio at
``http://127.0.0.1:1234``. The 1234 port is in turn an SSH tunnel
to the Mac Studio (lm-tunnel.service on the NUC). When the tunnel
or LM Studio itself is dead, every ticket falls back to cloud
escalation — slow + costly. This module probes /v1/models cheaply
and tries to bring the tunnel back before the runner claims a
ticket.

Returns ``{ok, models, restarted}``. Caller decides: when ``ok`` is
False, can opt to flip the run's Doer provider to a cloud profile
for the duration via ``set_force_provider``.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from aiforge_core.net.ssl import context_for as _ssl_context_for

log = logging.getLogger("aiforge.lm_health")


def _probe(url: str, timeout: float = 3.0) -> bool:
    try:
        models_url = url + "/v1/models"
        from aiforge_core.llm.user_agent import user_agent as _ua
        req = urllib.request.Request(models_url,
                                      headers={"User-Agent": _ua()})
        with urllib.request.urlopen(
            req, timeout=timeout, context=_ssl_context_for(models_url)
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _restart_tunnel(unit: str) -> bool:
    """``systemctl --user restart`` the LM Studio SSH-tunnel unit. Best
    effort — returns True on rc=0."""
    if shutil.which("systemctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "restart", unit],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def check_lm_health(*, restart_on_fail: bool = True) -> dict:
    """Return ``{ok, doer_ok, planner_ok, restarted}``."""
    base = os.environ.get("AIFORGE_LM_BASE_URL", "http://127.0.0.1:1234/v1")
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    doer_ok = _probe(base)
    planner_ok = _probe(
        os.environ.get("AIFORGE_PLANNER_LM_URL", "http://127.0.0.1:1235"),
    )
    restarted: list[str] = []
    if not doer_ok and restart_on_fail:
        if _restart_tunnel(os.environ.get(
                "AIFORGE_LM_TUNNEL_UNIT", "lm-tunnel")):
            restarted.append("doer")
            doer_ok = _probe(base, timeout=5.0)
    if not planner_ok and restart_on_fail:
        if _restart_tunnel(os.environ.get(
                "AIFORGE_LM_PLANNER_TUNNEL_UNIT", "lm-tunnel-planner")):
            restarted.append("planner")
            planner_ok = _probe(
                os.environ.get(
                    "AIFORGE_PLANNER_LM_URL", "http://127.0.0.1:1235",
                ),
                timeout=5.0,
            )
    return {
        "ok": doer_ok,
        "doer_ok": doer_ok,
        "planner_ok": planner_ok,
        "restarted": restarted,
    }


__all__ = ["check_lm_health"]
