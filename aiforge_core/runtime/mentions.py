"""Context @-mentions for chat — ``@file``, ``@folder/``, ``@url``, ``@problems``.

Cline-style explicit context injection: the user drops ``@path`` /
``@https://…`` / ``@problems`` into a message and the referenced content is
resolved and pinned into the agent's context for that turn — no guessing,
no search. Orthogonal to the automatic repo-map + memory recall: this is
the user saying "look HERE specifically".

``expand(text, cwd)`` returns a ``MENTIONED CONTEXT`` block (or "") plus the
list of resolved mentions, leaving the user's text untouched.
"""
from __future__ import annotations

import os
import re

# @token where token is a path (a/b.py), a dir (a/b/), a url, or @problems.
# Stops at whitespace; allows the usual path/url chars.
_MENTION_RE = re.compile(r"(?<![\w/])@([A-Za-z0-9._\-/:~]+)")

_MAX_FILE = 12000
_MAX_DIR = 200
_MAX_URL = 12000


def _root(cwd: str) -> str:
    return os.environ.get("AIFORGE_WORKSPACE_DIR") or cwd


def _resolve_path(cwd: str, rel: str) -> str | None:
    base = os.path.abspath(_root(cwd))
    p = rel if os.path.isabs(rel) else os.path.join(base, rel)
    p = os.path.abspath(os.path.expanduser(p))
    # keep inside the workspace root (defensive — no escaping via @../../)
    if not (p == base or p.startswith(base + os.sep)):
        return None
    return p


def _file_block(path: str, token: str) -> str:
    try:
        body = open(path, encoding="utf-8", errors="replace").read()
    except Exception as exc:  # noqa: BLE001
        return f"@{token} → (could not read: {exc})"
    trunc = "\n…(truncated)" if len(body) > _MAX_FILE else ""
    return f"@{token} (file):\n```\n{body[:_MAX_FILE]}{trunc}\n```"


def _dir_block(path: str, token: str) -> str:
    try:
        entries = sorted(
            (c + "/") if os.path.isdir(os.path.join(path, c)) else c
            for c in os.listdir(path))
    except Exception as exc:  # noqa: BLE001
        return f"@{token} → (could not list: {exc})"
    more = f"\n…(+{len(entries) - _MAX_DIR} more)" if len(entries) > _MAX_DIR else ""
    return f"@{token} (folder):\n" + "\n".join(entries[:_MAX_DIR]) + more


def _url_block(url: str) -> str:
    try:
        from aiforge_core.runtime.tools import fetch_url as _fu
        # fetch_url exposes a tool fn; call defensively across signatures.
        res = _fu.fetch_url({"url": url}) if hasattr(_fu, "fetch_url") else None
        if isinstance(res, dict):
            txt = res.get("text") or res.get("content") or res.get("body") or ""
            if txt:
                return f"@{url} (url):\n{str(txt)[:_MAX_URL]}"
            return f"@{url} → (fetch returned no text: {res.get('error', 'empty')})"
    except Exception:  # noqa: BLE001
        pass
    # Fallback: plain urllib GET.
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310
            data = r.read(_MAX_URL).decode("utf-8", "replace")
        return f"@{url} (url):\n{data}"
    except Exception as exc:  # noqa: BLE001
        return f"@{url} → (could not fetch: {exc})"


def _problems_block(cwd: str) -> str:
    """Best-effort workspace diagnostics — run the repo's typecheck and
    surface failures. Falls back to a TODO/FIXME scan."""
    try:
        from aiforge_core.runtime.tools.typecheck import typecheck
        res = typecheck({"path": "."}, _root(cwd)) if callable(typecheck) else None
        if isinstance(res, dict) and (res.get("output") or res.get("errors")):
            out = res.get("output") or res.get("errors")
            return f"@problems (typecheck):\n{str(out)[:_MAX_FILE]}"
    except Exception:  # noqa: BLE001
        pass
    return ("@problems → (no type-checker output available; run the project's "
            "test/typecheck tool to see diagnostics)")


def expand(text: str, cwd: str) -> tuple[str, list[str]]:
    """Resolve @-mentions in ``text``. Returns (context_block, tokens)."""
    tokens = _MENTION_RE.findall(text or "")
    if not tokens:
        return "", []
    blocks: list[str] = []
    resolved: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok in seen:
            continue
        seen.add(tok)
        low = tok.lower()
        if low == "problems":
            blocks.append(_problems_block(cwd))
        elif tok.startswith(("http://", "https://")) or tok.startswith("http"):
            blocks.append(_url_block(tok))
        else:
            p = _resolve_path(cwd, tok.rstrip("/"))
            if p is None:
                blocks.append(f"@{tok} → (outside workspace, skipped)")
            elif os.path.isdir(p):
                blocks.append(_dir_block(p, tok))
            elif os.path.isfile(p):
                blocks.append(_file_block(p, tok))
            else:
                blocks.append(f"@{tok} → (not found)")
        resolved.append(tok)
    if not blocks:
        return "", []
    return ("MENTIONED CONTEXT (the user explicitly referenced these — use "
            "them directly):\n" + "\n\n".join(blocks)), resolved


__all__ = ["expand"]
