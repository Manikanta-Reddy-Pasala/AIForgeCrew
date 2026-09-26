"""Rate-limit settings and buckets: the limits per category, hold caps, and
which roles count as background compaction."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    """Token bucket. Refills at ``rate`` tokens per second; cap at ``capacity``."""
    capacity: float
    rate: float
    tokens: float
    last: float

    def take(self, n: float) -> float:
        """Drain ``n`` tokens. Return seconds-to-wait when empty.

        Caller decides whether to sleep or fail.
        """
        now = time.time()
        elapsed = max(0.0, now - self.last)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        deficit = n - self.tokens
        return deficit / max(self.rate, 1e-9)


# Locks keyed by provider name so concurrent doer/feedback/learner
# calls don't trample each other's bucket math.
_LOCKS: dict[str, threading.Lock] = {}
# Guards ``rate_limiter._waiting``.
_WAIT_LOCK = threading.Lock()
_RPM_BUCKETS: dict[str, _Bucket] = {}
_TPM_BUCKETS: dict[str, _Bucket] = {}


def _bucket(store: dict[str, _Bucket], name: str,
            per_minute: float) -> _Bucket:
    """Get or create the named bucket. ``per_minute=0`` means no limit
    (caller skips the take call)."""
    if name not in store:
        cap = per_minute
        store[name] = _Bucket(
            capacity=cap, rate=cap / 60.0,
            tokens=cap, last=time.time(),
        )
    return store[name]


def _resolved_limits(provider: str,
                     declared: dict | None) -> tuple[float, float]:
    """Combine env override + provider-declared limits.

    Returns ``(rpm, tpm)``; either may be 0 to mean unlimited.
    """
    if declared is None:
        return 0.0, 0.0
    name_up = provider.upper()
    rpm = float(
        os.environ.get(f"AIFORGE_{name_up}_RPM")
        or declared.get("rpm", 0)
    )
    tpm = float(
        os.environ.get(f"AIFORGE_{name_up}_TPM")
        or declared.get("tpm", 0)
    )
    return rpm, tpm


# The operator's OWN ceiling, across every provider and every caller: chat,
# the team pipeline, jobs and the structured/memory path share it. The
# per-provider limits above are what a PROVIDER declares it will serve; this is
# what the person running the box is willing to send.
#
# A SLIDING WINDOW (see acquire_global), and on the MONOTONIC clock. Wall time
# would have been a silent kill switch: `_sends` holds absolute stamps, so one
# backwards NTP correction or a laptop resume leaves entries that never age out
# of `now - 60`, every caller takes the overrun path, and the ceiling is off for
# the length of the step while logging that it is working. The token bucket
# this replaced survived that by clamping `elapsed` to >= 0; a window of
# timestamps has no such self-healing, so it must use the clock that cannot go
# backwards. call_meter's own 60s ring made this exact choice for this exact
# reason.
_WINDOW_LOCK = threading.Lock()
# (timestamp, category) per send. The category ("compaction" | "chat") carries
# the per-bucket sub-ceiling; the list length is still the global count.
_sends: "list[tuple[float, str]]" = []
# Set by note_rate_limited: monotonic instant before which nothing may be sent
# to THAT PROVIDER because it said we are over its limit. Keyed, because the
# common setup is a cloud gateway for the doer and a local mlx/LM Studio for
# the learner — and a rejection from the gateway must not stall 60s of memory
# work against a server that declares no rate limit at all. The empty key is
# the catch-all for callers that do not know their provider, and everyone
# checks it.
_ANY = ""
_holds: "dict[str, float]" = {}


def _pkg():
    """The parent module, looked up on each call so a name patched there is the
    one used here."""
    import aiforge_core.llm.rate_limiter as package
    return package


def _setting(name: str, env: str, default: float) -> float:
    """Stored setting -> env -> built-in default, like every other runtime knob.

    Reading the env var alone would have made the Settings field inert: the
    store is what the UI writes, and a knob the UI cannot actually change is
    worse than one it never offered.
    """
    try:
        from aiforge_core.config import runtime_settings as _rs
        return float(_rs.get(name))
    except Exception:  # noqa: BLE001 — never let a settings read block a call
        try:
            raw = os.environ.get(env)
            return float(raw) if raw else default
        except (TypeError, ValueError):
            return default


def _hold_cap() -> float:
    """Longest hold a single server response may impose. `Retry-After: 3600`
    from a misconfigured or shared-tenant gateway would otherwise park every
    caller in the process for its full wait budget, once per call, for an hour
    — from one header. Same knob that caps the caller's own backoff."""
    return max(1.0, _pkg()._setting("llm_rate_limit_cap_s",
                                    "AIFORGE_LLM_RATE_LIMIT_CAP_S", 60.0))


def _compaction_hold_cap() -> float:
    """How long a background compaction/OKF send may WAIT for its slot before it
    is let through. Interactive chat deliberately overruns the ceiling rather
    than stall a classifier a user is waiting on (see :func:`acquire_global`);
    memory folding has no user waiting on it, so it strictly RESPECTS the
    ceiling by queuing for its turn instead. A 5-rpm bucket frees a slot every
    ~12s, so this is only the safety ceiling on a pathological wait. Set to 0 to
    fall back to the interactive overrun."""
    return max(0.0, _pkg()._setting("compaction_rate_limit_cap_s",
                                    "AIFORGE_COMPACTION_RATE_LIMIT_CAP_S", 900.0))


def _now() -> float:
    """The clock the ceiling runs on. Monotonic, never wall time — see above."""
    return time.monotonic()


#: Machine-wide default requests-per-minute ceiling when nothing overrides it.
#: Override via the ``llm_max_rpm`` setting or ``AIFORGE_LLM_MAX_RPM``; set
#: either to 0 to disable the ceiling.
_DEFAULT_GLOBAL_RPM = 30.0


def global_rpm() -> float:
    """Operator-set ceiling on model requests per minute; 0 = no ceiling.

    Resolves stored setting -> env -> built-in default, like every other runtime
    knob. The default is enforced, not merely advisory: with nothing set the
    ceiling is :data:`_DEFAULT_GLOBAL_RPM`, not unlimited.

    NOTE WHICH DEFAULT ANSWERS. ``runtime_settings`` supplies its own when
    nothing is stored, so the constant below is reached only if that read
    RAISES. The two used to disagree — this docstring promised 15 while the
    table returned 20 — and the table won every time, silently, on a gateway
    that allows exactly 20. A test now pins them together.
    """
    try:
        from aiforge_core.config import runtime_settings as _rs
        return max(0.0, float(_rs.get("llm_max_rpm")))
    except Exception:  # noqa: BLE001 — never let a settings read block a call
        raw = os.environ.get("AIFORGE_LLM_MAX_RPM")
        try:
            return max(0.0, float(raw)) if raw else _DEFAULT_GLOBAL_RPM
        except (TypeError, ValueError):
            return _DEFAULT_GLOBAL_RPM


#: Roles whose LLM traffic is memory/compaction, not interactive. They count
#: against the "compaction" category: 5/min while chat has sent in the last
#: minute, and the whole global ceiling (30) when chat has not.
#:
#: ``memory`` is here because distillation and consolidation moved to their own
#: reasoning role: left out, every fold counted against the interactive
#: ceiling and compaction crawled on a completely idle box — the opposite of
#: what the category exists for.
_COMPACTION_ROLES = frozenset({"learner", "memory"})


def _compaction_roles() -> frozenset:
    """:data:`_COMPACTION_ROLES`, plus whatever ``AIFORGE_MEMORY_MODEL_ROLE``
    points at.

    Pointing memory work at another role is a supported override, and it must
    not silently move that traffic onto the interactive ceiling. Read from the
    environment rather than importing md_store: this module sits underneath it.
    """
    extra = (os.environ.get("AIFORGE_MEMORY_MODEL_ROLE") or "").strip()
    return _COMPACTION_ROLES | ({extra} if extra else frozenset())

# 5 while chat is sending. When the last minute has no chat send, compaction
# may use the whole global ceiling (see rate_limiter._category_limit).
_DEFAULT_COMPACTION_RPM = 5.0
_DEFAULT_CHAT_RPM = 30.0


def _category(role: "str | None") -> str:
    """Which sub-ceiling this call counts against: 'compaction' or 'chat'."""
    return "compaction" if role in _compaction_roles() else "chat"


def _cat_rpm(cat: str) -> float:
    """The per-category ceiling in requests/minute; 0 = that category is bounded
    only by the global ceiling. Resolves stored setting -> env -> default, like
    :func:`global_rpm`.
    """
    if cat == "compaction":
        setting, env, default = "compaction_rpm", "AIFORGE_COMPACTION_RPM", \
            _DEFAULT_COMPACTION_RPM
    else:
        setting, env, default = "chat_rpm", "AIFORGE_CHAT_RPM", _DEFAULT_CHAT_RPM
    try:
        from aiforge_core.config import runtime_settings as _rs
        return max(0.0, float(_rs.get(setting)))
    except Exception:  # noqa: BLE001 — never let a settings read block a call
        raw = os.environ.get(env)
        try:
            return max(0.0, float(raw)) if raw else default
        except (TypeError, ValueError):
            return default
