"""Foundation: config/env paths, repo key, atomic + locked JSON IO, and the
shared in-process state (session store, lock, valid-value sets, logger).

Split out of the former single ``rule_capture`` module VERBATIM. No behaviour
change. ``_load_flags`` / ``_save_flags`` resolve ``_flags_path`` through the
package so an ``rc._flags_path`` monkeypatch (tests) still reaches them exactly
as it did when everything lived in one module.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import uuid
from pathlib import Path

try:
    import fcntl  # POSIX advisory file locks (macOS/Linux)
except ImportError:  # pragma: no cover — non-POSIX fallback
    fcntl = None  # type: ignore

log = logging.getLogger("aiforge.rule_capture")

_VALID_CATEGORIES = {"rule", "memory", "feedback", "none"}
_VALID_SCOPES = {"global", "project", "session"}

# Per-session ephemeral store (NOT persisted) — session-scoped rules/memories
# live only here, keyed by session_id, and vanish when the process exits.
_SESSION_ITEMS: dict[str, list[dict]] = {}

_LOCK = threading.Lock()


# ─────────────────────────── config / env ───────────────────────────

def _config_dir() -> Path:
    base = os.environ.get("AIFORGE_CONFIG_DIR", os.path.expanduser("~/.aiforge"))
    p = Path(base).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _flags_path() -> Path:
    return _config_dir() / "rule_flags.json"


def _index_path() -> Path:
    return _config_dir() / "captured_rules.json"


def _disabled() -> bool:
    return os.environ.get("AIFORGE_RULE_CAPTURE_DISABLE", "").strip().lower() \
        in ("1", "true", "yes", "on")


def _min_conf() -> float:
    try:
        return float(os.environ.get("AIFORGE_RULE_CAPTURE_MIN_CONFIDENCE", "0.6"))
    except ValueError:
        return 0.6


def _none() -> dict:
    return {"category": "none", "scope": "session", "canonical": "",
            "confidence": 0.0, "task_present": True}


# ─────────────────────────── repo key + atomic IO ───────────────────

def repo_key(cwd_or_root: str | None) -> str | None:
    """The canonical key a repo is filed under for gate flags. Delegates to the
    ONE resolver (repo_ident.repo_name → git-toplevel basename) so a flag keyed
    from a subdir matches one keyed from the repo root, and agrees with the key
    memory/rules use. Accepts a repo NAME or a path. None on empty."""
    if not cwd_or_root:
        return None
    try:
        from aiforge_core.runtime import repo_ident
        return repo_ident.repo_name(str(cwd_or_root), sentinel="") or None
    except Exception:  # noqa: BLE001 — fall back to the plain basename
        base = os.path.basename(os.path.normpath(str(cwd_or_root))).strip()
        return base or None


@contextlib.contextmanager
def _file_lock(path: Path):
    """Cross-process advisory lock around a read-modify-write of ``path`` (a
    sibling ``<path>.lock`` file). Combined with the in-process ``_LOCK`` this
    makes captured_rules.json / rule_flags.json updates safe under concurrent
    workers + threads. Degrades to a no-op when fcntl is unavailable."""
    if fcntl is None:
        yield
        return
    lock_path = Path(str(path) + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace) so a
    crashed/concurrent writer can never leave a half-written JSON file."""
    tmp = Path(f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ─────────────────────────── persistence helpers ────────────────────

def _load_index() -> dict:
    p = _index_path()
    if not p.is_file():
        return {"items": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "items" not in data:
            return {"items": {}}
        return data
    except Exception:  # noqa: BLE001
        return {"items": {}}


def _save_index(data: dict) -> None:
    try:
        _atomic_write(_index_path(), json.dumps(data, indent=2))
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture index save failed: %s", exc)


def _load_flags() -> dict:
    from aiforge_core.runtime import rule_capture as _rc
    p = _rc._flags_path()
    if not p.is_file():
        return {"global": {}, "repo": {}, "session": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"global": {}, "repo": {}, "session": {}}
        for k in ("global", "repo", "session"):
            data.setdefault(k, {})
        return data
    except Exception:  # noqa: BLE001
        return {"global": {}, "repo": {}, "session": {}}


def _save_flags(data: dict) -> None:
    from aiforge_core.runtime import rule_capture as _rc
    try:
        _atomic_write(_rc._flags_path(), json.dumps(data, indent=2))
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture flags save failed: %s", exc)
