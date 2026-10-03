"""The facts a final answer ends with: what the harness itself measured.

An answer says "done", "I already did it" or "nothing was changed"; the user
has no way to tell which of these the disk agrees with. This module writes one
short block under the answer from things the harness holds — git, and the tool
calls it ran — and never from the model's words:

* the files that differ from the commit the turn started on;
* the commits made in the turn;
* the commands the turn ran, and the last one that failed;
* when the turn changed nothing: the files this chat changed before;
* the last commit, and whether it has been pushed.

No model call is made for it. A turn that ran no tool in a chat that never
changed a file has no facts to show, and gets no block.
``AIFORGE_CHAT_FACTS_LINE=0`` turns it off.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.turn_facts_line")

HEAD = "Checked by the harness, not by the model"
_MAX_NAMES = 6


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_FACTS_LINE", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _names(paths, limit: int = _MAX_NAMES) -> str:
    paths = [str(p) for p in paths if p]
    shown = ", ".join(f"`{p}`" for p in paths[:limit])
    return shown + (f" (+{len(paths) - limit} more)" if len(paths) > limit else "")


#: What the harness itself writes into a workspace: never the turn's work.
_OWN = (".codegraph/", ".aiforge", ".pytest_cache/", "__pycache__/")


def _own(path: str) -> bool:
    return any(path.startswith(p) or f"/{p}" in path for p in _OWN)


def _changed(cwd, head0) -> "set | None":
    from aiforge_core.runtime.chat_agent._guards.turn_facts import changed_files
    found = changed_files(cwd, head0)
    return None if found is None else {p for p in found if not _own(p)}


def _commits(cwd, head0) -> list:
    """Short ids of the commits made since ``head0``, newest first."""
    if not cwd or not head0:
        return []
    from aiforge_core.runtime.chat_agent._guards.turn_facts import _git
    out = _git(cwd, "log", "--format=%h", f"{head0}..HEAD")
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


def _commands(session_id) -> "tuple[int, int, str]":
    """``(worked, failed, last failure)`` over this turn's actions that were
    not reads."""
    from aiforge_core.runtime import action_log
    acts = [e for e in action_log.entries(action_log.live_steps(session_id))
            if e.get("reads") is None and not e.get("read")]
    failed = [e for e in acts if e.get("ok") is False and not e.get("fixed")]
    worked = sum(1 for e in acts if e.get("ok") is not False or e.get("fixed"))
    last = action_log.strip_markers(action_log.text_of(failed[-1])) if failed else ""
    return worked, len(failed), last.lstrip("✗ ").strip()


def _earlier_files(session_id, cwd) -> list:
    """Files the earlier turns of this chat wrote (their write calls that did
    not fail), newest last, as the calls named them."""
    from aiforge_core.runtime import action_log
    from aiforge_core.runtime.tools.mutating import writes_files
    out: list = []
    for s in action_log._stored_steps(session_id):
        name = str(s.get("name") or "")
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        if not writes_files(name, args) or action_log._ok(s.get("result")) is False:
            continue
        path = str(args.get("path") or args.get("file") or args.get("file_path") or "")
        if cwd and path.startswith(str(cwd).rstrip("/") + "/"):
            path = path[len(str(cwd).rstrip("/")) + 1:]
        if path and path not in out:
            out.append(path)
    return out


def _repo_state(cwd) -> str:
    """The last commit and whether it has been pushed, from git."""
    if not cwd:
        return ""
    from aiforge_core.runtime.chat_agent._guards.turn_facts import _git
    last = (_git(cwd, "log", "-1", "--format=%h") or "").strip()
    if not last:
        return ""
    upstream = (_git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                     "@{u}") or "").strip()
    if upstream:
        ahead = (_git(cwd, "rev-list", "--count", "@{u}..HEAD") or "").strip()
        pushed = (f"pushed to `{upstream}`" if ahead == "0" else
                  f"{ahead} commit(s) not pushed to `{upstream}`" if ahead else "")
    elif not (_git(cwd, "remote") or "").strip():
        pushed = "not pushed (the repository has no remote)"
    else:
        pushed = "not pushed (the branch has no upstream)"
    return f"last commit `{last}`" + (f", {pushed}" if pushed else "")


def parts(session_id, cwd, head0) -> list:
    """The facts as short phrases, in reading order. Empty: nothing to show."""
    out: list = []
    changed = _changed(cwd, head0)
    worked, failed, last = _commands(session_id)
    if changed:
        out.append(f"files changed in this turn: {_names(sorted(changed))}")
    elif changed is not None:
        out.append("no file changed in this turn")
    commits = _commits(cwd, head0)
    if commits:
        n = len(commits)
        out.append(f"{n} commit{'s' if n != 1 else ''} made (`{commits[0]}`)")
    if worked or failed:
        line = f"actions run: {worked} worked"
        if failed:
            line += f", {failed} failed (last: {last[:160]})"
        out.append(line)
    earlier = _earlier_files(session_id, cwd)
    if not changed:
        if earlier:
            out.append(f"changed earlier in this chat: {_names(earlier)}")
        elif not (worked or failed or commits):
            return []                 # a plain answer: there is nothing to check
    if changed or commits or earlier:
        state = _repo_state(cwd)
        if state:
            out.append(state)
    return out


def suffix(session_id, cwd, head0) -> str:
    """The block for the end of a final answer, or ""."""
    if session_id is None or not enabled():
        return ""
    try:
        found = parts(session_id, cwd, head0)
    except Exception as exc:  # noqa: BLE001 — the block never blocks an answer
        log.debug("facts line skipped: %s", exc)
        return ""
    if not found:
        return ""
    return f"\n\n---\n_{HEAD}:_ " + " · ".join(found) + "."


def strip_copied(text: str) -> str:
    """``text`` without a facts block the model wrote itself (copied from an
    earlier answer in its context): only the harness writes one."""
    if HEAD not in (text or ""):
        return text
    kept = [ln for ln in text.splitlines() if HEAD not in ln]
    while kept and kept[-1].strip() in ("", "---"):
        kept.pop()
    return "\n".join(kept)


__all__ = ["HEAD", "enabled", "parts", "strip_copied", "suffix"]
