"""Testing on another machine sees only what was pushed.

A frequent real setup: AIForge runs in Docker on a laptop, the user says "test
it on the VM". The chat's edits are on its own local branch
(``aiforge/chat-<id>-…``), not even committed until the turn ends, so the VM
pulled the old code, or a branch name it never had ("couldn't find remote
ref", "pathspec … did not match"), and the agent made the same mistake again
the next time.

When a command reaches another host (ssh / scp / rsync) and runs git or a
build/test there, this adds one note to the result if the local work is not
on the remote yet — uncommitted edits, commits not pushed, a branch with no
upstream — or the remote says the branch does not exist: the local branch's
exact name and the commands that get the code there.
Off: ``AIFORGE_CHAT_REMOTE_SYNC_NOTE=0``.
"""
from __future__ import annotations

import os
import re
import subprocess

_REMOTE_WORK = re.compile(
    r"\bgit\s+(?:pull|fetch|checkout|switch|reset|clone|merge|rebase)\b|"
    r"\b(?:mvnw?|gradlew?|npm|yarn|pnpm|pytest|tox|cargo|go\s+(?:test|build)|make|ctest|dotnet)\b")
_BRANCH_MISSING = re.compile(
    r"couldn't find remote ref|did not match any file\(s\) known to git|"
    r"not a valid object name|unknown revision|invalid reference|"
    r"pathspec '[^']+' did not match|Remote branch \S+ not found", re.I)


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_REMOTE_SYNC_NOTE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _git(cwd: str, *args) -> "tuple[int, str]":
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", cwd, *args],
                           capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return 1, ""


def local_state(cwd: str) -> "dict | None":
    """Branch, upstream, unpushed commits and uncommitted files of ``cwd``'s repo."""
    code, branch = _git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    if code != 0 or not branch:
        return None
    code, upstream = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    upstream = upstream if code == 0 else ""
    unpushed = 0
    if upstream:
        _c, n = _git(cwd, "rev-list", "--count", f"{upstream}..HEAD")
        unpushed = int(n) if n.isdigit() else 0
    _c, st = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    dirty = [ln[3:] for ln in st.splitlines() if ln.strip()]
    _c, remotes = _git(cwd, "remote")
    return {"branch": branch, "upstream": upstream, "unpushed": unpushed, "dirty": dirty,
            "remote": (remotes.split() or ["origin"])[0] if remotes else ""}


def note(cmd: str, output: str, cwd: str) -> "str | None":
    """The note for a command that reached another host, or None."""
    if not enabled() or not cmd or not cwd:
        return None
    try:
        from aiforge_core.runtime.ops_memory import _targets
        if not _targets(cmd):
            return None
    except Exception:  # noqa: BLE001
        return None
    missing = bool(_BRANCH_MISSING.search(output or ""))
    if not missing and not _REMOTE_WORK.search(cmd):
        return None
    s = local_state(cwd)
    if not s:
        return None
    if not s["remote"]:
        if not missing:
            return None
        return (f"remote check: this repo has no git remote, so the other machine cannot fetch "
                f"`{s['branch']}`. Copy the changed files there (scp/rsync) instead of pulling.")
    gaps = []
    if s["dirty"]:
        shown = ", ".join(s["dirty"][:4]) + (" …" if len(s["dirty"]) > 4 else "")
        gaps.append(f"{len(s['dirty'])} file(s) changed but not committed ({shown})")
    if not s["upstream"]:
        gaps.append(f"branch `{s['branch']}` was never pushed")
    elif s["unpushed"]:
        gaps.append(f"{s['unpushed']} commit(s) on `{s['branch']}` not pushed")
    if not gaps and not missing:
        return None
    r, b = s["remote"], s["branch"]
    steps = []
    if s["dirty"]:
        steps.append("`git add -A && git commit -m \"…\"`")
    if s["dirty"] or not s["upstream"] or s["unpushed"]:
        steps.append(f"`git push -u {r} {b}` (here)")
    steps.append(f"on the host: `git fetch {r} {b} && git checkout {b}` "
                 f"(or `git checkout -B {b} {r}/{b}` if it exists there already)")
    why = "; ".join(gaps) if gaps else f"the host does not have branch `{b}`"
    return (f"remote check: the other machine only sees what is pushed. Here: {why}. "
            f"Your work is on branch `{b}` — do this, then run the test there again: "
            + " → ".join(steps) + ".")


__all__ = ["note", "local_state", "enabled"]
