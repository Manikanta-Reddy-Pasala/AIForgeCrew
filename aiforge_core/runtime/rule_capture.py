"""Deterministic, always-on Rule / Memory / Feedback capture.

Every user chat message is run through ONE capped LLM ``classify`` pass
(independent of which model the agent itself uses), so a directive, fact, or
correction the user states in passing is captured deterministically rather than
relying on the agent model choosing to call ``remember_rule``.

Pipeline (all in the chat handler, BEFORE the agent runs):

    classify(message) -> {category, scope, canonical, confidence, task_present}
        │  category != "none" and confidence >= threshold?
        ▼
    store(c)                  -> routes by category × scope (md_store / repo
                                 rules / AiForgeMemory / in-session store)
    recognize_gate_intent(c)  -> RECOGNIZES (does NOT set) a commit/delete
                                 gate-disable request, so the UI can OFFER an
                                 explicit, scoped, revocable opt-in

A gate is NEVER disabled by the classifier. Disabling one is a separate,
user-confirmed ``set_gate_flag`` call (the pill opt-in). ``flag_active`` ignores
chat-set flags entirely for autonomous runs (session_id is None).

Everything FAILS OPEN: any error in classify/store/recognition returns a safe
default and never raises into the chat turn. A capture must never break a chat.

See ``docs/superpowers/specs/2026-06-26-rule-memory-capture-design.md``.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
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
    """The single, canonical key a repo is filed under for gate flags — used
    by BOTH ``set_gate_flag`` and every ``flag_active`` check so a flag set
    under one spelling is found under the other. Accepts a repo NAME
    (``"myrepo"``) or a path (``"/a/b/myrepo"``) → its basename."""
    if not cwd_or_root:
        return None
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


# ─────────────────────────── classify ───────────────────────────────

_SYS = (
    "You are a STRICT classifier that detects whether a user's chat message "
    "carries something the assistant should REMEMBER and apply later.\n\n"
    "Classify into exactly one category:\n"
    "- \"rule\": a standing directive / instruction about how to behave "
    "(\"always use yarn\", \"commit directly, the machine has access\", "
    "\"never force-push\").\n"
    "- \"memory\": a durable fact/preference to recall later "
    "(\"the staging DB is at db.staging\", \"my name is Sam\").\n"
    "- \"feedback\": a correction/preference on prior behaviour, softer than a "
    "hard rule (\"that was too verbose\", \"prefer shorter commits\").\n"
    "- \"none\": an ordinary task/question with nothing to remember.\n\n"
    "Also choose a scope:\n"
    "- \"global\": applies everywhere, all repos/sessions.\n"
    "- \"project\": applies to THIS repo only.\n"
    "- \"session\": applies to THIS conversation only.\n\n"
    "Default to \"project\" when the user references this repo/folder, "
    "\"global\" for universal directives, \"session\" for a one-off.\n\n"
    "Set \"task_present\" true when the message ALSO asks you to DO something "
    "now (build/fix/run/answer) in addition to stating the rule; false when it "
    "is PURELY a rule/fact/correction with no action requested.\n\n"
    "For \"rule\" or \"feedback\" ONLY: if the rule is scoped to a specific "
    "topic (e.g. deploys, a specific tool, a specific kind of file) rather "
    "than a universal directive, set \"triggers\" to 1-3 short lowercase "
    "topic words; leave it an empty list [] when the rule should ALWAYS "
    "apply regardless of topic.\n\n"
    "Respond with STRICT JSON ONLY, no prose, no code fence:\n"
    '{\"category\":\"rule|memory|feedback|none\",'
    '\"scope\":\"global|project|session\",'
    '\"canonical\":\"<cleaned one-line directive/fact>\",'
    '\"confidence\":0.0-1.0,\"task_present\":true|false,'
    '\"triggers\":[]}'
)


def _llm_complete(role: str, messages: list[dict], **kw) -> str:
    from aiforge_core.llm.client import complete
    return complete(role, messages, **kw)


def _extract_json(text: str) -> dict | None:
    """First balanced {...} object → dict, or None. String-aware brace match
    so braces inside string values don't confuse it."""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except (ValueError, TypeError):
                    return None
    return None


def _parse_classification(raw: str) -> dict | None:
    obj = _extract_json(raw)
    if obj is None:
        return None
    cat = str(obj.get("category", "")).strip().lower()
    scope = str(obj.get("scope", "")).strip().lower()
    if cat not in _VALID_CATEGORIES:
        return None
    if scope not in _VALID_SCOPES:
        scope = "session"
    canonical = str(obj.get("canonical") or "").strip().replace("\n", " ")
    try:
        conf = float(obj.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    task_present = obj.get("task_present")
    if not isinstance(task_present, bool):
        task_present = True
    triggers_raw = obj.get("triggers") or []
    if not isinstance(triggers_raw, list):
        triggers_raw = []
    # Restrict to a charset safe for BOTH storage formats: the inline
    # "[triggers: a, b]" bullet (chat_agent._BULLET_TRIGGERS_RE) and the
    # "triggers: [a, b]" YAML frontmatter (_write_repo_rule / repo_rules).
    # Chars like ] , : # * { } " would corrupt one or the other — a
    # corrupted frontmatter parse drops triggers and silently flips a gated
    # rule to always-on, so strip everything outside [a-z0-9 _-].
    triggers = [re.sub(r"[^a-z0-9 _-]", "", str(t).lower()).strip()
                for t in triggers_raw if isinstance(t, str) and t.strip()][:3]
    triggers = [t for t in triggers if t]  # drop anything that sanitized to empty
    triggers = [t for t in triggers if re.search(r"[a-z0-9]", t)]  # drop pure-punctuation junk
    return {"category": cat, "scope": scope, "canonical": canonical,
            "confidence": conf, "task_present": task_present,
            "triggers": triggers}


def classify(message: str, *, repo: str | None = None,
             session_id=None) -> dict:
    """ONE capped LLM call → a classification dict. FAILS OPEN: any
    error / non-JSON / unknown category / below-threshold confidence →
    ``{"category": "none", ...}``. The kill-switch env
    ``AIFORGE_RULE_CAPTURE_DISABLE=1`` short-circuits to none."""
    if _disabled() or not (message or "").strip():
        return _none()
    role = os.environ.get("AIFORGE_RULE_CAPTURE_ROLE", "enhancer")
    try:
        timeout = int(os.environ.get("AIFORGE_RULE_CLASSIFY_TIMEOUT_S", "15"))
    except ValueError:
        timeout = 15
    try:
        raw = _llm_complete(
            role,
            [{"role": "system", "content": _SYS},
             {"role": "user", "content": message.strip()[:4000]}],
            max_tokens=250, temperature=0.0, timeout_s=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — fail open, never break the turn
        log.debug("rule_capture.classify llm error (none): %s", exc)
        return _none()
    c = _parse_classification(raw or "")
    if c is None:
        return _none()
    if c["category"] == "none" or not c["canonical"]:
        return _none()
    if c["confidence"] < _min_conf():
        return _none()
    return c


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
    p = _flags_path()
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
    try:
        _atomic_write(_flags_path(), json.dumps(data, indent=2))
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture flags save failed: %s", exc)


# ─────────────────────────── store ──────────────────────────────────

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "rule").lower()).strip("-")
    return (s or "rule")[:60]


def _write_repo_rule(repo_root: str, name: str, body: str,
                     triggers: list[str] | None = None) -> str | None:
    """Best-effort write of a Cursor-style rule into ``<repo_root>/.aiforge/
    rules/<slug>.md`` so the ticket/doer repo_rules pipeline honors it too.
    ``triggers`` (if any) makes the rule topic-gated instead of always-on.
    Returns the path written, or None on any failure."""
    try:
        d = Path(repo_root).expanduser() / ".aiforge" / "rules"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_slug(name)}.md"
        trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
        front = "---\n" + f"name: {name}\n"
        if trig:
            front += "triggers: [" + ", ".join(trig) + "]\n"
        else:
            front += "alwaysApply: true\n"
        front += "---\n\n"
        path.write_text(front + body.strip() + "\n", encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture repo-rule write failed: %s", exc)
        return None


def _session_add(session_id, item: dict) -> None:
    if session_id is None:
        return
    with _LOCK:
        _SESSION_ITEMS.setdefault(str(session_id), []).append(item)


def _do_store(c: dict, *, rid: str, repo: str | None, session_id,
              repo_root: str | None) -> dict:
    cat = c.get("category")
    scope = c.get("scope")
    canonical = (c.get("canonical") or "").strip()
    item = {
        "id": rid, "category": cat, "scope": scope, "canonical": canonical,
        "repo": repo, "session_id": (str(session_id) if session_id is not None else None),
        "location": "", "md_source": None, "md_bullet": None,
        "rule_path": None, "undone": False, "triggers": [],
        # Gate-disable flags this captured item explicitly enabled (via an opt-in
        # pill). Recorded here so undo/rescope/delete can REVOKE them — a deleted
        # gate-disabling rule must re-enable the gate.
        "applied_flags": [],
    }
    try:
        if scope == "session":
            item["location"] = "session"
            _session_add(session_id, item)
            return {"id": rid, "location": "session", "category": cat, "scope": scope}

        from aiforge_core.memory import md_store
        if cat in ("rule", "feedback"):
            triggers = c.get("triggers") or []
            item["triggers"] = triggers
            if scope == "global":
                src, title = "rules:global", "AIForge rules (all sessions)"
            else:  # project
                r = repo or "project"
                src, title = f"rules:{r}", f"{r} — rules"
            bullet_text = (
                f"[triggers: {', '.join(triggers)}] {canonical}"
                if triggers else canonical)
            md_store.append_bullet(source=src, title=title, bullet=bullet_text,
                                   kind=cat, tags=[cat, scope])
            item["md_source"] = src
            item["md_bullet"] = "- " + bullet_text
            item["location"] = f"md:{src}"
            if cat == "rule" and scope == "project" and repo_root:
                rp = _write_repo_rule(repo_root, canonical[:60] or "rule",
                                      canonical, triggers=triggers)
                if rp:
                    item["rule_path"] = rp
            elif cat == "rule" and scope == "global":
                # Also land GLOBAL rules in the canonical repo_rules store
                # (~/.aiforge/rules) — the SAME store the Library UI + the
                # ticket/doer pipeline read — so a directive captured in passing
                # shows up alongside remember_rule / Library-form rules instead
                # of living only in md_store (invisible to the Library).
                try:
                    from aiforge_core.runtime import repo_rules as _rr
                    _res = _rr.write_rule(canonical[:60] or "rule", canonical,
                                          globs=(triggers or None),
                                          always=not triggers)
                    if _res.get("ok"):
                        item["rule_path"] = _res.get("path")
                except Exception as exc:  # noqa: BLE001 — best-effort
                    log.debug("rule_capture global repo-rule write failed: %s", exc)
        else:  # memory
            from aiforge_core.runtime.tools.memory_write import memory_write
            mrepo = repo or "notes"
            memory_write(text=canonical, kind="note",
                         tags=[cat, scope], repo=mrepo)
            item["location"] = f"memory:{scope}"
    except Exception as exc:  # noqa: BLE001 — store must never raise
        log.debug("rule_capture store soft-fail: %s", exc)

    # Persist the index entry (global/project only — session is in-memory).
    with _LOCK, _file_lock(_index_path()):
        idx = _load_index()
        idx["items"][rid] = item
        _save_index(idx)
    return {"id": rid, "location": item["location"], "category": cat, "scope": scope}


def store(c: dict, *, repo: str | None = None, session_id=None,
          repo_root: str | None = None) -> dict:
    """Route a classification to its store by category × scope. Generates a
    stable uuid id. Never raises — soft-fails and logs."""
    rid = uuid.uuid4().hex
    try:
        return _do_store(c, rid=rid, repo=repo, session_id=session_id,
                         repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture.store failed: %s", exc)
        return {"id": rid, "location": "", "category": c.get("category"),
                "scope": c.get("scope")}


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
_GIT_HEAD_RE = re.compile(
    r"^\s*git\s+(?:-[A-Za-z]\S*\s+|\S+=\S+\s+)*(?:commit|add|push)\b",
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
    for entry in list(item.get("applied_flags") or []):
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


def list_flags() -> dict:
    """All active gate-disable flags, grouped by scope, for the Auto-approvals
    panel. Only truthy flags are listed."""
    try:
        flags = _load_flags()
    except Exception:  # noqa: BLE001
        return {"global": {}, "repo": {}, "session": {}}
    out: dict = {"global": {}, "repo": {}, "session": {}}
    for n, v in (flags.get("global") or {}).items():
        if v:
            out["global"][n] = True
    for r, d in (flags.get("repo") or {}).items():
        active = {n: True for n, v in (d or {}).items() if v}
        if active:
            out["repo"][r] = active
    for s, d in (flags.get("session") or {}).items():
        active = {n: True for n, v in (d or {}).items() if v}
        if active:
            out["session"][s] = active
    return out


# ─────────────────────────── capture pre-filter ─────────────────────

_GREETINGS = {
    "hi", "hii", "hey", "hello", "yo", "sup", "thanks", "thank you",
    "thx", "ok", "okay", "k", "cool", "nice", "great", "yes", "no",
    "yep", "nope", "sure", "got it", "good", "perfect", "done",
    "thanks so much", "thank you so much", "thanks a lot",
}
# The preference/directive cue gate now lives in ONE place shared by every
# capture path (rule_capture, preference_capture, chat_learner).
from aiforge_core.runtime.capture_cues import has_cue as _has_cue


def should_classify(message: str) -> bool:
    """Cheap deterministic pre-filter: should this message even reach the LLM
    classifier? Skips short messages, pure greetings/acks, and messages with NO
    preference/directive cue — so an ordinary turn ("hi", "fix the bug") never
    pays the classify cost."""
    m = (message or "").strip()
    if len(m) < 12:
        return False
    low = m.lower().strip(" .!?")
    if low in _GREETINGS:
        return False
    if _has_cue(m):
        return True
    # Deterministic gate-intent (e.g. "commit directly, the machine has access")
    # is also a worthwhile cue even without a keyword above.
    return recognize_gate_intent(m, category="rule") is not None


# Imperative BUILD verbs that, with a "now"/"then" cadence, mean a real task is
# present — the backstop that FORCES the agent to run even if the classifier
# (wrongly) said task_present=false. Never drop a real task.
_ACTIONABLE_VERB_RE = re.compile(
    r"\b(fix|add|create|implement|build|write|change|update|refactor|run|"
    r"delete|remove)\b", re.IGNORECASE)
_ACTIONABLE_TIME_RE = re.compile(r"\b(now|then)\b", re.IGNORECASE)


def looks_actionable(message: str) -> bool:
    """True when the message carries an imperative build verb alongside a
    'now'/'then' cadence (e.g. "...and now fix the bug") — a deterministic
    backstop so a combined rule+task message is never short-circuited as
    pure-capture."""
    m = message or ""
    return bool(_ACTIONABLE_VERB_RE.search(m) and _ACTIONABLE_TIME_RE.search(m))


# ─────────────────────────── transparency: list/rescope/undo ────────

def list_captured(repo: str | None = None, session_id=None) -> list[dict]:
    """Captured items for the transparency UI. Global items always; project
    items filtered by ``repo`` when given; plus this session's ephemeral
    items."""
    out: list[dict] = []
    try:
        idx = _load_index()
        for it in idx.get("items", {}).values():
            if it.get("undone"):
                continue
            if repo is not None:
                if it.get("scope") == "project" and it.get("repo") != repo:
                    continue
            out.append(it)
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture.list_captured failed: %s", exc)
    if session_id is not None:
        with _LOCK:
            out.extend(list(_SESSION_ITEMS.get(str(session_id), [])))
    return out


def _remove_storage(item: dict) -> None:
    """Best-effort reversal of an item's storage side effects."""
    # md_store bullet
    src = item.get("md_source")
    line = item.get("md_bullet")
    if src and line:
        try:
            from aiforge_core.memory import md_store
            p = md_store._find_by_source(src)
            if p is not None:
                kept = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                        if ln.strip() != line.strip()]
                p.write_text("\n".join(kept) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("rule_capture undo md bullet failed: %s", exc)
    # repo rule file
    rp = item.get("rule_path")
    if rp:
        try:
            Path(rp).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("rule_capture undo rule file failed: %s", exc)


def undo(rid: str) -> bool:
    """Remove a captured item (reverses md/repo-rule storage best-effort;
    REVOKES any gate flag it enabled so a deleted gate-disabling rule re-enables
    the gate). True when an item was found."""
    # Session items first.
    with _LOCK:
        for sid, items in _SESSION_ITEMS.items():
            for i, it in enumerate(items):
                if it.get("id") == rid:
                    found = items.pop(i)
                    break
            else:
                continue
            break
        else:
            found = None
    if found is not None:
        _clear_applied_flags(found)            # gate re-enabled
        return True
    idx = _load_index()
    item = idx.get("items", {}).get(rid)
    if not item:
        return False
    _clear_applied_flags(item)                 # gate re-enabled
    _remove_storage(item)
    with _LOCK, _file_lock(_index_path()):
        idx = _load_index()
        if rid in idx.get("items", {}):
            del idx["items"][rid]
            _save_index(idx)
    return True


def rescope(rid: str, new_scope: str, *, repo_root: str | None = None) -> dict:
    """Re-file a captured item under a new scope, preserving its id. Also MOVES
    any gate flag it enabled to the new scope (clear old, set new) so the
    gate-disable follows the rule. ``repo_root`` is threaded so a global→project
    rescope actually writes ``<repo>/.aiforge/rules/``."""
    new_scope = (new_scope or "").strip().lower()
    if new_scope not in _VALID_SCOPES:
        return {"ok": False, "error": f"invalid scope: {new_scope}"}

    # Locate the item (session store or persistent index).
    found: dict | None = None
    in_session = False
    with _LOCK:
        for items in _SESSION_ITEMS.values():
            for it in items:
                if it.get("id") == rid:
                    found = dict(it)
                    in_session = True
                    break
            if found:
                break
    if found is None:
        idx = _load_index()
        it = idx.get("items", {}).get(rid)
        if it:
            found = dict(it)
    if found is None:
        return {"ok": False, "error": "not found"}

    if found.get("scope") == new_scope:
        return {"id": rid, "scope": new_scope, "category": found.get("category")}

    old_flags = list(found.get("applied_flags") or [])

    # Remove the old storage + clear old gate flags, then re-store under the new
    # scope with the same id.
    _clear_applied_flags(found)
    if in_session:
        with _LOCK:
            for items in _SESSION_ITEMS.values():
                for i, it in enumerate(list(items)):
                    if it.get("id") == rid:
                        items.pop(i)
    else:
        _remove_storage(found)
        with _LOCK, _file_lock(_index_path()):
            idx = _load_index()
            idx.get("items", {}).pop(rid, None)
            _save_index(idx)

    c = {"category": found.get("category"), "scope": new_scope,
         "canonical": found.get("canonical")}
    res = _do_store(c, rid=rid, repo=found.get("repo"),
                    session_id=found.get("session_id"), repo_root=repo_root)
    # Move each gate flag to the new scope (recorded onto the new item). A move
    # to global is refused unless explicitly confirmed → the gate stays enabled.
    for entry in old_flags:
        set_gate_flag(entry.get("name"), scope=new_scope,
                      repo=found.get("repo"), session_id=found.get("session_id"),
                      rule_id=rid)
    return {"id": rid, "scope": new_scope, "category": found.get("category"),
            "location": res.get("location")}


__all__ = [
    "classify", "store", "list_captured", "rescope", "undo",
    "recognize_gate_intent", "GATE_INTENT_FLAG",
    "is_commit_command", "repo_key",
    "set_gate_flag", "clear_gate_flag", "flag_active", "flag_active_scope",
    "list_flags", "should_classify", "looks_actionable",
]
