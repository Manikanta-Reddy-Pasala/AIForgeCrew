"""What a parallel team run may build and where its leftovers go.

* :func:`bind_plan_to_spec` — after the SPEC review, the subtasks follow the
  SPEC's own list and never own a read-only file (see ``_protected`` and
  ``_spec_align``).
* :func:`dirty_overlap_stop` — a user-repo run refuses to plan over files the
  user has uncommitted edits to (the run works from their last commit).
* :func:`seal_run` — before the Changes view and the verdict: put back any
  read-only file a writer touched, then commit what the repair engine left
  uncommitted, so no stray edits are left behind.
"""
from __future__ import annotations

from . import _protected
from ._spec_align import align_to_spec


def bind_plan_to_spec(subs: list, spec_md: str, cwd: str, state: dict):
    """Yields notes; leaves the final list in ``state['subs']``."""
    prot, only = _protected.from_spec(spec_md)
    _protected.register(cwd, prot, only)
    aligned, dropped, added = align_to_spec(subs, spec_md)
    if dropped or added:
        bits = []
        if dropped:
            bits.append("dropped " + ", ".join(map(str, dropped[:6]))
                        + " (not in the reviewed SPEC)")
        if added:
            bits.append("added " + ", ".join(added[:6]) + " (listed in SPEC)")
        yield {"type": "thought", "role": "planner",
               "text": "Plan follows SPEC.md — " + "; ".join(bits) + "."}
    kept, blocked = _protected.filter_subtasks(aligned, cwd)
    if blocked:
        yield {"type": "thought", "role": "planner",
               "text": "Read-only files are not planned for editing: "
                       + ", ".join(f"`{b}`" for b in blocked[:6])
                       + " — you or the SPEC said not to change them."}
    if not kept and aligned:
        yield {"type": "message", "text":
               "Every planned change is to a file you asked me not to edit ("
               + ", ".join(f"`{b}`" for b in blocked[:6]) + "), so nothing was "
               "built. Tell me which file may change, or lift the restriction."}
    state["subs"] = kept


def dirty_overlap_stop(cwd: str, subs: list):
    """A user-repo run works from the user's last commit. If they have
    uncommitted edits to a file the plan changes, stop and say so instead of
    building over a stale copy. Yields the message; returns True to stop."""
    from aiforge_core.runtime import team_workspace
    ws = team_workspace.for_cwd(cwd)
    if ws is None or not ws.dirty:
        return False
    # _norm strips a leading "./" only — never the dot of ".github/…".
    planned = {_protected._norm(s.get("path") or "") for s in subs}
    hit = sorted(planned & {_protected._norm(d) for d in ws.dirty})
    if not hit:
        return False
    yield {"type": "message", "awaiting_input": True, "text":
           "You have uncommitted changes to " + ", ".join(f"`{h}`" for h in hit)
           + f" in `{ws.repo}`, and the team would change the same file(s) "
           "starting from your last commit — your edits would not be in its "
           "result. Commit or stash them, then ask again."}
    return True


def seal_run(cwd: str, start_sha: str):
    """Yields notes; returns the read-only files it put back (the caller's
    test result is stale then). See module docstring."""
    from aiforge_core.runtime import team_workspace

    from ._planning import _is_managed_workspace
    if team_workspace.for_cwd(cwd) is None and not _is_managed_workspace(cwd):
        return []       # a user's own checkout: never reset or commit there
    back = _protected.revert(cwd, start_sha or "HEAD")
    if back:
        yield {"type": "thought", "role": "system",
               "text": "Put back read-only file(s) a writer changed: "
                       + ", ".join(f"`{b}`" for b in back[:8])}
    try:
        done = team_workspace.seal(cwd, "reconcile: integration repairs")
    except team_workspace.SealError as exc:
        done = []
        yield {"type": "thought", "role": "system",
               "text": f"Could not commit the repair pass's edits ({exc}); "
                       "they stay uncommitted in the run's worktree."}
    if done:
        yield {"type": "thought", "role": "system",
               "text": f"Committed the repair pass's edits ({len(done)} file(s)) "
                       "— nothing is left uncommitted."}
    return back


def run_note(cwd: str) -> str:
    """The branch note for a user-repo run, once."""
    from aiforge_core.runtime import team_workspace
    ws = team_workspace.for_cwd(cwd)
    if ws is None:
        return ""
    ws.announced = True
    return "\n\n" + ws.summary()


__all__ = ["bind_plan_to_spec", "dirty_overlap_stop", "run_note", "seal_run"]
