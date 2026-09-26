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
    _protected.clear(cwd)
    _protected.register(cwd, _protected.from_texts(texts))
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
    if not tgt.retargeted:
        return cwd
    ws = yield from _open_team_workspace(tgt, texts, prompt, cwd, session_id,
                                         rctx, sequential)
    if ws is None:
        return cwd
    _af_log.info("team run targets the user-named folder %s (repo %s) on "
                 "branch %s in %s instead of %s", tgt.named, ws.repo,
                 ws.branch, ws.cwd, cwd)
    _protected.register(ws.cwd, _protected.from_texts(texts))
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
    """One Allow/Deny card (the chat jail's approval UX). True on Allow; the
    folder is then granted for the rest of the chat."""
    from aiforge_core.runtime import chat_approve, chat_write_grants
    seq = chat_approve.request(session_id)
    yield {"type": "approval", "id": seq, "name": "team_workspace",
           "args": {"path": folder}, "grant_roots": [folder],
           "reason": reason, "preview": ""}
    decision = chat_approve.wait(session_id)
    if decision.get("note") == "approval timed out":
        yield {"type": "approval_expired", "id": seq, "name": "team_workspace"}
    if decision.get("decision") != "approve":
        return False
    chat_write_grants.grant(session_id, [folder])
    return True


def _granted(session_id, folder) -> bool:
    from aiforge_core.runtime import chat_write_grants
    real = os.path.realpath(folder)
    return any(os.path.realpath(g) == real
               for g in chat_write_grants.granted(session_id))


def _approvals_on(session_id) -> bool:
    from aiforge_core.runtime import chat_approve
    return chat_approve.approvals_required(session_id)


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
        ws = _tw.open_run(folder, prompt, apply=_tw.wants_apply(texts),
                          fresh_repo=tgt.init_needed, session_cwd=cwd)
    except Exception as exc:  # noqa: BLE001
        yield _stop(rctx, f"Could not start the team run in `{folder}`: {exc}")
        return None
    if ask:
        ws.dirty = []              # the user allowed it knowing about them
    return ws
