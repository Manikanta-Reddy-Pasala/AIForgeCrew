"""Repair the permissions of an EXISTING config dir on boot.

``_atomic`` publishes everything it writes at 0600, and the comment there is
right that ``~/.ssh`` is the benchmark. But a mode applied at write time only
ever covers files written AFTER the fix — and a credential file is exactly the
kind that is written once and then read for years. On the machine this was
found, ``agent_config.json`` still carried a live ``api_key`` at 0644, months
after the hardening landed, because nothing had rewritten it. A fix that has
never applied to the file it names is not a fix.

So: repair on startup, and say what was repaired. Cheap (a handful of stats),
idempotent, and silent when there is nothing to do.

The SQLite databases were never in scope of the original hardening at all —
they are not written through ``_atomic``, so they inherit the umask. They hold
chat transcripts and the memory tree; on any shared box that is as sensitive as
the tokens, and arguably more revealing.

``AIFORGE_CONFIG_MODE`` still overrides the file mode for a deployment that
genuinely needs group reads, and ``AIFORGE_SKIP_PERM_REPAIR=1`` turns the pass
off entirely.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

log = logging.getLogger("aiforge.permissions")

# Everything here is owner-only. Named explicitly rather than globbed: a repair
# pass that walks the whole tree would also chmod files an operator deliberately
# shared, and the point is to fix what WE created.
_SECRET_FILES = (
    "agent_config.json",     # per-role api_key
    "integrations.json",     # jira / confluence / gitlab / email tokens
    "runtime.env",           # UI-persisted env, including DB URLs
    ".env",                  # if an operator put one here
    "aiforge.db",            # tickets
    "chat.db",               # every chat transcript
    "memory.db",             # the memory tree
    "memory_sources.db",
    "codemem.state.db",
    "jobs.db",
    "llm_rate.db",
    "approval_settings.json",
    "runtime_settings.json",
    "captured_rules.json",
    "rule_flags.json",
    "health.json",
)
# Directories whose CONTENTS are private: notes, transcripts, uploaded files.
_SECRET_DIRS = ("memory", "memory-archive", "logs", "chat_traces", "runs",
                "ticket-files", "backups", "checkpoints")


def _target_file_mode() -> int:
    raw = (os.environ.get("AIFORGE_CONFIG_MODE") or "").strip()
    if raw:
        try:
            return int(raw, 8)
        except ValueError:
            pass
    return 0o600


def _too_open(path: Path, want: int) -> bool:
    """True when the file grants anything to group or other that ``want`` does
    not. Never tightens beyond the target, and never touches a file that is
    already at least as private."""
    try:
        cur = stat.S_IMODE(path.lstat().st_mode)
    except OSError:
        return False
    return bool(cur & ~want & 0o077)


def repair(config_dir: str | Path | None = None) -> dict:
    """Tighten the config dir and its known private files. Returns a summary;
    never raises — a permissions pass must not stop the app from booting."""
    if str(os.environ.get("AIFORGE_SKIP_PERM_REPAIR", "")).strip().lower() in (
            "1", "true", "yes", "on"):
        return {"skipped": "AIFORGE_SKIP_PERM_REPAIR"}
    from aiforge_core.config.paths import config_dir as _cd

    root = Path(str(config_dir or _cd()))
    fixed: list[str] = []
    if not root.exists():
        return {"fixed": [], "root": str(root)}
    want_file = _target_file_mode()
    # A directory needs EXECUTE wherever it grants read, or the holder cannot
    # traverse it — including the owner. Deriving this as "0700 unless a custom
    # mode" got 0640 wrong: it produced 0650, which strips the owner's execute
    # bit and makes the operator's own config dir unreadable to the process
    # that just wrote it. Mirror every read bit into the matching execute bit.
    want_dir = want_file | ((want_file & 0o444) >> 2)
    for target, want, kind in (
        [(root, want_dir, "dir")]
        + [(root / n, want_file, "file") for n in _SECRET_FILES]
        + [(root / n, want_dir, "dir") for n in _SECRET_DIRS]
    ):
        # The PROBE can raise too, not just the chmod: exists()/is_symlink()
        # stat the path, and a dangling entry, an I/O error or a directory we
        # cannot traverse would take the whole boot with it.
        try:
            if not target.exists() or target.is_symlink():
                # A symlink is skipped deliberately: chmod follows it, so
                # "repairing" one would change the mode of whatever it points
                # at — possibly a file outside the config dir entirely.
                continue
        except OSError as exc:
            log.warning("permissions: could not inspect %s — %s", target, exc)
            continue
        if not _too_open(target, want):
            continue
        try:
            target.chmod(want)
            fixed.append(f"{target.name} -> {oct(want)}")
        except OSError as exc:      # not ours to chmod (a root-owned leftover)
            log.warning("permissions: could not tighten %s (%s) — %s",
                        target, kind, exc)
    if fixed:
        # Worth a real log line, not debug: it means these were readable by
        # every local account until this moment, and an operator may want to
        # rotate whatever was in them.
        log.warning("permissions: tightened %d path(s) under %s: %s. These were "
                    "readable by other local users; consider rotating any token "
                    "they held.", len(fixed), root, ", ".join(fixed))
    return {"fixed": fixed, "root": str(root)}


__all__ = ["repair"]
