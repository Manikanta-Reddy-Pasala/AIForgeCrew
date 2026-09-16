"""Turn history shaping, learning write-back, session summaries, and
turn-level logging."""
from __future__ import annotations

import os

from ._core import (
    _af_log,
)
from ._sessions import (
    _is_isolated_workspace,
)

_DIGEST_ARG_KEYS = ("path", "file", "cmd", "command", "query", "pattern")


def _step_mark(result) -> str:
    """Tiny outcome marker for one tool result — ✓ / ✗ / nothing."""
    if not isinstance(result, dict):
        return ""
    if result.get("ok") is False or result.get("error"):
        return "✗"
    return "✓" if result.get("ok") is True else ""


def _step_arg(args) -> str:
    """The one argument worth naming in the digest (the first of
    path/file/cmd/command/query/pattern the call carried), kept short."""
    if not isinstance(args, dict):
        return ""
    for k in _DIGEST_ARG_KEYS:
        if args.get(k):
            return str(args[k])[:48]
    return ""


def _step_digest(steps: list) -> str:
    """One compact line summarising what an assistant turn DID — tool calls +
    outcomes — so the next turn's history carries the agent's actions, not just
    its final prose. Fixes the 'forgets what it just did' amnesia: persisted
    `steps` were never fed back into context, so any work the model didn't
    transcribe into its final answer vanished."""
    if not isinstance(steps, list):
        return ""
    bits: list[str] = []
    for s in steps:
        if not isinstance(s, dict) or s.get("type") != "tool":
            continue
        name = s.get("name") or "tool"
        arg = _step_arg(s.get("args") or {})
        mark = _step_mark(s.get("result") or {})
        bits.append(f"{name}({arg}){mark}" if arg else f"{name}{mark}")
        if len(bits) >= 12:
            bits.append("…")
            break
    return ", ".join(bits)


def _history_row_content(m: dict, role: str) -> str:
    """The content for one persisted row, folding an assistant turn's tool DIGEST
    into it so the agent remembers its own prior actions. "" for a row with
    nothing to say (which the caller then skips)."""
    content = (m.get("content") or "").strip()
    if role != "assistant":
        return content
    digest = _step_digest(m.get("steps") or [])
    if not digest:
        return content
    return (content + f"\n[did: {digest}]").strip() if content else f"[did: {digest}]"


def _chat_history_for_agent(rows: list) -> list[dict]:
    """Build the agent's conversation history from persisted messages.

    Unlike a naive role+content copy, this (1) folds each assistant turn's tool
    DIGEST into its content so the agent remembers its own prior actions, (2)
    keeps assistant turns that did work but produced no final text (don't drop
    them — that left a gap AND broke user/assistant alternation), and (3) merges
    consecutive same-role turns (some providers reject two in a row)."""
    out: list[dict] = []
    for m in rows:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = _history_row_content(m, role)
        if not content:
            continue   # truly empty (e.g. a user turn with no text) — skip
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content   # merge same-role
        else:
            out.append({"role": role, "content": content})
    return out


_TOPIC_CUE_PHRASES = ("track ", "organize by", "organise by", "as a topic",
                      "remember this topic", "topic:")


def _warn_if_not_persisted(res, label: str, repo: str) -> None:
    """Warn when a writeback returned a real failure (not a skip). On a daemon
    thread with no HTTP surface, a "remember X" that fails to store is real data
    loss and a WARNING is the only signal."""
    if isinstance(res, dict) and res.get("ok") is False and res.get("skipped") is None:
        _af_log.warning("%s did NOT persist (repo=%s): %s", label, repo,
                        res.get("error"))


def _capture_chat_cue(prompt, repo: str, session_id) -> None:
    """An explicit "track this as a topic" → md capture (repo stamped) so it
    reaches the compaction axes.

    This used to ALSO store the raw prompt verbatim as a ``user_comment`` on any
    preference cue, which is what filled memory with chat turns ("can you add
    gitlab ci file for this repo", "attahced his solution"). Every turn already
    goes through preference_capture + chat_learner, which DISTIL it; the raw
    text added nothing but noise, so that branch is gone — and with it the
    ``pref_captured`` argument, which only ever guarded that branch.
    """
    try:
        from aiforge_core.memory import md_store as _md2
        low = (prompt or "").lower()
        if any(ph in low for ph in _TOPIC_CUE_PHRASES):
            _md2.capture("topic_suggestion", (prompt or "").strip(),
                         repo=repo, source=f"chat:{session_id or ''}",
                         evidence=f"chat session {session_id or '?'}")
    except Exception:  # noqa: BLE001
        pass


def _chat_learn_writeback(cwd, prompt, final_text, steps, session_id) -> None:
    """Single-chat (simple/plan) memory writeback on a daemon thread. The team
    pipeline runs a Learner node itself; the inline simple/plan path never did,
    so chat work never reached long-term memory. Distils + persists durable
    facts. Best-effort — a failure here must never affect the turn."""
    try:
        from aiforge_core.runtime import chat_learner, preference_capture
        from aiforge_core.runtime.chat_agent import _chat_repo_key
        # Same key resolution as RECALL (_chat_repo_key, git-toplevel basename) —
        # the old bare repo_key(cwd) filed subdir-pinned sessions under the subdir
        # while recall read the repo root, so facts were never found.
        repo = _chat_repo_key(cwd)
        # PREFERENCE FIRST — a preference-cue message ("use X as default", "from
        # now on…") is UPSERTED by subject and owns the turn. The learner still
        # runs on EVERY turn to distil the OTHER signal (technical learnings,
        # project-structure findings, durable intent the pref capture didn't
        # own); it dedups against memory so it won't re-emit the pref.
        pc = preference_capture.capture(prompt, repo=repo, session_id=session_id)
        lr = chat_learner.learn_from_chat(
            prompt=prompt, final_text=final_text, steps=steps, repo=repo,
            session_id=session_id)
        _warn_if_not_persisted(lr, "chat_learner", repo)
        _warn_if_not_persisted(pc, "preference_capture", repo)
        # …and the LIBRARY half: a turn that established a repeatable procedure
        # becomes a skill or workflow. Rules already capture themselves; skills
        # and workflows only ever existed when the agent remembered to ask for
        # one, so procedures — the thing most worth having back — were the one
        # thing nothing wrote down. Declines cheaply on an ordinary turn.
        from aiforge_core.runtime import artifact_capture
        artifact_capture.capture_from_chat(
            prompt=prompt, final_text=final_text, steps=steps, cwd=cwd,
            session_id=session_id)
        _capture_chat_cue(prompt, repo, session_id)
    except Exception as exc:  # noqa: BLE001
        _af_log.warning("chat learn/capture thread failed: %s", exc)


def _chat_summarize_session(cwd, session_id) -> None:
    """Boundary-gated per-SESSION summary → browsable md file + memory graph.
    Refreshes an upsert'd summary every N turns as the session grows (one
    cheap-tier LLM call, capped) so cross-session recall goes through
    unified_query's graph instead of a substring scan. Best-effort on a daemon
    thread — a failure here must never affect the turn."""
    try:
        from aiforge_core.runtime import chat_store, chat_summary
        from aiforge_core.runtime.chat_agent import _chat_repo_key
        every = 4
        try:
            every = max(1, int(os.environ.get(
                "AIFORGE_CHAT_SUMMARY_EVERY", "4")))
        except (TypeError, ValueError):
            every = 4
        n = len(chat_store.get_messages(session_id))
        if n <= 0 or n % every != 0:
            return
        _repo = _chat_repo_key(cwd)   # git-toplevel, matches recall
        chat_summary.summarize_session(session_id, _repo)
        # Auto-author a workflow from the session's WORKING
        # steps + file it into OKR memory with tags, so the
        # working commands are reusable and don't get redone.
        from aiforge_core.runtime import session_ledger
        session_ledger.capture_working_workflow(session_id, _repo)
        # OKR-DAG auto-authoring: extract durable Objectives/
        # KeyResults/Learnings from this session into the graph,
        # and write a session node from the executed steps.
        try:
            from aiforge_core.memory import okf as _okr
            from aiforge_core.runtime.chat_agent import _chat_repo_key
            # An unpinned chat runs in an isolated scratch
            # workspace (chat-workspaces/session-<id>) — NOT
            # a real repo. Scope its knowledge GLOBAL instead
            # of minting a phantom projects/session-<id>/ OKR
            # tree (one bogus "project" per session).
            _rkey = None if _is_isolated_workspace(cwd) \
                else _chat_repo_key(cwd)
            _msgs2 = chat_store.get_messages(session_id) or []
            _tx = "\n".join(
                f"{m.get('role')}: {m.get('content')}"
                for m in _msgs2 if isinstance(m, dict)
                and m.get("content"))[:8000]
            # classify each learning global vs THIS repo
            _okr.extract_and_save(_tx, repo=_rkey)
            _led = session_ledger.ledger_block(session_id)
            if _led:
                _okr.write_session_node(
                    title=f"chat session {session_id}",
                    body=_led, repo=_rkey)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _setup_chat_logger():
    """The shared "chat" observability logger + its emit fn, so the Logs "chat"
    tab tails one file. Returns ``(clog, emit)`` — ``(None, None)`` if the
    observability module is unavailable. (Don't stash a per-session ticket on the
    process-wide singleton — concurrent sessions would clobber it; the caller
    stamps ``session`` per emit.)"""
    try:
        from aiforge_core.observability.logging import emit, get_logger
        return get_logger("chat"), emit
    except Exception:  # noqa: BLE001
        return None, None


def _bind_turn_meter(session_id):
    """Bind THE per-turn request-meter boundary here (not inside the ReAct loop):
    the enhancer / team-downgrade classifier / capture probes below are requests
    THIS message caused, and team mode never enters run_chat_agent at all — a
    reset in the loop left its per-turn number cumulative for the session.
    Returns ``(meter, meter_token)``; metering must never break a turn."""
    try:
        from aiforge_core.llm import call_meter as _meter
        return _meter, _meter.bind_turn(_meter.turn_reset(session_id))
    except Exception:  # noqa: BLE001
        return None, None


def _maybe_downgrade_team(team, prompt, history, cwd, session_id):
    """Auto-route a small team follow-up down to simple mode. Run HERE (off the
    response-open path) so a slow/unreachable classify LLM never delays the
    StreamingResponse. Returns ``(team, auto_downgraded)``; routing must never
    block a turn."""
    if not team:
        return team, False
    try:
        from aiforge_core.runtime import turn_router as _tr
        if _tr.should_downgrade_team(prompt, history, cwd):
            _af_log.info("chat: team turn auto-downgraded to simple "
                         "(small follow-up) session=%s", session_id)
            return False, True
    except Exception as exc:  # noqa: BLE001
        _af_log.debug("turn_router skipped: %s", exc)
    return team, False


_TERMINAL_SUBTASK = {"done", "failed", "skipped", "won", "planned"}


def _note_staleness_notice(cwd):
    """Staleness auto-curation: a session bound to a jira/confluence context
    folder re-verifies that note when it crosses AIFORGE_NOTE_STALE_HOURS. The
    pre-check is cheap + network-free; the curation re-fetches the source so it
    is HARD time-boxed — a dead Jira must never stall the turn. Yields a curator
    thought ONLY when something actually drifted. FAILS OPEN."""
    try:
        from aiforge_core.runtime import note_curator as _nc
        _stale_note = _nc.stale_note_path(cwd)
        if _stale_note:
            import concurrent.futures as _ncf
            _cres = None
            _nex = _ncf.ThreadPoolExecutor(max_workers=1)
            try:
                _nbudget = float(os.environ.get(
                    "AIFORGE_NOTE_CURATE_BUDGET_S", "10"))
                _cres = _nex.submit(_nc.curate_note,
                                    _stale_note).result(timeout=_nbudget)
            except Exception as _nexc:  # noqa: BLE001 — timeout/any → skip
                _af_log.debug("note curation timed out/failed: %s", _nexc)
            finally:
                _nex.shutdown(wait=False)
            # Visible only when something actually drifted — a silent
            # freshness bump shouldn't add chat noise.
            if _cres and _cres.get("ok") and _cres.get("changes"):
                yield {"type": "thought", "role": "curator",
                       "text": ("Auto-curated stale note "
                                f"{os.path.basename(_stale_note)}: "
                                + "; ".join(_cres["changes"]))}
    except Exception as _nexc2:  # noqa: BLE001 — must never break a turn
        _af_log.debug("note staleness pass skipped: %s", _nexc2)
