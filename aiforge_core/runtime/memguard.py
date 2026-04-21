"""LM Studio RAM guard.

Called at tick-start (after claim_next, before _run_tool_loop). Ensures
the role's model is loaded; evicts non-hot LRU models first if the
weights-total would exceed the budget.

Why Python + deterministic, not LLM:
  - Runs on every tick. LLM overhead (5-15s) would be wasteful.
  - Decisions are rule-based (LRU + protected set + budget ceiling).
  - Must succeed sub-second to not delay the tick.

Env overrides:
  AIFORGE_RAM_BUDGET_GB      default 85   (LLM weights + KV only; OS+sidecars separate)
  AIFORGE_LMS_BIN            default ~/.lmstudio/bin/lms
  AIFORGE_MEMGUARD_DISABLE   if "1" → skip all calls (emergency bypass)
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

from .logging_setup import emit


LMS_BIN = os.environ.get("AIFORGE_LMS_BIN",
                         os.path.expanduser("~/.lmstudio/bin/lms"))
BUDGET_GB = float(os.environ.get("AIFORGE_RAM_BUDGET_GB", "75"))
# Overhead factor on raw weights. MLX on Apple Silicon memory-maps weight
# files; idle models sit mostly in OS page cache (not counted against RSS).
# 1.15 accounts for KV + framework overhead for actively inferring models.
_RAM_OVERHEAD = float(os.environ.get("AIFORGE_RAM_OVERHEAD", "1.15"))
# Post-tick behaviour: after a tick completes on a NON-protected role,
# immediately unload the role's model to free its KV cache.
RELEASE_AFTER_TICK = os.environ.get("AIFORGE_MEMGUARD_RELEASE", "1") == "1"
# Hard RAM ceiling on actual macOS (active + wired) combined pages.
# When breached, memguard will unload non-protected first, then fall back
# to protected if still over. Keeps the system out of swap/compression.
RAM_CEILING_GB = float(os.environ.get("AIFORGE_RAM_CEILING_GB", "85"))
DISABLE = os.environ.get("AIFORGE_MEMGUARD_DISABLE") == "1"

# Models never evicted by memguard. Their tick traffic dominates.
PROTECTED_MODELS = frozenset({
    "qwen3-coder-next",
    "qwen3.6-35b-a3b",
})


@dataclass
class LoadedModel:
    identifier: str
    size_gb: float
    context: int
    ttl_remaining_s: int  # negative if expired


def _lms_ps() -> dict[str, LoadedModel]:
    """Parse `lms ps` output into {identifier: LoadedModel}. Empty on error."""
    try:
        proc = subprocess.run([LMS_BIN, "ps"], capture_output=True,
                              timeout=10, check=False, text=True)
    except Exception:
        return {}
    out: dict[str, LoadedModel] = {}
    # Table format columns: IDENTIFIER, MODEL, STATUS, SIZE, CONTEXT, PARALLEL, DEVICE, TTL
    # TTL example: "8h / 8h"  "30m / 30m"  "56m / 1h"
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith(("IDENTIFIER", "-")):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        # Size column is like "8.07 GB" — two tokens. Find it.
        size_idx = None
        for i, tok in enumerate(parts):
            if tok in ("GB", "MB") and i > 0:
                size_idx = i - 1
                break
        if size_idx is None:
            continue
        try:
            size_val = float(parts[size_idx])
            if parts[size_idx + 1] == "MB":
                size_val /= 1024.0
        except ValueError:
            continue
        identifier = parts[0]
        # Context = next numeric after GB/MB pair
        ctx_idx = size_idx + 2
        try:
            context = int(parts[ctx_idx])
        except (ValueError, IndexError):
            context = 0
        # TTL "30m / 30m" — parse first token
        ttl_rem_s = 0
        for j in range(len(parts) - 1, -1, -1):
            m = re.match(r"^(\d+)([smh])$", parts[j])
            if m:
                n = int(m.group(1))
                unit = m.group(2)
                ttl_rem_s = n * {"s": 1, "m": 60, "h": 3600}[unit]
                break
        out[identifier] = LoadedModel(
            identifier=identifier, size_gb=size_val,
            context=context, ttl_remaining_s=ttl_rem_s,
        )
    return out


def _lms_unload(identifier: str, log) -> bool:
    try:
        proc = subprocess.run([LMS_BIN, "unload", identifier],
                              capture_output=True, timeout=15, check=False)
        ok = proc.returncode == 0
    except Exception as exc:
        emit(log, "memguard.unload_error",
             model=identifier, error=str(exc)[:200])
        return False
    emit(log, "memguard.unload", model=identifier, ok=ok)
    return ok


def _lms_load(identifier: str, ctx: int, ttl_s: int, log) -> bool:
    t0 = time.time()
    try:
        proc = subprocess.run(
            [LMS_BIN, "load", identifier,
             "--context-length", str(ctx),
             "--ttl", str(ttl_s), "--yes"],
            capture_output=True, timeout=300, check=False,
        )
        ok = proc.returncode == 0
    except Exception as exc:
        emit(log, "memguard.load_error",
             model=identifier, error=str(exc)[:200])
        return False
    dur = round(time.time() - t0, 2)
    emit(log, "memguard.load", model=identifier, ctx=ctx,
         ttl_s=ttl_s, ok=ok, dur_s=dur)
    return ok


def _size_estimate_gb(identifier: str) -> float:
    """Best-effort weights-size lookup. Falls back to `lms ls` parse.
    Returns 4.0 GB if unknown (conservative under-estimate to avoid
    over-eager eviction)."""
    # Fast path: our known catalogue (4-bit MLX quant sizes).
    known = {
        "qwen3-coder-next": 45.0,
        "qwen3.6-35b-a3b": 20.0,
        "gemma-4-26b-a4b-it": 16.0,
        "gemma-3-12b-it": 8.0,
        "gemma-4-e4b-it-mlx": 7.0,
        "gemma-4-e4b-it": 5.5,
        "google/gemma-3n-e4b": 6.0,
        "gemma-3-4b-it": 3.5,
        "qwen/qwen3-4b-thinking-2507": 2.5,
        "phi-4-mini-reasoning": 2.5,
        "mistralai/devstral-small-2-2512": 14.0,
    }
    if identifier in known:
        return known[identifier]
    return 4.0


def _effective_gb(loaded: dict[str, LoadedModel]) -> float:
    """Weights × overhead factor. KV cache + framework bloat included."""
    return sum(m.size_gb for m in loaded.values()) * _RAM_OVERHEAD


def _evict_one_lru(loaded: dict[str, LoadedModel], exclude: set[str],
                   log) -> str | None:
    """Evict the single LRU non-protected model. Returns its identifier."""
    candidates = sorted(
        (m for m in loaded.values()
         if m.identifier not in PROTECTED_MODELS
         and m.identifier not in exclude),
        key=lambda m: m.ttl_remaining_s,
    )
    if not candidates:
        return None
    victim = candidates[0]
    if _lms_unload(victim.identifier, log):
        return victim.identifier
    return None


def _host_ram_used_gb() -> float:
    """Return macOS (active + wired) RAM in GB. 0.0 on parse failure."""
    try:
        proc = subprocess.run(["vm_stat"], capture_output=True, timeout=5,
                              check=False, text=True)
    except Exception:
        return 0.0
    active_pages = 0
    wired_pages = 0
    page_size = 16384
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("Mach Virtual Memory Statistics"):
            # header line
            import re
            m = re.search(r"\(page size of (\d+) bytes\)", line)
            if m:
                page_size = int(m.group(1))
            continue
        parts = line.split(":")
        if len(parts) != 2:
            continue
        key = parts[0].lower()
        val = parts[1].strip().rstrip(".")
        if not val.isdigit():
            continue
        n = int(val)
        if "pages active" in key:
            active_pages = n
        elif "pages wired down" in key:
            wired_pages = n
    return round((active_pages + wired_pages) * page_size / (1024 ** 3), 2)


def enforce_ram_ceiling(log, reason: str = "pre-load") -> None:
    """If (active + wired) RAM exceeds RAM_CEILING_GB, evict models until
    under ceiling. Evicts non-protected first; falls back to protected
    models if still over. Meant to run at tick-start before ensure_loaded."""
    if DISABLE:
        return
    used_gb = _host_ram_used_gb()
    if used_gb <= 0 or used_gb <= RAM_CEILING_GB:
        return
    emit(log, "memguard.ceiling_breach",
         used_gb=used_gb, ceiling_gb=RAM_CEILING_GB, reason=reason)
    loaded = _lms_ps()
    # Pass 1: evict non-protected LRU
    candidates = sorted(
        (m for m in loaded.values() if m.identifier not in PROTECTED_MODELS),
        key=lambda m: m.ttl_remaining_s,
    )
    for victim in candidates:
        if _lms_unload(victim.identifier, log):
            emit(log, "memguard.ceiling_evict",
                 model=victim.identifier, protected=False)
        used_gb = _host_ram_used_gb()
        if used_gb <= RAM_CEILING_GB:
            return
    # Pass 2: if still over, evict protected (Planner first — Doer does
    # the actual work and is more expensive to cold-reload).
    loaded = _lms_ps()
    prot_order = ["qwen3.6-35b-a3b", "qwen3-coder-next"]
    for name in prot_order:
        if name in loaded:
            if _lms_unload(name, log):
                emit(log, "memguard.ceiling_evict",
                     model=name, protected=True)
            used_gb = _host_ram_used_gb()
            if used_gb <= RAM_CEILING_GB:
                return
    # Still over — log + give up. System compressor will handle it.
    emit(log, "memguard.ceiling_over",
         used_gb=_host_ram_used_gb(), ceiling_gb=RAM_CEILING_GB)


def release_after_tick(identifier: str, log) -> None:
    """Unload `identifier` after a tick finishes if it's non-protected.
    Frees KV cache + framework overhead immediately. Idle mmap'd weights
    would otherwise sit in the OS page cache — harmless, but KV-cache RAM
    isn't reclaimed until TTL (default 30min).

    Bypassed when AIFORGE_MEMGUARD_RELEASE=0.
    """
    if DISABLE or not RELEASE_AFTER_TICK or not identifier:
        return
    if identifier in PROTECTED_MODELS:
        return
    # Only unload if actually loaded (avoid noise).
    loaded = _lms_ps()
    if identifier not in loaded:
        return
    if _lms_unload(identifier, log):
        emit(log, "memguard.release", model=identifier)


def ensure_loaded(identifier: str, ctx: int, ttl_s: int, log) -> bool:
    """Load `identifier` at `ctx` if not already loaded. Evict LRU
    non-protected models first if budget would be exceeded OR if the
    initial load fails with "insufficient system resources".

    Returns True if the model is loaded at the end of the call.
    Emits memguard.* events for every decision.
    """
    if DISABLE or not identifier:
        return True
    loaded = _lms_ps()
    # Already loaded at ≥ requested ctx — done.
    existing = loaded.get(identifier)
    if existing is not None and existing.context >= ctx:
        return True

    target_gb = _size_estimate_gb(identifier)
    # If we're re-loading at a different ctx, unload current first.
    if existing is not None:
        _lms_unload(identifier, log)
        loaded = _lms_ps()

    # Pre-emptive eviction if budget would be exceeded (using overhead-
    # adjusted effective GB).
    current_eff = _effective_gb(loaded)
    if current_eff + target_gb * _RAM_OVERHEAD > BUDGET_GB:
        emit(log, "memguard.budget",
             budget_gb=BUDGET_GB, current_eff_gb=round(current_eff, 1),
             target_gb=target_gb,
             protected=sorted(PROTECTED_MODELS),
             evictable=[m.identifier for m in loaded.values()
                        if m.identifier not in PROTECTED_MODELS])
        while current_eff + target_gb * _RAM_OVERHEAD > BUDGET_GB:
            victim = _evict_one_lru(loaded, exclude={identifier}, log=log)
            if victim is None:
                break
            loaded = _lms_ps()
            current_eff = _effective_gb(loaded)

    # Try load. If LM Studio's own guardrail still rejects, retry-with-evict
    # up to 3 times — each time evict one more LRU non-protected model.
    for attempt in range(1, 4):
        if _lms_load(identifier, ctx, ttl_s, log):
            return True
        emit(log, "memguard.retry", model=identifier, attempt=attempt)
        loaded = _lms_ps()
        victim = _evict_one_lru(loaded, exclude={identifier}, log=log)
        if victim is None:
            emit(log, "memguard.over_budget",
                 model=identifier,
                 loaded=sorted(loaded.keys()),
                 protected=sorted(PROTECTED_MODELS))
            return False
    return False
