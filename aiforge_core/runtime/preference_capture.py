"""Auto-capture durable USER PREFERENCES from chat, mapped to memory and
UPSERTED so the user never has to restate them.

The gap: a stated preference ("use ENG as the default Jira project", "all repos
live under /work", "I prefer tabs") only stuck if the post-turn Learner LLM
happened to distil it, and even then it was APPENDED — restating a changed
preference piled up contradictions. This module closes both:

  1. A cheap regex GATE decides a turn *might* carry a preference.
  2. The LLM MAPS the message to a stable ``subject`` slug — reusing an existing
     preference's subject when it's about the same thing — and extracts the
     ``value``. This is the "map to an existing memory and update it" step.
  3. We UPSERT (:func:`sqlite_memory.upsert_by_tag`) under ``pref:<subject>`,
     repo-agnostic (GLOBAL) — so a restatement replaces the old value and the
     preference applies across every repo.

Best-effort: never raises into the chat path. Embedded (SQLite) backend only.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("aiforge.preference_capture")

# Cue gate now lives in the ONE shared table (capture_cues) — no per-path drift.
from aiforge_core.runtime.capture_cues import has_cue as _has_cue

_SLUG = re.compile(r"[^a-z0-9]+")


def _disabled() -> bool:
    return os.environ.get("AIFORGE_PREF_CAPTURE", "1") in ("0", "false", "no")


def _slug(s: str) -> str:
    return _SLUG.sub("-", (s or "").lower()).strip("-")[:48] or "pref"


def _existing_subjects(_repo: str | None) -> list[str]:
    """Subjects (``pref:<subject>``) already stored — fed to the LLM so it maps
    a restatement onto the SAME memory instead of minting a new one."""
    try:
        from aiforge_core.memory import sqlite_memory as _m
        subs = set()
        with _m._conn() as c:  # noqa: SLF001
            for r in c.execute("SELECT tags FROM memory_units").fetchall():
                for t in (json.loads(r["tags"] or "[]") or []):
                    if isinstance(t, str) and t.startswith("pref:"):
                        subs.add(t[5:])
        return sorted(subs)
    except Exception:  # noqa: BLE001
        return []


_SYS = (
    "You extract a durable USER PREFERENCE or standing instruction from a chat "
    "message, if present. A preference is a lasting choice the assistant should "
    "reuse (a default value, a convention, a tool/config setting, a repo path). "
    "It is NOT a one-off task request or a question.\n"
    "Reply with ONLY a JSON object: "
    '{\"is_preference\": bool, \"subject\": \"kebab-slug\", \"value\": \"the '
    'concise preference statement\", \"global\": bool}. '
    "Set is_preference=false for anything transient. Reuse an EXISTING subject "
    "verbatim when the message updates that same preference; else make a short "
    "stable slug. global=true unless the preference is clearly repo-specific.")


def capture(prompt: str, *, repo: str | None = None,
            session_id=None) -> dict:
    """Detect + upsert a durable preference from ``prompt``. Returns
    ``{ok, captured, subject?}``. Never raises."""
    # unused, deliberately: capture is per-repo; the session id rides along for the caller's convenience.
    del session_id
    if _disabled():
        return {"ok": False, "skipped": "disabled"}
    p = (prompt or "").strip()
    if len(p) < 4 or not _has_cue(p):
        return {"ok": False, "skipped": "no-gate"}
    subjects = _existing_subjects(None)
    try:
        from aiforge_core.llm import client as _llm
        raw = _llm.complete("learner", [
            {"role": "system", "content": _SYS},
            {"role": "user", "content":
                (f"Existing subjects: {subjects}\n\n" if subjects else "")
                + f"Message:\n{p[:1200]}"},
        ], temperature=0.0, max_tokens=200,
            timeout_s=int(os.environ.get("AIFORGE_PREF_CAPTURE_TIMEOUT_S", "30")))
    except Exception as exc:  # noqa: BLE001
        # LLM down → deterministic fallback: store the raw line under a slug of
        # the message so an explicit instruction still persists (no update-map).
        log.debug("pref_capture llm down: %s", exc)
        return _persist(_slug(p), p[:400], global_=True, fallback=True)

    obj = _parse(raw)
    if not obj or not obj.get("is_preference"):
        return {"ok": False, "skipped": "not-a-pref"}
    subject = _slug(obj.get("subject") or p[:40])
    value = (obj.get("value") or p).strip()[:500]
    return _persist(subject, value, global_=bool(obj.get("global", True)),
                    repo=repo)


def _parse(raw: str) -> dict | None:
    if not raw:
        return None
    t = raw.strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j <= i:
        return None
    try:
        d = json.loads(t[i:j + 1])
        return d if isinstance(d, dict) else None
    except (TypeError, ValueError):
        return None


def _persist(subject: str, value: str, *, global_: bool,
             repo: str | None = None, fallback: bool = False) -> dict:
    try:
        from aiforge_core.memory import sqlite_memory as _m
        scope_repo = None if global_ else repo
        _m.upsert_by_tag(
            text=value, tag=f"pref:{subject}", kind="preference",
            source="preference", tags=["preference", "chat"],
            metadata={"subject": subject, "global": global_},
            repo=scope_repo)
        log.info("pref captured subject=%s global=%s fallback=%s",
                 subject, global_, fallback)
        return {"ok": True, "captured": True, "subject": subject,
                "global": global_, "fallback": fallback}
    except Exception as exc:  # noqa: BLE001
        log.warning("pref persist failed: %s", exc)
        return {"ok": False, "error": str(exc)}


__all__ = ["capture"]
