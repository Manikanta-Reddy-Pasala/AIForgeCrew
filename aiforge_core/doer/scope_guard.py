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
    # Some callers (e.g. psql shell inserts) store literal '\n' escape
    # sequences instead of real newlines. Normalize so the line-based
    # parser works on either form.
    if "\\n" in body and "\n" not in body:
        body = body.replace("\\n", "\n")
    lower = body.lower()
    # Accept "## files", "## allowed files", "## file to edit",
    # "## file", "## files to edit". First matching header wins.
    headers = (
        "## allowed files",
        "## files to edit",
        "## file to edit",
        "## files",
        "## file",
    )
    for header in headers:
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
                # Capture first path-like token (contains / or . but not
                # spaces). Accept glob segments (``**``, ``*.ext``) so
                # directory allowlists like ``foo/bar/**`` round-trip.
                match = re.match(
                    r"`?([\w./*+-]+?\.\*?[A-Za-z0-9]*"
                    r"|[\w./*+-]+/[\w./*+-]+"
                    r"|[\w./-]+/\*\*)(?::\d+)?",
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

        Three match modes per allowlist entry:

        1. Literal file: ``abs_path.endswith(entry)`` or basename match.
        2. Directory glob ``foo/bar/**`` or ``foo/bar/*.java``: strip the
           trailing ``/**`` / ``/*.ext`` and do a substring match on the
           directory path, optionally filtered by extension.
        3. Empty allowlist: no constraint, permit all.
        """
        if not self.allowed:
            return
        abs_path = os.path.abspath(path)
        base = os.path.basename(abs_path)
        for entry in self.allowed:
            entry = entry.strip()
            if not entry:
                continue

            # Glob forms: '<prefix>/**', '<prefix>/**/*.ext', '<prefix>/*.ext'.
            # Strip the glob tail and check prefix containment + extension.
            m = re.match(r"^(.+?)/\*\*(?:/\*(\.[A-Za-z0-9]+))?$", entry)
            if m:
                prefix, ext = m.group(1), m.group(2)
                if prefix in abs_path and (not ext or abs_path.endswith(ext)):
                    return
                continue
            m = re.match(r"^(.+?)/\*(\.[A-Za-z0-9]+)$", entry)
            if m:
                prefix, ext = m.group(1), m.group(2)
                parent = os.path.dirname(abs_path)
                if prefix in parent and abs_path.endswith(ext):
                    return
                continue

            if abs_path.endswith(entry) or base == os.path.basename(entry):
                return
        raise ScopeViolation(path, self.allowed)
