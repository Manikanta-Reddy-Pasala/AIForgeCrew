"""A FINAL answer checked against what this turn left behind, for a turn that
DID change files.

``file_edit`` and ``zero_edit`` cover a turn that changed nothing. A turn that
changed SOME files slipped past both. Live ("continue simplifying all files"):
the agent rewrote two of four files, its own check command failed twice after
the last write, and the answer read "All files simplified … verified all
green", with a before/after row for two files no write had touched.

Two checks, each asked of the model at most once per turn. Both use facts the
harness holds, not the model's word:

* :class:`UnchangedFileClaimGuard` — the answer says a named file was changed,
  and that file is identical to what the turn started with (git: the commit
  the turn began on against the tree now, committed or not).
* :class:`UnpassedCheckGuard` — after the last file change a command the agent
  ran ended with an error, and no later run of that program passed.

The model gets the facts in one harness note and decides: do the missing work,
correct the answer, or reply with the single word ``SAME`` (the answer stands
as written). After ``SAME`` — or a new answer that repeats the claim — the
facts are put under the answer in one line, so the user is never left with a
statement the disk contradicts. No git, no session, plan / builder runs: no
check.

``AIFORGE_CHAT_FILE_CLAIM_GUARD=0`` / ``AIFORGE_CHAT_FAILED_CHECK_GUARD=0``
turn them off.
"""
from __future__ import annotations

import os
import re
import subprocess

from .file_edit import _RECAP_STRONG_RE, _SENT_SPLIT, _recap_before
from .zero_edit import _says_same

# ── which files the turn changed ─────────────────────────────────────────────


def _git(cwd, *args) -> "str | None":
    try:
        out = subprocess.run(["git", "--no-optional-locks", *args], cwd=str(cwd),
                             capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — a git hiccup never breaks a turn
        return None
    return out.stdout if out.returncode == 0 else None


def changed_files(cwd, head0) -> "set | None":
    """Paths that differ from the commit the turn started on (committed since,
    or still uncommitted, or new). None when git cannot say."""
    if not cwd or not head0:
        return None
    diff = _git(cwd, "diff", "--name-only", "--no-renames", head0)
    status = _git(cwd, "status", "--porcelain", "--untracked-files=all")
    if diff is None or status is None:
        return None
    out = {ln.strip() for ln in diff.splitlines() if ln.strip()}
    for ln in status.splitlines():
        path = ln[3:].strip()
        if " -> " in path:
            out.update(p.strip().strip('"') for p in path.split(" -> "))
        elif path:
            out.add(path.strip('"'))
    return out


# ── which files the answer says it changed ───────────────────────────────────

_PATH = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w/-])")
_CHANGE_VERB = re.compile(
    r"(?i)\b(?:applied|wrote|written|updated|modified|edited|patched|replaced|"
    r"inserted|refactored|implemented|created|corrected|adjusted|rewrote|"
    r"rewritten|overwrote|added|removed|deleted|fixed|renamed|appended|"
    r"simplified|reworked|restructured|shortened|extracted|converted|"
    r"migrated|consolidated)\b")
_DENIED = re.compile(
    r"(?i)\b(?:not|never|no|without|should|shall|must|ought to|need(?:s)? to|"
    r"have to|has to|to be|can be|could be|will be|would be|may be|might be|"
    r"recommend|suggest|consider|please)\b|n't\b")
_BOUNDARY = re.compile(r"(?i)[,;:]|\b(?:i|we)\b")


def _denied_before(clause: str, pos: int) -> bool:
    """The verb at ``pos`` is negated or only advised ("was not modified",
    "should be updated"): the few words before it, after the last comma."""
    pre = clause[max(0, pos - 28): pos]
    last = None
    for m in _BOUNDARY.finditer(pre):
        last = m
    if last is not None:
        pre = pre[last.end():]
    return bool(_DENIED.search(pre))


def _tracked(cwd) -> list:
    out = _git(cwd, "ls-files")
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


def _resolve(token: str, cwd, tracked: list) -> "str | None":
    """The repo path ``token`` names: itself when it is a file there, else the
    one tracked file with that basename."""
    token = token.lstrip("./") if token.startswith("./") else token
    if not token or token.startswith("/") or ".." in token.split("/"):
        return None
    if os.path.isfile(os.path.join(str(cwd), token)):
        return token
    if "/" not in token:
        hits = [p for p in tracked if p.rsplit("/", 1)[-1] == token]
        if len(hits) == 1:
            return hits[0]
    return None


def claimed_changed(text: str, cwd) -> list:
    """Repo files the answer says THIS turn changed, in the order named. A
    file counts when its clause has a change verb that is not negated, not
    advice and not a recap of an earlier turn."""
    if not text or not cwd:
        return []
    tracked: "list | None" = None
    out: list = []
    for clause in _SENT_SPLIT.split(text):
        if _RECAP_STRONG_RE.search(clause):
            continue
        for tok in _claimed_tokens(clause):
            if tracked is None:
                tracked = _tracked(cwd)
            path = _resolve(tok, cwd, tracked)
            if path and path not in out:
                out.append(path)
    return out


def _has_live_verb(text: str) -> bool:
    return any(not _denied_before(text, m.start()) and not _recap_before(text, m.start())
               for m in _CHANGE_VERB.finditer(text))


def _claimed_tokens(clause: str) -> list:
    """File tokens the clause ties to a change verb. A table row is about the
    file in its first cell, whatever cell the verb is in. Elsewhere the verb
    and the file must share a ``;``-separated part: in "`notes.txt` — removed;
    the directory now holds `index.html`" nothing is said about index.html."""
    if clause.lstrip().startswith("|"):
        cells = [c for c in clause.split("|") if c.strip()]
        return _PATH.findall(cells[0]) if cells and _has_live_verb(clause) else []
    out: list = []
    for part in clause.split(";"):
        if _has_live_verb(part):
            out += _PATH.findall(part)
    return out


def _on(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in ("0", "false", "no", "off")


def _names(paths, limit: int = 8) -> str:
    paths = sorted(paths)
    more = f" (+{len(paths) - limit} more)" if len(paths) > limit else ""
    return ", ".join(f"`{p}`" for p in paths[:limit]) + more


class _AskOnce:
    """Shared flow: on the first FINAL with a finding, save the answer and send
    the model the facts; on the next FINAL, ``SAME`` restores the answer. The
    facts line goes under an answer that still carries the finding."""

    flag = ""            # attribute on ``st`` set once the note was sent
    notice = ""

    def applies(self, st) -> bool:
        raise NotImplementedError

    def finding(self, st, text: str):
        raise NotImplementedError

    def note(self, found) -> str:
        raise NotImplementedError

    def footer(self, found) -> str:
        raise NotImplementedError

    def check(self, st, step):
        text = (step.get("text") or "").strip()
        if not text or not self.applies(st):
            return None
        if not getattr(st, self.flag, False):
            found = self.finding(st, text)
            if not found:
                return None
            setattr(st, self.flag, True)
            setattr(st, self.flag + "_answer", step.get("text") or "")
            yield {"type": "thought", "role": "system", "text": self.notice}
            st.convo.append({"role": "user", "content": self.note(found)})
            return "continue"
        if getattr(st, self.flag + "_closed", False):
            return None
        setattr(st, self.flag + "_closed", True)
        if _says_same(text):
            text = (getattr(st, self.flag + "_answer", "") or text).strip()
            step["text"] = text
        found = self.finding(st, text)
        if found:
            step["text"] = text + "\n\n" + self.footer(found)
        return None


class UnchangedFileClaimGuard(_AskOnce):
    """The answer names a file as changed that this turn left untouched."""

    flag = "file_claim_checked"
    notice = ("↻ the answer names files as changed that this turn did not "
              "touch — checking…")

    def __init__(self, cwd, readonly_mode, builder, plan_mode):
        self.cwd = cwd
        self.off = bool(readonly_mode or builder or plan_mode)

    def applies(self, st) -> bool:
        return (not self.off and bool(self.cwd) and bool(getattr(st, "head0", None))
                and _on("AIFORGE_CHAT_FILE_CLAIM_GUARD"))

    def finding(self, st, text: str):
        changed = changed_files(self.cwd, getattr(st, "head0", None))
        if not changed:
            return None          # nothing changed at all: zero_edit / file_edit
        untouched = [p for p in claimed_changed(text, self.cwd) if p not in changed]
        return (untouched, changed) if untouched else None

    def note(self, found) -> str:
        untouched, changed = found
        return (
            "[harness — not the user] A check before this turn ends. The user "
            "does not see this note. Your answer speaks of changes to "
            f"{_names(untouched)}, but on disk "
            f"{'that file is' if len(untouched) == 1 else 'those files are'} "
            "identical to the start of this turn: no write touched "
            f"{'it' if len(untouched) == 1 else 'them'}. The files this turn "
            f"did change: {_names(changed)}.\n"
            "If the user's request covers those untouched files, do that work "
            "now with tool calls, check it, then answer. If it does not, "
            "write the answer again so that it only reports what was really "
            "changed. If your answer does not say they were changed in this "
            "turn, reply with the single word SAME and it is sent as it is.")

    def footer(self, found) -> str:
        untouched, changed = found
        return (f"(Changed on disk in this turn: {_names(changed)}. "
                f"Not changed: {_names(untouched)}.)")


# ── a command that failed after the last change ──────────────────────────────

_ERRORISH = re.compile(r"Traceback|Error\b|Exception\b|FAILED|\berror:|\bfailed\b|"
                       r"AssertionError|\bpanic\b|BUILD FAILURE", re.I)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


_SETUP_WORDS = frozenset({"cd", "export", "source", "set", "pushd", "."})
_PREFIX_WORDS = frozenset({"env", "time", "sudo", "exec", "nice", "timeout"})


def _program(cmd: str) -> str:
    """The program a shell line runs: ``cd app && FOO=1 python3 -m pytest`` →
    ``python3``. The first command that is not shell setup."""
    for part in re.split(r"&&|\|\||;|\n", cmd or ""):
        words = [w for w in part.split()
                 if not _ENV_ASSIGN.match(w) and w not in _PREFIX_WORDS]
        if words and words[0] not in _SETUP_WORDS:
            return words[0].rsplit("/", 1)[-1]
    return ""


def unpassed_failures(steps: list) -> list:
    """``[(command, headline)]``: commands run after the turn's last file write
    that ended with an error and that no later passing run of the same program
    followed. Oldest first, at most three."""
    from aiforge_core.runtime import action_log
    from aiforge_core.runtime.cleanup_detect import CMD_TOOLS
    from aiforge_core.runtime.tools.mutating import writes_files

    last_write, cmds = -1, []
    for i, s in enumerate(steps or []):
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "")
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        res = s.get("result") if isinstance(s.get("result"), dict) else {}
        if writes_files(name, args) and res.get("ok") is not False:
            last_write = i
        elif name in CMD_TOOLS or name == "run_tests":
            cmd = str(args.get("cmd") or args.get("command") or name)
            cmds.append((i, cmd, res))
    if last_write < 0:
        return []
    out = []
    for n, (i, cmd, res) in enumerate(cmds):
        code = res.get("code")
        if i < last_write or not isinstance(code, int) or code == 0:
            continue
        body = " ".join(str(res.get(k) or "") for k in ("stdout", "stderr", "error"))
        if not _ERRORISH.search(body):
            continue             # grep found nothing, `test -f`: not an error
        prog = _program(cmd)
        if any(r.get("code") == 0 and _program(c) == prog for _, c, r in cmds[n + 1:]):
            continue
        err = action_log._first_error(res)
        out.append((action_log._clip(cmd, 100), f"exit {code}" + (f": {err}" if err else "")))
    return out[-3:]


class UnpassedCheckGuard(_AskOnce):
    """A command failed after the last file change and never passed again."""

    flag = "failed_check_checked"
    notice = ("↻ a command failed after the last file change and was not run "
              "again successfully — checking…")

    def __init__(self, readonly_mode, builder, plan_mode):
        self.off = bool(readonly_mode or builder or plan_mode)

    def applies(self, st) -> bool:
        return (not self.off and getattr(st, "session_id", None) is not None
                and getattr(st, "edits_made", 0) > 0
                and _on("AIFORGE_CHAT_FAILED_CHECK_GUARD"))

    def finding(self, st, text: str):
        try:
            from aiforge_core.runtime import action_log
            return unpassed_failures(action_log.live_steps(st.session_id)) or None
        except Exception:  # noqa: BLE001 — the check never blocks an answer
            return None

    @staticmethod
    def _lines(found) -> str:
        return "\n".join(f"- `{cmd}` — {head}" for cmd, head in found)

    def note(self, found) -> str:
        return (
            "[harness — not the user] A check before this turn ends. The user "
            "does not see this note. After your last file change, these "
            "commands you ran ended with an error, and no later run passed:\n"
            f"{self._lines(found)}\n"
            "So the files as they are now have not been seen passing. If such "
            "a command checks your change: fix the cause and run it again "
            "until it passes, or say plainly in your answer that it still "
            "fails and why. Do not write that the work was verified when the "
            "last run failed. If the failure has no bearing on the result, "
            "reply with the single word SAME and your answer is sent as it "
            "is.")

    def footer(self, found) -> str:
        cmd, head = found[-1]
        return (f"(After the last file change, `{cmd}` ended with an error "
                f"({head}) and was not run again successfully.)")


__all__ = ["UnchangedFileClaimGuard", "UnpassedCheckGuard", "changed_files",
           "claimed_changed", "unpassed_failures"]
