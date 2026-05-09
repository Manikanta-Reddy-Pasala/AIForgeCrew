"""Build a structural plan from a ticket body when the operator
spelled out file paths explicitly.

The Architect prompt (see :mod:`prompts.architect`) defines a
``{tree, symbols, imports}`` JSON contract that the Doer reads from
``state['structural_plan']`` to look up canonical owners of imported
symbols. **No live Architect runs in our v6 pipeline** — Architect is
external Claude Code that drafts tickets. So when an operator writes
a mega-ticket that lists explicit paths (e.g. the Audit Trail spec
that drove ONE-1), the Doer never sees a structural plan and tends
to drift into off-spec packages (`com.pos.backend.feature.audit/*`
when the spec said `com.pos.backend.audit/*`).

This module fixes that gap: parse the ticket body for explicit file
paths, build a minimal structural plan from the matches, and inject
it into the ADK prompt as a structured block the Doer reads. The
plan we generate isn't as rich as a real Architect's (no symbol map,
no per-file import allowlist) but the file-tree alone is enough to
ground the Doer to the operator's intended package layout — that
solves issue #3 from the ONE-1 postmortem.

Heuristic, not exhaustive: we extract paths that look like source
files (.py / .java / .ts / .tsx / .js / .rs / .go / .kt) AND have at
least 3 path components (filters out one-off mentions like "see
README.md"). We don't try to parse ``import`` statements or class
declarations — that's the real Architect's job.

Output shape mirrors the Architect's JSON contract so future code can
treat operator-supplied plans and Architect-supplied plans uniformly:

    {
      "tree": ["src/main/java/...", ...],
      "symbols": {},   # empty — heuristic doesn't infer symbols
      "imports": {},   # empty — heuristic doesn't infer imports
      "source": "ticket_body_heuristic",
    }
"""
from __future__ import annotations

import re
from typing import Any


# Minimum path component count to qualify — filters one-off mentions
# of e.g. ``README.md`` or ``setup.py``. A real source path usually has
# at least ``src/foo/bar.py`` (3 components) shape.
_MIN_PATH_COMPONENTS = 3

# Source file extensions we recognize. Add more as new languages land.
_SOURCE_EXTS: frozenset[str] = frozenset({
    ".py", ".java", ".kt", ".kts", ".scala", ".groovy",
    ".ts", ".tsx", ".js", ".jsx", ".mjs",
    ".go", ".rs", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".swift", ".m", ".mm",
})

# Path regex — tolerates both backtick-wrapped (`src/foo.java`) and
# bare paths in lists. The extension alternation MUST list multi-letter
# extensions before their single-letter prefixes (``tsx`` before ``ts``,
# ``mjs`` before ``js``, ``kts`` before ``kt``, ``hpp`` before ``h``,
# ``cpp``/``cc`` before ``c``, ``mm`` before ``m``) — otherwise alternation
# is left-greedy and ``Quux.tsx`` matches as ``Quux.ts``.
_PATH_RE = re.compile(
    r"(?:`)?"
    r"(?P<path>[A-Za-z0-9_./-]+/[A-Za-z0-9_./-]+\.(?:tsx|ts|jsx|mjs|js|"
    r"kts|kt|scala|groovy|py|java|go|rs|rb|php|"
    r"hpp|h|cpp|cc|c|cs|swift|mm|m))"
    r"(?:`)?",
)


def extract_paths(body: str) -> list[str]:
    """Return a deduped, source-ordered list of likely source paths
    mentioned in ``body``. Filters by extension whitelist + minimum
    component count.

    "Source order" matters because ticket bodies usually list paths in
    dependency order (models → daos → services → controllers); the
    Doer benefits from getting them in that order.
    """
    if not body:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _PATH_RE.finditer(body):
        path = match.group("path")
        if path in seen:
            continue
        if "/" not in path:
            continue
        # Tolerate but don't require leading slash; strip if present.
        path = path.lstrip("/")
        components = path.split("/")
        if len(components) < _MIN_PATH_COMPONENTS:
            continue
        ext_idx = path.rfind(".")
        if ext_idx < 0:
            continue
        ext = path[ext_idx:].lower()
        if ext not in _SOURCE_EXTS:
            continue
        seen.add(path)
        out.append(path)
    return out


def build_plan(ticket_body: str) -> dict[str, Any] | None:
    """Build a structural plan dict from ``ticket_body`` (or None when
    the body has no recognizable source paths).

    Caller (typically :mod:`adk_runner`) injects the resulting dict
    into ADK session state under ``structural_plan`` so downstream
    agents can read it. The Doer's prompt renders a human-readable
    version of the same data so the LM also sees it directly — most
    LMs read prompt text more reliably than session-state references.

    Returns:
        dict matching the Architect JSON contract shape, or None if
        the body lacks enough explicit paths to be useful (we use a
        floor of 3 paths; below that the Doer is better off exploring
        the codebase fresh).
    """
    paths = extract_paths(ticket_body)
    if len(paths) < 3:
        return None
    return {
        "tree": paths,
        "symbols": {},
        "imports": {},
        "source": "ticket_body_heuristic",
    }


def render_for_prompt(plan: dict[str, Any]) -> str:
    """Render ``plan`` as a Markdown block for the seed prompt.

    The Doer reads BOTH session state (``state['structural_plan']``)
    AND the prompt — putting the file tree in the prompt directly
    saves a tool call and makes the layout impossible to miss. We
    keep the rendering tight (just the file tree) because the
    heuristic ``symbols``/``imports`` are always empty and would be
    noise.
    """
    if not plan or not plan.get("tree"):
        return ""
    out = (
        "\n## Canonical file tree (from ticket spec)\n"
        "Stay inside this exact tree. Do NOT write files in similar-"
        "looking but different package paths (e.g. `feature/audit/` "
        "when the tree says `audit/`). If you need a file not on this "
        "list, justify it in your turn_log.\n"
    )
    for path in plan["tree"]:
        out += f"- `{path}`\n"
    return out


__all__ = ["extract_paths", "build_plan", "render_for_prompt"]
