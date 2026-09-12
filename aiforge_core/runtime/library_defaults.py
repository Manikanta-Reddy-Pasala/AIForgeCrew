"""Shipped defaults are READ-ONLY; an operator disables them, never edits them.

Rules, skills and workflows come in three layers: the defaults shipped inside
the package (``runtime/builtin_playbooks/``), the operator's own in
``~/.aiforge``, and a repo's in ``<repo>/.aiforge``. Only the last two are the
operator's to change.

"Delete" used to unlink the backing file wherever it lived — including inside
the installed package. That is wrong three ways: the deletion is undone by the
next upgrade (so it looks like the default "came back"), the package directory
is read-only in several of our install shapes (so it fails instead), and a
company default that one box can silently drop is not a company default.

So a delete of a shipped default is recorded HERE instead, as a name in
``<config>/library-disabled.json``, and every loader skips it. Reversible, local
to this box, and the shipped file is never touched. Customising a default is
unchanged: author one of your own with the same name and it shadows the
default by the ordinary layering rules.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from aiforge_core.config import _atomic

log = logging.getLogger("aiforge.library")
_LOCK = threading.Lock()
KINDS = ("rule", "skill", "workflow")

# Everything under this folder ships WITH the package and belongs to whoever
# builds it — the company, not the box.
_BUILTIN_ROOT = Path(__file__).resolve().parent / "builtin_playbooks"


def _path() -> Path:
    cfg = os.environ.get("AIFORGE_CONFIG_DIR", "").strip() \
        or os.path.expanduser("~/.aiforge")
    return Path(cfg) / "library-disabled.json"


def is_builtin(source: str) -> bool:
    """Whether ``source`` names a shipped default (a path under the package's
    builtin playbooks, or the ``builtin`` sentinel the loaders use)."""
    src = str(source or "").strip()
    if not src:
        return False
    if src == "builtin":
        return True
    try:
        return _BUILTIN_ROOT in Path(src).resolve().parents
    except OSError:
        return False


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def disabled(kind: str) -> set[str]:
    """Names of shipped defaults this box has turned off."""
    return {str(n) for n in (_load().get(kind) or []) if str(n).strip()}


def _save(data: dict) -> bool:
    try:
        _atomic.write_text(str(_path()), json.dumps(data, indent=1))
        return True
    except OSError as exc:
        log.warning("library: disabled list not saved: %s", exc)
        return False


def disable(kind: str, name: str) -> bool:
    """Turn a shipped default off on this box. Idempotent."""
    name = (name or "").strip()
    if kind not in KINDS or not name:
        return False
    with _LOCK:
        data = _load()
        names = [str(n) for n in (data.get(kind) or [])]
        if name not in names:
            names.append(name)
        data[kind] = names
        return _save(data)


def enable(kind: str, name: str) -> bool:
    """Put a disabled default back."""
    name = (name or "").strip()
    if kind not in KINDS or not name:
        return False
    with _LOCK:
        data = _load()
        data[kind] = [n for n in (data.get(kind) or []) if str(n) != name]
        return _save(data)


def state() -> dict:
    """What this box has disabled, for the Library page."""
    return {k: sorted(disabled(k)) for k in KINDS}


__all__ = ["KINDS", "disable", "disabled", "enable", "is_builtin", "state"]
