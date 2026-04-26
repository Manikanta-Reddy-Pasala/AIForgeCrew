"""@-mention parser — ``@file/path`` ``@docs/library`` ``@web/url``
inline expansion before chat dispatch.

KISS: pure regex parse + per-tag resolver. Returns the rewritten
query (with snippets inlined) plus a list of resolved attachments
the UI can render as chips.

Tag grammar (lenient):
- ``@file/<repo-relative-path>`` — substitutes a (lineno-prefixed)
  excerpt of the file (up to 120 lines).
- ``@docs/<library>[#anchor]`` — looks up doc chunk via
  ``aiforge_core.index.docs_index.lookup_doc(library, anchor)``.
- ``@web/<url>`` — passes URL to web_search tool, inlines first
  3 result snippets.
- ``@ticket/<id>`` — inlines ticket title + status + last comment.

Unknown tags are passed through verbatim.

Public surface:
- ``parse(text) -> list[Mention]`` — discovery only
- ``expand(text, *, worktree, top_k=3) -> tuple[str, list[dict]]`` —
  discovery + resolution + rewrite
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


_TAG_RE = re.compile(
    r"(?<!\w)@(?P<kind>file|docs|web|ticket)/(?P<value>\S+)",
)


@dataclass
class Mention:
    kind: str          # 'file' | 'docs' | 'web' | 'ticket'
    value: str         # path / library / url / ticket-id
    raw: str           # original ``@file/foo.py`` token
    resolved: str = "" # inline-substitution body
    metadata: dict = field(default_factory=dict)


def parse(text: str) -> list[Mention]:
    """Discover @-mentions; do NOT resolve."""
    return [
        Mention(kind=m.group("kind"), value=m.group("value"), raw=m.group(0))
        for m in _TAG_RE.finditer(text or "")
    ]


def expand(
    text: str, *, worktree: str | None = None, top_k: int = 3,
) -> tuple[str, list[dict]]:
    """Resolve every mention, return ``(rewritten_text, chips)``.

    ``chips`` is a flat list of dicts the UI can render alongside
    the answer. Failures append a ``[mention error]`` line in place
    of the substitution but never raise.
    """
    mentions = parse(text)
    if not mentions:
        return text, []

    chips: list[dict] = []
    rewritten = text
    for mention in mentions:
        try:
            body = _resolve(mention, worktree=worktree, top_k=top_k)
        except Exception as exc:
            body = f"[mention error: {exc}]"
        mention.resolved = body
        chips.append({
            "kind": mention.kind,
            "value": mention.value,
            "preview": body[:200],
        })
        rewritten = rewritten.replace(
            mention.raw,
            f"\n\n--- {mention.raw} ---\n{body}\n--- end {mention.raw} ---\n",
            1,
        )
    return rewritten, chips


# ───────── resolvers ───────────────────────────────────────────────


def _resolve(
    mention: Mention, *, worktree: str | None, top_k: int,
) -> str:
    if mention.kind == "file":
        return _resolve_file(mention.value, worktree=worktree)
    if mention.kind == "docs":
        return _resolve_docs(mention.value, top_k=top_k)
    if mention.kind == "web":
        return _resolve_web(mention.value)
    if mention.kind == "ticket":
        return _resolve_ticket(mention.value)
    return f"[unknown mention kind: {mention.kind}]"


def _resolve_file(path: str, *, worktree: str | None) -> str:
    base = Path(worktree) if worktree else Path.cwd()
    candidates = [base / path]
    if not path.startswith("/"):
        candidates.append(Path("/") / path)
    for cand in candidates:
        try:
            text = cand.read_text(errors="replace")
        except Exception:
            continue
        lines = text.splitlines()[:120]
        body = "\n".join(f"{i + 1:4d} | {ln}" for i, ln in enumerate(lines))
        return f"{cand}:\n{body}"
    return f"[file not found: {path}]"


def _resolve_docs(library: str, *, top_k: int) -> str:
    """Anchor split: ``library#topic``."""
    library, _, anchor = library.partition("#")
    try:
        from aiforge_core.index.docs_index import lookup_doc
    except Exception:
        return f"[docs index missing for {library}]"
    chunks = lookup_doc(library, anchor or "", top_k=top_k)
    if not chunks:
        return f"[no docs for {library}#{anchor}]"
    return "\n---\n".join(c["text"][:600] for c in chunks)


def _resolve_web(url: str) -> str:
    """Pass through to web_search tool. KISS — quick urlopen, no
    HTML parsing; the LLM can interpret raw text."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "aiforge-mentions/0.1",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read(64_000).decode("utf-8", "replace")
        return data[:8000]
    except Exception as exc:
        return f"[web fetch failed: {exc}]"


def _resolve_ticket(identifier: str) -> str:
    try:
        from aiforge_core.runtime import tickets as _tk
        t = _tk.get_by_identifier(identifier)
    except Exception as exc:
        return f"[ticket lookup error: {exc}]"
    if not t:
        return f"[ticket not found: {identifier}]"
    title = getattr(t, "title", "") or ""
    status = getattr(t, "status", "") or ""
    body = (getattr(t, "body", "") or "")[:600]
    return f"{identifier} · {status} · {title}\n\n{body}"


def has_mentions(text: str) -> bool:
    return bool(_TAG_RE.search(text or ""))
