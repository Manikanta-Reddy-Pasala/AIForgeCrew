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
    """Aggregate char cap for the whole mentions block. An explicit
    ``AIFORGE_MENTIONS_TOTAL_CHARS`` wins verbatim (0 disables); otherwise
    window-relative (A2): floor 48000 on a 32K window, ~10% of a bigger one."""
    env = os.environ.get("AIFORGE_MENTIONS_TOTAL_CHARS")
    if env is not None:
        try:
            return max(0, int(env))
        except (TypeError, ValueError):
            pass
    try:
        from aiforge_core.runtime.chat_agent import _window_scaled
        return _window_scaled(48000, 0.10)
    except Exception:  # noqa: BLE001
        return 48000


def _root(cwd: str) -> str:
    from aiforge_core.runtime import request_context
    return request_context.get_workspace_dir() or cwd


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


def _url_allowed(tok: str) -> bool:
    """A @-mention of a URL is a page to READ, not an endpoint we authenticate
    to, so this is a scheme check — see the note in doer_tools/_web."""
    return tok.lower().startswith(("http://", "https://"))


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
    # FALLBACK, but not a bypass: this used to be a bare urlopen, so an import
    # error in the line above turned the gated fetcher into an ungated one —
    # the switch held only while nothing went wrong.
    from aiforge_core.net import egress as _egress
    refusal = _egress.check(url)
    if refusal is not None:
        return f"@{url} → (not fetched: {refusal.get('error')})"
    try:
        import urllib.request

        from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
        try:
            guard_public_url(url)
        except SSRFBlocked as exc:
            if exc.kind != "dns":
                return f"@{url} → (blocked: {exc})"
        with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310
            data = r.read(_MAX_URL).decode("utf-8", "replace")
        return f"@{url} (url):\n{data}"
    except Exception as exc:  # noqa: BLE001
        return f"@{url} → (could not fetch: {exc})"


def _problems_block(_cwd: str) -> str:
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


def _dedupe_order(tokens: list[str]) -> list[str]:
    """Unique tokens, first-seen order preserved."""
    uniq: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok not in seen:
            seen.add(tok)
            uniq.append(tok)
    return uniq


def _resolve_one(tok: str, cwd: str) -> str:
    """The context block for a single @-mention — a problems dump, a fetched URL,
    or a workspace file/dir (with the outside-workspace / not-found fallbacks)."""
    low = tok.lower()
    if low == "problems":
        return _problems_block(cwd)
    if _url_allowed(tok):
        return _url_block(tok)
    p = _resolve_path(cwd, tok.rstrip("/"))
    if p is None:
        return f"@{tok} → (outside workspace, skipped)"
    if os.path.isdir(p):
        return _dir_block(p, tok)
    if os.path.isfile(p):
        return _file_block(p, tok)
    return f"@{tok} → (not found)"


def _fit_block(block: str, total: int, total_cap: int) -> str | None:
    """Trim ``block`` to the remaining char budget, or None when there is too
    little room to bother (it is counted as omitted instead)."""
    if not total_cap or total + len(block) <= total_cap:
        return block
    room = total_cap - total
    if room > 200:
        return block[:room] + "\n…(truncated to fit context)\n"
    return None


def expand(text: str, cwd: str) -> tuple[str, list[str]]:
    """Resolve @-mentions in ``text``. Returns (context_block, tokens)."""
    tokens = _MENTION_RE.findall(text or "")
    if not tokens:
        return "", []
    uniq = _dedupe_order(tokens)
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
        block = _fit_block(_resolve_one(tok, cwd), total, total_cap)
        if block is None:
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
