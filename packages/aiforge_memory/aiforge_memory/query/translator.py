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
DEFAULT_RERANK_URL = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")

# Tunables — env-overridable so we can experiment without code edits.
RRF_K = int(os.environ.get("AIFORGE_TRANSLATOR_RRF_K", "60"))
RERANK_TOPN = int(os.environ.get("AIFORGE_TRANSLATOR_RERANK_TOPN", "30"))
ENABLE_RERANK = os.environ.get("AIFORGE_TRANSLATOR_RERANK", "1") not in ("0", "false", "False")


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
    # Per-target retrieval scores (path → score, fqname → score). Surfaced
    # to the bundle so the UI can show "how confident" each anchor is.
    file_scores: dict[str, float] = field(default_factory=dict)
    symbol_scores: dict[str, float] = field(default_factory=dict)


def translate(
    text: str,
    *,
    repo: str,
    driver,
    top_k: int = 20,
) -> Grounding:
    """NL → entities. Best-effort: any sub-stage failure leaves Grounding
    partially populated and adds an entry to .errors.

    Pipeline:
        1. _expand_query: lightweight query expansion (CamelCase split +
           known-synonym injection). Cheap, no LLM.
        2. _chunk_fulltext_topk: Lucene over raw chunk text → file
           anchors (code-chunk vectors removed 2026-06-11).
        3. _fulltext_symbols: Lucene over Symbol_v2.signature.
        4. RRF fuse the two ranked file lists + path-prior bonus.
        5. _rerank (optional): cross-encoder rerank top-N to top-K.
        6. _expand_one_hop: pull 1-hop neighbours via IMPORTS/CALLS for
           the top files (recall boost for cross-file queries).
        7. _call_llm: ground to catalog/candidates only. Never invents.
    """
    g = Grounding()
    if not text.strip():
        return g

    # 1 — query expansion (CamelCase + synonyms)
    expanded = _expand_query(text)

    # 2/3 — collect ranked lists per source
    vector_files: list[str] = []
    fulltext_files: list[str] = []
    candidate_symbols: list[str] = []
    file_scores: dict[str, float] = {}
    file_texts: dict[str, str] = {}      # best chunk text per path (rerank)
    try:
        rows = _chunk_fulltext_topk(
            driver, repo=repo, text=expanded, k=max(top_k * 3, 50))
        for r in rows:
            fp = r.get("file_path")
            if fp and fp not in vector_files:
                vector_files.append(fp)
                file_scores[fp] = max(file_scores.get(fp, 0.0),
                                      float(r.get("score") or 0.0))
            if fp and fp not in file_texts and r.get("text"):
                file_texts[fp] = str(r["text"])
        candidate_symbols = _symbols_in(driver, repo=repo, files=vector_files[:top_k])
        g.used_top_k = len(rows)
    except Exception as exc:
        g.errors.append(f"embed/topk: {exc}")

    try:
        ft_syms, ft_files = _fulltext_symbols(
            driver, repo=repo, text=expanded, k=max(top_k * 3, 50),
        )
        fulltext_files = ft_files
        for fq in ft_syms:
            if fq not in candidate_symbols:
                candidate_symbols.append(fq)
    except Exception as exc:
        g.errors.append(f"fulltext: {exc}")

    # 4 — RRF fusion + path-prior bonus
    candidate_files = _rrf_fuse(
        ranked_lists=[vector_files, fulltext_files],
        path_prior=_path_prior(text, vector_files + fulltext_files),
        k=RRF_K,
    )

    # 5 — cross-encoder rerank if sidecar reachable. Score real content
    # (path + best matching chunk text), not the bare path string.
    if ENABLE_RERANK and candidate_files:
        try:
            top = candidate_files[:RERANK_TOPN]
            candidate_files = _rerank(
                query=text, docs=top,
                doc_texts=[_rerank_doc(p, file_texts) for p in top],
            ) + candidate_files[RERANK_TOPN:]
        except Exception as exc:
            g.errors.append(f"rerank: {exc}")

    # 6 — 1-hop graph expansion: pull files imported by / calling top hits
    try:
        neighbours = _expand_one_hop(
            driver, repo=repo, files=candidate_files[:5],
        )
        for fp in neighbours:
            if fp not in candidate_files:
                candidate_files.append(fp)
    except Exception as exc:
        g.errors.append(f"one-hop: {exc}")

    # Cap candidate set so the LLM prompt does not blow up
    candidate_files = candidate_files[: max(top_k * 2, 40)]

    # Service catalog
    services = _services_for(driver, repo=repo)

    # Hydrate file summaries for translator grounding (skip on error —
    # falls back to path-only grounding which is the prior behaviour).
    file_summaries: dict[str, str] = {}
    try:
        file_summaries = _file_summaries(
            driver, repo=repo, paths=candidate_files,
        )
    except Exception as exc:
        g.errors.append(f"summaries: {exc}")

    # Hydrate symbol summaries so the T2 grounding LLM can see what
    # each candidate symbol actually does (cuts grounding errors).
    sym_summaries: dict[str, str] = {}
    try:
        sym_summaries = _symbol_summaries(
            driver, repo=repo, fqnames=candidate_symbols,
        )
    except Exception as exc:
        g.errors.append(f"sym_summaries: {exc}")

    # T2 — LLM grounding
    try:
        raw = _call_llm(
            text=text, services=services,
            files=candidate_files, symbols=candidate_symbols,
            file_summaries=file_summaries, file_scores=file_scores,
            symbol_summaries=sym_summaries,
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

    # Surface retrieval scores so the UI / bundle can show confidence.
    # Files: vector chunk score for that path (0..1). Symbols: best
    # reciprocal-rank from RRF — proxy for how strongly retrieval voted.
    g.file_scores = {p: round(file_scores.get(p, 0.0), 4)
                     for p in g.files}
    if candidate_symbols:
        # Build rank-based score from the order symbols emerged.
        sym_rank = {fq: i for i, fq in enumerate(candidate_symbols)}
        g.symbol_scores = {
            fq: round(1.0 / (sym_rank[fq] + 1.0), 4)
            for fq in g.symbols if fq in sym_rank
        }
    return g


_CHUNK_FT_CYPHER = """
CALL db.index.fulltext.queryNodes('codemem_chunk_text_ft', $q, {limit: $k_query})
YIELD node AS c, score
WHERE c.repo = $repo
RETURN c.file_path AS file_path, c.text AS text, score
ORDER BY score DESC
LIMIT $k
"""


def _chunk_fulltext_topk(driver, *, repo: str, text: str, k: int) -> list[dict]:
    """NL→file anchoring via Lucene over raw chunk text.

    Replaces the removed code-chunk vector recall: same row shape
    (file_path, text, score) so RRF/rerank downstream is untouched —
    at zero embed cost and zero index staleness beyond the last ingest.
    """
    tokens = _tokenize_for_fulltext(text)
    if not tokens:
        return []
    q = " OR ".join(_escape_lucene(tok) for tok in tokens)
    # Lucene ranks globally; over-fetch so the repo filter leaves ~k.
    k_query = min(k * 4, 500)
    with driver.session() as s:
        rows = list(s.run(_CHUNK_FT_CYPHER, repo=repo, q=q, k=k,
                          k_query=k_query))
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


_FULLTEXT_CYPHER = """
CALL db.index.fulltext.queryNodes('codemem_symbol_signature_ft', $q)
YIELD node AS sym, score
WHERE sym.repo = $repo
RETURN sym.fqname AS fqname, sym.file_path AS file_path, score
ORDER BY score DESC
LIMIT $k
"""


_LUCENE_SPECIALS = re.compile(r'([+\-!(){}\[\]^"~*?:\\/])')
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _camel_split(token: str) -> list[str]:
    """`BusinessProductsController` → ['Business','Products','Controller']."""
    if not token or "_" in token:
        return [t for t in token.split("_") if t]
    parts = _CAMEL_RE.split(token)
    return [p for p in parts if p]


def _tokenize_for_fulltext(text: str) -> list[str]:
    """Split + camel-decompose + drop short tokens. De-dupe, lowercase."""
    raw = re.split(r"[\s/.\-]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        for piece in _camel_split(tok):
            piece = piece.strip()
            if len(piece) < 3:
                continue
            low = piece.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(piece)
    return out


def _escape_lucene(s: str) -> str:
    return _LUCENE_SPECIALS.sub(r"\\\1", s)


def _fulltext_symbols(
    driver, *, repo: str, text: str, k: int = 20,
) -> tuple[list[str], list[str]]:
    """Lucene fulltext search over Symbol fqname/signature/doc.
    Returns (symbol_fqnames, file_paths).

    CamelCase-aware: splits `BusinessProductsController` into its parts
    so signature-token matches work even when the query uses the joined
    form. Lucene specials are escaped.
    """
    tokens = _tokenize_for_fulltext(text)
    if not tokens:
        return [], []
    # Boost compound tokens: ride OR-of-pieces with quoted-phrase fallback
    q = " OR ".join(_escape_lucene(t) for t in tokens)
    syms: list[str] = []
    files: list[str] = []
    try:
        with driver.session() as s:
            for r in s.run(_FULLTEXT_CYPHER, repo=repo, q=q, k=k):
                syms.append(r["fqname"])
                fp = r.get("file_path")
                if fp and fp not in files:
                    files.append(fp)
    except Exception:
        return [], []
    return syms, files


# ─────────────────── retrieval-quality helpers ────────────────────────

# Cheap synonym table — code-domain terms users say vs. names in the repo.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "auth":         ("authentication", "jwt", "login", "token"),
    "jwt":          ("auth", "authentication", "token"),
    "crud":         ("controller", "service", "repository", "create", "update", "delete"),
    "endpoint":     ("controller", "api", "route"),
    "api":          ("controller", "endpoint"),
    "create":       ("save", "insert", "post"),
    "update":       ("patch", "put", "modify"),
    "delete":       ("remove", "destroy"),
    "list":         ("getAll", "findAll", "list"),
    "fetch":        ("get", "find", "load"),
    "validation":   ("validator", "validate", "check"),
    "sync":         ("dataSync", "push", "pull", "replication"),
    "push":         ("publish", "send", "sync"),
    "pull":         ("fetch", "request", "sync"),
    "test":         ("test", "spec", "junit"),
    "controller":   ("controller", "rest", "endpoint"),
    "service":      ("service", "impl"),
    "repository":   ("repo", "dao", "store"),
}


def _expand_query(text: str) -> str:
    """Append synonyms to query so embed and fulltext both see broader
    surface area. Original text is kept; synonyms appended after."""
    base_tokens = _tokenize_for_fulltext(text)
    extra: list[str] = []
    seen: set[str] = {t.lower() for t in base_tokens}
    for tok in base_tokens:
        for syn in _SYNONYMS.get(tok.lower(), ()):
            if syn.lower() not in seen:
                extra.append(syn)
                seen.add(syn.lower())
    if not extra:
        return text
    return text + "  " + " ".join(extra)


def _rrf_fuse(
    *,
    ranked_lists: list[list[str]],
    path_prior: dict[str, float] | None = None,
    k: int = 60,
) -> list[str]:
    """Reciprocal-rank fusion across multiple ranked lists, plus a
    path-prior float bonus per doc. RRF score = Σ 1/(k+rank). Stable
    across heterogeneous score scales (cosine + BM25)."""
    score: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, doc in enumerate(lst):
            score[doc] = score.get(doc, 0.0) + 1.0 / (k + rank + 1)
    if path_prior:
        for doc, bonus in path_prior.items():
            if doc in score:
                score[doc] += bonus
    return [doc for doc, _ in sorted(score.items(), key=lambda kv: -kv[1])]


def _path_prior(query: str, paths: list[str]) -> dict[str, float]:
    """Lightweight rule-based path bonus.

    `controller`/`endpoint` query → boost paths matching `*Controller*`.
    `service` → `*Service*`. `test` → `src/test/**`. Repository / DTO /
    Mapper similar. Bonus is small (≈ 0.02) so RRF stays dominant on
    truly-relevant docs but ties break in favour of intent-matched files.
    """
    q = query.lower()
    cues: list[tuple[str, float, callable]] = []
    if any(t in q for t in ("controller", "endpoint", "api", "route")):
        cues.append(("controller", 0.03, lambda p: "Controller" in p))
    if any(t in q for t in ("service", "business", "logic")):
        cues.append(("service", 0.02, lambda p: "Service" in p))
    if any(t in q for t in ("repository", "repo", "dao", "store")):
        cues.append(("repo", 0.02, lambda p: "Repository" in p or "Repo.java" in p))
    if any(t in q for t in ("dto", "request", "response", "model")):
        cues.append(("dto", 0.02, lambda p: any(s in p for s in ("Dto", "Request", "Response", "/model/"))))
    if any(t in q for t in ("test", "spec", "junit")):
        cues.append(("test", 0.04, lambda p: p.startswith("src/test/")))
    if any(t in q for t in ("mapper", "convert")):
        cues.append(("mapper", 0.02, lambda p: "Mapper" in p))
    out: dict[str, float] = {}
    if not cues:
        return out
    for p in paths:
        bonus = 0.0
        for _, w, fn in cues:
            try:
                if fn(p):
                    bonus += w
            except Exception:
                pass
        if bonus:
            out[p] = bonus
    return out


def _rerank_doc(path: str, file_texts: dict[str, str]) -> str:
    """Document the cross-encoder actually scores: the path plus the
    best-matching chunk text (capped) — a bare path carries almost no
    signal for a content model."""
    text = (file_texts.get(path) or "").strip()
    return f"{path}\n{text[:512]}" if text else path


def _rerank(
    *, query: str, docs: list[str], doc_texts: list[str] | None = None,
) -> list[str]:
    """Cross-encoder rerank via :8765 sidecar. Returns ``docs``
    reordered. ``doc_texts`` (same length) is what gets scored —
    defaults to the docs themselves. Falls back to identity on failure."""
    if not docs:
        return docs
    payload = doc_texts if doc_texts and len(doc_texts) == len(docs) else docs
    url = DEFAULT_RERANK_URL.rstrip("/") + "/rerank"
    try:
        r = httpx.post(
            url, json={"query": query, "texts": payload}, timeout=15.0,
        )
        r.raise_for_status()
        scores = r.json().get("scores") or []
        if len(scores) != len(docs):
            return docs
        ranked = sorted(zip(docs, scores), key=lambda x: -float(x[1]))
        return [d for d, _ in ranked]
    except Exception:
        return docs


def _expand_one_hop(driver, *, repo: str, files: list[str]) -> list[str]:
    """For each top file, fetch up to 3 IMPORTS or 3 incoming-IMPORTS
    neighbours. Adds related files the vector index missed."""
    if not files:
        return []
    cy = (
        "MATCH (f:File_v2 {repo:$repo}) WHERE f.path IN $files "
        "OPTIONAL MATCH (f)-[:IMPORTS]->(out:File_v2) "
        "OPTIONAL MATCH (in:File_v2)-[:IMPORTS]->(f) "
        "WITH collect(DISTINCT out.path) AS outs, collect(DISTINCT in.path) AS ins "
        "RETURN outs + ins AS paths"
    )
    out: list[str] = []
    try:
        with driver.session() as s:
            row = s.run(cy, repo=repo, files=files).single()
            for p in (row["paths"] or []) if row else []:
                if p and p not in out:
                    out.append(p)
    except Exception:
        return []
    return out[:20]


def _file_summaries(
    driver, *, repo: str, paths: list[str],
) -> dict[str, str]:
    """Bulk-fetch File_v2.summary for the candidate paths.
    Empty summaries (skipped or unindexed) are omitted from the result."""
    if not paths:
        return {}
    cy = (
        "MATCH (f:File_v2 {repo:$repo}) WHERE f.path IN $paths "
        "AND f.summary IS NOT NULL AND f.summary <> '' "
        "RETURN f.path AS path, f.summary AS summary"
    )
    with driver.session() as s:
        return {r["path"]: r["summary"] for r in s.run(cy, repo=repo, paths=paths)}


def _symbol_summaries(
    driver, *, repo: str, fqnames: list[str],
) -> dict[str, str]:
    """Bulk-fetch Symbol_v2.summary for candidate fqnames. Falls back
    to doc_first_line when no LLM summary exists yet — partial coverage
    is fine because the grounding LLM degrades gracefully without it."""
    if not fqnames:
        return {}
    cy = (
        "MATCH (s:Symbol_v2 {repo:$repo}) WHERE s.fqname IN $fqs "
        "RETURN s.fqname AS f, "
        "       coalesce(NULLIF(s.summary,''), s.doc_first_line, '') AS d"
    )
    with driver.session() as sess:
        out = {}
        for r in sess.run(cy, repo=repo, fqs=fqnames):
            d = (r["d"] or "").strip()
            if d:
                out[r["f"]] = d
        return out


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
    file_summaries: dict[str, str] | None = None,
    file_scores: dict[str, float] | None = None,
    symbol_summaries: dict[str, str] | None = None,
) -> str:
    """Real LLM call. Isolated for monkey-patching in tests.

    file_summaries: optional {path: summary} so the LLM grounds on
    semantics, not just filename. file_scores: optional {path: score}
    so the LLM weights vector-confidence into its pick. symbol_summaries:
    optional {fqname: behaviour-summary-or-docstring} so the LLM picks
    based on what each method actually does, not just by name shape."""
    from openai import OpenAI

    client = OpenAI(
        base_url=DEFAULT_LM_URL,
        api_key=os.environ.get("AIFORGE_CODEMEM_LM_KEY", "lm-studio"),
    )
    system = _system_prompt()

    # Build enriched candidate_files: each entry is {path, summary?, score?}
    # so the LLM has semantic + ranking context, not just a path string.
    enriched_files: list[dict] = []
    for p in files:
        item: dict = {"path": p}
        if file_summaries and file_summaries.get(p):
            item["summary"] = file_summaries[p][:300]
        if file_scores and p in file_scores:
            item["score"] = round(float(file_scores[p]), 3)
        enriched_files.append(item)

    # Same enrichment for symbols. Falls back to flat string list when
    # we have no summaries (older repos, pre-summary feature).
    enriched_symbols: list = []
    if symbol_summaries:
        for fq in symbols:
            d = symbol_summaries.get(fq, "")
            if d:
                enriched_symbols.append(
                    {"fqname": fq, "summary": d[:240]}
                )
            else:
                enriched_symbols.append({"fqname": fq})
    else:
        enriched_symbols = symbols

    payload = {
        "query": text,
        "services_catalog": services,
        "candidate_files": enriched_files,
        "candidate_symbols": enriched_symbols,
    }
    user = json.dumps(payload, indent=2)
    from aiforge_memory.llm_compat import response_format
    create_kwargs: dict = dict(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=800,
    )
    rf = response_format()
    if rf is not None:
        create_kwargs["response_format"] = rf
    resp = client.chat.completions.create(**create_kwargs)
    return resp.choices[0].message.content or ""


def _system_prompt() -> str:
    return (
        "You ground a natural-language code query against a known catalog.\n"
        "Input: JSON with `query`, `services_catalog`, `candidate_files`, `candidate_symbols`.\n"
        "\n"
        "candidate_files entries are objects: {path, summary, score}.\n"
        "  - `summary` describes what the file actually does (LLM-generated).\n"
        "  - `score` is the vector-similarity rank (higher = more relevant).\n"
        "\n"
        "Output: a single JSON object — no prose, no markdown fences.\n"
        "{\n"
        '  "intent":   string  (one of "fix" | "add" | "investigate" | "refactor" | "test" | "ops"),\n'
        '  "services": array of strings — MUST be names from services_catalog,\n'
        '  "files":    array of strings — MUST be paths from candidate_files[].path,\n'
        '  "symbols":  array of strings — MUST be fqnames from candidate_symbols,\n'
        '  "hops":     1 or 2,\n'
        '  "keywords": array of short tokens for downstream rerank\n'
        "}\n"
        "\n"
        "Grounding rules (prioritised):\n"
        "1. Strong-noun-match — if the query contains a domain noun "
        "(e.g. 'retry', 'auth', 'sync', 'webhook') AND a candidate file's "
        "path or summary contains that noun, prefer that file.\n"
        "2. Summary-semantic match — pick files whose summary best "
        "describes the *behaviour* the query asks about, not just the "
        "topic area.\n"
        "3. Vector-score tiebreak — when multiple candidates match by "
        "rules 1+2, prefer the higher score.\n"
        "4. Pick at most 3 services, 5 files, 8 symbols.\n"
        "5. Never invent names. Anything not in the catalog/candidates "
        "is dropped.\n"
        "6. Use hops=2 only when the query implies cross-service traversal.\n"
        "7. Output ONLY the JSON object."
    )
