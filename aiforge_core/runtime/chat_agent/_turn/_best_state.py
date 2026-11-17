"""Never end a turn worse than its best tested state.

A real Java service turn ran 124 minutes, got its tests to their best
point, kept editing, and was stopped by the time limit with 4 failing where
it had started with 1 (13/18 against 16/18 before it touched anything): the
last edits broke what an earlier state had right, and the stop handed that
back.

Each time a test command reaches a new best (fewest failures for that exact
command, run on the code as it then was), the working tree is snapshotted
cheaply (``git stash create`` for tracked files, untracked files copied).
When the turn is stopped by its time or step limit, the same command is run
once more if code changed after its last run; if the result is worse than the
best, the best snapshot is restored and the turn says so. Off:
``AIFORGE_CHAT_KEEP_BEST=0``. Soft-fail throughout.
"""
from __future__ import annotations

import os
import re
import subprocess

_MAX_UNTRACKED_BYTES = 2_000_000
_PIPE = re.compile(r"\s*(?:2>&1\s*)?\|\s*(?:tail|head|grep|egrep|less|more|cat|tee|sed|awk)\b.*$")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_KEEP_BEST", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _git(top: str, *args, timeout: int = 30) -> "subprocess.CompletedProcess | None":
    try:
        return subprocess.run(["git", "--no-optional-locks", "-C", top, *args],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _top(cwd: str) -> str:
    p = _git(cwd or ".", "rev-parse", "--show-toplevel")
    return p.stdout.strip() if p is not None and p.returncode == 0 else ""


def key_of(cmd: str) -> str:
    """The command as a key: display pipes dropped, spaces collapsed."""
    return " ".join(_PIPE.sub("", cmd or "").split())


def _snapshot(top: str) -> "dict | None":
    p = _git(top, "stash", "create")
    if p is None or p.returncode != 0:
        return None
    sha = p.stdout.strip()
    if not sha:                                   # nothing changed: HEAD is the state
        h = _git(top, "rev-parse", "HEAD")
        sha = h.stdout.strip() if h is not None and h.returncode == 0 else ""
    if not sha:
        return None
    untracked, size = {}, 0
    u = _git(top, "ls-files", "--others", "--exclude-standard")
    for rel in (u.stdout.splitlines() if u is not None and u.returncode == 0 else []):
        full = os.path.join(top, rel)
        try:
            n = os.path.getsize(full)
            if size + n > _MAX_UNTRACKED_BYTES:
                continue
            with open(full, "rb") as fh:
                untracked[rel] = fh.read()
            size += n
        except OSError:
            continue
    return {"sha": sha, "untracked": untracked}


def note(st, cmd: str, fails: int, cwd: str) -> None:
    """A test run of ``cmd`` on the current code had ``fails`` failures."""
    if not _on():
        return
    try:
        k = key_of(cmd)
        if not k:
            return
        edits = int(getattr(st, "edits_made", 0) or 0)
        last = st.__dict__.setdefault("best_last", {})
        last[k] = {"fails": int(fails), "edits": edits, "cmd": cmd}
        best = st.__dict__.setdefault("best_state", {})
        cur = best.get(k)
        if cur is not None and cur["fails"] <= int(fails):
            return
        top = _top(getattr(st, "cwd", "") or cwd)
        snap = _snapshot(top) if top else None
        if snap:
            best[k] = {"fails": int(fails), "edits": edits, "top": top, **snap}
    except Exception:  # noqa: BLE001
        return


def _fresh_fails(st, k: str, info: dict) -> "int | None":
    """The command's failures on the code as it is now (re-run if edits came
    after its last run)."""
    if info["edits"] == int(getattr(st, "edits_made", 0) or 0):
        return info["fails"]
    try:
        from aiforge_core.runtime.parallel_subtasks import _fail_count
        from aiforge_core.runtime.failure_signature import _ANSI
        from ._done_watch import _run
        from ._outcomes import _TESTS_RAN
        code, out = _run(_PIPE.sub("", info["cmd"]).strip(),
                         str(getattr(st, "cwd", "") or "."), timeout=600)
        out = _ANSI.sub("", out or "")
        if not _TESTS_RAN.search(out):
            return None
        return 0 if code == 0 and _fail_count(out) == 0 else max(1, _fail_count(out))
    except Exception:  # noqa: BLE001
        return None


def _restore(b: dict) -> bool:
    top, sha = b["top"], b["sha"]
    d = _git(top, "diff", "--name-only", sha)
    files = [f for f in (d.stdout.splitlines() if d is not None and d.returncode == 0 else []) if f]
    if files:
        p = _git(top, "checkout", sha, "--", *files, timeout=60)
        if p is None or p.returncode != 0:
            return False
    for rel, data in (b.get("untracked") or {}).items():
        try:
            full = os.path.join(top, rel)
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(data)
        except OSError:
            return False
    return True


#: A test program that died, not one that reported failures.
_CRASH = re.compile(r"Segmentation fault|Floating point exception|core dumped|Aborted|"
                    r"Bus error|Illegal instruction|stack overflow|panicked at .*SIGSEGV", re.I)


def _crashes(cmd: str, cwd: str) -> "bool | None":
    """``cmd`` run in ``cwd`` crashed (signal exit or crash text); None when it
    could not be run."""
    try:
        from ._done_watch import _run
        code, out = _run(_PIPE.sub("", cmd).strip(), cwd, timeout=600)
    except Exception:  # noqa: BLE001
        return None
    return (code is not None and (code >= 128 or code < 0)) or bool(_CRASH.search(out or ""))


def _revert_crash(st):
    """At give-up with no recorded best state: when the turn's last test
    command now crashes and the same command did not crash on the code the
    work started from, put the changed files back to that code. A real C++
    run gave up after a fix that divided by zero; the test program crashed and
    every test was lost, where the start passed nearly all of them."""
    last = getattr(st, "last_test_cmd", None)
    cwd = str(getattr(st, "cwd", "") or "")
    if not last or not cwd:
        return
    from aiforge_core.runtime.shell_writes import command_of
    cmd = command_of(last[1]) or ""
    if not cmd or not _crashes(cmd, cwd):
        return
    from ._contract_gate import work_base
    base = work_base(st)
    top = _top(cwd)
    if not base or not top:
        return
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="af-crash-base-")
    added = False
    try:
        r = _git(top, "worktree", "add", "--detach", tmp, base, timeout=60)
        added = r is not None and r.returncode == 0
        if not added:
            return
        sub = os.path.relpath(os.path.realpath(cwd), os.path.realpath(top))
        here = tmp if sub in (".", "") or sub.startswith("..") else os.path.join(tmp, sub)
        line = cmd.replace(os.path.realpath(top), tmp).replace(top, tmp)
        if _crashes(line, here) is not False:
            return                                   # the start crashed too, or unknown
    finally:
        if added:
            _git(top, "worktree", "remove", "--force", tmp, timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)
    d = _git(top, "diff", "--name-only", base)
    files = [f for f in (d.stdout.splitlines() if d is not None and d.returncode == 0 else []) if f]
    if not files:
        return
    kept = _git(top, "stash", "create")             # the crashing version stays reachable
    kept_sha = kept.stdout.strip() if kept is not None and kept.returncode == 0 else ""
    p = _git(top, "checkout", base, "--", *files, timeout=60)
    if p is None or p.returncode != 0:
        return
    st.best_restored = (key_of(cmd), "no crash", "crash")
    yield {"type": "thought", "role": "system",
           "text": f"↩ gave back the original code of {len(files)} file(s): after this "
                   f"turn's changes `{key_of(cmd)[:80]}` crashes, and it did not before"
                   + (f" (the changed version is kept: `git stash apply {kept_sha[:12]}`)"
                      if kept_sha else "")}


def restore_if_worse(st):
    """Generator, at a limit stop: put back the best tested state when the
    code as it stands tests worse."""
    if not _on():
        return
    if not (getattr(st, "best_state", None) or {}):
        try:
            yield from _revert_crash(st)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        best = getattr(st, "best_state", None) or {}
        last = getattr(st, "best_last", None) or {}
        for k, info in last.items():
            b = best.get(k)
            if not b or b["edits"] >= int(getattr(st, "edits_made", 0) or 0):
                continue                       # the best IS the current code
            now = _fresh_fails(st, k, info)
            if now is None or now <= b["fails"]:
                continue
            if _restore(b):
                yield {"type": "thought", "role": "system",
                       "text": f"↩ restored the best tested state of this turn: `{k[:90]}` had "
                               f"{b['fails']} failing there and {now} now"}
                st.best_restored = (k, b["fails"], now)
            return
    except Exception:  # noqa: BLE001
        return
