"""Where a team / build turn runs when the user named a folder: resolve it,
ask before touching it, and open the run's own branch + worktree (see
runtime/team_target and runtime/team_workspace)."""
from __future__ import annotations

import os

from ._core import (
    _af_log,
)


def _team_target_cwd(prompt, history, cwd, rctx, session_id=None,
                     sequential=False):
    """The folder a team/build run works in: the one the user named, else the
    session cwd — see runtime/team_target. A named folder that does not exist
    ends the turn with a question instead of a build in an invented
    path-shaped subfolder of the session workspace. A named repo is worked on
    through a new branch in its own worktree (runtime/team_workspace), after
    the user's consent; a named folder that is not a repo is only initialised
    when they allow it."""
    from aiforge_core.runtime import team_target as _tt
    from aiforge_core.runtime.parallel_subtasks import _protected
    texts = _tt.user_texts(prompt, history)
    tgt = _tt.resolve_team_target(texts, cwd)
    rules = _protected.rules_from_texts(texts)
    _protected.clear(cwd)
    _protected.register(cwd, **rules)
    rctx["prot_root"] = cwd
    if tgt.ignored:
        yield {"type": "thought", "role": "router", "text":
               "Not building in " + ", ".join(f"`{i}`" for i in tgt.ignored[:3])
               + " — too broad a folder (the team commits what it works on)."
               " Name the project folder itself to build there."}
    if tgt.missing and not tgt.retargeted:
        yield {"type": "message", "awaiting_input": True,
               "text": _tt.clarify_text(tgt.missing)}
        rctx["done"] = True
        return cwd
    from aiforge_core.runtime import team_run_life as _life
    resumed, closed_note = (_life.resume(session_id, tgt.cwd, prompt)
                            if tgt.retargeted else
                            (None, _life.forget_session(session_id)))
    if closed_note:
        yield {"type": "message", "role": "system", "supplementary": True,
               "text": closed_note}
    if not tgt.retargeted:
        return cwd
    if resumed is not None:
        from aiforge_core.runtime import team_workspace as _tw
        # "continue and merge it into main" applies; an earlier turn's
        # request to apply stands ("use postgres" answers a question, it
        # does not cancel the merge) unless this turn says not to.
        resumed.apply = (resumed.apply or _tw.wants_apply(prompt)) \
            and not _tw.refuses_apply(prompt)
        resumed.dirty = _tw.dirty_files(resumed.repo)
        _protected.clear(resumed.cwd)
        _protected.register(resumed.cwd, **rules)
        rctx["team_ws"], rctx["cwd"] = resumed, resumed.cwd
        yield {"type": "thought", "role": "router", "text":
               f"Continuing the team run on branch `{resumed.branch}` (its "
               "commits so far are kept)."}
        return resumed.cwd
    ws = yield from _open_team_workspace(tgt, texts, prompt, cwd, session_id,
                                         rctx, sequential)
    if ws is None:
        return cwd
    _af_log.info("team run targets the user-named folder %s (repo %s) on "
                 "branch %s in %s instead of %s", tgt.named, ws.repo,
                 ws.branch, ws.cwd, cwd)
    _protected.register(ws.cwd, **rules)
    rctx["team_ws"], rctx["cwd"] = ws, ws.cwd
    note = f"Working in `{ws.repo}` — the folder named in your message"
    if tgt.named != ws.repo:
        note += f" (git root of `{tgt.named}`)"
    note += (f" — on a new branch `{ws.branch}` in a separate worktree, so "
             f"your checked-out branch"
             + (f" `{ws.user_branch}`" if ws.user_branch else "")
             + " and working tree are not touched")
    if tgt.others:
        note += "; also named (not the build target): " \
                + ", ".join(f"`{o}`" for o in tgt.others)
    yield {"type": "thought", "role": "router", "text": note + "."}
    return ws.cwd


def _ask_consent(session_id, folder, reason):
    """One Allow/Deny card (the chat jail's approval UX). True on Allow. The
    consent is to run the TEAM there — it is not a write grant: the folder is
    not added to the chat's writable roots, so no file tool can write into
    the user's working tree (the run writes its own worktree)."""
    from aiforge_core.runtime import chat_approve, team_run_life
    seq = chat_approve.request(session_id)
    yield {"type": "approval", "id": seq, "name": "team_workspace",
           "args": {"path": folder}, "grant_roots": [],
           "reason": reason, "preview": ""}
    decision = chat_approve.wait(session_id)
    if decision.get("note") == "approval timed out":
        yield {"type": "approval_expired", "id": seq, "name": "team_workspace"}
    if decision.get("decision") != "approve":
        return False
    team_run_life.remember_consent(session_id, folder)
    return True


def _granted(session_id, folder) -> bool:
    from aiforge_core.runtime import chat_write_grants, team_run_life
    if team_run_life.consented(session_id, folder):
        return True
    real = os.path.realpath(folder)
    return any(os.path.realpath(g) == real
               for g in chat_write_grants.granted(session_id))


def _approvals_on(session_id) -> bool:
    from aiforge_core.runtime import chat_approve
    return chat_approve.approvals_required(session_id)


def _holds_config_dir(folder) -> bool:
    """The repo is an ancestor of the config dir (a dotfiles repo at ~)."""
    try:
        from aiforge_core.config.paths import config_dir
        from aiforge_core.runtime.team_run_life import fold
        cfg, repo = fold(str(config_dir())), fold(folder)
        return cfg == repo or cfg.startswith(repo.rstrip(os.sep) + os.sep)
    except Exception:  # noqa: BLE001
        return False


def _stop(rctx, text):
    rctx["done"] = True
    return {"type": "message", "awaiting_input": True, "text": text}


def _open_team_workspace(tgt, texts, prompt, cwd, session_id, rctx,
                         sequential):
    """Consent, then the run's branch + worktree. None (turn ended) when the
    user says no, nobody can be asked, or git fails."""
    from aiforge_core.runtime import team_target as _tt
    from aiforge_core.runtime import team_workspace as _tw
    folder = tgt.cwd
    if _holds_config_dir(folder):
        yield _stop(rctx, f"`{folder}` contains AIForge's own config folder, "
                    "where team runs keep their worktrees — a team run cannot "
                    "work on a repo that holds its own workspace. Name the "
                    "project folder inside it instead.")
        return None
    granted = session_id is not None and _granted(session_id, folder)
    if tgt.init_needed and not granted:
        if session_id is None:
            yield _stop(rctx, _tt.init_question(folder)
                        + " Say so in a chat and I will set it up.")
            return None
        ok = yield from _ask_consent(session_id, folder,
                                     _tt.init_question(folder))
        if not ok:
            yield _stop(rctx, f"Not started — `{folder}` stays as it is.")
            return None
        granted = True
    if tgt.init_needed and not _tw.init_repo(folder):
        yield _stop(rctx, f"Could not set up git in `{folder}` (it holds more "
                    "than a few thousand files, or `git init` failed), so "
                    "nothing was built. Name the project folder itself.")
        return None
    dirty = [] if tgt.init_needed else _tw.dirty_files(folder)
    ask = (session_id is not None and not granted
           and (_approvals_on(session_id) or (dirty and sequential)))
    if dirty and sequential and not ask and not granted:
        yield _stop(rctx, "Your working tree in `" + folder + "` has "
                    "uncommitted changes (" + ", ".join(dirty[:5]) + "). The "
                    "team starts from your last commit and would not see them "
                    "— commit or stash them, then ask again.")
        return None
    if ask:
        reason = (f"Let the team work in `{folder}`? It creates a new branch "
                  "there and commits its work on it, in a separate worktree; "
                  "your checked-out branch and working tree are not changed.")
        if dirty:
            reason += (" Your uncommitted changes (" + ", ".join(dirty[:5])
                       + ") are not part of the team's starting point.")
        if not (yield from _ask_consent(session_id, folder, reason)):
            yield _stop(rctx, f"Not started — nothing in `{folder}` was changed.")
            return None
    try:
        ws = _tw.open_run(folder, prompt, apply=_tw.wants_apply(prompt),
                          fresh_repo=tgt.init_needed, session_cwd=cwd)
    except Exception as exc:  # noqa: BLE001
        yield _stop(rctx, f"Could not start the team run in `{folder}`: {exc}")
        return None
    if ask:
        ws.dirty = []              # the user allowed it knowing about them
    return ws


# ── the run's end: close, or keep it for "continue" ───────────────────────

def watch(rctx, events):
    """Pass ``events`` through, noting a turn that ends waiting for the user
    (a planner question) — such a run is kept for the answer."""
    try:
        for ev in events:
            if isinstance(ev, dict) and ev.get("awaiting_input"):
                rctx["awaiting"] = True
            yield ev
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()


def _stopped(session_id) -> bool:
    try:
        from aiforge_core.runtime import chat_cancel
        return session_id is not None and chat_cancel.is_cancelled(session_id)
    except Exception:  # noqa: BLE001
        return False


def finish_run(rctx, session_id):
    """End of the turn: a run the user can pick up again (Stop pressed, or it
    asked a question) keeps its branch + worktree for the next turn of this
    chat; any other run is closed. Yields at most one note."""
    from aiforge_core.runtime import team_run_life
    from aiforge_core.runtime import team_workspace as _tw
    from aiforge_core.runtime.parallel_subtasks import _protected
    if rctx.get("prot_root"):
        _protected.clear(rctx.pop("prot_root"))
    ws = rctx.get("team_ws")
    if ws is None or ws.closed or ws.parked:
        return
    reason = ("question" if rctx.get("awaiting")
              else "stopped" if _stopped(session_id) else "")
    if reason and session_id is not None:
        text = team_run_life.park(session_id, ws, reason)
    else:
        text = _tw.close_quiet(ws)
    if text:
        yield {"type": "message", "role": "system", "supplementary": True,
               "text": text}


def abort_run(rctx) -> None:
    """The turn failed or the client went away: clean up now (no yield)."""
    from aiforge_core.runtime import team_workspace as _tw
    from aiforge_core.runtime.parallel_subtasks import _protected
    if rctx.get("prot_root"):
        _protected.clear(rctx.pop("prot_root"))
    ws = rctx.get("team_ws")
    if ws is not None and not ws.closed and not ws.parked:
        try:
            _tw.close_quiet(ws)
        except Exception as exc:  # noqa: BLE001
            _af_log.warning("team run cleanup failed: %s", exc)


def localize(rctx, prompt, history):
    """``(prompt, history)`` with the user's repo paths rewritten relative to
    the run's worktree (runtime/team_target.localize_paths)."""
    ws = rctx.get("team_ws")
    if ws is None:
        return prompt, history
    from aiforge_core.runtime.team_target import localize_paths
    out = []
    for m in history or []:
        if isinstance(m, dict) and (m.get("role") or "user") == "user" \
                and isinstance(m.get("content"), str):
            m = dict(m, content=localize_paths(m["content"], ws.repo))
        out.append(m)
    return localize_paths(prompt, ws.repo), out
