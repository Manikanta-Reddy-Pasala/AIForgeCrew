"""Stage 3 — LLM service extraction + operator override merge.

Sends the RepoMix pack + a strict-JSON system prompt to the planner
LLM and parses the result into a list of `ServiceDraft`. If
`<repo_path>/.aiforge/services.yaml` exists, operator entries (by
name) replace the LLM draft. Hallucinated file paths (paths that
don't exist in the actual repo) are dropped silently.

Public surface:
    extract_services(pack_text, repo_path, repo_name) -> list[ServiceDraft]
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROMPT_PATH = Path(__file__).parent / "prompts" / "service_extract.txt"
DEFAULT_LM_URL = os.environ.get(
    "AIFORGE_CODEMEM_LM_URL",
    os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
)
DEFAULT_MODEL = os.environ.get(
    "AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct"
)


class ServiceExtractError(RuntimeError):
    pass


@dataclass
class ServiceDraft:
    name: str
    description: str = ""
    role: str = ""
    tech_stack: list[str] = field(default_factory=list)
    port: int | None = None
    files: list[str] = field(default_factory=list)
    source: str = "llm"   # "llm" | "manual"


def extract_services(
    pack_text: str,
    *,
    repo_path: str | Path,
    repo_name: str,
    max_input_chars: int = 240_000,
) -> list[ServiceDraft]:
    """Pack → LLM → drafts → operator override merge → validated drafts."""
    pack = _truncate(pack_text, max_input_chars)
    system = PROMPT_PATH.read_text()
    user = f"Repository name: {repo_name}\n\n{pack}"

    raw = _call_llm(pack, system=system, user=user)
    drafts = _parse(raw)
    if drafts is None:
        strict = system + "\n\nReminder: output ONLY a JSON object."
        raw2 = _call_llm(pack, system=strict, user=user)
        drafts = _parse(raw2)
    if drafts is None:
        raise ServiceExtractError("LLM returned invalid JSON twice")

    # Operator override merge
    merged = _merge_overrides(drafts, repo_path=repo_path)
    # Drop hallucinated paths
    return _validate_files(merged, repo_path=repo_path)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    tail = text[-int(limit * 0.4):]
    return f"{head}\n\n[TRUNCATED {len(text) - limit} chars]\n\n{tail}"


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _parse(raw: str) -> list[ServiceDraft] | None:
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    services = obj.get("services") or []
    if not isinstance(services, list):
        return None
    out: list[ServiceDraft] = []
    for s in services:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        out.append(ServiceDraft(
            name=str(s["name"]),
            description=str(s.get("description", "")),
            role=str(s.get("role", "")),
            tech_stack=[str(x) for x in (s.get("tech_stack") or [])],
            port=int(s["port"]) if isinstance(s.get("port"), int) else None,
            files=[str(x) for x in (s.get("files") or [])],
            source="llm",
        ))
    return out


def _merge_overrides(
    drafts: list[ServiceDraft], *, repo_path: str | Path,
) -> list[ServiceDraft]:
    yaml_path = Path(repo_path) / ".aiforge" / "services.yaml"
    if not yaml_path.is_file():
        return drafts
    try:
        data = yaml.safe_load(yaml_path.read_text()) or {}
    except yaml.YAMLError:
        return drafts
    overrides = data.get("services") or []
    if not isinstance(overrides, list):
        return drafts

    by_name: dict[str, ServiceDraft] = {d.name: d for d in drafts}
    for o in overrides:
        if not isinstance(o, dict) or not o.get("name"):
            continue
        port = o.get("port")
        by_name[str(o["name"])] = ServiceDraft(
            name=str(o["name"]),
            description=str(o.get("description", "")),
            role=str(o.get("role", "")),
            tech_stack=[str(x) for x in (o.get("tech_stack") or [])],
            port=int(port) if isinstance(port, int) else None,
            files=[str(x) for x in (o.get("files") or [])],
            source="manual",
        )
    return list(by_name.values())


def _validate_files(
    drafts: list[ServiceDraft], *, repo_path: str | Path,
) -> list[ServiceDraft]:
    repo_root = Path(repo_path).resolve()
    for d in drafts:
        kept = []
        for rel in d.files:
            candidate = (repo_root / rel).resolve()
            try:
                candidate.relative_to(repo_root)
            except ValueError:
                continue   # outside repo
            if candidate.is_file():
                kept.append(rel)
        d.files = kept
    return drafts


def _call_llm(pack_text: str, *, system: str = "", user: str = "") -> str:
    """Real LLM call. Isolated for monkey-patching in tests."""
    from openai import OpenAI

    client = OpenAI(
        base_url=DEFAULT_LM_URL,
        api_key=os.environ.get("AIFORGE_CODEMEM_LM_KEY", "lm-studio"),
    )
    resp = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=6000,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""
