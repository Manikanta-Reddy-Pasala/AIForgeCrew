"""Read-only file allowlist — Aider's --read-only equivalent.

Some files should be visible to the doer (so it understands
context) but never edited. Examples: third-party DTOs imported
from ``oneshell-commons``, generated MapStruct impls, vendored
contracts. Keep them readable, refuse writes.

The list comes from:
1. Ticket body's ``## Read-only files`` section (if present).
2. ``.aiforge/READONLY`` in the worktree (one path per line).
3. Env ``AIFORGE_DOER_READONLY`` (colon-separated).

Used by :class:`scope_guard.ScopeGuard` — tools call ``is_readonly``
before write tools fire. Read tools (file_read, grep, glob) ignore
the list entirely.
"""
from __future__ import annotations

import os
import re


def _from_body(body: str) -> set[str]:
    if not body:
        return set()
    m = re.search(
        r"##\s*Read-only files\b[^\n]*\n(.*?)(?:\n##|\Z)",
        body, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return set()
    block = m.group(1)
    out: set[str] = set()
    for line in block.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def _from_file(worktree: str) -> set[str]:
    p = os.path.join(worktree, ".aiforge", "READONLY")
    if not os.path.isfile(p):
        return set()
    out: set[str] = set()
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    return out


def _from_env() -> set[str]:
    raw = os.environ.get("AIFORGE_DOER_READONLY", "")
    return {p.strip() for p in raw.split(":") if p.strip()}


def collect(worktree: str, ticket_body: str) -> set[str]:
    """Union of all three sources."""
    return _from_body(ticket_body) | _from_file(worktree) | _from_env()


def is_readonly(abs_path: str, readonly_set: set[str], worktree: str) -> bool:
    """True if ``abs_path`` matches any entry in the read-only set.

    Match by suffix on each pattern so callers can pass either
    repo-relative or absolute paths.
    """
    if not readonly_set:
        return False
    rel = os.path.relpath(abs_path, worktree) if abs_path.startswith("/") else abs_path
    for pat in readonly_set:
        if rel == pat or rel.endswith("/" + pat) or abs_path.endswith(pat):
            return True
    return False
