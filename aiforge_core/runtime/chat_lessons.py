"""What a chat has already learned the hard way: the user's corrections and
the approaches that failed. Kept per chat, verbatim, outside the history.

A condense keeps the last few asks at 120 chars and the last few error lines;
after two or three condenses "don't touch the read-only repo" or "that's the
wrong file" is gone and the model makes the same mistake again. The handoff's
list of failed approaches was cleared when the turn ended. This module keeps
both for the life of the chat, pins them in every condense note and every
turn's context, and hands each correction to project memory so a later chat
recalls it too.

Store: ``<chat scratch>/session-<id>/.aiforge/lessons.json`` (a temporary chat
keeps it in memory only). Off switch: ``AIFORGE_CHAT_LESSONS=0``. Soft-fail
everywhere: a broken store never breaks a turn.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("aiforge.chat_lessons")

MARK = "LESSONS FROM THIS CHAT"
#: Corrections and failed approaches kept, newest last.
MAX_CORRECTIONS = 10
MAX_FAILED = 8
_TEXT_CHARS = 500
_SAID_CHARS = 160
_BLOCK_CHARS = 3500

_LOCK = threading.Lock()
_MEM: dict = {}          # temporary chats: session id -> lessons

# The user telling the agent it got something wrong, or what not to do. Only
# read on a message that follows an answer (there is something to correct).
_CORRECTION = re.compile(
    r"\b(?:"
    r"that'?s (?:wrong|not (?:right|correct|it|what))"
    r"|(?:is|was|are|were|looks?) (?:wrong|incorrect|broken)"
    r"|wrong (?:again|file|repo|repository|branch|folder|place|way|path|command|output|answer)"
    r"|not what i (?:asked|wanted|meant|said)"
    r"|incorrect"
    r"|you (?:forgot|missed|broke|ignored|skipped|deleted|overwrote|didn'?t|did not|"
    r"keep|still|again|already|were (?:told|asked)|should(?:n'?t| not| have)|"
    r"must(?:n'?t| not)|can'?t|cannot)"
    r"|i (?:told|said|asked) you"
    r"|same (?:mistake|error|issue|problem) again"
    r"|(?:don'?t|do not|never|stop) (?:do|doing|use|using|touch|touching|change|changing|"
    r"edit|editing|run|running|commit|committing|push|pushing|delete|deleting|create|"
    r"add|adding|modify|modifying|rewrite|rewriting|guess|guessing|ask|asking)"
    r"|instead of"
    r"|why did you"
    r"|no[,.!]? (?:not|use|do|it'?s|that'?s|the|you|don'?t|wrong)"
    r"|not (?:like that|that one|this one|there)"
    r")", re.I)


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_LESSONS", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _path(session_id) -> str:
    from aiforge_core.runtime import repo_ident
    return os.path.join(repo_ident.chat_scratch_root(), f"session-{session_id}",
                        ".aiforge", "lessons.json")


def _temporary(session_id) -> bool:
    try:
        from aiforge_core.runtime import chat_private
        return bool(chat_private.is_temporary(session_id))
    except Exception:  # noqa: BLE001
        return False


def _empty() -> dict:
    return {"corrections": [], "failed": []}


def _load(session_id) -> dict:
    if _temporary(session_id):
        return json.loads(json.dumps(_MEM.get(str(session_id)) or _empty()))
    try:
        with open(_path(session_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    return {"corrections": [c for c in data.get("corrections") or []
                            if isinstance(c, dict) and c.get("text")],
            "failed": [f for f in data.get("failed") or []
                       if isinstance(f, dict) and f.get("text")]}


def _save(session_id, data: dict) -> None:
    if _temporary(session_id):
        _MEM[str(session_id)] = data
        return
    from aiforge_core.config import _atomic
    path = _path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic.write_text(path, json.dumps(data, indent=1))


def lessons(session_id) -> dict:
    """``{"corrections": [...], "failed": [...]}`` for the chat (empty when off)."""
    if not enabled() or session_id is None:
        return _empty()
    try:
        with _LOCK:
            return _load(session_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons read failed (session=%s): %s", session_id, exc)
        return _empty()


def _flat(text: str, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def _users_words(text: str) -> str:
    """The user's own words: the harness blocks a message carries are cut."""
    try:
        from aiforge_core.runtime.chat_agent._turn._convo import strip_turn_note
        text = strip_turn_note(text or "")
    except Exception:  # noqa: BLE001
        pass
    return text.split("\n\n---\n[Interpreted request")[0]


def is_correction(text: str) -> bool:
    """Does the message tell the agent it got something wrong / what not to do?"""
    return bool(_CORRECTION.search(_users_words(text or "")[:4000]))


def note_correction(session_id, text: str, said: str = "",
                    turn: "int | None" = None) -> "dict | None":
    """Keep the user's correction (and a line of the answer it corrects).
    Returns the stored entry, or None (off, not a correction, a repeat)."""
    if not enabled() or session_id is None:
        return None
    words = _flat(_users_words(text), _TEXT_CHARS)
    if not words:
        return None
    entry = {"text": words, "said": _flat(said, _SAID_CHARS), "turn": turn,
             "at": int(time.time())}
    try:
        with _LOCK:
            data = _load(session_id)
            prev = next((c for c in data["corrections"] if c.get("text") == words), None)
            if prev is not None and not entry["said"]:
                entry["said"] = prev.get("said") or ""
            items = [c for c in data["corrections"] if c.get("text") != words]
            items.append(entry)
            data["corrections"] = items[-MAX_CORRECTIONS:]
            _save(session_id, data)
        return entry
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons write failed (session=%s): %s", session_id, exc)
        return None


def note_failed(session_id, items) -> int:
    """Keep the approaches a run found not to work (handoff
    ``failed_approaches``). Returns how many were new."""
    if not enabled() or session_id is None or not items:
        return 0
    try:
        with _LOCK:
            data = _load(session_id)
            have = [f.get("text") for f in data["failed"]]
            new = 0
            for raw in items:
                t = _flat(raw, 240)
                if not t:
                    continue
                if t in have:
                    have.remove(t)
                    data["failed"] = [f for f in data["failed"] if f.get("text") != t]
                else:
                    new += 1
                data["failed"].append({"text": t, "at": int(time.time())})
                have.append(t)
            data["failed"] = data["failed"][-MAX_FAILED:]
            _save(session_id, data)
        return new
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons write failed (session=%s): %s", session_id, exc)
        return 0


# ── the project's lessons: corrections from earlier chats on the same repo ──

MAX_PROJECT = 8


def _project_path(repo: str) -> str:
    from aiforge_core.config.paths import config_dir
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo)[:120] or "repo"
    return os.path.join(os.path.expanduser(str(config_dir())), "project_lessons",
                        f"{safe}.json")


def _repo_of(session_id) -> str:
    try:
        from aiforge_core.runtime import chat_store, rule_capture
        sess = chat_store.get_session(session_id) or {}
        return rule_capture.repo_key(sess.get("cwd") or "") or ""
    except Exception:  # noqa: BLE001
        return ""


def project_lessons(repo: str) -> list:
    if not repo:
        return []
    try:
        with open(_project_path(repo), encoding="utf-8") as fh:
            data = json.load(fh)
        return [c for c in data if isinstance(c, dict) and c.get("text")]
    except (OSError, ValueError, TypeError):
        return []


def _note_project(repo: str, entry: dict, session_id) -> None:
    if not repo or not entry:
        return
    from aiforge_core.config import _atomic
    with _LOCK:
        items = [c for c in project_lessons(repo) if c.get("text") != entry["text"]]
        items.append({**entry, "session": str(session_id)})
        path = _project_path(repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic.write_text(path, json.dumps(items[-MAX_PROJECT:], indent=1))


def render(data: dict, live_failed=(), earlier=()) -> str:
    """The block for ``data`` plus a run's own not-yet-stored failures ("" when
    there is nothing). Bounded: oldest lines go first."""
    corr = list(data.get("corrections") or [])[-MAX_CORRECTIONS:]
    failed = [f.get("text") for f in data.get("failed") or []]
    for t in live_failed or ():
        t = _flat(t, 240)
        if t and t not in failed:
            failed.append(t)
    failed = failed[-MAX_FAILED:]
    have = {c.get("text") for c in corr}
    earlier = [c for c in earlier or () if c.get("text") not in have][-MAX_PROJECT:]
    if not corr and not failed and not earlier:
        return ""
    head = (f"{MARK} — the user already corrected you on these, or they were "
            "tried and did not work. They still hold: do not repeat them.")
    lines: list = []
    for c in earlier:
        lines.append(f'- in an earlier chat on this project the user said: "{c["text"]}"')
    for c in corr:
        said = f' (you had said: "{c["said"]}")' if c.get("said") else ""
        lines.append(f'- the user said: "{c["text"]}"{said}')
    for t in failed:
        lines.append(f"- tried and failed: {t}")
    while lines and len(head) + sum(len(x) + 1 for x in lines) > _BLOCK_CHARS:
        lines.pop(0)
    return "\n".join([head] + lines) if lines else ""


def block(st_or_session) -> str:
    """The block for a chat loop state (its live failed approaches included)
    or a bare session id. "" when off / nothing learned."""
    if not enabled():
        return ""
    if isinstance(st_or_session, (int, str)) or st_or_session is None:
        sid, live = st_or_session, ()
    else:
        sid = getattr(st_or_session, "session_id", None)
        live = getattr(st_or_session, "failed_approaches", None) or ()
    data = lessons(sid) if sid is not None else _empty()
    earlier = []
    if sid is not None and not _temporary(sid):
        earlier = [c for c in project_lessons(_repo_of(sid))
                   if c.get("session") != str(sid)]
    try:
        return render(data, live, earlier)
    except Exception:  # noqa: BLE001
        return ""


def remember_in_project(session_id, entry: dict, repo: "str | None") -> None:
    """Hand a correction to project memory (a ``gotcha``) so a LATER chat on
    the same project recalls it. Only for chats that learn; never for a
    temporary chat."""
    if not entry or session_id is None or _temporary(session_id):
        return
    if os.environ.get("AIFORGE_CHAT_LESSONS_TO_MEMORY", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    try:
        from aiforge_core.runtime import chat_store
        if not chat_store.session_learns(session_id):
            return
        _note_project(repo or "", entry, session_id)
        from aiforge_core.runtime.tools.memory_write import memory_write
        said = f' (after the agent said: "{entry["said"]}")' if entry.get("said") else ""
        memory_write(text=f'User correction: "{entry["text"]}"{said}',
                     kind="gotcha", tags=["correction", "chat_lessons"],
                     repo=repo or "notes", source="chat_lessons")
    except Exception as exc:  # noqa: BLE001
        log.debug("correction not handed to memory: %s", exc)


def forget(session_id) -> None:
    """The chat was deleted: drop its lessons."""
    if session_id is None:
        return
    with _LOCK:
        _MEM.pop(str(session_id), None)
        try:
            os.remove(_path(session_id))
        except OSError:
            pass


__all__ = ["MARK", "block", "enabled", "forget", "is_correction", "lessons",
           "note_correction", "note_failed", "project_lessons",
           "remember_in_project", "render"]
