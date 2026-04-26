"""Plain-text → structured Intent → EnrichedTicket.

Single entry point for ALL human input (chat, ticket POST). Two
stages, both deterministic-by-design:

    raw_text ─► classify() ─► Intent           (LLM, strict JSON)
    Intent   ─► enrich()   ─► EnrichedTicket   (UnifiedContext fan-out)

The point: by the time Planner/Doer see anything, the ticket already
carries focal_files, reference_files, build commands, similar past
resolutions and matching T3 patterns. Doer prompt assembly degrades
to "render the bundle"; no more file-keyed empty-context collapse.

LLM = local Qwen 3.6 27B via LM Studio (port 1235). No cloud calls.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Literal

import httpx

# LM Studio (planner port). Single shared endpoint.
_LM_URL = os.environ.get(
    "AIFORGE_INTENT_LM_URL",
    os.environ.get("AIFORGE_PLANNER_LM_URL", "http://127.0.0.1:1235/v1"),
)
_LM_MODEL = os.environ.get(
    "AIFORGE_INTENT_MODEL",
    os.environ.get("AIFORGE_PLANNER_MODEL", "qwen3.6-27b"),
)
_LM_KEY = os.environ.get("AIFORGE_LM_API_KEY", "lm-studio")

Action = Literal[
    "add", "edit", "fix", "remove", "dup", "investigate",
    "refactor", "test", "doc", "ops",
]


@dataclass
class Intent:
    """Machine-readable summary of what the user is asking for."""
    action: Action
    entity: str = ""             # noun: "priceLists", "checkout API", "sync rule"
    reference_pattern: str = ""  # exemplar: "businessProducts", "POST /checkout"
    repo_hint: str = ""          # repo name guess from text
    keywords: list[str] = field(default_factory=list)
    raw_text: str = ""

    def search_query(self) -> str:
        """Best query string for retrieval — entity + ref + keywords."""
        parts = [self.entity, self.reference_pattern, *self.keywords[:6]]
        return " ".join(p for p in parts if p).strip() or self.raw_text[:200]


@dataclass
class EnrichedTicket:
    """Output of enrich(). Drop-in for Planner/Doer prompt assembly."""
    title: str
    body: str
    intent: Intent
    allowed_files: list[str] = field(default_factory=list)
    reference_files: list[str] = field(default_factory=list)
    similar_tickets: list[dict] = field(default_factory=list)
    t3_recipes: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)   # build/test/lint/...
    acceptance: list[str] = field(default_factory=list)
    repo: str = ""
    sources_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["intent"] = asdict(self.intent)
        return d


# ──────────── classify ─────────────────────────────────────────────

_CLASSIFY_SYS = (
    "You translate plain English software requests into a strict JSON "
    "object. Output ONE JSON object, no prose, no markdown. Schema:\n"
    "{\n"
    '  "action": "add|edit|fix|remove|dup|investigate|refactor|test|doc|ops",\n'
    '  "entity": "<the noun being acted on>",\n'
    '  "reference_pattern": "<exemplar to mirror, or empty>",\n'
    '  "repo_hint": "<repo name guess, or empty>",\n'
    '  "keywords": ["<3-8 search keywords>"]\n'
    "}\n"
    "Rules: action MUST be from the enum. entity is short (1-4 words). "
    "If user says 'like X' or 'similar to X' or 'mirror X', X is the "
    "reference_pattern. Keywords help retrieval — include code-like "
    "tokens (class names, file extensions, collection names)."
)


def classify(text: str, *, timeout: float = 30.0) -> Intent:
    """LLM-classify raw text → Intent. Falls back to keyword heuristic
    on LLM error so callers never block on a missing LM Studio."""
    text = (text or "").strip()
    if not text:
        return Intent(action="investigate", raw_text="")
    try:
        raw = _call_llm_json(text, timeout=timeout)
        return _parse_intent(raw, text)
    except Exception:
        return _heuristic_intent(text)


def _call_llm_json(text: str, *, timeout: float) -> dict:
    payload = {
        "model": _LM_MODEL,
        "messages": [
            {"role": "system", "content": _CLASSIFY_SYS},
            {"role": "user", "content": text[:4000]},
        ],
        "temperature": 0.0,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
    }
    r = httpx.post(
        f"{_LM_URL}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {_LM_KEY}"},
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()["choices"][0]["message"]["content"]
    # Some local servers ignore response_format; strip code fences.
    body = body.strip()
    if body.startswith("```"):
        body = re.sub(r"^```\w*\n?|\n?```$", "", body, flags=re.M).strip()
    # Take first {...} balanced object.
    i, j = body.find("{"), body.rfind("}")
    if i >= 0 and j > i:
        body = body[i:j + 1]
    return json.loads(body)


_VALID_ACTIONS = {
    "add", "edit", "fix", "remove", "dup", "investigate",
    "refactor", "test", "doc", "ops",
}


def _parse_intent(d: dict, raw: str) -> Intent:
    action = (d.get("action") or "investigate").lower().strip()
    if action not in _VALID_ACTIONS:
        action = "investigate"
    kw = d.get("keywords") or []
    if not isinstance(kw, list):
        kw = [str(kw)]
    return Intent(
        action=action,                                # type: ignore[arg-type]
        entity=str(d.get("entity") or "")[:80],
        reference_pattern=str(d.get("reference_pattern") or "")[:80],
        repo_hint=str(d.get("repo_hint") or "")[:60],
        keywords=[str(k)[:40] for k in kw[:10] if str(k).strip()],
        raw_text=raw,
    )


_VERB_HINTS = {
    "add": "add", "create": "add", "implement": "add",
    "fix": "fix", "bug": "fix", "broken": "fix",
    "remove": "remove", "delete": "remove", "drop": "remove",
    "edit": "edit", "change": "edit", "update": "edit", "modify": "edit",
    "duplicate": "dup", "duplicates": "dup", "dedup": "dup",
    "refactor": "refactor", "cleanup": "refactor",
    "test": "test", "doc": "doc", "document": "doc",
    "deploy": "ops", "restart": "ops", "rollback": "ops",
}


def _heuristic_intent(text: str) -> Intent:
    """LLM-free fallback. Last resort when LM Studio is down."""
    low = text.lower()
    action: Action = "investigate"
    for verb, mapped in _VERB_HINTS.items():
        if re.search(rf"\b{verb}\b", low):
            action = mapped                           # type: ignore[assignment]
            break
    # crude reference pattern via "like X" / "similar to X" / "mirror X"
    ref = ""
    m = re.search(r"\b(?:like|similar to|mirror|same as|reference[d]? table)\s+`?([A-Za-z][\w/.-]+)`?", text)
    if m:
        # Strip trailing sentence punctuation — the regex's [\w/.-]+
        # is greedy on '.' so `businessProducts.` survives. ripgrep
        # -F treats that period literally and finds nothing.
        ref = m.group(1).rstrip(".,;:!?")
    # Token-rich keywords: words with mixed case OR underscores OR len >= 7.
    # Preserve source order — first occurrence in text wins (so "add X
    # like Y" picks X as the entity, not Y).
    seen: set[str] = set()
    kws: list[str] = []
    for t in re.findall(r"[A-Za-z][\w/.-]{2,}", text):
        if not (any(c.isupper() for c in t[1:]) or "_" in t or len(t) >= 7):
            continue
        if t in seen:
            continue
        seen.add(t)
        kws.append(t)
        if len(kws) >= 10:
            break
    # Entity = first keyword that is NOT the reference pattern.
    entity = ""
    for k in kws:
        if k != ref:
            entity = k
            break
    return Intent(
        action=action,
        entity=entity,
        reference_pattern=ref,
        repo_hint="",
        keywords=kws[:8],
        raw_text=text,
    )


# ──────────── enrich ──────────────────────────────────────────────


def enrich(text: str, *, role: str = "sr_developer",
           token_budget: int = 4000) -> EnrichedTicket:
    """Plain text → EnrichedTicket. Public entry point.

    Calls classify() then UnifiedContext to gather all context layers.
    Bundle is rendered into prompt-ready strings inside the returned
    EnrichedTicket. Best-effort — never raises; populates ``errors``.
    """
    intent = classify(text)
    title = _derive_title(text, intent)
    errors: list[str] = []
    bundle = None
    try:
        from aiforge_core.context import UnifiedContext
        bundle = UnifiedContext().for_intent(
            intent, role=role, token_budget=token_budget,
        )
    except Exception as exc:
        errors.append(f"context: {exc}")

    if bundle is None:
        return EnrichedTicket(
            title=title, body=text, intent=intent, errors=errors,
        )

    return EnrichedTicket(
        title=title,
        body=text,
        intent=intent,
        allowed_files=bundle.focal_files,
        reference_files=bundle.reference_files,
        similar_tickets=bundle.similar_tickets,
        t3_recipes=bundle.t3_recipes,
        commands=bundle.commands,
        acceptance=bundle.acceptance,
        repo=bundle.repo,
        sources_used=bundle.sources_used,
        errors=errors + bundle.errors,
    )


def _derive_title(text: str, intent: Intent) -> str:
    """Short title from the first line OR action+entity."""
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if first and len(first) <= 90:
        return first
    if intent.entity:
        verb = intent.action.capitalize()
        return f"{verb} {intent.entity}"[:90]
    return (first or "Untitled")[:90]
