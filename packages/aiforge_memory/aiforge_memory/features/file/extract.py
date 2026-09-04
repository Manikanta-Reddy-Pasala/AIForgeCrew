"""Stage 6 — per-file LLM summary.

For every WalkedFile that has a recognized lang and reasonable size,
ask the planner LLM for a concise summary + 3-5 purpose tags. Write
the result onto the File_v2 node.

Skips:
    - parse_error files
    - files with > MAX_FILE_BYTES (default 32 KB) — too large to summarize cheaply
    - files with no symbols (likely stub/empty)

Soft contract:
    - LLM bad JSON twice → keep previous summary, set last_error
    - LLM unreachable → skip silently, increment counter
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aiforge_memory.features.symbol.extract import WalkedFile

PROMPT_PATH = Path(__file__).parent / "prompts" / "file_summary.txt"
DEFAULT_LM_URL = os.environ.get(
    "AIFORGE_CODEMEM_LM_URL",
    os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
)
DEFAULT_MODEL = os.environ.get(
    "AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct"
)

MAX_FILE_BYTES = int(os.environ.get("AIFORGE_CODEMEM_FILE_SUMMARY_MAX_BYTES", "32768"))


@dataclass
class FileSummary:
    repo: str
    path: str
    summary: str = ""
    purpose_tags: list[str] = field(default_factory=list)
    skipped_reason: str = ""    # "" | "too_large" | "no_symbols" | "parse_error"


def summarize_files(
    walked: list[WalkedFile],
    *,
    repo: str,
    repo_root: str | Path,
) -> list[FileSummary]:
    """Per-file summarization. Each call is independent."""
    out: list[FileSummary] = []
    repo_root = Path(repo_root)

    for wf in walked:
        fs = FileSummary(repo=repo, path=wf.path)
        reason, content = _read_or_skip(wf, repo_root)
        if reason:
            fs.skipped_reason = reason
        else:
            try:
                parsed = _summarize_one(content or b"", wf)
                if parsed is not None:
                    fs.summary, fs.purpose_tags = parsed
            except Exception:
                fs.skipped_reason = "llm_error"
        out.append(fs)
    return out


def _read_or_skip(wf: WalkedFile,
                  repo_root: Path) -> tuple[str, bytes | None]:
    """``(skip_reason, content)`` — reason is "" when the file should be summarized.

    Four "record why and move on" branches used to sit inline in the loop, each
    repeating the same three lines, which is what made one straight-line
    function hard to follow. The decision is here; the loop just records it.
    """
    if wf.parse_error:
        return "parse_error", None
    if not wf.symbols and wf.lang == "other":
        return "no_symbols", None
    try:
        content = (repo_root / wf.path).read_bytes()
    except OSError:
        return "io_error", None
    if len(content) > MAX_FILE_BYTES:
        return "too_large", None
    return "", content


def _summarize_one(content: bytes,
                   wf: WalkedFile) -> tuple[str, list[str]] | None:
    """The model's ``(summary, tags)`` for one file, or None if it never emitted
    parseable JSON. Transport errors propagate — the caller records them."""
    text = content.decode("utf-8", errors="replace")
    parsed = _parse(_call_llm(text, path=wf.path, lang=wf.lang))
    if parsed is None:
        # One retry with a blunter instruction: a model that wrapped the JSON in
        # prose usually complies when told to output only the object.
        strict = PROMPT_PATH.read_text() + \
            "\n\nReminder: output ONLY the JSON object."
        parsed = _parse(_call_llm(text, path=wf.path, lang=wf.lang,
                                  system_override=strict))
    return parsed


# Grouped so the precedence is visible: the anchors belong to their OWN
# branch (an opening fence at the start of a line, a closing fence at the end),
# which is what `^a|b$` already meant and what a reader could not see.
_FENCE_RE = re.compile(r"(?:^```(?:json)?\s*\n?)|(?:\n?```\s*$)", re.MULTILINE)


def _parse(raw: str) -> tuple[str, list[str]] | None:
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    summary = str(obj.get("summary", "")).strip()
    tags = obj.get("purpose_tags") or []
    if not isinstance(tags, list):
        return None
    tags = [str(t).strip().lower() for t in tags if str(t).strip()]
    if not summary or not tags:
        return None
    return summary, tags[:5]


def _call_llm(
    content: str, *, path: str, lang: str,
    system_override: str | None = None,
) -> str:
    """Real LLM call. Isolated for monkey-patching in tests."""
    from openai import OpenAI

    client = OpenAI(
        base_url=DEFAULT_LM_URL,
        api_key=os.environ.get("AIFORGE_CODEMEM_LM_KEY", "lm-studio"),
    )
    system = system_override or PROMPT_PATH.read_text()
    user = f"File: {path}\nLanguage: {lang}\n\n{content}"
    from aiforge_memory.llm_compat import response_format
    create_kwargs: dict = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 600,
    }
    rf = response_format()
    if rf is not None:
        create_kwargs["response_format"] = rf
    resp = client.chat.completions.create(**create_kwargs)
    return resp.choices[0].message.content or ""
