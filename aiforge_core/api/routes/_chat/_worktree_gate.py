"""Turn start: the chat's worktree, the once-per-project "where?" question, and
the list of OLD worktrees with the user's answer to it.

Runs before the agent, with no model call. What it decides is left on ``pc``:

* ``pc.cwd``          — the chat's worktree when it has (or now gets) one;
* ``pc._wt_gate``     — ``{"text", "awaiting"}``: the turn ends with this
  message and no agent runs (the question, or the summary of a cleanup);
* ``pc._wt_notices``  — lines shown before the agent's work (where the
  worktree is, the old-worktree list when nothing is being asked).

Fail-open: an error anywhere here leaves the turn running in the project
folder, as it always could.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _publish(pc, text: str) -> None:
    try:
        pc.run.publish({"type": "thought", "role": "system", "text": text})
    except Exception:  # noqa: BLE001
        pass


def _replay(pc, original: str) -> None:
    """Run ``original`` (the request the question held back) as this turn's
    request: the prompt, the body and the last user row of the history. The
    earlier copy of it and the question are folded out of the history."""
    pc.prompt = original
    try:
        pc.body.content = original
    except Exception:  # noqa: BLE001
        pass
    hist = getattr(pc, "history", None)
    if not isinstance(hist, list) or not hist:
        return
    from aiforge_core.runtime import worktree_prefs as wp
    # From the held request on, the rows are only the question and the
    # answers to it: the request itself stands in for all of them.
    for i in range(len(hist) - 2, -1, -1):
        row = hist[i]
        if row.get("role") == "user" and str(row.get("content") or "").strip() == original.strip() \
                and any(wp.QUESTION_MARK in str(r.get("content") or "")
                        for r in hist[i + 1:-1] if r.get("role") == "assistant"):
            del hist[i:-1]
            break
    if hist[-1].get("role") == "user":
        hist[-1] = {**hist[-1], "content": original}


def _short(text: str, n: int = 120) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _cleanup_reply(state: dict, cmd) -> str:
    from aiforge_core.runtime import worktree_inventory as inv
    old = state.get("old") or {}
    repo, paths = old.get("repo") or "", old.get("paths") or []
    verb, picks = cmd
    if verb == "keep":
        inv.snooze(repo, old.get("key") or inv.set_key([{"path": p} for p in paths]))
        return (f"Kept all {len(paths)} old worktree{'s' if len(paths) != 1 else ''}. "
                f"I won't list them again for {inv.KEEP_DAYS} days.")
    lines = inv.delete(repo, paths, picks)
    return "Old worktrees:\n" + "\n".join(f"- {ln}" for ln in lines)


def _rest_is_request(rest: str) -> bool:
    words = [w for w in (rest or "").replace(",", " ").split()
             if w.lower() not in ("and", "then", "also", "please", "thanks", "ok", "okay")]
    return len(words) >= 3


def run(pc) -> None:
    pc._wt_gate = None
    pc._wt_notices = []
    try:
        _run(pc)
    except Exception as exc:  # noqa: BLE001 — never block the turn on this
        log.warning("worktree gate failed: %s", exc)
        pc._wt_gate = None
        _publish(pc, f"⚠ Could not make a separate workspace ({str(exc)[:160]}); "
                     "working in the project folder.")


def _run(pc) -> None:
    from aiforge_core.runtime import chat_store, chat_worktree
    from aiforge_core.runtime import worktree_prefs as wp
    sid = pc.session_id
    text = (getattr(pc, "prompt", "") or "").strip()
    wake = bool(getattr(pc, "_wake", False))
    state = wp.session_state(sid) if not wake else {}
    notes: list[str] = []
    place_override, ignore_answers = None, False

    # 1. A reply to the old-worktree list: handled here, no model.
    cmd, rest = (None, text)
    if state.get("old"):
        from aiforge_core.runtime import worktree_inventory as inv
        cmd, rest = inv.parse_command(text)
        if cmd:
            summary = _cleanup_reply(state, cmd)
            state["old"] = None
            wp.save_session_state(sid, state)
            notes.append(summary)

    # 2. A reply to the "where?" question.
    ask = state.get("ask")
    if ask:
        choice = wp.parse_choice(rest if cmd else text)
        if choice:
            wp.set_place(ask["repo"], choice)
            state["ask"] = None
            wp.save_session_state(sid, state)
            original = ask.get("prompt") or ""
            if original:
                _replay(pc, original)
            notes.append(f"Saved for {ask['repo']}: chats here {wp.PLACE_WORDS[choice]}. "
                         + wp.how_to_change(ask["repo"])
                         + (f" Now running your request: “{_short(original)}”." if original else ""))
            place_override, ignore_answers = choice, True
        elif cmd and not _rest_is_request(rest):
            # Only the cleanup was answered: the question still stands.
            holder = chat_worktree.config_holder(ask["repo"])
            q = wp.question(ask["repo"], holder, writable=os.access(ask["repo"], os.W_OK))
            pc._wt_gate = {"text": "\n\n".join(notes + [q]), "awaiting": True}
            return
        else:
            state["ask"] = None
            wp.save_session_state(sid, state)
            if cmd:
                pc.prompt = rest
                _replay_text(pc, rest)
            if ask.get("nonblocking"):
                # The question did not hold the request (it already ran in the
                # default place): a message that is not a choice is just the
                # next request — no note about the question.
                place_override, ignore_answers = None, False
                return _run_rest(pc, sid, text if not cmd else rest, state, notes,
                                 place_override, ignore_answers, wake)
            notes.append("That was not one of the worktree choices (1, 2 or 3), so this chat "
                         "uses the default — its own worktree inside the project — and nothing "
                         "was saved; the next new chat here asks again.")
            place_override, ignore_answers = "repo", True
    elif cmd:
        if _rest_is_request(rest):
            pc.prompt = rest
            _replay_text(pc, rest)
        else:
            pc._wt_gate = {"text": "\n\n".join(notes), "awaiting": False}
            return

    return _run_rest(pc, sid, text, state, notes, place_override, ignore_answers, wake)


def _blocking() -> bool:
    """The "where?" question holds the request until answered (the old way).
    Default off (owner 11-16: do not wait on the user for what is not a
    deletion): the request runs at once in the default place and the answer,
    when it comes, is saved for the project's next chats."""
    return os.environ.get("AIFORGE_CHAT_WORKTREE_ASK_BLOCK", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _run_rest(pc, sid, text, state, notes, place_override, ignore_answers, wake) -> None:
    from aiforge_core.runtime import chat_store, chat_worktree
    from aiforge_core.runtime import worktree_prefs as wp
    # 3. The chat's worktree.
    sess = chat_store.get_session(sid)
    wd = chat_worktree.workdir_of(sess)
    if wd:
        pc.cwd = wd                  # same worktree in every mode
        pc._wt_notices = notes
        return
    repo = chat_worktree.fresh_repo(sess, ignore_answers=ignore_answers)
    if not repo:
        pc._wt_notices = notes
        return
    place = place_override or wp.place_for(repo)

    # A harness (wake) turn neither lists nor asks: nobody is there to answer.
    old_text, old_state = ("", None) if wake else _old_list(repo, state)
    if place is None and wp.ask_enabled() and not wake and not _blocking():
        holder = chat_worktree.config_holder(repo)
        q = wp.question(repo, holder, writable=os.access(repo, os.W_OK))
        state["ask"] = {"prompt": "", "repo": repo, "at": wp.now_iso(), "nonblocking": True}
        wp.save_session_state(sid, state)
        notes.append(q + "\n\n(Not waiting for this: this chat uses its own worktree inside "
                         "the project for now; your answer is saved for the next chats here.)")
        place = "repo"
    if place is None and wp.ask_enabled() and not wake:
        holder = chat_worktree.config_holder(repo)
        q = wp.question(repo, holder, writable=os.access(repo, os.W_OK))
        state["ask"] = {"prompt": text, "repo": repo, "at": wp.now_iso()}
        if old_state:
            state["old"] = old_state
            state.setdefault("shown", []).append(old_state["key"])
        wp.save_session_state(sid, state)
        pc._wt_gate = {"text": "\n\n".join(notes + [q] + ([old_text] if old_text else [])),
                       "awaiting": True}
        return
    if old_state:
        state["old"] = old_state
        state.setdefault("shown", []).append(old_state["key"])
        wp.save_session_state(sid, state)
        notes.append(old_text)
    place = place or "repo"
    if place == "none":
        pc._wt_notices = notes
        return
    _make(pc, place, ignore_answers, notes)


def _replay_text(pc, text: str) -> None:
    hist = getattr(pc, "history", None)
    if isinstance(hist, list) and hist and hist[-1].get("role") == "user":
        hist[-1] = {**hist[-1], "content": text}


def _old_list(repo: str, state: dict):
    """The old-worktree list to show now (text, state row), or ("", None)."""
    try:
        from aiforge_core.runtime import worktree_inventory as inv
        old = inv.old_entries(repo)
        if not old:
            return "", None
        key = inv.set_key(old)
        if key in (state.get("shown") or []) or inv.snoozed(repo, key):
            return "", None
        return inv.format_list(old), {"repo": repo, "key": key,
                                      "paths": [e["path"] for e in old]}
    except Exception as exc:  # noqa: BLE001 — a listing never blocks the turn
        log.debug("old worktree listing skipped: %s", exc)
        return "", None


def _make(pc, place: str, ignore_answers: bool, notes: list) -> None:
    from aiforge_core.runtime import chat_runs, chat_worktree
    from aiforge_core.runtime import worktree_prefs as wp
    sess_mode = getattr(getattr(pc, "body", None), "mode", "simple")
    sess = None
    try:
        from aiforge_core.runtime import chat_store
        sess = chat_store.get_session(pc.session_id)
    except Exception:  # noqa: BLE001
        pass
    if not chat_worktree.eligible(sess, sess_mode, ignore_answers=ignore_answers, place=place):
        pc._wt_notices = notes
        return
    try:
        chat_runs.set_phase(pc.session_id, "creating this chat's own workspace (git worktree)")
    except Exception:  # noqa: BLE001
        pass
    _publish(pc, "🌿 Creating this chat's own workspace: a git worktree on a branch "
                 "of its own, so other chats in this project do not touch your files.")
    try:
        data = chat_worktree.ensure(pc.session_id, place=place, ignore_answers=ignore_answers)
    except Exception as exc:  # noqa: BLE001
        pc._wt_notices = notes
        _publish(pc, f"⚠ Could not make a separate workspace ({str(exc)[:160]}); "
                     "working in the project folder.")
        return
    if not data:
        pc._wt_notices = notes
        return
    pc.cwd = data["path"]
    note = (f"🌿 This chat works in {data['path']} on branch `{data['branch']}` "
            f"(from `{data['base_branch'] or 'HEAD'}`). ")
    if data.get("dirty_at_start"):
        n = len(data["dirty_at_start"])
        note += (f"Your folder has {n} uncommitted file{'s' if n != 1 else ''}; "
                 "they are not in this workspace. ")
    note += wp.how_to_change(data.get("repo") or "")
    notes.append(note.strip())
    pc._wt_notices = notes
