"""Scope guard for the smolagents Doer.

Wraps write tools with an allowlist derived from the ticket body's
``## Files`` or ``## Allowed files`` section.  Any write to a path
outside that list raises :class:`ScopeViolation`.
"""
from __future__ import annotations

import os
import re


class ScopeViolation(Exception):
    """Raised when a write tool targets a path outside the allowlist."""

    def __init__(self, path: str, allowed: set[str]) -> None:
        self.path = path
        self.allowed = allowed
        super().__init__(
            f"scope violation: {path!r} not in allowed set {sorted(allowed)}"
        )


def parse_allowed_files(body: str) -> set[str]:
    """Extract paths from the ``## Files`` or ``## Allowed files`` section.

    Returns an empty set when no matching header is found (no constraint).
    """
    if not body:
        return set()
    lower = body.lower()
    # Accept both "## files" and "## allowed files"
    for header in ("## allowed files", "## files"):
        idx = lower.find(header)
        if idx >= 0:
            nl = body.find("\n", idx)
            if nl < 0:
                return set()
            section = body[nl + 1:]
            end = section.find("\n## ")
            if end >= 0:
                section = section[:end]
            paths: set[str] = set()
            for line in section.splitlines():
                stripped = line.strip().lstrip("-*").lstrip()
                if not stripped or stripped.startswith("#"):
                    break
                # Capture first path-like token (contains / or . but not spaces)
                match = re.match(
                    r"`?([\w./-]+\.[A-Za-z0-9]+|[\w./-]+/[\w./-]+)(?::\d+)?",
                    stripped,
                )
                if match:
                    paths.add(match.group(1).strip("`"))
            return paths
    return set()


class ScopeGuard:
    """Check write paths against an allowlist parsed from a ticket body."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed

    def check(self, path: str) -> None:
        """Raise :class:`ScopeViolation` if *path* is not in the allowlist.

        Matches by suffix (allows both full repo-relative path and basename).
        When the allowlist is empty, all paths are permitted (no ## Files
        section present in ticket → no scope constraint).
        """
        if not self.allowed:
            return
        abs_path = os.path.abspath(path)
        base = os.path.basename(abs_path)
        for entry in self.allowed:
            entry = entry.strip()
            if not entry:
                continue
            if abs_path.endswith(entry) or base == os.path.basename(entry):
                return
        raise ScopeViolation(path, self.allowed)
