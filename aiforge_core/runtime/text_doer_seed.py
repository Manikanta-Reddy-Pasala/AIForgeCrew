"""The Doer's seed prompt: the pipeline state, sized to the model's window,
with the codegraph mandate when a code graph is available."""
from __future__ import annotations

import json
import os
from typing import Any

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
    """The enforce-codegraph preamble, or "" when codegraph isn't usable on this
    run. Gated by the SINGLE shared gate (binary + real index for this repo +
    not env-disabled + not opted out per-ticket) so the mandate never bans grep
    on an un-indexed repo and honors the A/B opt-out on the local text path."""
    try:
        from aiforge_core.runtime.tools import codegraph as _cg
        return _CODEGRAPH_MANDATE if _cg.enabled_for_run() else ""
    except Exception:  # noqa: BLE001 — never break seed assembly
        return ""


def _budgeted_seed(state: dict, parts: list[str], remaining: int) -> str:
    """The high-priority sections in full, then the bulky ones sharing what is
    left — an even split so BOTH briefs survive (truncated) rather than the
    first eating the whole pool."""
    for key in _SEED_HIGH:
        remaining = _emit_section(parts, remaining, key,
                                  _present_text(state, key))
    low = [(k, t) for k, t in
           ((k, _present_text(state, k)) for k in _SEED_LOW) if t]
    for i, (key, text) in enumerate(low):
        left = len(low) - i
        share = remaining // left if left else remaining
        remaining = _emit_section(parts, remaining, key, text, cap=share)
    return "".join(parts)


def _unbudgeted_seed(state: dict, parts: list[str]) -> str:
    """The original plain concatenation — the fallback when budgeting fails."""
    for key, label in _SEED_VARS:
        text = _present_text(state, key)
        if text:
            parts.append(f"\n--- {label} ---\n{text}")
    return "".join(parts)


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
    head = [mandate, _SEED_HEADER] if mandate else [_SEED_HEADER]
    try:
        remaining = _seed_budget_chars() - len(_SEED_HEADER) - len(mandate)
        return _budgeted_seed(state, list(head), remaining)
    except Exception:  # noqa: BLE001
        return _unbudgeted_seed(state, list(head))
