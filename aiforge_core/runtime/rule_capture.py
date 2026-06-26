"""Deterministic, always-on Rule / Memory / Feedback capture.

Every user chat message is run through ONE capped LLM ``classify`` pass
(independent of which model the agent itself uses), so a directive, fact, or
correction the user states in passing is captured deterministically rather than
relying on the agent model choosing to call ``remember_rule``.

Pipeline (all in the chat handler, BEFORE the agent runs):

    classify(message) -> {category, scope, canonical, confidence, task_present}
        │  category != "none" and confidence >= threshold?
        ▼
    store(c)            -> routes by category × scope (md_store / repo rules /
                           AiForgeMemory / in-session store)
    apply_behavioral(c) -> recognizes commit/delete "never re-ask" rule intents
                           and sets per-scope gate flags the approval gates read

Everything FAILS OPEN: any error in classify/store/apply_behavioral returns a
safe default and never raises into the chat turn. A capture must never break a
chat.

See ``docs/superpowers/specs/2026-06-26-rule-memory-capture-design.md``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path

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
    "Respond with STRICT JSON ONLY, no prose, no code fence:\n"
    '{\"category\":\"rule|memory|feedback|none\",'
    '\"scope\":\"global|project|session\",'
    '\"canonical\":\"<cleaned one-line directive/fact>\",'
    '\"confidence\":0.0-1.0,\"task_present\":true|false}'
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
    return {"category": cat, "scope": scope, "canonical": canonical,
            "confidence": conf, "task_present": task_present}


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
        _index_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
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
        _flags_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture flags save failed: %s", exc)


# ─────────────────────────── store ──────────────────────────────────

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "rule").lower()).strip("-")
    return (s or "rule")[:60]


def _write_repo_rule(repo_root: str, name: str, body: str) -> str | None:
    """Best-effort write of a Cursor-style rule into ``<repo_root>/.aiforge/
    rules/<slug>.md`` so the ticket/doer repo_rules pipeline honors it too.
    Returns the path written, or None on any failure."""
    try:
        d = Path(repo_root).expanduser() / ".aiforge" / "rules"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_slug(name)}.md"
        front = ("---\n"
                 f"name: {name}\n"
                 "alwaysApply: true\n"
                 "---\n\n")
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
        "rule_path": None, "undone": False,
    }
    try:
        if scope == "session":
            item["location"] = "session"
            _session_add(session_id, item)
            return {"id": rid, "location": "session", "category": cat, "scope": scope}

        from aiforge_core.memory import md_store
        if cat in ("rule", "feedback"):
            if scope == "global":
                src, title = "rules:global", "AIForge rules (all sessions)"
            else:  # project
                r = repo or "project"
                src, title = f"rules:{r}", f"{r} — rules"
            md_store.append_bullet(source=src, title=title, bullet=canonical,
                                   kind=cat, tags=[cat, scope])
            item["md_source"] = src
            item["md_bullet"] = "- " + canonical
            item["location"] = f"md:{src}"
            if scope == "project" and repo_root:
                rp = _write_repo_rule(repo_root, canonical[:60] or "rule", canonical)
                if rp:
                    item["rule_path"] = rp
        else:  # memory
            from aiforge_core.runtime.tools.memory_write import memory_write
            mrepo = repo or "notes"
            memory_write(text=canonical, kind="note",
                         tags=[cat, scope], repo=mrepo)
            item["location"] = f"memory:{scope}"
    except Exception as exc:  # noqa: BLE001 — store must never raise
        log.debug("rule_capture store soft-fail: %s", exc)

    # Persist the index entry (global/project only — session is in-memory).
    with _LOCK:
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


# ─────────────────────────── apply_behavioral ───────────────────────

# Recognized high-value rule intents → gate flags (the "never re-ask" core).
_COMMIT_PHRASES = (
    "commit directly", "commit without asking", "commit automatically",
    "auto commit", "auto-commit", "autocommit",
    "don't ask before commit", "do not ask before commit",
    "dont ask before commit", "without asking before commit",
    "machine has access", "machine has full access",
)
_DELETE_PHRASES = (
    "delete without asking", "allow delete", "allow deletes",
    "don't ask before delet", "do not ask before delet",
    "dont ask before delet", "without asking before delet",
    "trust delete", "delete automatically", "auto delete", "auto-delete",
)

_GIT_COMMIT_RE = re.compile(r"\bgit\s+(?:-[A-Za-z]\S*\s+|\S+=\S+\s+)*"
                            r"(commit|add|push)\b", re.IGNORECASE)


def is_commit_command(cmd: str) -> bool:
    """True when a shell command is a git commit/add/push (the actions the
    ``commit_auto_approve`` flag auto-approves)."""
    return bool(_GIT_COMMIT_RE.search(cmd or ""))


def _set_flag(name: str, *, scope: str, repo: str | None, session_id) -> None:
    with _LOCK:
        flags = _load_flags()
        if scope == "session" and session_id is not None:
            flags["session"].setdefault(str(session_id), {})[name] = True
        elif scope == "project" and repo:
            flags["repo"].setdefault(repo, {})[name] = True
        else:  # global (or unscoped fallback)
            flags["global"][name] = True
        _save_flags(flags)


def apply_behavioral(c: dict, *, repo: str | None = None,
                     session_id=None) -> list[str]:
    """Recognize commit/delete "never re-ask" rule intents in the canonical
    directive and set the per-scope gate flags the approval gates consult.
    Returns the flags applied (for the UI note); [] for unrecognized rules."""
    if c.get("category") not in ("rule", "feedback"):
        return []
    text = (c.get("canonical") or "").lower()
    scope = c.get("scope") or "global"
    applied: list[str] = []
    try:
        if any(p in text for p in _COMMIT_PHRASES):
            _set_flag("commit_auto_approve", scope=scope, repo=repo,
                      session_id=session_id)
            applied.append("commit_auto_approve")
        if any(p in text for p in _DELETE_PHRASES):
            _set_flag("allow_delete", scope=scope, repo=repo,
                      session_id=session_id)
            applied.append("allow_delete")
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture.apply_behavioral failed: %s", exc)
    return applied


def flag_active(name: str, *, repo: str | None = None, session_id=None) -> bool:
    """Is gate flag ``name`` active for this context? Precedence:
    session → repo → global (first level that defines it wins)."""
    try:
        flags = _load_flags()
    except Exception:  # noqa: BLE001
        return False
    if session_id is not None:
        sess = flags.get("session", {}).get(str(session_id), {})
        if name in sess:
            return bool(sess[name])
    if repo:
        rep = flags.get("repo", {}).get(repo, {})
        if name in rep:
            return bool(rep[name])
    g = flags.get("global", {})
    if name in g:
        return bool(g[name])
    return False


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
    memory writes are marked removed). True when an item was found."""
    # Session items first.
    with _LOCK:
        for sid, items in _SESSION_ITEMS.items():
            for i, it in enumerate(items):
                if it.get("id") == rid:
                    items.pop(i)
                    return True
    idx = _load_index()
    item = idx.get("items", {}).get(rid)
    if not item:
        return False
    _remove_storage(item)
    with _LOCK:
        idx = _load_index()
        if rid in idx.get("items", {}):
            del idx["items"][rid]
            _save_index(idx)
    return True


def rescope(rid: str, new_scope: str) -> dict:
    """Re-file a captured item under a new scope, preserving its id."""
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

    # Remove the old storage, then re-store under the new scope with same id.
    if in_session:
        with _LOCK:
            for items in _SESSION_ITEMS.values():
                for i, it in enumerate(list(items)):
                    if it.get("id") == rid:
                        items.pop(i)
    else:
        _remove_storage(found)
        with _LOCK:
            idx = _load_index()
            idx.get("items", {}).pop(rid, None)
            _save_index(idx)

    c = {"category": found.get("category"), "scope": new_scope,
         "canonical": found.get("canonical")}
    res = _do_store(c, rid=rid, repo=found.get("repo"),
                    session_id=found.get("session_id"), repo_root=None)
    return {"id": rid, "scope": new_scope, "category": found.get("category"),
            "location": res.get("location")}


__all__ = [
    "classify", "store", "apply_behavioral", "flag_active",
    "list_captured", "rescope", "undo", "is_commit_command",
]
