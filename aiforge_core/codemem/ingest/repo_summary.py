"""Stage 2 — LLM repo summary.

Sends the RepoMix pack + a strict-JSON system prompt to the planner
LLM (qwen3.6-27b at LM Studio :1235 by default) and parses the result
into a `RepoSummary` dataclass. One automatic retry on invalid JSON
with a stricter system suffix; second failure raises RepoSummaryError.

The actual transport call is isolated in ``_call_llm`` so the unit
tests can monkey-patch it without standing up LM Studio.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROMPT_PATH = Path(__file__).parent / "prompts" / "repo_summary.txt"
DEFAULT_LM_URL = os.environ.get(
    "AIFORGE_CODEMEM_LM_URL",
    os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
)
DEFAULT_MODEL = os.environ.get(
    "AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct"
)


class RepoSummaryError(RuntimeError):
    pass


@dataclass
class RepoSummary:
    lang_primary: str = ""
    build_cmd: str = ""
    test_cmd: str = ""
    lint_cmd: str = ""
    run_cmd: str = ""
    portforward_cmds: list[str] = field(default_factory=list)
    conventions_md: str = ""
    runbook_md: str = ""


def summarize(
    pack_text: str,
    *,
    repo_name: str,
    max_input_chars: int = 240_000,
) -> RepoSummary:
    """Pack → LLM → RepoSummary. Retries once on bad JSON."""
    pack = _truncate(pack_text, max_input_chars)
    system = PROMPT_PATH.read_text()
    user = f"Repository name: {repo_name}\n\n{pack}"

    raw = _call_llm(pack, system=system, user=user)
    parsed = _parse(raw)
    if parsed is not None:
        return parsed

    # Retry with a stricter suffix
    strict_system = system + "\n\nReminder: output ONLY a JSON object — no prose."
    raw2 = _call_llm(pack, system=strict_system, user=user)
    parsed2 = _parse(raw2)
    if parsed2 is not None:
        return parsed2

    raise RepoSummaryError("LLM returned invalid JSON twice")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    tail = text[-int(limit * 0.4):]
    return f"{head}\n\n[TRUNCATED {len(text) - limit} chars]\n\n{tail}"


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _parse(raw: str) -> RepoSummary | None:
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return RepoSummary(
        lang_primary=str(obj.get("lang_primary", "")),
        build_cmd=str(obj.get("build_cmd", "")),
        test_cmd=str(obj.get("test_cmd", "")),
        lint_cmd=str(obj.get("lint_cmd", "")),
        run_cmd=str(obj.get("run_cmd", "")),
        portforward_cmds=[str(x) for x in obj.get("portforward_cmds", []) or []],
        conventions_md=str(obj.get("conventions_md", "")),
        runbook_md=str(obj.get("runbook_md", "")),
    )


def _call_llm(pack_text: str, *, system: str = "", user: str = "") -> str:
    """Real LLM call. Isolated for monkey-patching in tests.

    `pack_text` is kept in the signature so tests can introspect it,
    but the actual prompt assembled below is what hits the LLM.
    """
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
        max_tokens=4000,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""
