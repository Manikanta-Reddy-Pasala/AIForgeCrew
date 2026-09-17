"""Merging subtask branches: dirty-tree warnings and resolving conflicts hunk by hunk."""
from __future__ import annotations

import os
import re

from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS


def _pkg():
    """The parent module, looked up on each call so a name patched there is the
    one used here."""
    import aiforge_core.runtime.parallel_subtasks._worktree as package
    return package


def _dirty_warning(cwd: str) -> str | None:
    """B3 — warn (don't block) when ``cwd`` has uncommitted changes (EXCLUDING
    the agent's own artifacts) that a winner/branch merge could collide with.

    Returns a clear operator-facing message, or None when the tree is clean /
    the check itself fails. Best-effort: the artifact pathspecs are excluded so
    a stray ``.aiforge`` file never trips the warning."""
    try:
        # ``.gitignore`` is excluded: _ensure_git_workspace appends the
        # agent-artifact lines via ensure_artifact_gitignore BEFORE this check,
        # so on every default run the tree shows ` M .gitignore` and would
        # falsely warn. The agent's own gitignore edit isn't an operator change.
        st = _pkg()._git(["status", "--porcelain", "--", ".", *_EXCLUDE_PATHSPECS,
                   ":(exclude).gitignore"], cwd)
    except Exception:  # noqa: BLE001
        return None
    if (st.stdout or "").strip():
        return ("workspace has uncommitted changes — merge may fail; "
                "commit or stash first")
    return None


def _conflict_hunks(text: str) -> list[dict]:
    """Every git conflict hunk, as ``{head, incoming, block, span}``.

    A line scan with running offsets, not
    ``<<<<<<<[^\\n]*\\n(.*?)\\n=======\\n(.*?)\\n>>>>>>>`` under DOTALL: two lazy
    quantifiers spanning a whole file is the denial-of-service shape a scanner
    asks about, and conflict markers are whole lines anyway. ``span`` is the
    character range of the whole hunk, so breadcrumbs and the replacement keep
    working exactly as they did.
    """
    hunks: list[dict] = []
    pos = 0
    start_pos = None
    head: list[str] | None = None
    incoming: list[str] | None = None
    for line in (text or "").splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped.startswith("<<<<<<<"):
            start_pos, head, incoming = pos, [], None
        elif head is not None and incoming is None and stripped == "=======":
            incoming = []
        elif incoming is not None and stripped.startswith(">>>>>>>"):
            end = pos + len(line)
            hunks.append({"head": "\n".join(head or []),
                          "incoming": "\n".join(incoming),
                          "block": text[start_pos:end],
                          "span": (start_pos, end)})
            start_pos = head = incoming = None
        elif incoming is not None:
            incoming.append(stripped)
        elif head is not None:
            head.append(stripped)
        pos += len(line)
    return hunks


def _hunk_breadcrumbs(content: str, span: tuple, n: int) -> tuple:
    """N lines of ambient code above/below a conflict hunk — grounds the model's
    indentation + parameter bindings (self.width vs a param, the parent class's
    base indent) so an out-of-context resolution doesn't break syntax."""
    start_char, end_char = span
    line_start = content[:start_char].count("\n")
    line_end = content[:end_char].count("\n")
    lines = content.splitlines(keepends=True)
    above = "".join(lines[max(0, line_start - n):line_start])
    below = "".join(lines[line_end + 1:min(len(lines), line_end + 1 + n)])
    return above, below


def _resolve_conflict_hunk(goal: str, path: str, head: str, incoming: str,
                           above: str = "", below: str = "", attempt: int = 1) -> str:
    """Minimal-context conflict resolver: feed ONLY this hunk (+ goal + a few
    breadcrumb lines) and get back the merged block — no whole file, no markers/
    fences. On a retry (attempt>1) it's told the last try broke syntax + given
    wider ambient scope."""
    from aiforge_core.llm.client import complete as _complete
    retry = ("\nCRITICAL: your previous resolution broke syntax/compilation. More "
             "surrounding code is shown below — match its brackets, indentation and "
             "variable/parameter names EXACTLY.\n" if attempt > 1 else "")
    prompt = (
        "You are a stateless Git conflict-resolution compilation step. Merge the "
        "two versions of the CONFLICTING HUNK into ONE syntactically-correct result "
        "that fulfils the goal and keeps the valid features of BOTH sides, lining up "
        "seamlessly with the ambient code. Output ONLY the raw replacement block — "
        "no git markers, no ``` fences, no prose." + retry + "\n\n"
        + (f"GOAL: {goal[:600]}\n\n" if goal else "")
        + f"FILE: {path}\n\n"
        + (f"[AMBIENT CODE ABOVE]\n{above}\n\n" if above else "")
        + f"[CONFLICTING HUNK]\n<<<<<<< HEAD\n{head}\n=======\n{incoming}\n>>>>>>> incoming\n\n"
        + (f"[AMBIENT CODE BELOW]\n{below}\n" if below else ""))
    try:
        out = _complete("doer", [
            {"role": "system", "content": "Output only the resolved code block, "
             "nothing else."},
            {"role": "user", "content": prompt}], max_tokens=2048) or ""
    except Exception:  # noqa: BLE001
        return ""
    # Strip surrounding BLANK LINES only — never leading spaces. The block is
    # spliced back into the file verbatim, so eating the first line's indent
    # breaks the very indentation the prompt asks the model to match, and the
    # file then fails the syntax check that follows.
    # One alternation used to do both fences: `^[ \t]*```\w*\n?|\n?[ \t]*```[ \t]*$`.
    # The `|` sat OUTSIDE both anchors, so which anchor bound which branch was
    # anyone's guess to read (S5850), and the two optional `\n?`s around a
    # `[ \t]*` run gave it super-linear backtracking on a long block (S8786).
    # A fence the model emits is always a line of its own, so say that: drop any
    # line that is nothing but a fence, with an optional language tag. One
    # `[ \t]*` run on each side and no third in the middle — two whitespace
    # runs separated only by an optional `\w*` is exactly the ambiguity that
    # made the old pattern backtrack.
    out = re.sub(r"^[ \t]*```\w*[ \t]*$\n?", "",
                 out.strip("\n").rstrip(), flags=re.M)
    out = re.sub(r"^\s*(<<<<<<<|=======|>>>>>>>).*$", "", out, flags=re.M)
    return out.strip("\n")


def _resolve_all_hunks(backup: str, goal: str, relpath: str, budget: int,
                       attempt: int) -> str:
    """Resolve every conflict hunk in ``backup`` with ``budget`` lines of
    breadcrumb context; a hunk the model can't resolve falls back to keeping
    HEAD. Returns the rewritten text (may still carry markers → caller widens)."""
    new = backup
    for hunk in _conflict_hunks(backup):
        above, below = _hunk_breadcrumbs(backup, hunk["span"], budget)
        res = _pkg()._resolve_conflict_hunk(goal, relpath, hunk["head"],
                                     hunk["incoming"], above, below, attempt)
        if not res:
            res = hunk["head"]                  # fallback: keep HEAD
        new = new.replace(hunk["block"], res + "\n", 1)
    return new


def _still_conflicted(text: str) -> bool:
    """True when the resolved text still carries git conflict markers."""
    return "<<<<<<<" in text or "=======" in text or ">>>>>>>" in text


def _syntax_ok(relpath: str, text: str) -> bool:
    """Whether ``text`` passes the syntax guard for ``relpath`` (fails open on a
    guard error)."""
    try:
        from aiforge_core.runtime.syntax_guard import validate_syntax
        ok, _ = validate_syntax(relpath, text)
        return ok
    except Exception:  # noqa: BLE001
        return True


def _resolve_file_conflicts(repo: str, relpath: str, goal: str,
                            max_attempts: int = 3) -> bool:
    """Widen-context-retry state machine for ONE conflicted file: resolve every
    hunk with breadcrumbs; if the file fails syntax, roll back to the conflicted
    state and retry with a wider breadcrumb budget (5 → 15 → 25). Deterministic
    rollback; only a small token tax per widen."""
    fp = os.path.join(repo, relpath)
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            backup = fh.read()
    except Exception:  # noqa: BLE001
        return False
    budget = 5
    for attempt in range(1, max_attempts + 1):
        new = _resolve_all_hunks(backup, goal, relpath, budget, attempt)
        if _still_conflicted(new):
            budget += 10
            continue                                # markers left → widen + retry
        if not _syntax_ok(relpath, new):
            budget += 10                            # syntax fail → widen + retry
            continue
        try:
            with open(fp, "w", encoding="utf-8") as fh:
                fh.write(new)
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def _resolve_conflicts(repo: str, goal: str) -> bool:
    """Resolve every conflicted file via the breadcrumb + widen-retry machine,
    git-add each. Returns True only if ALL files resolve cleanly (else abort)."""
    pkg = _pkg()
    p = pkg._git(["diff", "--name-only", "--diff-filter=U"], repo)
    files = [f for f in p.stdout.splitlines() if f.strip()]
    if not files:
        return False
    for f in files:
        if not pkg._resolve_file_conflicts(repo, f, goal):
            return False
        pkg._git(["add", "--", f], repo)
    return True


def _merge_branch(repo: str, _base_branch: str, branch: str) -> tuple[bool, str]:
    """Merge ``branch`` into ``base_branch`` (checked out in ``repo``). Returns
    (ok, info). On conflict, RESOLVES the hunks (minimal-context) rather than
    dropping the subtask's work; aborts only if resolution fails."""
    pkg = _pkg()
    p = pkg._git(["merge", "--no-edit", branch], repo)
    if p.returncode == 0:
        return True, "merged"
    # conflict → try to auto-resolve the hunks (the safety valve for concurrency)
    if os.environ.get("AIFORGE_RESOLVE_CONFLICTS", "1") not in ("0", "false"):
        try:
            if pkg._resolve_conflicts(repo, pkg._spec_goal(repo)):
                c = pkg._git(["commit", "--no-edit", "-m",
                          "resolve: automated subtask merge conflict"], repo)
                if c.returncode == 0:
                    return True, "merged (conflicts auto-resolved)"
        except Exception:  # noqa: BLE001
            pass
    # resolution failed / disabled → abort to leave the base branch clean
    pkg._git(["merge", "--abort"], repo)
    return False, (p.stdout + p.stderr)[:300]


# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
