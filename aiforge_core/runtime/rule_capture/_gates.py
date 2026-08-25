"""Gate-intent recognition (OFFER only, never sets a flag), whole-command
commit matching, and explicit gate-flag set/clear/query + applied-flag bookkeeping.

Split out of the former single ``rule_capture`` module VERBATIM.
"""
from __future__ import annotations

import os
import re

from ._base import (
    _LOCK,
    _SESSION_ITEMS,
    _file_lock,
    _flags_path,
    _index_path,
    _load_flags,
    _load_index,
    _save_flags,
    _save_index,
    log,
    repo_key,
)


# ─────────────────────────── gate-intent recognition ────────────────
#
# IMPORTANT SAFETY BOUNDARY: recognition NEVER sets a gate flag. It only
# RECOGNIZES that a rule *might* be asking to disable an approval gate, so the
# UI can OFFER the user an explicit, scoped, revocable opt-in (the pill button).
# Disabling a gate is always a separate, user-confirmed ``set_gate_flag`` call.

# "Strong" auto-approve phrases — sufficient on their own (still negation-gated).
_COMMIT_STRONG = (
    "commit directly", "commit without asking", "commit automatically",
    "auto commit", "auto-commit", "autocommit", "commit on my behalf",
    "don't ask before commit", "do not ask before commit",
    "dont ask before commit", "without asking before commit",
    "stop asking before commit", "no need to ask before commit",
)
# "Weak" phrases — only count when an explicit action token co-occurs, so a bare
# "the machine has access" with no commit/push verb does NOT trigger an offer.
_COMMIT_WEAK = (
    "machine has access", "machine has full access", "you have access",
    "full access", "i trust you",
)
_COMMIT_ACTIONS = ("commit", "push", "git add")

_DELETE_STRONG = (
    "delete without asking", "delete automatically",
    "don't ask before delet", "do not ask before delet",
    "dont ask before delet", "without asking before delet",
    "stop asking before delet", "allow delete", "allow deletes",
)
_DELETE_WEAK: tuple[str, ...] = ()
_DELETE_ACTIONS = ("delete", "remove")

# Negation that FLIPS OFF an auto-approve intent: a negation token directly
# governing the action verb ("never commit", "don't auto-commit", "do not
# delete"). The lookahead keeps the POSITIVE "no-ask" forms ("don't ask before
# commit", "without asking") from being read as negations of the action.
_NEG_GUARD = (r"\b(?:never|do\s+not|don'?t|not|no|without)\s+"
              r"(?!ask|asking|prompt|prompting|confirm|confirming|confirmation|"
              r"check|checking|need|require|me\b|stopping)"
              r"(?:auto[\s-]?)?")
_NEG_COMMIT_RE = re.compile(_NEG_GUARD + r"(?:commit|push)", re.IGNORECASE)
_NEG_DELETE_RE = re.compile(_NEG_GUARD + r"(?:delete|remove)", re.IGNORECASE)


def _intent(text: str, strong: tuple, weak: tuple, actions: tuple,
            neg_re: re.Pattern) -> bool:
    has_action = any(a in text for a in actions)
    hit = any(p in text for p in strong) or (
        has_action and any(p in text for p in weak))
    if not hit:
        return False
    if neg_re.search(text):           # negation-aware: "never commit" → reject
        return False
    return True


def recognize_gate_intent(canonical, *, category: str = "rule") -> str | None:
    """Return ``"commit"`` / ``"delete"`` / ``None`` if a captured rule *reads
    like* a request to stop asking before commits / deletes — used ONLY to OFFER
    the user an explicit opt-in. NEVER sets a flag.

    Negation-aware ("never commit directly" → None), requires an action token to
    co-occur for weak phrases, and only applies to ``category == "rule"`` (a
    softer "feedback" is never treated as a gate-disable request).

    ``canonical`` may be the classification dict or the canonical string."""
    if isinstance(canonical, dict):
        category = canonical.get("category", category)
        canonical = canonical.get("canonical", "")
    if category != "rule":
        return None
    text = (canonical or "").lower().strip()
    if not text:
        return None
    if _intent(text, _COMMIT_STRONG, _COMMIT_WEAK, _COMMIT_ACTIONS, _NEG_COMMIT_RE):
        return "commit"
    if _intent(text, _DELETE_STRONG, _DELETE_WEAK, _DELETE_ACTIONS, _NEG_DELETE_RE):
        return "delete"
    return None


# Intent → the gate flag the (explicit) opt-in would set.
GATE_INTENT_FLAG = {"commit": "commit_auto_approve", "delete": "allow_delete"}
_VALID_FLAGS = set(GATE_INTENT_FLAG.values())


# ─────────────────────────── gate command matching ──────────────────

# A shell separator / expansion anywhere means the command is NOT a single git
# invocation — so a chained `git add . && curl x|sh` is never auto-approved.
_SHELL_SEP_RE = re.compile(r"&&|\|\||;|\||\n|\$\(|`")
# ATOMIC GROUP, and it matters. `(?:-[A-Za-z]\S*\s+|\S+=\S+\s+)*` is two
# ambiguous alternatives under a `*`, so a long run of option-like tokens that
# never reaches commit/add/push backtracks exponentially — and the input is a
# shell command the MODEL supplies. `re.search` holds the interpreter, so
# neither Stop nor a signal can preempt it: the turn wedges for good. `(?>...)`
# (Python 3.11+, and this project requires >=3.11) forbids re-entering the
# group once it has matched, which is exactly the backtracking that explodes.
_GIT_HEAD_RE = re.compile(
    r"^\s*git\s+(?>(?:-[A-Za-z]\S*|\S+=\S+)\s+)*(?:commit|add|push)\b",
    re.IGNORECASE)


def is_commit_command(cmd: str) -> bool:
    """True ONLY when the WHOLE command is a single ``git commit|add|push``
    invocation. Rejects anything containing a shell separator/expansion
    (``&&``, ``||``, ``;``, ``|``, newline, ``$(``, backtick) so a chained
    command after a git verb (``git commit && rm -rf``, ``git add . | sh``) is
    NOT treated as an auto-approvable commit."""
    cmd = cmd or ""
    if _SHELL_SEP_RE.search(cmd):
        return False
    return bool(_GIT_HEAD_RE.match(cmd))


# ─────────────────────────── explicit gate flags ────────────────────

def set_gate_flag(name: str, *, scope: str, repo: str | None = None,
                  session_id=None, rule_id: str | None = None,
                  allow_global: bool = False) -> dict:
    """EXPLICITLY enable a gate-disable flag for a scope. This is the ONLY way a
    gate gets disabled — never a classifier decision.

    - REFUSES ``scope == "global"`` unless ``allow_global=True`` (the UI offers
      only session/repo; global needs a dedicated, separately-confirmed action).
    - Never WIDENS scope: a ``session`` request with no ``session_id`` or a
      ``project`` request with no ``repo`` is DROPPED (logged, no-op) rather
      than falling through to global.
    - When ``rule_id`` is given, records the flag on that captured item's
      ``applied_flags`` so undo/rescope/delete can revoke it.
    """
    if name not in _VALID_FLAGS:
        return {"ok": False, "applied": False, "reason": f"unknown flag: {name}"}
    scope = (scope or "").strip().lower()
    if scope == "global" and not allow_global:
        return {"ok": False, "applied": False, "scope": scope,
                "reason": "global gate-disable requires explicit confirmation"}
    if scope == "session" and session_id is None:
        log.warning("set_gate_flag drop: session scope with no session_id (%s)", name)
        return {"ok": False, "applied": False, "scope": scope,
                "reason": "session scope needs a session_id"}
    if scope == "project" and not repo:
        log.warning("set_gate_flag drop: project scope with no repo (%s)", name)
        return {"ok": False, "applied": False, "scope": scope,
                "reason": "project scope needs a repo"}
    if scope not in ("global", "project", "session"):
        return {"ok": False, "applied": False, "scope": scope,
                "reason": f"invalid scope: {scope}"}
    rkey = repo_key(repo)
    try:
        with _LOCK, _file_lock(_flags_path()):
            flags = _load_flags()
            if scope == "session":
                flags["session"].setdefault(str(session_id), {})[name] = True
            elif scope == "project":
                flags["repo"].setdefault(rkey, {})[name] = True
            else:  # global, allow_global already verified
                flags["global"][name] = True
            _save_flags(flags)
    except Exception as exc:  # noqa: BLE001
        log.debug("set_gate_flag failed: %s", exc)
        return {"ok": False, "applied": False, "scope": scope, "reason": str(exc)}
    if rule_id:
        _record_applied_flag(rule_id, name, scope, rkey, session_id)
    return {"ok": True, "applied": True, "name": name, "scope": scope,
            "repo": rkey}


def clear_gate_flag(name: str, *, scope: str, repo: str | None = None,
                    session_id=None) -> bool:
    """Revoke a gate-disable flag for a scope. Returns True when something was
    removed."""
    scope = (scope or "").strip().lower()
    rkey = repo_key(repo)
    removed = False
    try:
        with _LOCK, _file_lock(_flags_path()):
            flags = _load_flags()
            if scope == "session" and session_id is not None:
                removed = flags.get("session", {}).get(
                    str(session_id), {}).pop(name, None) is not None
            elif scope == "project" and rkey:
                removed = flags.get("repo", {}).get(
                    rkey, {}).pop(name, None) is not None
            elif scope == "global":
                removed = flags.get("global", {}).pop(name, None) is not None
            if removed:
                _save_flags(flags)
    except Exception as exc:  # noqa: BLE001
        log.debug("clear_gate_flag failed: %s", exc)
    return removed


def _record_applied_flag(rule_id: str, name: str, scope: str,
                         rkey: str | None, session_id) -> None:
    """Append ``name`` to a captured item's ``applied_flags`` (with the scope it
    was set at) so undo/rescope can revoke exactly that flag."""
    entry = {"name": name, "scope": scope, "repo": rkey,
             "session_id": (str(session_id) if session_id is not None else None)}
    # Session items live in memory; persistent items in the index.
    with _LOCK:
        for items in _SESSION_ITEMS.values():
            for it in items:
                if it.get("id") == rule_id:
                    it.setdefault("applied_flags", []).append(entry)
                    return
    with _LOCK, _file_lock(_index_path()):
        idx = _load_index()
        it = idx.get("items", {}).get(rule_id)
        if it is not None:
            it.setdefault("applied_flags", []).append(entry)
            _save_index(idx)


def _clear_applied_flags(item: dict) -> None:
    """Revoke every gate flag a captured item enabled (re-enabling the gate)."""
    for entry in item.get("applied_flags") or []:
        try:
            clear_gate_flag(entry.get("name"), scope=entry.get("scope") or "",
                            repo=entry.get("repo"),
                            session_id=entry.get("session_id"))
        except Exception as exc:  # noqa: BLE001
            log.debug("_clear_applied_flags failed: %s", exc)


def flag_active(name: str, *, repo: str | None = None, session_id=None) -> bool:
    """Is gate flag ``name`` active for this context?

    AUTONOMOUS runs (``session_id is None``) IGNORE all chat-set global/repo/
    session flags — an autonomous ticket run must never be weakened by a flag a
    chat set. It honors ONLY an explicit env opt-in
    ``AIFORGE_AUTONOMOUS_<NAME>=1``.

    An ATTACHED chat session honors session → repo → global (first level that
    defines it wins)."""
    if session_id is None:
        env = os.environ.get(f"AIFORGE_AUTONOMOUS_{name.upper()}", "").strip().lower()
        return env in ("1", "true", "yes", "on")
    try:
        flags = _load_flags()
    except Exception:  # noqa: BLE001
        return False
    sess = flags.get("session", {}).get(str(session_id), {})
    if name in sess:
        return bool(sess[name])
    rkey = repo_key(repo)
    if rkey:
        rep = flags.get("repo", {}).get(rkey, {})
        if name in rep:
            return bool(rep[name])
    g = flags.get("global", {})
    if name in g:
        return bool(g[name])
    return False


def flag_active_scope(name: str, *, repo: str | None = None,
                      session_id=None) -> str | None:
    """The scope at which ``name`` is active for this context (for audit
    events), or None. Mirrors ``flag_active`` precedence."""
    if session_id is None:
        env = os.environ.get(f"AIFORGE_AUTONOMOUS_{name.upper()}", "").strip().lower()
        return "env" if env in ("1", "true", "yes", "on") else None
    try:
        flags = _load_flags()
    except Exception:  # noqa: BLE001
        return None
    if name in flags.get("session", {}).get(str(session_id), {}):
        return "session"
    rkey = repo_key(repo)
    if rkey and name in flags.get("repo", {}).get(rkey, {}):
        return "repo"
    if name in flags.get("global", {}):
        return "global"
    return None


def _prune_stale_session_flags(flags: dict) -> bool:
    """Drop session-scoped gate flags whose session no longer exists (deleted /
    old numbering) — a per-session auto-approve is meaningless once that session
    is gone, but it lingered in the Auto-approvals panel forever (e.g. the stale
    "commits auto-approved · session 7020"). Returns True if anything changed."""
    session_flags = flags.get("session") or {}
    if not session_flags:
        return False
    try:
        from aiforge_core.runtime import chat_store
        live = {str(s.get("id")) for s in (chat_store.list_sessions() or [])}
    except Exception:  # noqa: BLE001 — can't confirm liveness → leave as-is
        return False
    stale = [s for s in session_flags if str(s) not in live]
    for s in stale:
        session_flags.pop(s, None)
    if stale:
        flags["session"] = session_flags
    return bool(stale)


def _active_nested(section: dict) -> dict:
    """``{key: {name: True}}`` for the truthy flags of a nested (repo/session)
    scope, dropping keys with nothing active."""
    out: dict = {}
    for key, d in (section or {}).items():
        active = {n: True for n, v in (d or {}).items() if v}
        if active:
            out[key] = active
    return out


def list_flags() -> dict:
    """All active gate-disable flags, grouped by scope, for the Auto-approvals
    panel. Only truthy flags are listed. Stale session flags (session deleted)
    are pruned + persisted so the panel self-cleans."""
    try:
        flags = _load_flags()
    except Exception:  # noqa: BLE001
        return {"global": {}, "repo": {}, "session": {}}
    try:
        if _prune_stale_session_flags(flags):
            _save_flags(flags)          # persist the cleanup
    except Exception:  # noqa: BLE001 — pruning is best-effort, never break listing
        pass
    return {
        "global": {n: True for n, v in (flags.get("global") or {}).items() if v},
        "repo": _active_nested(flags.get("repo") or {}),
        "session": _active_nested(flags.get("session") or {}),
    }
