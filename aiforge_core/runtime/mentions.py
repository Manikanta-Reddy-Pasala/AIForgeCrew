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


def _mentions_max() -> int:
    """Max number of @-mentions resolved in one turn (env
    ``AIFORGE_MENTIONS_MAX``, default 8). Beyond this the rest are noted as
    omitted — 26 ``@file`` mentions would otherwise inject ~312K chars."""
    try:
        return max(1, int(os.environ.get("AIFORGE_MENTIONS_MAX", "8")))
    except (TypeError, ValueError):
        return 8


def _mentions_total_chars() -> int:
    """Aggregate char cap for the whole mentions block (env
    ``AIFORGE_MENTIONS_TOTAL_CHARS``, default 48000; 0 disables)."""
    try:
        return max(0, int(os.environ.get("AIFORGE_MENTIONS_TOTAL_CHARS", "48000")))
    except (TypeError, ValueError):
        return 48000


def _root(cwd: str) -> str:
    return os.environ.get("AIFORGE_WORKSPACE_DIR") or cwd


def _resolve_path(cwd: str, rel: str) -> str | None:
    # realpath (not abspath) so a symlink inside the workspace that points
    # OUT of it can't escape the AIFORGE_WORKSPACE_DIR clamp.
    base = os.path.realpath(os.path.expanduser(_root(cwd)))
    p = rel if os.path.isabs(rel) else os.path.join(base, rel)
    p = os.path.realpath(os.path.expanduser(p))
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
    # Canonical fetcher (str arg → {ok, body}); falls back to urllib.
    try:
        from aiforge_core.runtime.doer_tools import fetch_url
        res = fetch_url(url)
        if isinstance(res, dict) and res.get("ok") and res.get("body"):
            return f"@{url} (url):\n{str(res['body'])[:_MAX_URL]}"
        if isinstance(res, dict) and not res.get("ok"):
            return f"@{url} → (could not fetch: {res.get('error', 'error')})"
    except Exception:  # noqa: BLE001
        pass
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310
            data = r.read(_MAX_URL).decode("utf-8", "replace")
        return f"@{url} (url):\n{data}"
    except Exception as exc:  # noqa: BLE001
        return f"@{url} → (could not fetch: {exc})"


def _problems_block(cwd: str) -> str:
    """Best-effort workspace diagnostics — run the repo's typecheck and
    surface failures. ``typecheck()`` resolves its own root."""
    try:
        from aiforge_core.runtime.tools.typecheck import typecheck
        res = typecheck()
        if isinstance(res, dict):
            out = (res.get("output") or res.get("errors")
                   or res.get("stdout") or res.get("stderr"))
            if out:
                return f"@problems (typecheck):\n{str(out)[:_MAX_FILE]}"
            if res.get("ok"):
                return "@problems → (typecheck clean — no diagnostics)"
    except Exception:  # noqa: BLE001
        pass
    return ("@problems → (no type-checker output available; run the project's "
            "test/typecheck tool to see diagnostics)")


def expand(text: str, cwd: str) -> tuple[str, list[str]]:
    """Resolve @-mentions in ``text``. Returns (context_block, tokens)."""
    tokens = _MENTION_RE.findall(text or "")
    if not tokens:
        return "", []
    # Dedupe preserving order.
    uniq: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok not in seen:
            seen.add(tok)
            uniq.append(tok)
    max_n = _mentions_max()
    total_cap = _mentions_total_chars()
    blocks: list[str] = []
    resolved: list[str] = []
    total = 0
    omitted = 0
    for tok in uniq:
        # Count / aggregate caps: beyond them, note as omitted (don't resolve).
        if len(resolved) >= max_n or (total_cap and total >= total_cap):
            omitted += 1
            continue
        low = tok.lower()
        if low == "problems":
            block = _problems_block(cwd)
        elif tok.startswith(("http://", "https://")):
            block = _url_block(tok)
        else:
            p = _resolve_path(cwd, tok.rstrip("/"))
            if p is None:
                block = f"@{tok} → (outside workspace, skipped)"
            elif os.path.isdir(p):
                block = _dir_block(p, tok)
            elif os.path.isfile(p):
                block = _file_block(p, tok)
            else:
                block = f"@{tok} → (not found)"
        if total_cap and total + len(block) > total_cap:
            room = total_cap - total
            if room > 200:
                block = block[:room] + "\n…(truncated to fit context)\n"
            else:
                omitted += 1
                continue
        blocks.append(block)
        resolved.append(tok)
        total += len(block)
    if not blocks:
        return "", []
    tail = f"\n\n…({omitted} more mentions omitted to fit context)" if omitted else ""
    return ("MENTIONED CONTEXT (the user explicitly referenced these — use "
            "them directly):\n" + "\n\n".join(blocks) + tail), resolved


__all__ = ["expand"]
