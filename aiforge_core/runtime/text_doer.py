"""TEXT-PROTOCOL Doer fallback for local models.

The native pipeline Doer (``agents.doer`` → an ADK ``LlmAgent`` using
NATIVE function-calling) does nothing on local mlx-lm models: the
mlx_lm 0.31 "zero tool_use" bug means the model never serialises native
tool calls. The chat agent already drives a proven TEXT protocol
(``ACTION:``/``ARGS_JSON:``/``FINAL:``, parsed from model text) that
works on those same local backends. This module reuses
``chat_agent.run_chat_agent`` as an ALTERNATE Doer, wrapped as an ADK
graph node so it drops straight into the pipeline in the native Doer's
place.

Three pieces (structured for testability):

  1. :func:`run_text_doer` — the pure, ADK-free core. Folds the pipeline
     state vars into a seed message, drives the chat ReAct loop to
     completion, and harvests ``doer_outcome`` plus the
     ``tests_ok``/``typecheck_ok``/``lint_ok`` quality signals (the same
     signals the native path sets via an ADK after_tool_callback — see
     :func:`aiforge_core.runtime.quality_gate.make_quality_signal_callback`).
  2. :func:`make_text_doer_node` — the thin ADK adapter: a ``node(...)``
     that reads state, resolves the per-ticket worktree cwd, runs the
     core, and writes the results back into ``ctx.state``.
  3. :func:`should_use_text_protocol` — the opt-in-safe switch:
     ``AIFORGE_DOER_PROTOCOL`` (``text`` / ``native`` / ``auto``), default
     ``auto`` = text ONLY when the Doer endpoint is local.

Everything soft-fails: the text Doer must never crash the pipeline build
or a run. On any error the run degrades to a partial (error outcome), so
the loop / validator handle it gracefully.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

# Doer tool name → quality-signal state key. This MIRRORS
# ``quality_gate._TOOL_SIGNAL_KEYS`` exactly (run_tests→tests_ok,
# typecheck→typecheck_ok, format→lint_ok); we replicate the native
# after_tool_callback's mapping over the text loop's tool events because a
# FunctionNode has no ADK tool callbacks.
_SIGNAL_KEYS = ("tests_ok", "typecheck_ok", "lint_ok")

# State vars the native Doer prompt templates (runtime/prompts/doer.py).
# (state key, human label) — order matters for the seed's readability.
_SEED_VARS: tuple[tuple[str, str], ...] = (
    ("plan_md", "PLAN"),
    ("context_brief_md", "GATHERED CONTEXT (repo map / conventions / research)"),
    ("memory_brief_md", "MEMORY (prior facts / decisions / failures)"),
    ("toolchain_md", "TOOLCHAIN (host-verified commands — use these as-is)"),
    ("user_prefs_md", "USER PREFERENCES"),
    ("rules_md", "REPO RULES (follow them exactly)"),
    ("verifier_verdict", "VERIFIER VERDICT (heed any rejection reasons)"),
    ("feedback_verdict", "FEEDBACK ON YOUR PRIOR ATTEMPT (a loop re-run — fix "
                         "what this rejected; don't repeat it)"),
    ("replan_note", "REPLAN NOTE (set only on a re-plan — go smaller)"),
)
_SEED_KEYS = tuple(k for k, _ in _SEED_VARS)

_SEED_HEADER = (
    "You are the Doer on an autonomous engineering pipeline. Implement the "
    "plan below in THIS workspace: explore the relevant files, make the edits, "
    "run the project's tests, and fix until green. When the work is complete "
    "(or you hit a hard blocker you cannot pass), reply with a line starting "
    "`FINAL:` and a concise summary of what you changed and how to run + test "
    "it. Call tools via the ACTION/ARGS_JSON protocol — do not narrate."
)


def _stringify(val: Any) -> str:
    if isinstance(val, str):
        return val
    try:
        return json.dumps(val, ensure_ascii=False, default=str, indent=2)
    except Exception:  # noqa: BLE001
        return str(val)


# ── seed budgeting (Fix C1) ─────────────────────────────────────────────
# The seed is ONE user message assembled on turn 1, before any history
# exists — so ``chat_agent._compact_convo`` (which only condenses the
# middle of a running history) can NEVER shrink it. An unbounded plan +
# gathered-context + memory brief therefore overflows a small local window
# on the very first call. We cap the seed to a fraction of the resolved
# window and spend that budget by PRIORITY: keep the plan + corrective
# signals full, truncate the bulky gathered context / memory first.

_SEED_LABELS = dict(_SEED_VARS)
# Keep these fullest (planning + corrective signal), in priority order.
_SEED_HIGH: tuple[str, ...] = (
    "plan_md", "replan_note", "feedback_verdict", "verifier_verdict",
    "toolchain_md", "user_prefs_md", "rules_md",
)
# Bulky, truncate-FIRST context — share whatever budget the high tier left.
_SEED_LOW: tuple[str, ...] = ("context_brief_md", "memory_brief_md")
_SEED_TRUNC_MARK = "\n…(truncated to fit context)\n"


def _seed_budget_chars() -> int:
    """Total char budget for the Doer seed = a fraction (default 0.55, env
    ``AIFORGE_SEED_BUDGET_FRAC``) of the resolved context window in chars
    (``context_window`` tokens × 4), floored at 8000. Reserves the rest of
    the window for the agent's actual work (tool output, edits, replies)."""
    try:
        frac = float(os.environ.get("AIFORGE_SEED_BUDGET_FRAC", "0.55"))
    except (TypeError, ValueError):
        frac = 0.55
    try:
        from aiforge_core.config import runtime_settings
        win = int(runtime_settings.get("context_window"))
    except Exception:  # noqa: BLE001
        win = 32768
    return max(int(win * 4 * frac), 8000)


def _present_text(state: dict, key: str) -> str:
    raw = state.get(key)
    if raw is None:
        return ""
    return _stringify(raw).strip()


def _emit_section(parts: list[str], remaining: int, key: str, text: str,
                  cap: int | None = None) -> int:
    """Append ``key``'s section to ``parts`` within ``remaining`` chars (and an
    optional per-section ``cap``), truncating the body with a marker if needed.
    Returns the updated remaining budget. Sections are concatenated (each
    carries its own leading newline) so the running total is exact."""
    if not text:
        return remaining
    label = _SEED_LABELS.get(key, key.replace("_", " ").upper())
    prefix = f"\n--- {label} ---\n"
    overhead = len(prefix)
    limit = remaining if cap is None else min(remaining, cap)
    if limit - overhead <= 0:
        return remaining                       # no room even for the header
    avail = limit - overhead
    if len(text) > avail:
        keep = avail - len(_SEED_TRUNC_MARK)
        if keep <= 0:
            return remaining
        text = text[:keep] + _SEED_TRUNC_MARK
    parts.append(prefix + text)
    return remaining - overhead - len(text)


def _build_seed(state: dict) -> str:
    """Fold the present, non-empty state vars into one BUDGETED seed message.

    Assembled in priority order against a running char budget
    (:func:`_seed_budget_chars`): the plan + corrective signals stay full,
    the bulky gathered-context / memory briefs share whatever budget is
    left (each truncated with a marker, dropped only if nothing remains).
    Soft-fail: on ANY error, fall back to the original un-budgeted
    concatenation so a budgeting slip can never crash the Doer."""
    try:
        budget = _seed_budget_chars()
        parts = [_SEED_HEADER]
        remaining = budget - len(_SEED_HEADER)
        for key in _SEED_HIGH:
            remaining = _emit_section(parts, remaining, key,
                                      _present_text(state, key))
        low = [(k, _present_text(state, k)) for k in _SEED_LOW]
        low = [(k, t) for k, t in low if t]
        n = len(low)
        for i, (key, text) in enumerate(low):
            # Even split of the remaining pool so BOTH bulky briefs survive
            # (truncated) rather than the first eating it all.
            share = remaining // (n - i) if (n - i) else remaining
            remaining = _emit_section(parts, remaining, key, text, cap=share)
        return "".join(parts)
    except Exception:  # noqa: BLE001
        parts = [_SEED_HEADER]
        for key, label in _SEED_VARS:
            text = _present_text(state, key)
            if text:
                parts.append(f"\n--- {label} ---\n{text}")
        return "".join(parts)


def run_text_doer(
    state: dict,
    cwd: str,
    *,
    role: str = "doer",
    max_steps: int | None = None,
    complete_fn: Callable[..., str] | None = None,
) -> dict:
    """Run the Doer as a TEXT-protocol ReAct loop (ADK-free, testable core).

    Builds a seed from the pipeline ``state`` vars, drives
    ``chat_agent.run_chat_agent`` to completion, and harvests the outcome +
    quality signals. Returns ``{"doer_outcome": str, "tests_ok": bool|None,
    "typecheck_ok": bool|None, "lint_ok": bool|None}``.

    Soft-fail: any exception → an error outcome with ``None`` signals; NEVER
    raises (the pipeline must not crash).
    """
    result: dict = {"doer_outcome": "", "tests_ok": None,
                    "typecheck_ok": None, "lint_ok": None}
    try:
        from aiforge_core.runtime import chat_agent

        seed = _build_seed(state)
        # Scope allowlist enforcement (Fix 3): the native Doer had a
        # scope_guard before_tool_callback, which a FunctionNode can't carry —
        # so on this LOCAL text path scope_allowlist_globs was NEVER enforced
        # (only the worktree jail). Thread the ticket's globs into the chat
        # loop so an out-of-scope file write/patch is refused before it lands.
        # Empty/absent globs => no restriction (back-compat).
        scope_raw = state.get("scope_allowlist_globs") or []
        if isinstance(scope_raw, str):
            scope_raw = [p.strip() for p in scope_raw.split(",") if p.strip()]
        scope_globs = [g for g in scope_raw if isinstance(g, str) and g]
        signals: dict[str, bool] = {}
        last_msg = ""
        err_text = ""
        for ev in chat_agent.run_chat_agent(
            [{"role": "user", "content": seed}],
            cwd=cwd, role=role, max_steps=max_steps,
            complete_fn=complete_fn, session_id=None, mode="act",
            scope_globs=scope_globs or None,
        ):
            etype = ev.get("type")
            if etype == "tool":
                # Replicate quality_gate.make_quality_signal_callback: map the
                # run_tests / typecheck / format tool RESULT's ``ok`` bool onto
                # the matching signal key. Only a real bool counts (a missing /
                # errored tool leaves the signal unset, matching native).
                key = _TOOL_SIGNAL_KEYS.get(ev.get("name") or "")
                res = ev.get("result")
                if key and isinstance(res, dict):
                    ok = res.get("ok")
                    if isinstance(ok, bool):
                        signals[key] = ok
            elif etype == "message":
                txt = ev.get("text")
                if txt:
                    last_msg = txt        # last FINAL / message text wins
            elif etype == "error":
                txt = ev.get("text")
                if txt:
                    err_text = txt        # fallback outcome if no message
            elif etype == "done":
                break
        result["doer_outcome"] = (
            last_msg or err_text or "text-doer produced no final output")
        result.update(signals)
    except Exception as exc:  # noqa: BLE001 — never crash the pipeline
        result["doer_outcome"] = f"text-doer error: {exc}"
    return result


# quality_gate is the source of truth for the tool→signal mapping; import it
# lazily-at-module-load so a broken import can't take the whole module down.
try:
    from aiforge_core.runtime.quality_gate import _TOOL_SIGNAL_KEYS
except Exception:  # noqa: BLE001 — fall back to the documented mapping
    _TOOL_SIGNAL_KEYS = {
        "run_tests": "tests_ok",
        "typecheck": "typecheck_ok",
        "format": "lint_ok",
    }


def _resolve_cwd() -> str:
    """The per-ticket worktree the Doer's tools run against — same resolution
    the native Doer tools use (``AIFORGE_WORKSPACE_DIR`` then
    ``AIFORGE_REPO_ROOT``), falling back to the process cwd."""
    return (os.environ.get("AIFORGE_WORKSPACE_DIR")
            or os.environ.get("AIFORGE_REPO_ROOT")
            or os.getcwd())


async def _text_doer_node(ctx):  # type: ignore[no-untyped-def]
    """ADK node body: snapshot the seed vars from ``ctx.state``, run the
    text Doer off the event loop, write the outcome + signals back. Mirrors
    the ctx/state access of ``graph_pipeline._loop_gate``."""
    import asyncio

    state = ctx.state
    snapshot = {k: state.get(k) for k in _SEED_KEYS}
    # Carry the ticket's scope allowlist so run_text_doer can enforce it on
    # the write tools (Fix 3). Not a seed var (never rendered into the prompt).
    snapshot["scope_allowlist_globs"] = state.get("scope_allowlist_globs")
    cwd = _resolve_cwd()
    # Restore the worktree jail: the native path had the C6 scope_guard
    # before_tool_callback, which a FunctionNode can't carry. The runner sets
    # AIFORGE_REPO_ROOT (not AIFORGE_WORKSPACE_DIR), so chat_agent's path jail
    # is otherwise inactive here. Pin AIFORGE_WORKSPACE_DIR = cwd so the text
    # Doer's file tools can't write outside the per-ticket worktree.
    _prev_ws = os.environ.get("AIFORGE_WORKSPACE_DIR")
    if cwd:
        os.environ["AIFORGE_WORKSPACE_DIR"] = cwd
    try:
        out = await asyncio.to_thread(run_text_doer, snapshot, cwd)
    finally:
        if cwd:
            if _prev_ws is None:
                os.environ.pop("AIFORGE_WORKSPACE_DIR", None)
            else:
                os.environ["AIFORGE_WORKSPACE_DIR"] = _prev_ws
    state["doer_outcome"] = out.get("doer_outcome", "")
    # Only set a signal when its tool actually ran (value not None) — matches
    # the native after_tool_callback, which never writes a signal for a tool
    # that didn't fire.
    for key in _SIGNAL_KEYS:
        val = out.get(key)
        if val is not None:
            state[key] = val


def make_text_doer_node():
    """Return an ADK ``node`` (named ``doer``) wrapping :func:`_text_doer_node`
    so it slots into the pipeline graph exactly where the native Doer went."""
    from google.adk.workflow import node
    return node(_text_doer_node, name="doer")


def should_use_text_protocol(role: str = "doer") -> bool:
    """Decide whether the Doer should run the TEXT protocol.

    ``AIFORGE_DOER_PROTOCOL``:
      * ``text``   → always True.
      * ``native`` → always False.
      * ``auto`` / unset / anything else → auto-detect: True when the Doer
        endpoint is LOCAL (base_url contains 127.0.0.1 / localhost), else
        False.

    Soft-fails to False (the safe native default) on any error. Default is
    ``auto`` so a local-endpoint deployment auto-gets the text protocol and a
    cloud one keeps native — no flag to flip, and never a behavior change for
    cloud.
    """
    mode = (os.environ.get("AIFORGE_DOER_PROTOCOL") or "auto").strip().lower()
    if mode == "text":
        return True
    if mode == "native":
        return False
    try:
        from aiforge_core.llm import router
        ep = router.resolve(role)
        base = (getattr(ep, "base_url", "") or "").lower()
        return ("127.0.0.1" in base) or ("localhost" in base)
    except Exception:  # noqa: BLE001 — native is the safe default
        return False


__all__ = ["run_text_doer", "make_text_doer_node", "should_use_text_protocol"]
