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
  AIFORGE_LMS_TTL=0                   --ttl seconds for ``lms load``.
                                      0 (default) OMITS the flag entirely so
                                      the model stays loaded until an
                                      explicit ``lms unload``. Any positive
                                      value passes through unchanged for
                                      operators who want a finite TTL.
                                      LM Studio's own default if no flag is
                                      passed is "no idle unload" — that's
                                      what we want for production tickets.
  AIFORGE_LMS_CTX=262144              --context-length passed to ``lms load``.
                                      LM Studio JIT-loads at 4K otherwise,
                                      which truncates Doer prompts on any
                                      ticket touching more than ~2 files.
                                      256K is the contractual default; 64K
                                      is the absolute floor (clamped here).
  AIFORGE_LMS_WARMUP_S=60             post-load sleep before re-probe
  AIFORGE_LMS_SSH_TIMEOUT_S=120       outer SSH timeout
  AIFORGE_LMS_PARALLEL=1              LM Studio --parallel value. Default
                                      1 — pipeline only issues one inflight
                                      request per role, so the LM Studio
                                      default of 4 just reserves 4× the
                                      KV cache for nothing and was the root
                                      cause of MLX Metal-buffer crashes
                                      on Mac Studio's 96GB unified memory.
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
    # TTL=0 (default) means "no idle unload" — keep the model loaded
    # until an explicit ``lms unload``. Any positive value passes
    # through unchanged for operators who want a finite TTL.
    ttl = _int_env("AIFORGE_LMS_TTL", 0)
    warmup = _int_env("AIFORGE_LMS_WARMUP_S", 60)
    ssh_timeout = _int_env("AIFORGE_LMS_SSH_TIMEOUT_S", 120)
    # Mac Studio (96 GB unified memory) easily holds Qwen-Coder-Next 80B
    # at 4-bit + a 256K KV cache; 256K became the default after ticket
    # ONE-116 needed >32K to fit the multi-file ticket body + 10 round
    # trips. The hard floor stays at 65536 — anything smaller risks the
    # original 4K-truncation bug, regardless of operator override.
    ctx = max(_int_env("AIFORGE_LMS_CTX", 262144), 65536)

    # `lms server start` is idempotent — exits 0 if already running.
    # `lms load <model> --context-length <n>` pins the ctx; the TTL
    # flag is appended only when ttl>0 because omitting it tells LM
    # Studio "no idle unload", which is what we want for production
    # tickets that may pause between turns.
    # PARALLEL=1 by default. The pipeline only ever issues one inflight
    # request per role, so the LM Studio default PARALLEL=4 just
    # reserves 4× the KV cache for nothing. ONE-117 surfaced this:
    # 131K-ctx KV × 4 lanes pushed Mac Studio's 96GB unified memory
    # past the headroom and triggered MLX Metal command-buffer aborts.
    # Override via AIFORGE_LMS_PARALLEL — bump for genuine concurrent
    # serving (UI demos), keep at 1 for ticket runners.
    parallel = max(_int_env("AIFORGE_LMS_PARALLEL", 1), 1)
    if model:
        load_cmd = (
            f"{bin_name} load {model} "
            f"--context-length {ctx} --parallel {parallel}"
        )
        if ttl > 0:
            load_cmd += f" --ttl {ttl}"
        remote = f"{bin_name} server start && {load_cmd}"
    else:
        remote = f"{bin_name} server start"

    log.info(
        "lms_autostart: ssh %s -> %s (warmup=%ds, ctx=%d, parallel=%d, ttl=%s)",
        host, remote, warmup, ctx, parallel,
        f"{ttl}s" if ttl > 0 else "off (manual unload only)",
    )
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10",
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


# ─── Operator-driven explicit (re)load ────────────────────────────────


# Hard floor shared with try_start: never load below 64K — smaller risks
# the original 4K-truncation bug regardless of operator intent.
_CTX_FLOOR = 65536


def load_model_now(
    model: str,
    context_length: int,
    *,
    ttl: int = 0,
    parallel: int | None = None,
    host: str | None = None,
    ssh_timeout: int = 300,
) -> dict:
    """Explicitly (re)load ``model`` on the LM Studio host at a chosen
    context length and report the SSH outcome.

    Unlike :func:`try_start` this is an operator action (the UI "set
    context window" control): no per-process cache, no warmup probe —
    it unloads any running copy so the new ctx takes effect, issues the
    load, and returns ``{"ok": bool, ...}``. Blocking until ``lms load``
    returns or ``ssh_timeout`` elapses.
    """
    host = host or _ssh_host()
    if not host:
        return {"ok": False, "error": "no AIFORGE_LMS_HOST configured"}
    if not model:
        return {"ok": False, "error": "model required"}

    bin_name = os.environ.get("AIFORGE_LMS_BIN", "lms")
    ctx = max(int(context_length), _CTX_FLOOR)
    par = max(parallel if parallel is not None
              else _int_env("AIFORGE_LMS_PARALLEL", 1), 1)
    load_cmd = (
        f"{bin_name} load {model} "
        f"--context-length {ctx} --parallel {par} -y"
    )
    if ttl and ttl > 0:
        load_cmd += f" --ttl {ttl}"
    # Unload the existing instance first (ignore failure if not loaded)
    # so the new context length actually takes effect on reload.
    remote = (
        f"{bin_name} server start && "
        f"{bin_name} unload {model} 2>/dev/null; {load_cmd}"
    )

    log.info("lms_load_now: ssh %s -> %s (timeout=%ds)",
             host, remote, ssh_timeout)
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             host, remote],
            capture_output=True, text=True, timeout=ssh_timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ssh timeout after {ssh_timeout}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"ssh failed: {exc}"}

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "").strip()[:300],
            "model": model, "context_length": ctx,
        }
    return {"ok": True, "model": model, "context_length": ctx,
            "parallel": par, "ttl": ttl}


# ─── Mid-pipeline crash recovery ───────────────────────────────────────


_CRASH_MARKERS: tuple[str, ...] = (
    "model has crashed",
    "no models loaded",
    "exit code: null",
    "model crashed",
    "lms load",
)


def looks_like_lm_crash(err: str) -> bool:
    """Sniff a LiteLLM/OpenAI error string for the LM-Studio MLX
    crash/unload signatures we want to auto-recover from.

    Conservative — only matches strings LM Studio is known to return
    when the MLX engine kills the model mid-session. Generic HTTP
    5xx, rate limits, and connection refused go to the cloud chain
    via normal escalation, not here.
    """
    if not err:
        return False
    low = err.lower()
    return any(m in low for m in _CRASH_MARKERS)


def try_recover(api_base: str) -> bool:
    """Force a re-attempt of :func:`try_start` even if already tried
    once in this process. Used by EscalatingLlm when LM Studio
    crashes mid-pipeline — the per-process cache would otherwise lock
    us out of recovery for the rest of the run.
    """
    log.warning("lms_autostart: forced recovery (cache cleared)")
    reset()
    return try_start(api_base)


__all__ = ["try_start", "try_recover", "looks_like_lm_crash", "reset"]
