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
    "run the project's tests, and fix until green. Call tools via the "
    "ACTION/ARGS_JSON protocol — do not narrate.\n"
    "\n"
    "Work smart (you are a smaller local model — do not waste turns):\n"
    "- CONTEXT-FIRST: the PLAN / GATHERED CONTEXT / MEMORY / TOOLCHAIN / REPO "
    "RULES blocks below were assembled FOR this ticket. Read them BEFORE any "
    "grep/list_dir; don't re-discover files, symbols, or build/test commands "
    "they already name. Use memory_lookup + lsp to jump to code, not blind grep.\n"
    "- VERIFY AFTER EVERY EDIT: after changing a file, run typecheck, then "
    "run_tests on the relevant slice, then format; read failures and fix until "
    "GREEN before the next edit. Never call the work done on unverified code.\n"
    "- MINIMAL DIFF: touch only the files the ticket needs; smallest correct "
    "change; match existing conventions; no debug prints or leftover scaffolding.\n"
    "- NO CIRCLING: never re-read a file you've read or repeat a failing call "
    "unchanged; if truly blocked, say so specifically.\n"
    "- HOST-TOOLCHAIN vs CODE: if a build/test fails because the HOST lacks a "
    "tool or has the wrong VERSION (e.g. 'release version N not supported', "
    "'invalid target release', 'Unsupported class file major version', "
    "'command not found: mvn/gradle/kotlinc/java'), that is an OPERATOR install "
    "task — NOT a code fix. Do not downgrade the target, stub the build, or "
    "fake green. Reply `FINAL:` with `OPERATOR: install <tool+version from the "
    "error>` and quote the error line. Read the real error; never guess.\n"
    "\n"
    "When the work is complete (typecheck + tests green) or you hit a hard "
    "blocker you cannot pass, reply with a line starting `FINAL:` and a concise "
    "summary of what you changed, the evidence it works (commands + results), "
    "and how to run + test it."
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


def _seed_budget_chars(role: str = "doer") -> int:
    """Total char budget for the Doer seed (convo[1]).

    CO-BUDGETED with the system prompt (convo[0]) + the reserved reply so the
    two un-condensable turn-1 messages plus output never overflow the window
    (Fix A1). From the window (``context_window`` tokens × 4 chars) we first
    subtract the reservations that AREN'T available to the seed — the model's
    reply (``max_output_tokens`` × 4) and the system-prompt reservation
    (``AIFORGE_SYS_PROMPT_FRAC`` of the window) — then take
    ``AIFORGE_SEED_BUDGET_FRAC`` (default 0.35) of what's left, floored at 8000.

    With SEED_FRAC + SYS_PROMPT_FRAC + out/window ≤ 1.0 this leaves headroom
    for the running conversation. Uses the SAME per-role resolved window as the
    other budgets (A3). Scales with the window at both 32K and 256K."""
    try:
        seed_frac = float(os.environ.get("AIFORGE_SEED_BUDGET_FRAC", "0.35"))
    except (TypeError, ValueError):
        seed_frac = 0.35
    try:
        sys_frac = float(os.environ.get("AIFORGE_SYS_PROMPT_FRAC", "0.35"))
    except (TypeError, ValueError):
        sys_frac = 0.35
    try:
        from aiforge_core.config import model_registry
        win = int(model_registry.effective_context_window(role))
    except Exception:  # noqa: BLE001
        win = 32768
    try:
        from aiforge_core.config import runtime_settings
        out_tok_chars = int(runtime_settings.get("max_output_tokens")) * 4
    except Exception:  # noqa: BLE001
        out_tok_chars = 8192 * 4
    win_chars = win * 4
    # C1: at a small window (≤16K) the raw max_output reservation can equal the
    # WHOLE window and the 8000 floors then push seed+sys+out past window×4.
    # Cap the output reservation at a fraction of the window so it never eats
    # >40% of a small box, and SCALE the floor down when little is left.
    out_chars = _out_reserve_chars(win_chars, out_tok_chars)
    sys_reserve = int(win_chars * sys_frac)
    usable = win_chars - out_chars - sys_reserve
    floor = min(8000, max(0, usable) // 3)
    return max(int(usable * seed_frac), floor)


def _out_reserve_frac() -> float:
    """Fraction of the window the model's reply may reserve (default 0.4,
    env ``AIFORGE_OUT_RESERVE_FRAC``). Caps the output reservation so on a
    small window it can't swallow the whole context (C1). Clamped to (0,1]."""
    try:
        v = float(os.environ.get("AIFORGE_OUT_RESERVE_FRAC", "0.4"))
    except (TypeError, ValueError):
        v = 0.4
    return min(1.0, max(0.01, v))


def _out_reserve_chars(win_chars: int, out_tok_chars: int) -> int:
    """Output-reservation chars = min(max_output_tokens×4, window×frac) — the
    reply never eats more than ``_out_reserve_frac`` of the window (C1)."""
    return min(out_tok_chars, int(win_chars * _out_reserve_frac()))


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


# ENFORCE codegraph tool use (not context push). When a CodeGraph index exists
# for the repo, the Doer MUST call codegraph before editing existing symbols.
# The system-prompt directive alone did NOT move the local model (measured:
# arm A had it, made 0 codegraph calls, defaulted to grep); an imperative in
# the SEED does (measured: the force-run called explore/callers/impact within
# 10s and returned all 5 exact call sites). Injected only when available() so a
# repo with no index never gets a broken instruction.
_CODEGRAPH_MANDATE = (
    "MANDATORY — CodeGraph is indexed for THIS repo. It is the authoritative "
    "source for code relations; grep is NOT allowed for finding callers.\n"
    "- BEFORE editing ANY existing function/class/method, your FIRST actions "
    "MUST be: codegraph_callers(symbol) to get every call site (file:line + the "
    "enclosing function) AND codegraph_impact(symbol) for the blast radius. "
    "Update every site it reports.\n"
    "- To locate a definition use codegraph_query(query); to orient in an "
    "unfamiliar area use codegraph_explore(query) — before any grep/list_dir.\n"
    "- Do NOT grep or cat to discover who calls a symbol — call codegraph. "
    "Skipping this is a defect: you will miss call sites.\n\n"
)


def _codegraph_mandate() -> str:
    """The enforce-codegraph preamble, or "" when no index is queryable."""
    try:
        from aiforge_core.runtime.tools import codegraph as _cg
        return _CODEGRAPH_MANDATE if _cg.available() else ""
    except Exception:  # noqa: BLE001 — never break seed assembly
        return ""


def _build_seed(state: dict) -> str:
    """Fold the present, non-empty state vars into one BUDGETED seed message.

    Assembled in priority order against a running char budget
    (:func:`_seed_budget_chars`): the plan + corrective signals stay full,
    the bulky gathered-context / memory briefs share whatever budget is
    left (each truncated with a marker, dropped only if nothing remains).
    When a CodeGraph index exists, a MANDATORY codegraph-first preamble is
    prepended (see :data:`_CODEGRAPH_MANDATE`).
    Soft-fail: on ANY error, fall back to the original un-budgeted
    concatenation so a budgeting slip can never crash the Doer."""
    mandate = _codegraph_mandate()
    try:
        budget = _seed_budget_chars()
        parts = [mandate, _SEED_HEADER] if mandate else [_SEED_HEADER]
        remaining = budget - len(_SEED_HEADER) - len(mandate)
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
        parts = [mandate, _SEED_HEADER] if mandate else [_SEED_HEADER]
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

        def _one_pass(seed_msg: str) -> int:
            """Drive one full chat ReAct loop; update last_msg/err_text/signals
            in the enclosing scope; return the number of EDIT-tool calls it
            made (file_write/file_patch/editor/…)."""
            nonlocal last_msg, err_text
            edits = 0
            for ev in chat_agent.run_chat_agent(
                [{"role": "user", "content": seed_msg}],
                cwd=cwd, role=role, max_steps=max_steps,
                complete_fn=complete_fn, session_id=None, mode="act",
                scope_globs=scope_globs or None, strict_finish=True,
            ):
                etype = ev.get("type")
                if etype == "tool":
                    name = ev.get("name") or ""
                    if name in _EDIT_TOOLS:
                        res = ev.get("result")
                        # count only edits that actually landed (ok is not False)
                        if not (isinstance(res, dict) and res.get("ok") is False):
                            edits += 1
                    key = _TOOL_SIGNAL_KEYS.get(name)
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
            return edits

        total_edits = _one_pass(seed)
        # No-edit guard: a local model routinely HALLUCINATES that the change
        # "already exists", runs only a compile, and declares success WITHOUT
        # writing a single file (trace: ACTION run_command mvnw compile, 0
        # file_write) — the base repo compiles green, so it reads as done. Force
        # a corrective pass that DEMANDS a real edit. Bounded; opt-out via
        # AIFORGE_DOER_MIN_EDIT_RETRIES=0. Only fires when the last pass finished
        # cleanly (not a stop/deadline banner) with zero edits.
        try:
            _retries = int(os.environ.get("AIFORGE_DOER_MIN_EDIT_RETRIES", "1"))
        except (TypeError, ValueError):
            _retries = 1
        _attempt = 0
        while (total_edits == 0 and _attempt < _retries
               and not _is_stopped_outcome(last_msg or err_text or "")):
            _attempt += 1
            more = _one_pass(seed + _NO_EDIT_CORRECTION)
            total_edits += more
        result["edit_count"] = total_edits
        outcome = (last_msg or err_text or "text-doer produced no final output")
        result["doer_outcome"] = outcome
        result.update(signals)
        # Fix 3a: chat_agent emits a plain "(stopped: ..." banner when it hits
        # the runaway safety cap / turn deadline WITHOUT finishing. Harvesting
        # that as the outcome (as we do) must not read as a clean pass — flag
        # the run incomplete so the quality gate downgrades a model ``pass``.
        # Mirrors parallel_subtasks' ``.startswith("(stopped:")`` detection.
        stopped = _is_stopped_outcome(outcome)
        # Zero edits after the corrective retry = the Doer never implemented
        # anything (hallucinated "already done"). That is NOT a clean pass —
        # flag incomplete so the quality gate / feedback downgrade the model's
        # self-reported success (belt-and-suspenders with the runner's
        # empty-diff → blocked demotion).
        no_edits = total_edits == 0
        result["stopped"] = stopped
        result["incomplete"] = stopped or no_edits
        if no_edits and not stopped:
            result["doer_outcome"] = (
                "INCOMPLETE: the Doer made ZERO file edits — no change was "
                "implemented (likely assumed the feature already existed). "
                + outcome[:400])
    except Exception as exc:  # noqa: BLE001 — never crash the pipeline
        result["doer_outcome"] = f"text-doer error: {exc}"
    return result


def _is_stopped_outcome(text: str) -> bool:
    """True when the outcome is chat_agent's runaway/deadline stop banner —
    an INCOMPLETE run, not a real FINAL. Matches a leading ``(stopped:`` (as
    parallel_subtasks does) and, defensively, the banner anywhere in the text."""
    t = (text or "").strip()
    return t.startswith("(stopped:") or "(stopped:" in t


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

# Tools that actually MUTATE files — used by the no-edit guard to tell a real
# implementation pass from a hallucinated "already done" one (which only reads
# + compiles). Canonical names + the aliases chat_agent may surface.
# NOTE: run_command/bash/run_shell are shells, NOT edits — deliberately absent.
_EDIT_TOOLS = frozenset({
    "file_write", "file_patch", "multi_edit", "str_replace", "editor",
    "rename_symbol", "write", "patch", "edit",
})

# Appended to the seed on a corrective retry when the Doer finished with zero
# edits. Confronts the specific failure: assuming the change already exists.
_NO_EDIT_CORRECTION = (
    "\n\n=== CORRECTION (you made ZERO file edits) ===\n"
    "You finished WITHOUT calling file_write or file_patch even once. Running a "
    "compile or reading files is NOT implementing the change. Do NOT assume the "
    "feature already exists — it does NOT. Re-read the exact target files, then "
    "you MUST call file_patch (or file_write) to make the required change, and "
    "verify the diff is non-empty BEFORE you compile. Do not reply FINAL until "
    "you have actually edited the file(s)."
)


def _resolve_cwd() -> str:
    """The per-ticket worktree the Doer's tools run against — same resolution
    the native Doer tools use (``AIFORGE_WORKSPACE_DIR`` then
    ``AIFORGE_REPO_ROOT``), falling back to the process cwd."""
    from aiforge_core.runtime import request_context
    return (request_context.get_workspace_dir()
            or request_context.get_repo_root()
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
    # Request-scoped workspace jail. The contextvar isolates concurrent ticket
    # runs on different worktrees (env is process-global → clobbers). It's set
    # BEFORE asyncio.to_thread, which copies the current context into the worker
    # thread, so run_text_doer's file tools observe the right jail. The env set
    # is kept for the subprocess path + any non-context-propagating reader.
    from aiforge_core.runtime import request_context
    _prev_ws = os.environ.get("AIFORGE_WORKSPACE_DIR")
    ws_token = None
    if cwd:
        os.environ["AIFORGE_WORKSPACE_DIR"] = cwd
        ws_token = request_context.set_workspace_dir(cwd)
    try:
        out = await asyncio.to_thread(run_text_doer, snapshot, cwd)
    finally:
        if ws_token is not None:
            request_context.reset_workspace_dir(ws_token)
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
    # Fix 3a: propagate the incomplete-stop flag so the quality gate
    # (feedback.make_quality_gate_after_callback → quality_gate.evaluate)
    # downgrades a model ``pass`` to ``fail``. A capped/incomplete text-Doer
    # run must NOT be eligible to ship as pass.
    if out.get("stopped") or out.get("incomplete"):
        state["doer_incomplete"] = True


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
