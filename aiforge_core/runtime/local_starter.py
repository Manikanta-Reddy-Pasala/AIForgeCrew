"""SSH-based auto-start for the local mlx-lm endpoint.

When :mod:`local_probe` flags the local server as dead the runner used
to immediately swap to a cloud default. With this module wired in
``maybe_substitute_primary`` first SSHes to the host that runs LM
Studio, fires ``lms server start`` + ``lms load <model>``, sleeps the
configured warmup, and re-probes. If the server is alive after that
the local fast path is preserved for the rest of the run; otherwise
the cloud default still saves the ticket.

We pay the warmup latency (≈30-60s for a 35B 4-bit MLX model) ONCE
per process — a module-level guard records both successes and failures
so we never re-attempt within the same runner invocation. The cache
key is ``(host, model)`` so an operator who flips models between
tickets gets a fresh attempt, not a stale "already tried" miss.

Env knobs (all optional):
  AIFORGE_LMS_AUTOSTART_DISABLE=1    skip auto-start entirely
  AIFORGE_LMS_HOST=user@host          SSH target (default: AIFORGE_CLAUDE_HOST)
  AIFORGE_LMS_BIN=lms                 lms CLI path on the remote host
  AIFORGE_LMS_MODEL=<id>              model id to ``lms load``
  AIFORGE_LMS_TTL=43200               --ttl seconds for ``lms load``
                                      (12h default; LM Studio's own default
                                      is 1h which idle-unloads mid-run)
  AIFORGE_LMS_WARMUP_S=60             post-load sleep before re-probe
  AIFORGE_LMS_SSH_TIMEOUT_S=120       outer SSH timeout
"""
from __future__ import annotations

import logging
import os
import subprocess
import time


log = logging.getLogger("aiforge.local_starter")


# ``{(host, model): (success?, when_attempted)}`` — only one attempt
# per process per (host, model). Cleared by tests via ``reset()``.
_ATTEMPTED: dict[tuple[str, str], tuple[bool, float]] = {}


def reset() -> None:
    """Test-only — clears the per-process attempt cache."""
    _ATTEMPTED.clear()


def _ssh_host() -> str:
    """Resolve the SSH target. Falls back to ``AIFORGE_CLAUDE_HOST`` so
    operators that already have one keychain-bridge SSH alias don't
    have to add a second."""
    return (
        os.environ.get("AIFORGE_LMS_HOST")
        or os.environ.get("AIFORGE_CLAUDE_HOST")
        or ""
    )


def _model_id() -> str:
    """Pick the model name to load. Defaults to qwen3-coder-next per
    the operator's profile preset. Empty means "skip the load step";
    we still try ``lms server start`` in that case."""
    return os.environ.get("AIFORGE_LMS_MODEL", "qwen3-coder-next")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _disabled() -> bool:
    return os.environ.get("AIFORGE_LMS_AUTOSTART_DISABLE", "0") in ("1", "true")


def try_start(api_base: str) -> bool:
    """Try to bring the local LM Studio endpoint back up.

    Returns True iff the post-warmup re-probe succeeds. Returns False
    quickly when:

    * auto-start is disabled by env
    * no SSH host is configured
    * we've already attempted (success or fail) in this process

    On success the caller skips the cloud-default substitution and
    keeps the local primary cfg.
    """
    if _disabled():
        return False
    host = _ssh_host()
    if not host:
        log.info("lms_autostart: no AIFORGE_LMS_HOST configured")
        return False

    model = _model_id()
    cache_key = (host, model)
    cached = _ATTEMPTED.get(cache_key)
    if cached is not None:
        # Already tried in this process — honour the prior outcome.
        return cached[0]

    bin_name = os.environ.get("AIFORGE_LMS_BIN", "lms")
    ttl = _int_env("AIFORGE_LMS_TTL", 43200)
    warmup = _int_env("AIFORGE_LMS_WARMUP_S", 60)
    ssh_timeout = _int_env("AIFORGE_LMS_SSH_TIMEOUT_S", 120)

    # `lms server start` is idempotent — exits 0 if already running.
    # `lms load <model> --ttl <s>` loads into VRAM and sets the idle
    # timer; LM Studio's default 1h TTL trips mid-run on a long ticket.
    if model:
        remote = f"{bin_name} server start && {bin_name} load {model} --ttl {ttl}"
    else:
        remote = f"{bin_name} server start"

    log.info(
        "lms_autostart: ssh %s -> %s (warmup=%ds)",
        host, remote, warmup,
    )
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes",
             "-o", f"ConnectTimeout=10",
             host, remote],
            capture_output=True, text=True, timeout=ssh_timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("lms_autostart: ssh timeout after %ds", ssh_timeout)
        _ATTEMPTED[cache_key] = (False, time.monotonic())
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("lms_autostart: ssh failed: %s", exc)
        _ATTEMPTED[cache_key] = (False, time.monotonic())
        return False

    if proc.returncode != 0:
        log.warning(
            "lms_autostart: remote rc=%d stderr=%r",
            proc.returncode, (proc.stderr or "")[:200],
        )
        _ATTEMPTED[cache_key] = (False, time.monotonic())
        return False

    # Server reports back fast but model load happens async — sleep
    # the configured warmup window before checking liveness.
    log.info("lms_autostart: load issued, sleeping %ds for warmup", warmup)
    time.sleep(warmup)

    # Bust the probe cache so we actually re-hit the endpoint instead
    # of reading the "dead 60s ago" verdict we cached on entry.
    from . import local_probe
    local_probe._CACHE.pop(api_base, None)
    alive = local_probe.is_alive(api_base)
    log.info("lms_autostart: post-warmup probe alive=%s", alive)
    _ATTEMPTED[cache_key] = (alive, time.monotonic())
    return alive


__all__ = ["try_start", "reset"]
