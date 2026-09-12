"""Shared destructive-delete detection.

Policy: agents act autonomously for everything that unblocks the job
(install, build, run, edit, move, commit) — EXCEPT deleting files / data,
which must be confirmed by the user first. This module centralises the
pattern match used by both the conversational chat agent (``run_command``)
and the team Doer (``bash``). Override the policy per-process with
``AIFORGE_ALLOW_DELETE=1`` (or the chat-specific ``AIFORGE_CHAT_ALLOW_DELETE``).

Inside the docker-mode sandbox the agent owns the box and the folders the user
mounted into it, so a LOCAL file delete runs free there. Still confirmed even in
the box: deleting AIForge's own data (its config folder — settings,
credentials, memory — outside ``repos/`` and the chat workspaces), and deletes
that reach past the box (a database table, a Kubernetes object) or destroy a
device.
"""
from __future__ import annotations

import os
import re

_DELETE_PATTERNS = [
    # BOUNDED, and the inner class requires a letter. `(-[a-z]*\s+)*` let an
    # option match as just "-" plus spaces, so a long run of dashes and spaces
    # could be split many ways before the match failed. Nobody passes rm more
    # than a few option groups.
    r"\brm\s+(?:-[a-z]+\s+){0,5}",   # rm, rm -rf, rm -f …
    r"\brmdir\b", r"\bunlink\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+branch\s+-D\b",
    r"\bfind\b.*-delete\b",
    r"\bdrop\s+(table|database)\b",
    r"\btruncate\s+table\b",
    r"\btruncate\s+-",                # coreutils: truncate -s 0 file
    # `dd ... of=<file>` overwrites/truncates the target (raw disk OR a file);
    # of=/dev/null is a harmless sink so exclude it.
    r"\bdd\b.*\bof=(?!/dev/null\b)",
    r"\bcp\s+/dev/null\s",           # cp /dev/null file → truncates file
    r">\s*/dev/sd",                   # writing over a raw disk
    # NOTE: plain `>` output redirection (`cat x > out.txt`, `npm build > log`)
    # is creation/overwrite, NOT a delete — and the autonomous Doer has no
    # approval path, so flagging it would hard-fail every redirect. Not matched.
    r"\bmkfs\b", r"\bshred\b",
    r"\bdocker\s+(rm|rmi|volume\s+rm|system\s+prune)\b",
    r"\bkubectl\s+delete\b",
]
_COMPILED = [re.compile(p) for p in _DELETE_PATTERNS]
# The LOCAL file deletes — free inside the sandbox (the rest still ask there).
_LOCAL_DELETES = frozenset({
    r"\brm\s+(?:-[a-z]+\s+){0,5}", r"\brmdir\b", r"\bunlink\b",
    r"\bgit\s+clean\b", r"\bgit\s+reset\s+--hard\b", r"\bgit\s+branch\s+-D\b",
    r"\bfind\b.*-delete\b", r"\btruncate\s+-", r"\bdd\b.*\bof=(?!/dev/null\b)",
    r"\bcp\s+/dev/null\s",
})
# Sub-folders of the AIForge config folder that are the agent's working area,
# not AIForge's own data.
_WORK_AREAS = frozenset({"repos", "chat-workspaces", "workspaces", "tmp"})


def _in_sandbox() -> bool:
    return os.environ.get("AIFORGE_SANDBOX", "").strip().lower() in (
        "1", "true", "yes", "on")


_CFG_DIRNAME = ".aiforge"


def _config_forms() -> list[str]:
    home = os.environ.get("HOME", "")
    default = os.path.join(home, _CFG_DIRNAME)
    cfg = os.environ.get("AIFORGE_CONFIG_DIR") or default
    forms = {cfg.rstrip("/")}
    # The same folder written the ways a command can spell it — a literal ~ and
    # the two $HOME forms the shell would expand before the guard ever sees them.
    forms.update(f"{p}/{_CFG_DIRNAME}" for p in ("~", "$HOME", "${HOME}"))
    if home:
        forms.add(default)
    return [f for f in forms if f]


def touches_aiforge_data(cmd: str) -> bool:
    """Whether ``cmd`` names AIForge's own data — the config folder itself or
    anything in it outside the working areas (repos/, chat workspaces)."""
    for form in _config_forms():
        for m in re.finditer(re.escape(form) + r"(/[^\s'\";|&)]*)?", cmd or ""):
            first = (m.group(1) or "").strip("/").split("/")[0]
            if first not in _WORK_AREAS:
                return True
    return False

REFUSAL = (
    "Refused: this command deletes files/data. The agent does every other "
    "operation autonomously but must ASK before deleting. Stop and ask the "
    "user to confirm this exact command; only re-run it after they agree."
)


def _protected(path: str) -> bool:
    """AIForge's own data: the config folder, anything in it outside the
    working areas, or any folder that CONTAINS it (`rm -rf ~`)."""
    cfg = os.path.realpath(os.environ.get("AIFORGE_CONFIG_DIR")
                           or os.path.join(os.environ.get("HOME", "/"), ".aiforge"))
    p = os.path.realpath(path)
    if p == cfg or cfg.startswith(p.rstrip(os.sep) + os.sep):
        return True
    if p.startswith(cfg + os.sep):
        return os.path.relpath(p, cfg).split(os.sep)[0] not in _WORK_AREAS
    return False


def _deletes_aiforge_data(cmd: str, cwd: "str | None") -> bool:
    """Whether any path ``cmd`` deletes resolves into AIForge's own data —
    following `cd`, `~` and relative paths (the shell-write reader), and the
    working folder itself for the delete forms that act on it implicitly
    (`git clean`, `git reset --hard`, `find . -delete`, `rm *`)."""
    here = cwd or os.getcwd()
    if touches_aiforge_data(cmd):
        return True
    try:
        from aiforge_core.runtime.shell_writes import shell_write_targets
        targets = shell_write_targets(cmd, here)
    except Exception:  # noqa: BLE001 — unreadable: the textual check stands
        targets = []
    if re.search(r"\bgit\s+(clean|reset)\b|\bfind\b", cmd or ""):
        targets.append(here)
    return any(_protected(t) for t in targets)


def is_destructive_delete(cmd: str, cwd: "str | None" = None) -> bool:
    if not cmd:
        return False
    low = cmd.lower()
    hits = [p.pattern for p in _COMPILED if p.search(low)]
    if not hits:
        return False
    if (_in_sandbox() and all(h in _LOCAL_DELETES for h in hits)
            and not _deletes_aiforge_data(cmd, cwd)):
        return False            # the box and its mounted folders are the agent's
    return True


def allow_delete(env_keys: tuple[str, ...] = ("AIFORGE_ALLOW_DELETE",)) -> bool:
    for k in env_keys:
        if os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False
