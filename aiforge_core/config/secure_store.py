"""One folder holds every credential this install owns.

The config dir grew organically and the secrets grew with it: per-role API keys
in ``agent_config.json``, Jira / Confluence / GitLab / SMTP tokens in
``integrations.json``, MCP server keys and headers in ``mcp_servers.json``, and
whatever the Settings UI persisted into ``runtime.env`` — each sitting beside
caches, catalogs and health snapshots that carry nothing at all. Nobody can
back up, exclude from a sync, audit or hand-inspect a directory shaped like
that, and the one time it mattered a live ``api_key`` sat at 0644 for months
because a write-time mode fix could not reach a file nothing rewrote.

So they move into ``$AIFORGE_CONFIG_DIR/security/`` — the directory 0700, every
file 0600, nothing else in it. What that buys, concretely:

* one path to exclude from a backup or a sync, and one to audit;
* a mode fix that applies to the DIRECTORY, so a file created inside it later
  cannot quietly be world-readable;
* a truthful answer to "where are the keys", which is the question an operator
  actually asks.

**Migration is a MOVE, not a copy.** Copying would leave the original readable
exactly where it always was — the strictly worse outcome of having the secret
in two places and having fixed nothing. Each file moves on first resolution and
the legacy path stops existing.

Not moved, deliberately: the SQLite databases. They carry transcripts and the
memory tree — sensitive, and already repaired to 0600 by ``permissions`` — but
they are opened by path from several places, carry ``-wal`` / ``-shm``
siblings, and may be open when a boot repair runs. Moving a live database to
tidy a directory is a bad trade.

``AIFORGE_SECURITY_DIR`` puts the folder somewhere else (an encrypted volume,
say). ``AIFORGE_SECURE_STORE=0`` turns the whole thing off and keeps every file
where it is — the rollback, for a deployment that mounts these paths.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from aiforge_core.config.paths import config_dir

log = logging.getLogger("aiforge.secure_store")

_DIR_NAME = "security"
_DIR_MODE = 0o700
_FILE_MODE = 0o600

# Files whose contents are, or can be, a credential. Named explicitly: a rule
# like "every .json" would sweep in caches and catalogs, and moving those buys
# nothing while breaking whatever reads them by path.
SECRET_FILES = (
    "agent_config.json",     # per-role api_key
    "integrations.json",     # jira / confluence / gitlab / email tokens
    "mcp_servers.json",      # remote MCP api keys + auth headers
    "runtime.env",           # UI-persisted env: DB URLs, keys, tokens
    ".env",                  # if an operator put one in the config dir
)


def enabled() -> bool:
    """False keeps every file at its legacy path (the rollback switch)."""
    return str(os.environ.get("AIFORGE_SECURE_STORE", "1")).strip().lower() \
        not in ("0", "false", "no", "off")


def security_dir(*, create: bool = False) -> Path:
    """The credential folder. ``create`` only on a WRITE path.

    Reading must not mkdir: a read that creates directories turns "is anything
    configured?" into a side effect, and on a config dir the process cannot
    write it would raise out of a question that has a perfectly good answer.
    """
    raw = (os.environ.get("AIFORGE_SECURITY_DIR") or "").strip()
    d = Path(os.path.expanduser(raw)) if raw else \
        Path(str(config_dir())) / _DIR_NAME
    if create:
        d.mkdir(parents=True, exist_ok=True)
        _tighten(d, _DIR_MODE)
    return d


def _tighten(path: Path, mode: int) -> None:
    """Best effort. A mode we cannot set is worth a log, never an exception:
    this runs on the read path of the config the app needs to boot."""
    try:
        if path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
    except OSError as exc:
        log.warning("secure_store: could not chmod %s to %s — %s",
                    path, oct(mode), exc)


def legacy_path(name: str) -> Path:
    return Path(str(config_dir())) / name


def _migrate_one(name: str, dest: Path) -> bool:
    """Move a legacy file into the folder. True when something moved.

    ``shutil.move`` then chmod, rather than a copy: the point of the exercise
    is that the secret stops being where it was.
    """
    src = legacy_path(name)
    try:
        if not src.is_file() or src.is_symlink() or dest.exists():
            return False
    except OSError:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _tighten(dest.parent, _DIR_MODE)
        shutil.move(str(src), str(dest))
        _tighten(dest, _FILE_MODE)
    except OSError as exc:
        log.warning("secure_store: could not move %s into %s — %s",
                    name, dest.parent, exc)
        return False
    log.info("secure_store: moved %s into %s", name, dest.parent)
    return True


def secure_path(name: str) -> Path:
    """Where ``name`` lives now — migrating it on first use.

    The resolution order is what keeps this safe to land mid-flight: a file
    already in the folder wins, a legacy file is moved and then wins, and a
    file that does not exist yet resolves INTO the folder so the first write
    lands there. Callers keep asking for a name and never learn the layout.
    """
    if not enabled():
        return legacy_path(name)
    dest = security_dir() / name
    try:
        if dest.exists():
            return dest
    except OSError:
        return legacy_path(name)
    _migrate_one(name, dest)
    return dest


def migrate_all() -> dict:
    """Move every known credential file. Called once on boot; safe to repeat.

    Returns a summary rather than logging only, so the boot step that calls it
    can say what happened — a silent migration of the operator's credentials is
    exactly the kind of change that should announce itself.
    """
    if not enabled():
        return {"skipped": "AIFORGE_SECURE_STORE=0", "moved": []}
    moved: list[str] = []
    root = security_dir(create=True)
    for name in SECRET_FILES:
        if _migrate_one(name, root / name):
            moved.append(name)
    if moved:
        log.warning("secure_store: moved %d credential file(s) into %s: %s",
                    len(moved), root, ", ".join(moved))
    return {"moved": moved, "dir": str(root)}


def paths() -> dict:
    """Every credential file this install would use, and whether it exists.

    For the API/UI and for an operator asking the only question that matters
    about a secrets folder: what is in it.
    """
    out = {}
    for name in SECRET_FILES:
        p = secure_path(name)
        try:
            exists = p.is_file()
            mode = oct(p.stat().st_mode & 0o777) if exists else ""
        except OSError:
            exists, mode = False, ""
        out[name] = {"path": str(p), "exists": exists, "mode": mode}
    return out


__all__ = ["SECRET_FILES", "enabled", "legacy_path", "migrate_all", "paths",
           "secure_path", "security_dir"]
