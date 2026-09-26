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
  3. :func:`should_use_text_protocol` — the opt-in switch:
     ``AIFORGE_DOER_PROTOCOL`` (``text`` / ``native`` / ``auto``). Native is
     now the DEFAULT everywhere; only ``text`` selects this fallback (for a
     genuinely tool-incapable local runtime like mlx-lm).

Everything soft-fails: the text Doer must never crash the pipeline build
or a run. On any error the run degrades to a partial (error outcome), so
the loop / validator handle it gracefully.
"""
from __future__ import annotations

import os
from collections.abc import Callable

from aiforge_core.runtime.tools.mutating import FILE_WRITE_TOOLS

from .text_doer_seed import (  # noqa: F401  # re-exported
    _CODEGRAPH_MANDATE,
    _SEED_HEADER,
    _SEED_HIGH,
    _SEED_LABELS,
    _SEED_LOW,
    _SEED_TRUNC_MARK,
    _SEED_VARS,
    _budgeted_seed,
    _build_seed,
    _codegraph_mandate,
    _emit_section,
    _out_reserve_chars,
    _out_reserve_frac,
    _present_text,
    _seed_budget_chars,
    _stringify,
    _unbudgeted_seed,
)

# Doer tool name → quality-signal state key. This MIRRORS
# ``quality_gate._TOOL_SIGNAL_KEYS`` exactly (run_tests→tests_ok,
# typecheck→typecheck_ok, format→lint_ok); we replicate the native
# after_tool_callback's mapping over the text loop's tool events because a
# FunctionNode has no ADK tool callbacks.
_SIGNAL_KEYS = ("tests_ok", "typecheck_ok", "lint_ok")
_SEED_KEYS = tuple(k for k, _ in _SEED_VARS)


class _PassOutcome:
    """What one ReAct pass produced: the texts, the quality signals, the edits."""

    __slots__ = ("last_msg", "err_text", "signals", "edits", "failure")

    def __init__(self) -> None:
        self.last_msg = ""
        self.err_text = ""
        self.signals: dict[str, bool] = {}
        self.edits = 0
        self.failure: list | None = None     # last test run's failure; [] = green

    def text(self) -> str:
        return self.last_msg or self.err_text or ""


def _scope_globs(state: dict) -> list[str]:
    """Scope allowlist enforcement (Fix 3): the native Doer had a scope_guard
    before_tool_callback, which a FunctionNode can't carry — so on this LOCAL
    text path scope_allowlist_globs was NEVER enforced (only the worktree
    jail). Threading the ticket's globs into the chat loop refuses an
    out-of-scope file write/patch before it lands. Empty/absent globs => no
    restriction (back-compat)."""
    raw = state.get("scope_allowlist_globs") or []
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",") if p.strip()]
    return [g for g in raw if isinstance(g, str) and g]


def _absorb_tool_event(ev: dict, out: _PassOutcome) -> None:
    name = ev.get("name") or ""
    res = ev.get("result")
    # count only edits that actually landed (ok is not False)
    if name in _EDIT_TOOLS and not (isinstance(res, dict)
                                    and res.get("ok") is False):
        out.edits += 1
    key = _TOOL_SIGNAL_KEYS.get(name)
    if key and isinstance(res, dict) and isinstance(res.get("ok"), bool):
        out.signals[key] = res["ok"]
    if isinstance(res, dict) and (key == "tests_ok" or _is_test_run(name, ev.get("args"))):
        from aiforge_core.runtime.quality_gate import test_failure
        out.failure = test_failure(res)


def _is_test_run(name, args) -> bool:
    """A shell command that runs the tests counts like run_tests."""
    try:
        from aiforge_core.runtime.chat_agent._turn._outcomes import _is_test_run as _t
        return _t(name, args if isinstance(args, dict) else {})
    except Exception:  # noqa: BLE001
        return False


def _one_pass(seed_msg: str, *, cwd: str, role: str, max_steps, complete_fn,
              scope_globs: list[str], out: _PassOutcome) -> None:
    """Drive one full chat ReAct loop, folding its events into ``out``."""
    from aiforge_core.runtime import chat_agent
    for ev in chat_agent.run_chat_agent(
        [{"role": "user", "content": seed_msg}],
        cwd=cwd, role=role, max_steps=max_steps,
        complete_fn=complete_fn, session_id=None, mode="act",
        scope_globs=scope_globs or None, strict_finish=True,
    ):
        etype = ev.get("type")
        if etype == "tool":
            _absorb_tool_event(ev, out)
        elif etype == "message" and ev.get("text"):
            out.last_msg = ev["text"]        # last FINAL / message text wins
        elif etype == "error" and ev.get("text"):
            out.err_text = ev["text"]        # fallback outcome if no message
        elif etype == "done":
            return


def _min_edit_retries() -> int:
    try:
        return int(os.environ.get("AIFORGE_DOER_MIN_EDIT_RETRIES", "1"))
    except (TypeError, ValueError):
        return 1


def _no_edit_verdict(outcome: str, total_edits: int) -> dict:
    """The stopped/incomplete flags and, when nothing was written, a corrected
    outcome line.

    chat_agent emits a plain "(stopped: ..." banner when it hits the runaway
    safety cap / turn deadline WITHOUT finishing (Fix 3a). Harvesting that as
    the outcome must not read as a clean pass. And zero edits after the
    corrective retry means the Doer never implemented anything (it hallucinated
    "already done") — also not a clean pass, so the quality gate / feedback
    downgrade the model's self-reported success (belt-and-braces with the
    runner's empty-diff → blocked demotion).
    """
    stopped = _is_stopped_outcome(outcome)
    no_edits = total_edits == 0
    verdict = {"stopped": stopped, "incomplete": stopped or no_edits}
    if no_edits and not stopped:
        verdict["doer_outcome"] = (
            "INCOMPLETE: the Doer made ZERO file edits — no change was "
            "implemented (likely assumed the feature already existed). "
            + outcome[:400])
    return verdict


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
        seed = _build_seed(state)
        out = _PassOutcome()
        kw = {"cwd": cwd, "role": role, "max_steps": max_steps,
              "complete_fn": complete_fn, "scope_globs": _scope_globs(state),
              "out": out}
        _one_pass(seed, **kw)
        # No-edit guard: a local model routinely HALLUCINATES that the change
        # "already exists", runs only a compile, and declares success WITHOUT
        # writing a single file (trace: ACTION run_command mvnw compile, 0
        # file_write) — the base repo compiles green, so it reads as done. Force
        # a corrective pass that DEMANDS a real edit. Bounded; opt-out via
        # AIFORGE_DOER_MIN_EDIT_RETRIES=0. Only fires when the last pass finished
        # cleanly (not a stop/deadline banner) with zero edits.
        retries = _min_edit_retries()
        attempt = 0
        while (out.edits == 0 and attempt < retries
               and not _is_stopped_outcome(out.text())):
            attempt += 1
            # Drop pass-1's quality signals: they were measured on the UNEDITED
            # tree (the hallucinated "already done" pass). A stale typecheck_ok/
            # tests_ok=True must not survive to vouch for pass-2's real edit if
            # the model forgets to re-verify. The corrective pass re-populates.
            out.signals.clear()
            _one_pass(seed + _NO_EDIT_CORRECTION, **kw)
        outcome = out.text() or "text-doer produced no final output"
        result["edit_count"] = out.edits
        if out.failure is not None:
            result["iter_failure"] = out.failure
        result["doer_outcome"] = outcome
        result.update(out.signals)
        result.update(_no_edit_verdict(outcome, out.edits))
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
# + compiles). The shared list, MINUS ``format``: a formatter pass rewrites
# files without implementing anything, so on its own it must still trip the
# guard. (The private copy also lacked file_create/create_file, so a Doer that
# only CREATED files was told it had made zero edits.)
# Shells (run_command/bash) are not edits — they are not in the list at all.
_EDIT_TOOLS = FILE_WRITE_TOOLS - {"format"}

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
    # What the last test run failed on — the loop gate's same-failure rule.
    if out.get("iter_failure") is not None:
        state["_iter_fail"] = out["iter_failure"]


def make_text_doer_node():
    """Return an ADK ``node`` (named ``doer``) wrapping :func:`_text_doer_node`
    so it slots into the pipeline graph exactly where the native Doer went."""
    from google.adk.workflow import node
    return node(_text_doer_node, name="doer")


def should_use_text_protocol(_role: str = "doer") -> bool:
    """Decide whether the Doer should run the TEXT protocol.

    ``AIFORGE_DOER_PROTOCOL``:
      * ``text``   → always True.
      * ``native`` / ``auto`` / unset / anything else → False (native).

    Native tool-calling is now the DEFAULT everywhere — the same mechanism
    OpenWebUI uses on these OpenAI-compatible endpoints (LM Studio etc.), which
    handle native FC correctly, so the local-endpoint text heuristic was
    penalising them for no reason. The text protocol remains available for a
    genuinely tool-incapable local runtime (notably mlx-lm's 'zero tool_use'
    bug) by setting ``AIFORGE_DOER_PROTOCOL=text``.
    """
    mode = (os.environ.get("AIFORGE_DOER_PROTOCOL") or "native").strip().lower()
    return mode == "text"


__all__ = ["run_text_doer", "make_text_doer_node", "should_use_text_protocol"]
