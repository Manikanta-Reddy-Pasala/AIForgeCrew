"""NL query → grounded entities (services / files / symbols).

Two stages:
    T1 embed query (bge-m3) → top-K Chunk vector hits → candidate
       File and Symbol short-list
    T2 LLM (qwen3.6) given (query, services_catalog, top_files, top_symbols)
       returns strict JSON: {intent, services[], files[], symbols[],
       hops, keywords[]}
       — always picks names from the supplied catalog/candidates,
       never invents

Hallucinated names (not in candidates / catalog) are silently dropped
and surfaced via bundle.errors.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

PROMPT_PATH = Path(__file__).parent.parent / "ingest" / "prompts" / "translate.txt"
DEFAULT_LM_URL = os.environ.get(
    "AIFORGE_CODEMEM_LM_URL",
    os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
)
DEFAULT_MODEL = os.environ.get(
    "AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct"
)
DEFAULT_EMBED_URL = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")


@dataclass
class Grounding:
    intent: str = ""
    services: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    hops: int = 1
    keywords: list[str] = field(default_factory=list)
    used_top_k: int = 0
    errors: list[str] = field(default_factory=list)


def translate(
    text: str,
    *,
    repo: str,
    driver,
    top_k: int = 20,
) -> Grounding:
    """NL → entities. Best-effort: any sub-stage failure leaves Grounding
    partially populated and adds an entry to .errors."""
    g = Grounding()
    if not text.strip():
        return g

    # T1 — embed + Cypher vector top-K
    candidate_files: list[str] = []
    candidate_symbols: list[str] = []
    try:
        vec = _embed_query(text)
        rows = _vector_topk(driver, repo=repo, vec=vec, k=top_k)
        for r in rows:
            fp = r.get("file_path")
            if fp and fp not in candidate_files:
                candidate_files.append(fp)
        candidate_symbols = _symbols_in(driver, repo=repo, files=candidate_files)
        g.used_top_k = len(rows)
    except Exception as exc:
        g.errors.append(f"embed/topk: {exc}")

    # Service catalog
    services = _services_for(driver, repo=repo)

    # T2 — LLM grounding
    try:
        raw = _call_llm(
            text=text, services=services,
            files=candidate_files, symbols=candidate_symbols,
        )
        parsed = _parse(raw)
        if parsed:
            g.intent = parsed.get("intent", "")
            g.hops = max(1, min(int(parsed.get("hops", 1) or 1), 2))
            g.keywords = [str(k) for k in (parsed.get("keywords") or [])]
            g.services = [s for s in (parsed.get("services") or []) if s in services]
            g.files = [f for f in (parsed.get("files") or []) if f in candidate_files]
            g.symbols = [s for s in (parsed.get("symbols") or []) if s in candidate_symbols]
        else:
            g.errors.append("translator: invalid JSON")
    except Exception as exc:
        g.errors.append(f"translator: {exc}")

    # If LLM returned nothing usable, fall back to top embed candidates
    if not (g.services or g.files or g.symbols):
        g.files = candidate_files[:5]
        g.symbols = candidate_symbols[:5]

    return g


def _embed_query(text: str) -> list[float]:
    url = DEFAULT_EMBED_URL.rstrip("/") + "/embed"
    r = httpx.post(url, json={"text": text}, timeout=10.0)
    r.raise_for_status()
    return [float(x) for x in r.json().get("embedding", [])]


_VECTOR_CYPHER = """
CALL db.index.vector.queryNodes('codemem_chunk_embed', $k, $vec)
YIELD node AS c, score
WHERE c.repo = $repo
RETURN c.file_path AS file_path, c.text AS text, score
ORDER BY score DESC
LIMIT $k
"""


def _vector_topk(driver, *, repo: str, vec: list[float], k: int) -> list[dict]:
    if not vec:
        return []
    with driver.session() as s:
        rows = list(s.run(_VECTOR_CYPHER, repo=repo, vec=vec, k=k))
    return [dict(r) for r in rows]


def _symbols_in(driver, *, repo: str, files: list[str]) -> list[str]:
    if not files:
        return []
    cy = (
        "MATCH (f:File_v2 {repo:$repo})-[:DEFINES]->(s:Symbol_v2) "
        "WHERE f.path IN $files RETURN s.fqname AS fq LIMIT 60"
    )
    with driver.session() as sess:
        rows = list(sess.run(cy, repo=repo, files=files))
    return [r["fq"] for r in rows]


def _services_for(driver, *, repo: str) -> list[str]:
    with driver.session() as s:
        rows = list(s.run(
            "MATCH (r:Repo {name:$n})-[:OWNS_SERVICE]->(s:Service) "
            "RETURN s.name AS name", n=repo,
        ))
    return [r["name"] for r in rows]


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _parse(raw: str) -> dict | None:
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _call_llm(
    *,
    text: str,
    services: list[str],
    files: list[str],
    symbols: list[str],
) -> str:
    """Real LLM call. Isolated for monkey-patching in tests."""
    from openai import OpenAI

    client = OpenAI(
        base_url=DEFAULT_LM_URL,
        api_key=os.environ.get("AIFORGE_CODEMEM_LM_KEY", "lm-studio"),
    )
    system = _system_prompt()
    payload = {
        "query": text,
        "services_catalog": services,
        "candidate_files": files,
        "candidate_symbols": symbols,
    }
    user = json.dumps(payload, indent=2)
    resp = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=800,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""


def _system_prompt() -> str:
    return (
        "You ground a natural-language code query against a known catalog.\n"
        "Input: JSON with `query`, `services_catalog`, `candidate_files`, `candidate_symbols`.\n"
        "\n"
        "Output: a single JSON object — no prose, no markdown fences.\n"
        "{\n"
        '  "intent":   string  (one of "fix" | "add" | "investigate" | "refactor" | "test" | "ops"),\n'
        '  "services": array of strings — MUST be names from services_catalog,\n'
        '  "files":    array of strings — MUST be paths from candidate_files,\n'
        '  "symbols":  array of strings — MUST be fqnames from candidate_symbols,\n'
        '  "hops":     1 or 2,\n'
        '  "keywords": array of short tokens for downstream rerank\n'
        "}\n"
        "\n"
        "Rules:\n"
        "- Pick at most 3 services, 5 files, 8 symbols.\n"
        "- Never invent names. Anything not in the catalog/candidates is dropped.\n"
        "- Prefer the most specific match. If query mentions a service, list it.\n"
        "- Use hops=2 only when the query implies cross-service traversal.\n"
        "- Output ONLY the JSON object."
    )
