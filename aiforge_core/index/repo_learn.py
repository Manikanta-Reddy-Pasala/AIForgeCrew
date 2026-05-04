"""LLM-driven per-file learning — every controller/service/repo gets
a structured summary written to T2 memory.

For each file under the repo's `src/main/...` tree:
  1. Call Qwen 3.6 27B with the file content + a fixed prompt asking
     for: purpose, key methods, exposed contracts, dependencies.
  2. Persist the summary as a `:Fact` node (tier=t2, wing='code/<repo>/<basename>')
     with bge-m3 1024-d embedding so vector search hits it.

Idempotent: skips files whose sha1 hasn't changed since the last run
(checks T2 metadata.sha1). Re-run-friendly via cron.

Public surface:
    learn_repo(repo: str, *, kinds=None, limit=None, sleep_s=0.0) -> dict
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Iterable

import httpx

from aiforge_core.runtime.config import (
    LM_STUDIO_API_KEY,
    PLANNER_MODEL,
)


# Per-file LLM prompt. KISS — one round-trip per file.
_LEARN_SYS = (
    "You are a senior code reviewer. Read the file and produce a concise "
    "structured summary. Output JSON ONLY, no prose, no markdown. Schema:\n"
    "{\n"
    '  "purpose": "<1-2 sentence what this class/file does>",\n'
    '  "kind": "controller|service|service_impl|repository|config|util|model|other",\n'
    '  "key_methods": ["<methodName>: <one-line behaviour>", ...],   # up to 8\n'
    '  "exposes": ["<API endpoint OR public contract>", ...],         # up to 8\n'
    '  "depends_on": ["<class/service used>", ...],                   # up to 8\n'
    '  "tags": ["<short keyword>", ...]                               # up to 6\n'
    "}\n"
    "Stay under 1000 chars total. Skip getters/setters/toString."
)


_KIND_PATTERNS = {
    "controller": (r"@(?:Rest)?Controller\b", "Controller.java"),
    "service_impl": (r"\bclass \w+ServiceImpl\b", "ServiceImpl.java"),
    "service": (r"\binterface \w+Service\b|@Service\b", "Service.java"),
    "repository": (r"@Repository\b|extends (?:Mongo|Jpa|Crud)Repository", "Repository.java"),
    "config": (r"@Configuration\b", "Config.java"),
}


def _detect_kind(path: str, content: str) -> str | None:
    """Decide which 'kind' bucket this file belongs to. Order matters —
    ServiceImpl must beat Service."""
    bn = os.path.basename(path)
    for kind, (regex, suffix) in _KIND_PATTERNS.items():
        if bn.endswith(suffix) or re.search(regex, content):
            return kind
    return None


def _enumerate_files(worktree: str, kinds: set[str] | None) -> list[tuple[str, str]]:
    """Walk worktree, yield (kind, abs_path) for every Java/Kotlin file
    matching one of the requested kinds (default: all)."""
    out: list[tuple[str, str]] = []
    from aiforge_core.index.noise import is_noise_path, prune_dirnames
    src_root = os.path.join(worktree, "src", "main")
    if not os.path.isdir(src_root):
        src_root = worktree
    for dirpath, dirnames, filenames in os.walk(src_root):
        prune_dirnames(dirnames)
        for fn in filenames:
            if not (fn.endswith(".java") or fn.endswith(".kt")):
                continue
            full = os.path.join(dirpath, fn)
            if is_noise_path(full):
                continue
            try:
                head = Path(full).read_text(
                    encoding="utf-8", errors="replace",
                )[:4000]
            except Exception:
                continue
            kind = _detect_kind(full, head)
            if kind is None:
                continue
            if kinds and kind not in kinds:
                continue
            out.append((kind, full))
    return out


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _llm_summarise(file_path: str, content: str, *,
                   timeout: float = 60.0) -> dict | None:
    """One round-trip per file. Returns parsed dict or None on error."""
    lm_url = os.environ.get(
        "AIFORGE_INTENT_LM_URL",
        os.environ.get("AIFORGE_PLANNER_LM_URL",
                       "http://127.0.0.1:1235/v1"),
    )
    model = os.environ.get(
        "AIFORGE_INTENT_MODEL",
        PLANNER_MODEL,
    )
    head = content[:8000]
    user = (
        f"File: {os.path.basename(file_path)}\n"
        f"Path: {file_path}\n\n"
        f"```java\n{head}\n```"
    )
    try:
        r = httpx.post(
            f"{lm_url}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _LEARN_SYS},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.0,
                # Bumped — Qwen3.6 thinking-mode chews ~400 tokens
                # before emitting the JSON. With 600 we hit
                # finish_reason=length and content stays empty.
                "max_tokens": 1500,
                "response_format": {"type": "json_object"},
                # Try to suppress thinking on stacks that honour it.
                "chat_template_kwargs": {"enable_thinking": False},
            },
            headers={"Authorization": f"Bearer {LM_STUDIO_API_KEY}"},
            timeout=timeout,
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        body = (msg.get("content") or "").strip()
        # Fallback: mlx-lm puts post-thinking text into 'reasoning'
        # when content stayed empty. Strip <think>…</think> wrappers.
        if not body:
            raw = (msg.get("reasoning") or "").strip()
            if raw:
                body = re.sub(
                    r"<think>.*?</think>", "", raw, flags=re.DOTALL,
                ).strip() or raw
        if body.startswith("```"):
            body = re.sub(r"^```\w*\n?|\n?```$", "", body, flags=re.M).strip()
        i, j = body.find("{"), body.rfind("}")
        if i >= 0 and j > i:
            body = body[i:j + 1]
        return json.loads(body)
    except Exception:
        return None


def _summary_text(parsed: dict, file_path: str, repo: str) -> str:
    """Render the parsed summary as a human-readable + searchable text
    blob. Stored verbatim in :Fact.text and used for embedding."""
    parts = [
        f"[{repo}] {os.path.basename(file_path)} ({parsed.get('kind') or 'other'})",
        parsed.get("purpose") or "",
    ]
    km = parsed.get("key_methods") or []
    if km:
        parts.append("Key methods:")
        for m in km[:8]:
            parts.append(f"  - {m}")
    ex = parsed.get("exposes") or []
    if ex:
        parts.append("Exposes:")
        for e in ex[:8]:
            parts.append(f"  - {e}")
    dep = parsed.get("depends_on") or []
    if dep:
        parts.append("Depends on: " + ", ".join(dep[:8]))
    tags = parsed.get("tags") or []
    if tags:
        parts.append("Tags: " + ", ".join(tags[:6]))
    parts.append(f"Path: {file_path}")
    return "\n".join(p for p in parts if p)


def _existing_sha1(file_id: str) -> str | None:
    try:
        from aiforge_core.legacy.rag.neo4j_memory import driver
        cy = (
            "MATCH (m:Memory {id:$id}) "
            "RETURN m.metadata AS md LIMIT 1"
        )
        with driver().session() as s:
            r = s.run(cy, id=file_id).single()
            if not r:
                return None
            md = r["md"] or {}
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    return None
            return md.get("sha1") if isinstance(md, dict) else None
    except Exception:
        return None


def _persist_fact(*, repo: str, file_path: str, parsed: dict,
                  content_sha1: str) -> str | None:
    """Write the summary to T2 memory. Stub — runtime.memory removed."""
    return None


def learn_repo(repo: str, *,
               kinds: Iterable[str] | None = None,
               limit: int | None = None,
               sleep_s: float = 0.0) -> dict:
    """Walk the repo, summarise each controller/service/repo via LLM,
    persist as :Fact. Returns counts dict.

    - ``kinds``: subset of {controller, service, service_impl, repository,
      config}. Default: all five.
    - ``limit``: cap files processed per call (operator can chunk via
      cron iterations).
    - ``sleep_s``: delay between LLM calls (rate-limit guard for
      shared LM Studio).
    """
    base = os.environ.get("AIFORGE_REPOS_BASE", "/home/mani/codeRepo")
    worktree = os.path.join(base, repo)
    if not os.path.isdir(worktree):
        return {"repo": repo, "error": f"not a dir: {worktree}"}
    targets = _enumerate_files(worktree, set(kinds) if kinds else None)
    counts = {
        "repo": repo,
        "scanned": len(targets),
        "summarised": 0,
        "skipped_unchanged": 0,
        "errors": 0,
    }
    for kind, abs_path in targets[: limit or len(targets)]:
        try:
            content = Path(abs_path).read_text(
                encoding="utf-8", errors="replace",
            )
        except Exception:
            counts["errors"] += 1
            continue
        sha = _sha1(content[:32000])
        # Stable id derived from rel path → re-run replaces.
        rel = abs_path[len(worktree) + 1:] if abs_path.startswith(worktree) else abs_path
        file_id = f"{repo}:{rel}".lower()
        prior = _existing_sha1(file_id)
        if prior == sha:
            counts["skipped_unchanged"] += 1
            continue
        parsed = _llm_summarise(abs_path, content)
        if parsed is None:
            counts["errors"] += 1
            continue
        if _persist_fact(repo=repo, file_path=abs_path,
                         parsed=parsed, content_sha1=sha):
            counts["summarised"] += 1
        else:
            counts["errors"] += 1
        if sleep_s:
            time.sleep(sleep_s)
    return counts


__all__ = ["learn_repo"]
