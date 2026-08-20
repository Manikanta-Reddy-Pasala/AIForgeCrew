"""Operator-tunable runtime knobs, persisted + UI-editable.

Two global LLM knobs the operator chooses (NO hardcoded constant wins
over an explicit choice). Defaults are LOCAL-FIRST — sized so the product
runs on a real small local box (a 32K-window quantized/mlx model) out of
the box; an operator with a bigger model raises them via the env vars.

* ``max_output_tokens`` — generation cap sent to the model. Too small
  truncates a doer's file-write tool-call args mid-string; too large
  reserves the whole window for output on a small model. Default 8192
  (fits any window; a local tool-call turn rarely needs more). Raise via
  ``AIFORGE_LLM_MAX_TOKENS``.
* ``context_window``    — assumed input context window (tokens). Feeds
  the router's escalation/threshold sizing AND the chat condense budget,
  so a too-large value makes the agent overflow a small window before the
  auto-condense safety net fires. Default 131072 (128K) — modern local
  models (Qwen3.x / GLM / Gemma) all ship 128K, so use it as the baseline.
  A small-window box overrides down via ``AIFORGE_LOCAL_CTX_WINDOW``.

Resolution order for each knob (first that yields a value wins):
  1. ``runtime_settings.json`` (this store — the UI writes here)
  2. the documented env var (back-compat / headless override)
  3. the built-in default below

Storage: ``$AIFORGE_CONFIG_DIR/runtime_settings.json`` (default
``~/.aiforge``). Kept in its OWN file so it never interferes with the
per-role ``agent_config.json`` load/merge logic.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("aiforge.runtime_settings")

# knob -> (env var consulted when the store has no value, built-in default)
_SPEC: dict[str, tuple[str, int]] = {
    "max_output_tokens": ("AIFORGE_LLM_MAX_TOKENS", 8192),
    "context_window": ("AIFORGE_LOCAL_CTX_WINDOW", 131072),
    # 0/1 flag: force-treat the chat model as vision-capable (for a self-hosted
    # multimodal model the allowlist doesn't recognise). Auto-detection by model
    # id still applies when this is 0.
    "vision_capable": ("AIFORGE_CHAT_VISION_CAPABLE", 0),
    # 0/1 "cave mode": lean, hallucination-safe context — smaller repo map,
    # condense HISTORY early (~40% window) so small local models don't drift +
    # invent edits as context grows. Quality blocks (skills/workflows/mentions/
    # recall) are KEPT. DEFAULT 1 (standard across all local models); an operator
    # on a strong big-window cloud model opts OUT with AIFORGE_CAVE_MODE=0 or the
    # setting. (A stale seeded 0 from the old default is cleared by
    # _migrate_stale_cave_default so this default actually takes effect.)
    "cave_mode": ("AIFORGE_CAVE_MODE", 1),
    # 0/1: summarise the dropped middle with the model (code-aware) on condense,
    # instead of the cheap heuristic breadcrumb. Swappable model: AIFORGE_COMPACT_ROLE.
    "compact_llm": ("AIFORGE_COMPACT_LLM", 0),
    # Dynamic-context injection knobs — each 0/1 DISABLE flag (default 0 = the
    # block is injected every turn). Modelled as disable-flags historically,
    # when a stored 0 was discarded as "unset"; `get()` has honoured an
    # explicitly stored 0 since, which is what lets chat_safety_cap use 0 to
    # mean "no cap".
    "ctx_no_recall": ("AIFORGE_CTX_NO_RECALL", 0),
    "ctx_no_mentions": ("AIFORGE_CTX_NO_MENTIONS", 0),
    "ctx_no_skills": ("AIFORGE_CTX_NO_SKILLS", 0),
    "ctx_no_workflows": ("AIFORGE_CTX_NO_WORKFLOWS", 0),
    "ctx_no_repomap": ("AIFORGE_CTX_NO_REPOMAP", 0),
    "ctx_no_summary": ("AIFORGE_CTX_NO_SUMMARY", 0),
    # Per-turn chat budget guards (see runtime/chat_agent/_context/_limits.py).
    # These are RUNAWAY guards, not task budgets — a turn that is still making
    # progress extends them chat_cap_extensions times before it is stopped.
    # 0 = NO step cap (the turn runs until it is done, stalls, hits the
    # deadline, or the user presses Stop).
    "chat_safety_cap": ("AIFORGE_CHAT_SAFETY_CAP", 2000),
    # Seconds of wall clock for ONE turn; 0 = no deadline.
    "chat_turn_deadline_s": ("AIFORGE_CHAT_TURN_DEADLINE_S", 3600),
    # 0 = never auto-extend (hard stop at the cap/deadline, the old behaviour).
    "chat_cap_extensions": ("AIFORGE_CHAT_CAP_EXTENSIONS", 2),
    # Step cap for runs with NOBODY watching (jobs, analysis fan-out, subtask
    # runners): Stop is gated on a session id, so these cannot be interrupted.
    # Deliberately NOT zeroable — see _limits._unattended_cap.
    "chat_unattended_cap": ("AIFORGE_CHAT_UNATTENDED_CAP", 2000),
    # Operator ceiling on model requests per minute. 0 = no ceiling.
    #
    # Covers everything that goes through the LLM client (chat, the routers and
    # classifiers, jobs, direct callers) and the ADK/team pipeline. It does NOT
    # cover embeddings/rerank, the instructor-backed structured path, or the
    # memory daemon — that runs in its OWN process, and this bucket is
    # per-process, so the ceiling is per API process, not per machine.
    #
    # A throttle, not a guard: one agent turn is routinely 10-40 calls, so a
    # low value queues ordinary work rather than preventing it. It never fails
    # a call — past AIFORGE_LLM_MAX_WAIT_S it warns and lets it through.
    "llm_max_rpm": ("AIFORGE_LLM_MAX_RPM", 5),
}

# Sanity bounds — reject obviously-bad values from the API/UI so a typo
# can't wedge the pipeline (e.g. 0 or a negative cap).
_BOUNDS: dict[str, tuple[int, int]] = {
    "max_output_tokens": (256, 1_000_000),
    "context_window": (1024, 10_000_000),
    "vision_capable": (0, 1),
    "cave_mode": (0, 1),
    "compact_llm": (0, 1),
    "ctx_no_recall": (0, 1),
    "ctx_no_mentions": (0, 1),
    "ctx_no_skills": (0, 1),
    "ctx_no_workflows": (0, 1),
    "ctx_no_repomap": (0, 1),
    "ctx_no_summary": (0, 1),
    # lo=0 is deliberate: 0 means "no cap", the same way it already does for
    # the turn deadline. An operator who wants an unbounded turn should not
    # have to type 1000000 and hope.
    "chat_safety_cap": (0, 1_000_000),
    "chat_turn_deadline_s": (0, 86_400),
    "chat_cap_extensions": (0, 50),
    "chat_unattended_cap": (1, 1_000_000),
    "llm_max_rpm": (0, 100_000),
}


# set_many/unset READ-MODIFY-WRITE a shared file. _fc.write_json is atomic per
# write, not per transaction, so two overlapping writers lose one side — and the
# Reset button makes that a one-click path next to a normal save. This
# serialises writers in THIS process (the UI and the API share it); a second
# process editing the same file concurrently is still last-writer-wins.
_WRITE_LOCK = threading.Lock()


def _path() -> Path:
    root = Path(os.environ.get("AIFORGE_CONFIG_DIR",
                               os.path.expanduser("~/.aiforge")))
    root.mkdir(parents=True, exist_ok=True)
    return root / "runtime_settings.json"


from aiforge_core.config import _filecache as _fc


def _read_store() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = _fc.read_json(p)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("runtime_settings.json unreadable: %s", exc)
        return {}


def get(name: str) -> int:
    """Resolve a knob: stored value → env var → built-in default."""
    if name not in _SPEC:
        raise KeyError(name)
    env_var, default = _SPEC[name]
    stored = _read_store().get(name)
    # An EXPLICITLY stored value wins — including 0, so the UI can override an
    # env-set 0/1 knob back off (a stored 0 used to be discarded → env stuck it
    # on). Reject an out-of-bounds stored value (e.g. a hand-edited/corrupt 0 for
    # max_output_tokens) so it can't wedge the pipeline — fall through to env/default.
    if isinstance(stored, int):
        lo, hi = _BOUNDS.get(name, (None, None))
        if lo is None or (lo <= stored <= hi):
            return stored
    raw = os.environ.get(env_var)
    if raw:
        try:
            # int(float(...)): an env var written as "1800.5" or "7200.0" is a
            # perfectly ordinary thing to find in a unit file. Reading it as
            # "unparseable → default" made the UI report a number the runtime
            # was not using, and "confirming" that field then overwrote the
            # operator's own env value.
            return int(float(raw))
        except ValueError:
            pass
    return default


def explicit(name: str) -> int | None:
    """The EXPLICITLY-set value of a knob — a stored (UI) value or the env var
    — or ``None`` when only the built-in default would apply. Lets callers layer
    auto-detection BETWEEN an operator's explicit choice and the static default
    (see ``model_registry.effective_context_window``). Unlike :func:`get`, this
    never falls back to the default."""
    if name not in _SPEC:
        raise KeyError(name)
    env_var, _default = _SPEC[name]
    stored = _read_store().get(name)
    if isinstance(stored, int):
        lo, hi = _BOUNDS.get(name, (None, None))
        if lo is None or (lo <= stored <= hi):
            return stored
    raw = os.environ.get(env_var)
    if raw:
        try:
            return int(float(raw))
        except ValueError:
            pass
    return None


def stored(name: str) -> "int | None":
    """The value SAVED in the store (UI), ignoring env and defaults.

    Callers that parse the env var themselves — the chat turn deadline accepts
    fractions, which this integer store cannot hold — need to know whether the
    operator's UI choice should win, without ``explicit()`` collapsing their
    env value to an int on the way past."""
    if name not in _SPEC:
        raise KeyError(name)
    val = _read_store().get(name)
    if isinstance(val, int):
        lo, hi = _BOUNDS.get(name, (None, None))
        if lo is None or (lo <= val <= hi):
            return val
    return None


def all_settings() -> dict[str, int]:
    """Current resolved value of every knob (for the GET endpoint)."""
    return {name: get(name) for name in _SPEC}


def set_many(values: dict[str, Any]) -> dict[str, int]:
    """Persist the given knobs (only recognised, in-bounds keys). Returns
    the full resolved settings afterwards. Raises ValueError on a bad
    value so the API can surface a 400."""
    with _WRITE_LOCK:
        return _set_many_locked(values)


def _set_many_locked(values: dict[str, Any]) -> dict[str, int]:
    store = _read_store()
    for name, val in values.items():
        if name not in _SPEC:
            continue
        try:
            ival = int(val)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer")
        lo, hi = _BOUNDS[name]
        if not (lo <= ival <= hi):
            raise ValueError(f"{name} must be between {lo} and {hi}")
        store[name] = ival
    # Stamp the cave-default migration marker on any write, so a store CREATED
    # after this change (a fresh install saving settings) is never later
    # "migrated" — its cave_mode value, including a deliberate 0, is a real
    # operator choice. Only PRE-EXISTING stores (no marker) get the one-time
    # stale-0 clear in _migrate_stale_cave_default.
    store["_cave_default_v2"] = 1
    _fc.write_json(_path(), store)   # atomic + busts the read cache
    return all_settings()


def unset(names: "list[str] | tuple[str, ...]") -> dict[str, int]:
    """Forget stored values so a knob falls back to its env var / default.

    Without this the store is a one-way door: ``get`` prefers any in-bounds
    stored value, so the documented env override goes permanently dead on a box
    the moment someone touches the UI — and nothing in the UI could bring it
    back. Unknown names are ignored. Returns the resolved settings afterwards.
    """
    with _WRITE_LOCK:
        return _unset_locked(names)


def _unset_locked(names) -> dict[str, int]:
    store = _read_store()
    changed = False
    for name in names or ():
        if name in _SPEC and name in store:
            store.pop(name, None)
            changed = True
    if changed:
        _fc.write_json(_path(), store)
    return all_settings()


def _migrate_stale_cave_default() -> None:
    """One-time: the ``cave_mode`` default flipped 0→1 (cave is now standard
    across all models). A store seeded with the OLD default 0 — or written by an
    early UI save that persisted every knob — would otherwise pin cave OFF
    forever and defeat the new default. Clear a stale ``cave_mode == 0`` ONCE
    (guarded by a persisted marker), so it reverts to the new default. A
    deliberate opt-out an operator sets AFTER this migration is preserved
    (the marker stops the migration re-running). Idempotent + atomic; on a
    fresh install (no store file) there's nothing to migrate. Never raises."""
    try:
        p = _path()
        if not p.exists():
            return
        store = _read_store()
        if store.get("_cave_default_v2"):
            return
        if store.get("cave_mode") == 0:
            store.pop("cave_mode", None)   # revert to the new default (1)
        store["_cave_default_v2"] = 1
        _fc.write_json(p, store)
    except Exception:  # noqa: BLE001 — a migration must never break startup
        pass


_migrate_stale_cave_default()


__all__ = ["get", "explicit", "stored", "all_settings", "set_many", "unset"]
