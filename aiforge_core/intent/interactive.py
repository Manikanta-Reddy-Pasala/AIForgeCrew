"""Interactive classification loop for the CLI.

Goal: turn the regex/LLM classifier into a teaching session. The user
types natural language, the agent classifies, asks clarifying
questions when confidence is low, persists the operator's answer to
``<repo>/.aiforge/synonyms.yml`` so the same phrase never has to be
asked again, then re-runs classify until all fields are confident.

ONLY runs in CLI mode (``aiforge ticket new --interactive ...``). The
HTTP ticket POST path stays fully automatic — operators are never
blocked at the API layer waiting for stdin.

Public surface:
    classify_with_clarification(text, *, repo=None) -> Intent
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable

from aiforge_core.intent.classifier import (
    Intent,
    _heuristic_intent,
    _LM_URL,
    _LM_MODEL,
    _LM_KEY,
    _VALID_ACTIONS,
)


# ──────────── classifier with confidence ──────────────────────────


_CLASSIFY_WITH_CONF_SYS = (
    "You translate plain English software requests into a strict JSON "
    "object. Output ONE JSON object, no prose, no markdown. Schema:\n"
    "{\n"
    '  "action": "add|edit|fix|remove|dup|investigate|refactor|test|doc|ops",\n'
    '  "entity": "<the noun being acted on>",\n'
    '  "reference_pattern": "<exemplar to mirror, or empty>",\n'
    '  "repo_hint": "<repo name guess, or empty>",\n'
    '  "keywords": ["<3-8 search keywords>"],\n'
    '  "confidence": {"action": "high|medium|low", '
    '"entity": "high|medium|low", '
    '"reference_pattern": "high|medium|low", '
    '"repo_hint": "high|medium|low"}\n'
    "}\n"
    "Rules: action MUST be from the enum. Mark a field 'low' if you\n"
    "had to guess between 2+ plausible candidates OR the field was\n"
    "absent entirely. Mark 'high' only when the field is unambiguously\n"
    "stated. Be honest — low confidence is preferred over a wrong\n"
    "high-confidence guess."
)


@dataclass
class ClassifyResult:
    intent: Intent
    confidence: dict       # {"action": "high|medium|low", ...}


def _llm_classify_with_confidence(text: str, *,
                                  timeout: float = 30.0) -> ClassifyResult | None:
    """LLM classify + per-field confidence. None on any error."""
    try:
        import httpx
        payload = {
            "model": _LM_MODEL,
            "messages": [
                {"role": "system", "content": _CLASSIFY_WITH_CONF_SYS},
                {"role": "user", "content": text[:4000]},
            ],
            "temperature": 0.0,
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
        }
        r = httpx.post(
            f"{_LM_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {_LM_KEY}"},
            timeout=timeout,
        )
        r.raise_for_status()
        body = r.json()["choices"][0]["message"]["content"].strip()
        if body.startswith("```"):
            body = re.sub(r"^```\w*\n?|\n?```$", "", body, flags=re.M).strip()
        i, j = body.find("{"), body.rfind("}")
        if i >= 0 and j > i:
            body = body[i:j + 1]
        d = json.loads(body)
    except Exception:
        return None
    action = (d.get("action") or "investigate").lower()
    if action not in _VALID_ACTIONS:
        action = "investigate"
    kw = d.get("keywords") or []
    if not isinstance(kw, list):
        kw = [str(kw)]
    intent = Intent(
        action=action,                                  # type: ignore[arg-type]
        entity=str(d.get("entity") or "")[:80],
        reference_pattern=str(d.get("reference_pattern") or "")[:80],
        repo_hint=str(d.get("repo_hint") or "")[:60],
        keywords=[str(k)[:40] for k in kw[:10] if str(k).strip()],
        raw_text=text,
    )
    conf = d.get("confidence") or {}
    if not isinstance(conf, dict):
        conf = {}
    return ClassifyResult(intent=intent, confidence={
        k: str(conf.get(k) or "medium").lower()
        for k in ("action", "entity", "reference_pattern", "repo_hint")
    })


# ──────────── clarification IO ────────────────────────────────────


_DEFAULT_PROMPTER: Callable[[str], str] = input


def _ask(prompt: str, *, ask: Callable[[str], str] = _DEFAULT_PROMPTER) -> str:
    """Read one line from operator. Strips whitespace; empty → ''."""
    try:
        return ask(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _is_low(conf: dict, field: str) -> bool:
    return conf.get(field, "medium").lower() == "low"


def _persist_synonym(repo: str | None, phrase: str, mapping: str) -> str | None:
    """Append a synonym row to ``<repo>/.aiforge/synonyms.yml`` (or
    the global file when ``repo`` is None). Returns the path written
    to, or None on failure."""
    if not phrase.strip() or not mapping.strip():
        return None
    base = os.environ.get("AIFORGE_REPOS_BASE", "/home/mani/codeRepo")
    if repo:
        target_dir = os.path.join(base, repo, ".aiforge")
    else:
        target_dir = os.path.join(base, ".aiforge-global")
    try:
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, "synonyms.yml")
        line = f"{phrase.strip().lower()}: {mapping.strip()}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return path
    except Exception:
        return None


# ──────────── public entry point ──────────────────────────────────


def classify_with_clarification(
    text: str, *, repo: str | None = None,
    ask: Callable[[str], str] = _DEFAULT_PROMPTER,
    auto_persist: bool = True,
    max_rounds: int = 3,
) -> Intent:
    """Classify ``text`` and ask the operator clarifying questions
    until all fields are 'medium'+ confidence.

    Each operator answer is persisted to synonyms.yml (when
    ``auto_persist=True``) so a future identical phrase won't ask the
    same question. After ``max_rounds`` of clarification, returns the
    best-effort intent regardless of remaining low-confidence fields.

    On LM Studio failure → falls back to heuristic + skips
    clarification (offline-friendly)."""
    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        result = _llm_classify_with_confidence(text)
        if result is None:
            # LLM down → heuristic, no clarification.
            return _heuristic_intent(text)
        conf = result.confidence
        intent = result.intent
        # Show current state, ask only the LOW-confidence fields.
        weak = [f for f in ("action", "entity",
                            "reference_pattern", "repo_hint")
                if _is_low(conf, f)]
        if not weak:
            return intent

        # Print current best guess so operator knows what to clarify.
        print(f"\n[classify round {rounds}/{max_rounds}] "
              f"action={intent.action} entity={intent.entity!r} "
              f"ref={intent.reference_pattern!r} repo={intent.repo_hint!r}")
        addendum_parts: list[str] = []
        for f in weak:
            current = getattr(intent, f if f != "reference_pattern"
                              else "reference_pattern")
            ans = _ask(f"  ? {f} (current: {current!r}, low confidence) → ", ask=ask)
            if not ans:
                continue
            # Apply answer to intent.
            if f == "action":
                if ans.lower() in _VALID_ACTIONS:
                    intent.action = ans.lower()  # type: ignore[assignment]
            elif f == "entity":
                intent.entity = ans[:80]
            elif f == "reference_pattern":
                intent.reference_pattern = ans[:80]
            elif f == "repo_hint":
                intent.repo_hint = ans[:60]
            # Persist the operator's correction so the same input
            # next time hits synonyms.yml first.
            if auto_persist:
                # Capture what they corrected — use the entity as
                # the LHS (it's the natural anchor in the text).
                phrase = (intent.entity or intent.reference_pattern or
                          ans).lower()
                if phrase and phrase != ans.lower():
                    _persist_synonym(repo or intent.repo_hint, phrase, ans)
            addendum_parts.append(f"User clarified {f}: {ans}")

        if not addendum_parts:
            # Operator skipped all questions — accept current guess.
            return intent
        # Re-classify with the operator's addendum so the LLM has
        # explicit guidance for the next round (covers cascading
        # ambiguity: clarifying the entity may change the action).
        text = text + "\n\n[Clarifications]\n" + "\n".join(addendum_parts)
    # Hit max rounds; return whatever we have.
    last = _llm_classify_with_confidence(text)
    return last.intent if last else _heuristic_intent(text)


__all__ = ["classify_with_clarification", "ClassifyResult"]
