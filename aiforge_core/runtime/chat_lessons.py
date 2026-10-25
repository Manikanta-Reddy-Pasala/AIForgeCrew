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
MAX_RULES = 8
_TEXT_CHARS = 500
_SAID_CHARS = 160
_BLOCK_CHARS = 3500

_LOCK = threading.Lock()
_MEM: dict = {}          # temporary chats: session id -> lessons

# The user telling the agent it got something wrong. Only read on a message
# that follows an answer (there is something to correct). Kept narrow: a
# false match is pinned as "the user corrected you" for the rest of the chat.
_STRONG = (
    r"that'?s (?:wrong|not (?:right|correct|what i (?:asked|wanted|meant)))"
    r"|(?:that|this|it) (?:is|was) (?:wrong|incorrect|not right)"
    r"|wrong (?:again|file|repo|repository|branch|folder|place|path|command|"
    r"output|answer|one)"
    r"|not what i (?:asked|wanted|meant|said)"
    r"|you (?:forgot|missed|broke|ignored|skipped|deleted|overwrote|"
    r"didn'?t (?:listen|read|follow|run|check|test)|did not (?:listen|read|follow|run|check|test)|"
    r"were (?:told|asked)|should(?:n'?t| not)(?: have)?)\b"
    r"|i (?:already )?told you"
    r"|(?:same|that) (?:mistake|error) again|again (?:the )?same (?:mistake|error)")
_CORRECTION = re.compile(
    r"\b(?:" + _STRONG +
    r"|^\s*no[,.!]+ (?:not|that'?s|it'?s|wrong|don'?t|stop)"
    r"|(?:don'?t|do not|never|stop) (?:touch|touching|change|changing|edit|editing|"
    r"commit|committing|push|pushing|delete|deleting|modify|modifying|rewrite|"
    r"rewriting|guess|guessing)\b"
    r"|not (?:like that|that one|this one)"
    r"|why did you (?:delete|remove|change|edit|break|touch|overwrite|revert|commit|push)"
    r"|i (?:already )?asked you"
    r")", re.I | re.M)
#: The subset that goes to the PROJECT's lessons (every later chat): only an
#: unmistakable "you got it wrong", never a one-off "don't commit yet".
_STRONG_RE = re.compile(r"\b(?:" + _STRONG + r")", re.I)


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
    return {"corrections": [], "failed": [], "rules": []}


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
                       if isinstance(f, dict) and f.get("text")],
            "rules": [r for r in data.get("rules") or []
                      if isinstance(r, dict) and r.get("text")]}


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


def is_strong_correction(text: str) -> bool:
    """An unmistakable "you got it wrong" — worth keeping for the project."""
    return bool(_STRONG_RE.search(_users_words(text or "")[:4000]))


def drop_after_turn(session_id, turn: int) -> None:
    """The chat was cut back to ``turn`` user messages (edit-and-resend): the
    corrections made in the turns that are gone are dropped too."""
    if session_id is None:
        return
    try:
        with _LOCK:
            data = _load(session_id)
            data["corrections"] = [c for c in data["corrections"]
                                   if not (isinstance(c.get("turn"), int)
                                           and c["turn"] > turn)]
            _save(session_id, data)
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons write failed (session=%s): %s", session_id, exc)


def note_rule(session_id, text: str, rid: "str | None" = None) -> None:
    """A rule the user set for this chat (rule capture, scope "session"):
    the in-process list is gone on a restart, this file is not."""
    t = _flat(text, _TEXT_CHARS)
    if not enabled() or session_id is None or not t:
        return
    try:
        with _LOCK:
            data = _load(session_id)
            items = [r for r in data["rules"] if r.get("text") != t]
            items.append({"text": t, "id": rid, "at": int(time.time())})
            data["rules"] = items[-MAX_RULES:]
            _save(session_id, data)
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons write failed (session=%s): %s", session_id, exc)


def rules(session_id) -> list:
    """The chat's kept rules (``{"text", "id"}``), for the rules list after a
    restart emptied the in-process one."""
    return list(lessons(session_id).get("rules") or [])


def drop_rule_id(rid: str) -> bool:
    """Undo a kept rule by its capture id, whichever chat holds it. True when
    one was removed."""
    if not rid:
        return False
    from aiforge_core.runtime import repo_ident
    base = os.path.join(repo_ident.chat_scratch_root())
    hit = False
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    for name in names:
        if not name.startswith("session-"):
            continue
        sid = name[len("session-"):]
        try:
            with _LOCK:
                data = _load(sid)
                keep = [r for r in data["rules"] if r.get("id") != rid]
                if len(keep) != len(data["rules"]):
                    data["rules"] = keep
                    _save(sid, data)
                    hit = True
        except Exception:  # noqa: BLE001
            continue
    for sid, data in list(_MEM.items()):
        keep = [r for r in data.get("rules") or [] if r.get("id") != rid]
        if len(keep) != len(data.get("rules") or []):
            data["rules"] = keep
            hit = True
    return hit


def drop_rule(session_id, text: str) -> None:
    """The user undid that rule: the agent must stop following it."""
    t = _flat(text, _TEXT_CHARS)
    if session_id is None or not t:
        return
    try:
        with _LOCK:
            data = _load(session_id)
            data["rules"] = [r for r in data["rules"] if r.get("text") != t]
            _save(session_id, data)
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons write failed (session=%s): %s", session_id, exc)


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


_REPO_CACHE: dict = {}


def _repo_of(session_id) -> str:
    """The chat's project key (cached: read on every step's pin)."""
    key = str(session_id)
    if key in _REPO_CACHE:
        return _REPO_CACHE[key]
    try:
        from aiforge_core.runtime import chat_store, rule_capture
        sess = chat_store.get_session(session_id) or {}
        repo = rule_capture.repo_key(sess.get("cwd") or "") or ""
    except Exception:  # noqa: BLE001
        return ""
    if len(_REPO_CACHE) > 512:
        _REPO_CACHE.clear()
    _REPO_CACHE[key] = repo
    return repo


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
    rules = [r.get("text") for r in data.get("rules") or []][-MAX_RULES:]
    have = {c.get("text") for c in corr}
    earlier = [c for c in earlier or () if c.get("text") not in have][-MAX_PROJECT:]
    if not corr and not failed and not earlier and not rules:
        return ""
    head = (f"{MARK} — the user's corrections and instructions so far, and "
            "what was tried and did not work. Do not repeat these mistakes; a "
            "newer message from the user overrides an older line here.")
    lines: list = []
    for c in earlier:
        lines.append(f'- in an earlier chat on this project the user said: "{c["text"]}"')
    for c in corr:
        said = f' (you had said: "{c["said"]}")' if c.get("said") else ""
        lines.append(f'- the user said: "{c["text"]}"{said}')
    for t in rules:
        lines.append(f"- rule the user set for this chat: {t}")
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
    if data.get("rules") and sid is not None:
        # A rule still in the live session list is already in the rule book.
        try:
            from aiforge_core.runtime.rule_capture._base import _SESSION_ITEMS
            live_rules = {_flat(it.get("canonical"), _TEXT_CHARS)
                          for it in _SESSION_ITEMS.get(str(sid), [])}
            data["rules"] = [r for r in data["rules"] if r.get("text") not in live_rules]
        except Exception:  # noqa: BLE001
            pass
    earlier = []
    if sid is not None and not _temporary(sid):
        earlier = [c for c in project_lessons(_repo_of(sid))
                   if c.get("session") != str(sid)]
    try:
        return render(data, live, earlier)
    except Exception:  # noqa: BLE001
        return ""


def remember_in_project(session_id, entry: dict, repo: "str | None") -> None:
    """Keep a correction in the PROJECT's lessons so a later chat on the same
    repository is told too. Only for chats that learn; never for a temporary
    chat. A plain file the chat's deletion can take back (a memory note could
    not be)."""
    if not entry or not repo or session_id is None or _temporary(session_id):
        return
    if os.environ.get("AIFORGE_CHAT_LESSONS_PROJECT", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    try:
        from aiforge_core.runtime import chat_store
        if not chat_store.session_learns(session_id):
            return
        _note_project(repo, entry, session_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("correction not kept for the project: %s", exc)


def forget(session_id) -> None:
    """The chat was deleted: drop its lessons."""
    if session_id is None:
        return
    repo = _repo_of(session_id)
    with _LOCK:
        _MEM.pop(str(session_id), None)
        try:
            os.remove(_path(session_id))
        except OSError:
            pass
        before = project_lessons(repo) if repo else []
        items = [c for c in before if c.get("session") != str(session_id)]
        if repo and len(items) != len(before):
            # What this chat taught the project goes with it.
            try:
                from aiforge_core.config import _atomic
                _atomic.write_text(_project_path(repo), json.dumps(items, indent=1))
            except OSError:
                pass
    _REPO_CACHE.pop(str(session_id), None)


__all__ = ["MARK", "block", "enabled", "forget", "is_correction", "lessons",
           "drop_after_turn", "drop_rule", "drop_rule_id", "is_strong_correction",
           "rules", "note_correction", "note_failed",
           "note_rule", "project_lessons",
           "remember_in_project", "render"]
