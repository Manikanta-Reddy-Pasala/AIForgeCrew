"""Where a project's chats work, asked ONCE per project, and the chat's pending
question about it.

A chat on a git project works in a worktree of its own (:mod:`chat_worktree`).
Where that worktree lives is the project owner's call, so the first chat in a
project without a setting asks (the producer ends the turn with the question,
before any work) and the answer is kept for the project:

``<config>/worktree_prefs.json``::

    {"/abs/repo": {"place": "repo" | "config" | "none", "set_at": "<iso>"}}

* ``repo``   — ``<repo>/.aiforge-worktrees/<chat>/work`` (kept out of git);
* ``config`` — ``<config>/chat-worktrees/<slug>-<hash>/<chat>/work``;
* ``none``   — no worktree: chats edit the project folder itself.

An explicit AIFORGE_CHAT_WORKTREE_PLACE (repo / config) or
AIFORGE_CHAT_WORKTREES=0 wins: then nothing is asked. AIFORGE_CHAT_WORKTREE_ASK=0
turns the question off (the default ``repo`` is used, nothing saved), for
servers that only automated clients talk to.

The chat's pending state (the question it was asked and the request it asked
it on, the old-worktree list it was shown) lives in
``<config>/worktree_pending.json`` keyed by session id, so it survives a server
restart. Every function here fails open: a broken file reads as empty.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

PLACES = ("repo", "config", "none")

_LOCK = threading.RLock()

#: Words in the question every reply check looks for (and the history fold).
QUESTION_MARK = "has no worktree setting yet"

_ENV_PLACE = {
    "repo": "repo", "in-repo": "repo", "inside": "repo", "project": "repo",
    "config": "config", "home": "config", "outside": "config",
    "none": "none", "off": "none", "no": "none",
}


# ── files ────────────────────────────────────────────────────────────────────

def _cfg() -> str:
    from aiforge_core.config.paths import config_dir
    return str(config_dir())


def prefs_path() -> str:
    return os.path.join(_cfg(), "worktree_prefs.json")


def pending_path() -> str:
    return os.path.join(_cfg(), "worktree_pending.json")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: str, data: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        return False


def _key(repo: str) -> str:
    return os.path.realpath(os.path.expanduser(str(repo)))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── the project's setting ────────────────────────────────────────────────────

def env_place() -> "str | None":
    """The place an explicit env forces, or None when the env says nothing."""
    if os.environ.get("AIFORGE_CHAT_WORKTREES", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return "none"
    raw = os.environ.get("AIFORGE_CHAT_WORKTREE_PLACE", "").strip().lower()
    return _ENV_PLACE.get(raw) if raw else None


def ask_enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_WORKTREE_ASK", "1").strip().lower() not in (
        "0", "false", "no", "off")


def get(repo: "str | None") -> "dict | None":
    if not repo:
        return None
    with _LOCK:
        row = _load(prefs_path()).get(_key(repo))
    if isinstance(row, dict) and row.get("place") in PLACES:
        return row
    return None


def all_prefs() -> dict:
    with _LOCK:
        data = _load(prefs_path())
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and v.get("place") in PLACES}


def set_place(repo: str, place: str) -> dict:
    if place not in PLACES:
        raise ValueError(f"place must be one of {', '.join(PLACES)}")
    row = {"place": place, "set_at": now_iso()}
    with _LOCK:
        data = _load(prefs_path())
        data[_key(repo)] = row
        _save(prefs_path(), data)
    return row


def clear(repo: str) -> bool:
    with _LOCK:
        data = _load(prefs_path())
        gone = data.pop(_key(repo), None) is not None
        if gone:
            _save(prefs_path(), data)
    return gone


def place_for(repo: "str | None") -> "str | None":
    """env first, then the project's saved choice; None when neither says."""
    forced = env_place()
    if forced:
        return forced
    row = get(repo)
    return row["place"] if row else None


def needs_ask(repo: "str | None") -> bool:
    return bool(repo) and ask_enabled() and place_for(repo) is None


# ── reading the reply ────────────────────────────────────────────────────────

_NUM = re.compile(r"^\W*(?:option|choice|number|no\.?|#)?\s*([123])\s*[).:]?\s*"
                  r"(?:please|pls|thanks|thank you)?\W*$", re.I)
_NONE_WORDS = re.compile(r"\b(?:no\s+worktrees?|without\s+(?:a\s+)?worktree|none|"
                         r"in[\s-]?place|directly|don['’]?t\s+use\s+(?:a\s+)?worktree)\b", re.I)
_CONFIG_WORDS = re.compile(r"\b(?:aiforge(?:['’]s)?|config|home\s+folder|home\s+dir(?:ectory)?|"
                           r"outside(?:\s+the\s+(?:project|repo))?)\b", re.I)
_REPO_WORDS = re.compile(r"\b(?:inside|in\s+the\s+(?:project|repo)|project|repo|repository)\b", re.I)
_MAX_WORDS = 8


def parse_choice(text: "str | None") -> "str | None":
    """``repo`` / ``config`` / ``none`` when ``text`` answers the question, else
    None. A long message is a request of its own, never a choice."""
    t = (text or "").strip()
    if not t:
        return None
    m = _NUM.match(t)
    if m:
        return {"1": "repo", "2": "config", "3": "none"}[m.group(1)]
    if len(t.split()) > _MAX_WORDS:
        return None
    if _NONE_WORDS.search(t):
        return "none"
    if _CONFIG_WORDS.search(t):
        return "config"
    if _REPO_WORDS.search(t):
        return "repo"
    return None


PLACE_WORDS = {
    "repo": "work in their own worktree inside the project",
    "config": "work in their own worktree in AIForge's folder",
    "none": "edit the project folder directly (no worktree)",
}


def question(repo: str, holder: str, *, writable: bool = True) -> str:
    """The once-per-project question, naming exact paths."""
    inside = os.path.join(repo, ".aiforge-worktrees") + os.sep
    note = "" if writable else " — not writable here, so 2 is used instead"
    return (f"This project ({repo}) {QUESTION_MARK}. Where should chats here work?\n"
            f"1) Own worktree inside the project: {inside} (git-ignored){note}\n"
            f"2) Own worktree in AIForge's folder: {holder}{os.sep}\n"
            "3) No worktree: edit the project folder directly\n"
            "Reply 1, 2 or 3. Your request runs right after.")


def how_to_change(repo: str) -> str:
    return ("To change it for this project: PUT /api/chat/worktree-prefs "
            f'{{"repo": "{repo}", "place": "repo|config|none"}}, or DELETE '
            f"/api/chat/worktree-prefs?repo={repo} to be asked again.")


# ── a chat's pending state ───────────────────────────────────────────────────

def session_state(session_id) -> dict:
    if session_id is None:
        return {}
    with _LOCK:
        row = _load(pending_path()).get(str(session_id))
    return dict(row) if isinstance(row, dict) else {}


def save_session_state(session_id, state: "dict | None") -> None:
    if session_id is None:
        return
    with _LOCK:
        data = _load(pending_path())
        if state:
            data[str(session_id)] = {**state, "updated": time.time()}
        else:
            data.pop(str(session_id), None)
        # A chat's state older than 30 days is dead weight.
        cut = time.time() - 30 * 86400
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and float(v.get("updated") or 0) >= cut}
        _save(pending_path(), data)


__all__ = ["PLACES", "QUESTION_MARK", "env_place", "ask_enabled", "get", "all_prefs",
           "set_place", "clear", "place_for", "needs_ask", "parse_choice", "question",
           "how_to_change", "session_state", "save_session_state", "PLACE_WORDS"]
