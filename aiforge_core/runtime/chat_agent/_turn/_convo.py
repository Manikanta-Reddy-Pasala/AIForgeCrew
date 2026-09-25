"""Building the turn's conversation: the budget-capped system prompt with its
codegraph and sandbox directives, the history folded into it, and
directives appended mid-turn."""
from __future__ import annotations

import os

from .._context import (
    _WEB_LOOKUP_DIRECTIVE,
    _cap_system_prompt,
    _cave_mode,
    _compress_prompt,
    _has_web_intent,
    _split_asks,
    _sys_prompt_budget_chars,
    _text_of,
)
from .._native import _batch_cap
from .._prompt_text import _SYSTEM, BATCH_READS_RULE, LONG_RUN_RULE
from .._tools import (
    _preferences_context,
    _rules_context,
)
from ._blocks import (
    _append_context_blocks,
    _prepend_priority_blocks,
)


def _codegraph_directive(cwd, readonly_mode) -> str:
    """Ensure this repo's codegraph index (skipped in read-only modes — the
    build writes a .codegraph/ dir into the repo) and return the "CODEGRAPH IS
    AVAILABLE" tool directive when the shared gate says it is usable this run,
    else ''."""
    try:
        from aiforge_core.runtime.tools import codegraph as _cg
        if not readonly_mode:
            _cg.ensure_indexed(cwd)
        if _cg.enabled_for_run(cwd):
                return (
                    "\n\nCODEGRAPH IS AVAILABLE (a pre-built code-relation index "
                    "for THIS repo). USE IT — do not rediscover with grep what "
                    "the graph already knows:\n"
                    "- BEFORE editing or extending any EXISTING symbol, call "
                    "codegraph_callers AND codegraph_impact on it to find every "
                    "call site + everything a change would affect. Grep misses "
                    "call sites and matches comments/strings; the graph does not.\n"
                    "- To ORIENT on an unfamiliar area, call codegraph_explore "
                    "with the task in plain words FIRST (before list_dir/grep).\n"
                    "- To locate a definition, use codegraph_query, not grep.\n"
                    "Tools:\n"
                    "- codegraph_explore  {\"query\": \"where amounts are parsed\"}  "
                    "relevant symbols + their source for a natural-language query\n"
                    "- codegraph_query    {\"query\": \"clean_amount\"}   find a "
                    "symbol + its defining file:line\n"
                    "- codegraph_callers  {\"symbol\": \"foo\"}   every caller of foo\n"
                    "- codegraph_callees  {\"symbol\": \"foo\"}   what foo calls\n"
                    "- codegraph_impact   {\"symbol\": \"foo\"}   blast-radius of "
                    "changing foo — ALWAYS call before editing a shared symbol")
    except Exception:  # noqa: BLE001 — never break prompt build
        pass
    return ""


def _seed_prompt(messages, cwd, readonly_mode):
    """Seed the system prompt: extract the raw last-user message, load cave/rules/
    prefs, format the core prompt, and apply the catalog + codegraph gates.
    Returns (last_user, cave, rules, prefs, sys_msg)."""
    last_user = next(
        (_text_of(m) for m in reversed(messages)
         if (m.get("role") or "user") == "user" and m.get("content")), "")
    # _text_of flattens a multimodal (vision) turn's list content to text, so the
    # .split() below can't crash on a list.
    last_user = last_user.split("\n\n---\n[Interpreted request")[0].strip() or last_user

    # Inject a fresh repo map every turn so the agent ALWAYS knows the
    # directory structure of the working dir without re-searching it on
    # each follow-up question (the conversation history only carries prior
    # answers, not the structure it discovered last turn).
    cave = _cave_mode()
    rules = _rules_context(cwd, last_user)
    prefs = _preferences_context(cwd)
    sys_msg = _SYSTEM.format(cwd=cwd)
    # Advertise only integrations this install can reach. Same principle as the
    # CodeGraph gate below: a tool the model is told about but that always
    # answers `*_not_configured` costs prompt budget and invites a wrong pick.
    try:
        from .._catalog_gate import gate_catalog
        sys_msg, _ungated = gate_catalog(sys_msg)
    except Exception:  # noqa: BLE001 — never let gating break a turn
        pass
    # CodeGraph tools are advertised ONLY when actually usable on this run — the
    # single shared gate (binary + real index for THIS repo + not env-disabled +
    # not opted out per-ticket). Otherwise the model would be told to call a tool
    # that always errors (un-indexed repo) / the A/B "without" arm would leak.
    # Without this block the tools are in TOOLS but absent from the catalog, so
    # the model never learns they exist.
    sys_msg += _codegraph_directive(cwd, readonly_mode)
    sys_msg += _sandbox_directive(readonly_mode)
    return last_user, cave, rules, prefs, sys_msg


def _language_directive(role) -> str:
    """The reply language chosen in Settings, or "" (no preference)."""
    try:
        from aiforge_core.config import response_language
        return response_language.for_role(role)
    except Exception:  # noqa: BLE001 — never break prompt build
        return ""


def _sandbox_directive(readonly_mode: bool) -> str:
    """Inside the docker-mode sandbox the agent owns the box: it must install
    whatever the task needs and finish, not stop at "command not found"."""
    try:
        from aiforge_core.runtime.tools import command_risk
        if readonly_mode or not command_risk.in_sandbox():
            return ""
    except Exception:  # noqa: BLE001
        return ""
    repos = os.environ.get("AIFORGE_REPO_ROOT") or "~/.aiforge/repos"
    return ("\n\nSANDBOX: you run inside a disposable Ubuntu 24.04 box with "
            "passwordless sudo and open network. Install ANY tool the task "
            "needs — ensure_runtime, or `sudo apt-get update && sudo apt-get "
            "install -y <pkg>`, pip, npm — and complete the task; never stop "
            "because a tool is missing. Packages come from the configured "
            f"internal registries. Projects live in {repos}; the user's own "
            "files outside the mounted folders are not reachable. Pushing, "
            "opening PRs and deleting data still need the user's OK.")


def _build_convo(messages, cwd, role, *, readonly_mode, plan_mode,
                 analyze_mode, builder, strict_finish, session_id, native=False,
                 unlimited=False):
    """Build the ReAct conversation: assemble the budget-capped system prompt
    (rules, prefs, banners, catalog/codegraph gates, multi-ask checklist, and
    every dynamic context block via the shared bundle), fold history + vision
    images into the message list. Returns
    ``(convo, bundle, asks, dropped_playbooks)``."""
    last_user, cave, rules, prefs, sys_msg = _seed_prompt(messages, cwd, readonly_mode)
    # Multi-part message (simple mode has no enhancer/spec, so nothing else
    # tracks the parts): derive an ASK CHECKLIST and pin it HIGH in the
    # system prompt — the model must cover every part, not answer #1 and stop.
    # Skipped on strict_finish (pipeline Doer / subtask runners): there the
    # "user message" is a MACHINE-built seed whose instruction bullets
    # ("CONTEXT-FIRST…", "MINIMAL DIFF…") are style rules, not asks — counting
    # them made the Doer enumerate its own charter in FINAL and burned an extra
    # model turn on the completeness gate every run.
    _asks = [] if (builder or strict_finish) else _split_asks(last_user)
    sys_msg = _prepend_priority_blocks(
        sys_msg, _asks, prefs, rules, analyze_mode, plan_mode, builder)
    # Reply language (Settings): part of the protected core, never trimmed —
    # a half-kept block said "write in X" and lost its "never change code or
    # protocol markers" clause. (llm.client adds it for every other call.)
    _lang = _language_directive(role)
    if _lang:
        sys_msg += "\n\n" + _lang
    # C2: budget the (un-condensable) system prompt. The CORE prompt + rules
    # above are ALWAYS kept; each optional block below is appended via a
    # budget-aware helper that truncates/drops it (lowest priority = appended
    # last = dropped first) when it would blow the cap. `_cap_system_prompt`
    # is the final backstop guaranteeing len(sys_msg) <= cap.
    _sys_cap = _sys_prompt_budget_chars(role)
    _sys_core_len = len(sys_msg)
    _sys_dropped: list[str] = []
    _sys_seen_blocks: set[str] = set()

    def _add_sys_block(label: str, block: str) -> None:
        nonlocal sys_msg
        if not block:
            return
        # A skill, workflow, OKF brief, or memory hit already in this turn
        # (the seed, or an earlier block) is not pasted again. The first copy
        # stays. A changed body still comes through whole.
        _kind = {"skills": "skill", "workflows": "workflow",
                 "recall": "memory", "project-memory": "okf"}.get(label)
        if _kind:
            from aiforge_core.runtime.context_seen import shrink_block
            block = shrink_block(
                [*messages, {"role": "system", "content": sys_msg}], _kind, block)
            if not block:
                return
        # R7: don't spend budget on a block whose exact text was already added
        # (e.g. prev-session vs a recall block that surfaced the same content).
        _bkey = " ".join(block.split())
        if _bkey in _sys_seen_blocks:
            return
        _sys_seen_blocks.add(_bkey)
        addition = "\n\n" + block
        if len(sys_msg) + len(addition) <= _sys_cap:
            sys_msg += addition
            return
        room = _sys_cap - len(sys_msg)
        if room > 400:              # enough left for a meaningful truncated slice
            sys_msg += addition[:room] + "\n…(truncated to fit context)\n"
        _sys_dropped.append(label)

    # WEB-LOOKUP directive FIRST — it's short + critical, so it must outrank the
    # big optional blocks (repo-map/recall) under a tight window (blocks added
    # LATER drop first). Without top priority the "no web access" notice got
    # trimmed exactly when context was full, and the model answered from stale
    # memory. Read-only tool → safe in plan/analyze too. (Detected on last_user;
    # a bare URL is excluded — it already routes to web_crawl.)
    if last_user and _has_web_intent(last_user):
        _add_sys_block("web-lookup", _WEB_LOOKUP_DIRECTIVE)
    if unlimited and len(_asks) > 1 and not readonly_mode and not builder:
        _add_sys_block("long-run", LONG_RUN_RULE)
    if native and _batch_cap() > 1:
        _add_sys_block("batch-reads", BATCH_READS_RULE)

    _bundle, _img_blocks = _append_context_blocks(
        _add_sys_block, cwd, last_user, messages, session_id, role, cave)
    if _sys_dropped:                # one-line note so the trim is visible
        _add_sys_block("_note", "[context note: dropped/trimmed lower-priority "
                       "blocks to fit the window: " + ", ".join(_sys_dropped) + "]")
    # A dropped WORKFLOWS/SKILLS block means the agent may skip a mandatory
    # user procedure (e.g. branch-then-MR) — surface that to the USER instead
    # of failing silently inside the prompt.
    _dropped_playbooks = [b for b in ("workflows", "skills") if b in _sys_dropped]
    # Final backstop: guarantee the system prompt is under the cap (keeps the
    # core + rules at the front; truncates the injected tail).
    sys_msg = _cap_system_prompt(sys_msg, _sys_cap, protect=_sys_core_len)
    sys_msg = _compress_prompt(sys_msg)   # trim whitespace bloat (caveman-style)
    convo = _history_to_convo(sys_msg, messages, _img_blocks)
    return convo, _bundle, _asks, _dropped_playbooks


def _history_to_convo(sys_msg, messages, _img_blocks):
    """Assemble the message list: system prompt + each history turn, then fold
    this turn's vision image parts into the latest user turn (multimodal
    content) when the model is vision-capable. Returns ``convo``."""
    convo: list[dict] = [{"role": "system", "content": sys_msg}]
    for m in messages:
        r = m.get("role") or "user"
        convo.append({"role": "assistant" if r == "assistant" else "user",
                      "content": m.get("content") or ""})
    # When the model is vision-capable, fold the actual images into the latest
    # user turn (multimodal content) so it can SEE them, not just their text.
    if _img_blocks:
        for _m in reversed(convo):
            if _m.get("role") == "user":
                _m["content"] = [{"type": "text", "text": _m.get("content") or ""},
                                 *_img_blocks]
                break
    return convo


def _append_directive(st, _directive):
    """Append a steer/reject directive as a user turn — merged into a trailing
    user turn (list-safe for a vision turn) to avoid two consecutive user turns
    that break some providers, else a fresh turn."""
    # If the last turn is already a user message (e.g. the
    # OBSERVATION we just appended after a tool step), MERGE the
    # steer into it — two consecutive user turns break some
    # providers (claude_local). Otherwise append a fresh user turn.
    _last = st.convo[-1] if st.convo else None
    if _last is not None and _last.get("role") == "user":
        _c = _last.get("content")
        if isinstance(_c, list):
            # A VISION turn's content is a list of parts. `+=` on a
            # list extends it with the string's CHARACTERS: one
            # steer became 364 single-character parts, invisible to
            # _text_of (so the condenser and the context meter both
            # missed it) while still being sent.
            _c.append({"type": "text", "text": _directive})
        else:
            _last["content"] = f"{_c or ''}\n\n{_directive}"
    else:
        st.convo.append({"role": "user", "content": _directive})
