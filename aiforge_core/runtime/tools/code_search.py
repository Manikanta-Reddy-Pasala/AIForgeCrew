"""Semantic / hybrid code search — no codegraph binary required.

Ranks source files by embedding similarity to a natural-language query
(plus a keyword boost), using the same ``local_embed`` backends as memory
(hash / model2vec / API). Works immediately on any cwd; no separate index
build step. Cap the walk so a huge monorepo stays interactive.
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
    ".cs", ".rb", ".php", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".m", ".mm", ".scala", ".vue", ".svelte", ".md",
}
_NOISE = {
    ".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", "target", "vendor", ".next",
    "coverage", ".aiforge-worktrees", "worktrees", ".worktrees",
}
_WORD = re.compile(r"[a-z0-9_]{3,}", re.I)
_MAX_FILES = 250
_MAX_BYTES = 80_000
_SNIPPET = 600
#: (path, mtime_ns, size) -> embedding — process-local so a second query in
#: the same chat does not re-embed every file.
_VEC_CACHE: dict[tuple[str, int, int], list[float]] = {}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def _embed(text: str) -> list[float]:
    from aiforge_core.memory import local_embed
    return list(local_embed.embed(text[:8000]))


def _walk(root: Path, limit: int) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _NOISE and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() not in _EXTS:
                continue
            try:
                if p.stat().st_size > _MAX_BYTES:
                    continue
            except OSError:
                continue
            out.append(p)
            if len(out) >= limit:
                return out
    return out


def _summary(path: Path, root: Path) -> tuple[str, str]:
    """``(rel, text_for_embed)`` — path + leading content / defs."""
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rel, rel
    lines = raw.splitlines()
    keep: list[str] = [rel]
    for i, line in enumerate(lines[:200], 1):
        s = line.strip()
        if not s:
            continue
        if (s.startswith(("def ", "class ", "function ", "export ", "fn ",
                          "pub fn", "func ", "type ", "interface ",
                          "struct ", "impl ", "# ", "## "))
                or " = " in s[:40]):
            keep.append(f"{i}:{s[:160]}")
        if len(keep) >= 40:
            break
    if len(keep) < 5:
        keep.extend(lines[:30])
    body = "\n".join(keep)[:4000]
    return rel, body


def _keyword_boost(query: str, text: str) -> float:
    q = {w.lower() for w in _WORD.findall(query)}
    if not q:
        return 0.0
    t = text.lower()
    hit = sum(1 for w in q if w in t)
    return hit / max(1, len(q))


def search(query: str, cwd: str | None = None, *,
           top_k: int = 8, path: str | None = None) -> dict:
    """Rank code under ``cwd`` (or ``path``) for a natural-language ``query``."""
    q = (query or "").strip()
    if len(q) < 2:
        return {"ok": False, "error": "missing 'query'"}
    root = Path(path or cwd or ".").resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"not a directory: {root}"}
    try:
        top_k = max(1, min(int(top_k), 20))
    except (TypeError, ValueError):
        top_k = 8
    try:
        file_cap = int(os.environ.get("AIFORGE_CODE_SEARCH_FILES", str(_MAX_FILES)))
    except ValueError:
        file_cap = _MAX_FILES
    file_cap = max(50, min(file_cap, 3000))

    try:
        qvec = _embed(q)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"embed failed: {exc}"}

    files = _walk(root, file_cap)
    scored: list[tuple[float, dict]] = []
    for fp in files:
        rel, summary = _summary(fp, root)
        try:
            st = fp.stat()
            key = (str(fp), int(getattr(st, "st_mtime_ns", 0) or 0), int(st.st_size))
            vec = _VEC_CACHE.get(key)
            if vec is None:
                vec = _embed(summary)
                _VEC_CACHE[key] = vec
                if len(_VEC_CACHE) > 4000:
                    # drop an arbitrary older half — process cache, not durable
                    for k in list(_VEC_CACHE)[:2000]:
                        _VEC_CACHE.pop(k, None)
            sim = _cosine(qvec, vec)
        except Exception:  # noqa: BLE001
            sim = 0.0
        boost = _keyword_boost(q, summary)
        score = 0.75 * sim + 0.25 * boost
        if score < 0.05 and boost < 0.2:
            continue
        try:
            snippet = fp.read_text(encoding="utf-8", errors="replace")[:_SNIPPET]
        except OSError:
            snippet = summary[:_SNIPPET]
        scored.append((score, {
            "path": rel,
            "score": round(score, 4),
            "sim": round(sim, 4),
            "keyword": round(boost, 4),
            "snippet": snippet,
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    hits = [h for _, h in scored[:top_k]]
    return {
        "ok": True,
        "query": q,
        "root": str(root),
        "files_scanned": len(files),
        "hits": hits,
        "note": ("hybrid embed+keyword ranking — no codegraph binary needed; "
                 "set AIFORGE_EMBED_BACKEND=model2vec for paraphrase quality"),
    }


def code_search(args: dict, cwd: str | None = None) -> dict:
    """Tool entry: ``{query, top_k?, path?}``."""
    return search(
        str(args.get("query") or ""),
        cwd,
        top_k=args.get("top_k", 8),
        path=args.get("path"),
    )


__all__ = ["search", "code_search"]
