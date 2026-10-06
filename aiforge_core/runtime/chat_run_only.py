"""Files a chat made only to do its work stay out of its commit.

A chat's worktree is committed after every turn, whole. The agent is told to
keep a helper script, a one-off test driver or a probe in its scratch folder; a
small model writes ``check_fix.py`` next to the code anyway, runs it, and the
turn's commit then carries it to the user's branch.

A file is held out of the commit only when ALL of this is true:

* it is new — never committed on the chat's branch — and sits in the top
  folder of the project (a file placed in a folder was put somewhere);
* a command of the turn RAN it, in the plain form (``python check_fix.py``,
  ``bash try.sh``, ``./probe.sh``, ``pytest test_tmp.py``), and was not
  refused or failed to start — a lint or a syntax check is not a run;
* nothing else in the project names it;
* neither the user's words nor the agent's answer name it, and the user did
  not ask for a script, a tool, a migration, an example…;
* it is not a test the user asked for, or one in a project that keeps its
  tests in the top folder;
* the turn also changed a file the project already had (a turn whose only
  product is new files made them as its work).

Any doubt keeps the file in the commit, as before. A held file is not moved or
deleted: it stays in the worktree, untracked, and is committed the moment
something refers to it, the user names it, the agent writes to it again or
stages it itself, a team turn runs in the chat, or the chat is deleted.
``AIFORGE_CHAT_HOLD_HELPERS=0`` turns this off.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

log = logging.getLogger(__name__)

#: Interpreters: ``<interpreter> <file> …`` runs the file. Anything between
#: the two (``-m py_compile``, ``-n``, ``--check``, ``-c``) is not a plain run
#: and is not read as one — a file that was only linted is part of the change.
_INTERPRETERS = ("python", "pypy", "bash", "sh", "zsh", "node", "ruby", "perl", "php")
_TEST_NAMES = ("test_", "tests_")
_TEST_MARKS = ("_test.", ".test.", ".spec.", "_spec.")
_MAX_FILES = 40
#: More than this many at once is not "a helper": hold none (and bound the cost).
_MAX_HELD = 6
#: The user asked for something that is, or may be, a file to run.
_ASKED_FOR = re.compile(
    r"\b(scripts?|tools?|cli|commands?|utilit(?:y|ies)|helpers?|migrat\w*|examples?|demos?|"
    r"benchmarks?|runners?|jobs?|tasks?|hooks?|servers?|entry\s*points?|main)\b", re.IGNORECASE)
_ASKED_TEST = re.compile(r"\b(tests?|specs?|testing|coverage|tdd)\b", re.IGNORECASE)


class _GitFailed(RuntimeError):
    """git did not answer: nothing is known, so nothing is held."""


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_HOLD_HELPERS", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _git(args, cwd, timeout: float = 30, ok=(0,)) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _GitFailed(str(exc)) from exc
    if p.returncode not in ok:
        raise _GitFailed(f"git {args[0]} exited {p.returncode}")
    return p.stdout or ""


def _is_interpreter(head: str) -> bool:
    base = os.path.basename(head)
    return any(base == r or (base.startswith(r) and base[len(r):].replace(".", "").isdigit())
               for r in _INTERPRETERS)             # python3.12, node20


def _rel(tok: str, wt: str) -> str:
    """``tok`` as a file of the worktree, relative to it ('' when it is not one)."""
    tok = tok.split("::", 1)[0]                      # pytest node id
    if not tok or tok.startswith("-"):
        return ""
    path = os.path.normpath(tok if os.path.isabs(tok) else os.path.join(wt, tok))
    root = os.path.normpath(wt)
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return ""
    return path[len(root) + 1:].replace(os.sep, "/")


def ran(steps, wt: str) -> set:
    """Worktree-relative files a shell command in ``steps`` ran as a program.
    Reading stops at the first ``cd``: a shell can keep its folder from one
    call to the next, and a relative path then means another place."""
    from aiforge_core.runtime import shell_writes as sw
    out: set = set()
    for s in steps or []:
        if not isinstance(s, dict) or str(s.get("name") or "") not in sw.SHELL_TOOLS:
            continue
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        cwd = str(args.get("cwd") or args.get("workdir") or "")
        if cwd and os.path.normpath(cwd) != os.path.normpath(wt):
            continue
        res = s.get("result")
        if not isinstance(res, dict) or res.get("denied") or res.get("rejected") \
                or res.get("blocked") or res.get("ok") is False and not _exited(res):
            continue                                 # refused, or never started
        for line in sw._strip_heredocs(sw.command_of(args)):
            for seg in sw._segments(sw._tokens(line)):
                seg = sw._strip_prefixes(seg)
                if not seg:
                    continue
                if os.path.basename(seg[0]) in ("cd", "pushd", "popd"):
                    return out
                out |= _run_targets(seg, wt)
    return out


def _exited(res: dict) -> bool:
    """The command started and ended with a code (a failing check script DID run)."""
    return any(isinstance(res.get(k), int) for k in ("exit_code", "returncode", "code", "rc"))


def _run_targets(seg: list, wt: str) -> set:
    head, rest = seg[0], seg[1:]
    base = os.path.basename(head)
    if base in ("pytest", "py.test") or (_is_interpreter(head) and rest[:2] == ["-m", "pytest"]):
        # A test runner runs every file it is given.
        return {r for r in (_rel(t, wt) for t in rest) if r}
    if _is_interpreter(head):
        rel = _rel(rest[0], wt) if rest else ""
        return {rel} if rel else set()
    if "/" in head:                                  # `./probe.sh arg`
        rel = _rel(head, wt)
        return {rel} if rel else set()
    return set()


def _named_elsewhere(rel: str, wt: str, others: set, pathspecs) -> bool:
    """True when a file other than ``rel`` (and the other candidates) names it:
    by file name, or by its name without the extension (an import)."""
    base = os.path.basename(rel)
    stem = os.path.splitext(base)[0]
    needles = [["-F", "-e", base]]
    if stem and stem != base:
        needles.append(["-w", "-F", "-e", stem])
    for needle in needles:
        hits = _git(["grep", "-l", "-I", "-z", "--untracked", *needle, "--", *pathspecs],
                    wt, 20, ok=(0, 1))
        if any(h and h != rel and h not in others for h in hits.split("\0")):
            return True
    return False


def _test_named(rel: str) -> bool:
    base = os.path.basename(rel).lower()
    return base.startswith(_TEST_NAMES) or any(m in base for m in _TEST_MARKS)


def _projects_test(rel: str, wt: str, user_words) -> bool:
    """A test that belongs to the project: one the user asked for, or one in a
    project that keeps tests where this one is."""
    if not _test_named(rel):
        return False
    if any(_ASKED_TEST.search(t or "") for t in user_words):
        return True
    tracked = _git(["ls-files", "-z", "--", ":(top,glob)*"], wt).split("\0")
    return any(_test_named(t) for t in tracked if t)


def _said(rel: str, texts) -> bool:
    base = os.path.basename(rel).lower()
    return any(base in (t or "").lower() for t in texts)


def _user_words(steps, prompt: str) -> list:
    """The turn's message and what the user typed while it ran."""
    out = [prompt or ""]
    for s in steps or []:
        if isinstance(s, dict) and s.get("to"):
            out.append(str(s.get("to")))
        if isinstance(s, dict) and s.get("role") == "steer":
            out.append(str(s.get("text") or ""))
    return out


def _written(steps, wt: str) -> set:
    """Files a write tool or a shell command of the turn wrote to: the agent
    worked ON them."""
    out: set = set()
    root = os.path.realpath(wt)

    def add(raw: str) -> None:
        if not raw:
            return
        path = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(root, raw))
        if path.startswith(root + os.sep):
            out.add(path[len(root) + 1:].replace(os.sep, "/"))
    try:
        from aiforge_core.runtime import shell_writes as sw
        from aiforge_core.runtime.turn_facts_line import _write_paths
        for p in _write_paths(steps, wt):
            add(p)
        for s in steps or []:
            if isinstance(s, dict) and str(s.get("name") or "") in sw.SHELL_TOOLS \
                    and isinstance(s.get("args"), dict):
                for p in sw.shell_write_targets(sw.command_of(s["args"]), wt):
                    add(p)
    except Exception:  # noqa: BLE001
        pass
    return out


def untracked(wt: str, pathspecs) -> list:
    out = _git(["ls-files", "--others", "--exclude-standard", "-z", "--", *pathspecs], wt)
    return [p for p in out.split("\0") if p]


def held(wt: str, steps, prompt: str, final_text: str, pathspecs, before=()) -> list:
    """The new files of the worktree to leave out of the commit: the ones this
    turn only ran, and those held on an earlier turn that still qualify (a held
    file the agent has written to again since is work in progress: released)."""
    if not enabled():
        return []
    try:
        new = untracked(wt, pathspecs)
        if not new or len(new) > _MAX_FILES:
            return []
        was_run = ran(steps, wt) | ({p for p in before if p} - _written(steps, wt))
        # The facts block under an answer lists every changed file: the
        # harness wrote it, the agent did not name the file.
        from aiforge_core.runtime import turn_facts_line
        answer = "\n".join(ln for ln in (final_text or "").splitlines()
                           if turn_facts_line.HEAD not in ln)
        user = _user_words(steps, prompt)
        if any(_ASKED_FOR.search(t or "") for t in user):
            return []
        texts = [*user, answer]
        candidates = {p for p in new if p in was_run and "/" not in p
                      and not _said(p, texts) and not _projects_test(p, wt, user)}
        if len(candidates) > _MAX_HELD:
            return []
        return sorted(p for p in candidates
                      if not _named_elsewhere(p, wt, candidates - {p}, pathspecs))
    except Exception as exc:  # noqa: BLE001 — in doubt, commit as before
        log.debug("run-only files not worked out in %s: %s", wt, exc)
        return []


__all__ = ["enabled", "held", "ran", "untracked"]
