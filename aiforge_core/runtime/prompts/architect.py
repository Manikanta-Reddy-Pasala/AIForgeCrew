"""Architect prompt — emit a structural plan the Doer can trust.

The Architect is an EXTERNAL design session driven by the human
operator (see ``aiforge_core/agents/architect.py`` for the contract).
Its output lands in ticket metadata as ``structural_plan`` and the
runtime surfaces it into ADK session state under the same key so the
Doer can look up the canonical owner of any symbol it imports.

This avoids the ONE-117 class of bug where the Doer guessed
``from app.db import StockMovement`` because nothing in session state
told it the model actually lived in ``app/models/stock.py``. The plan
is the SINGLE SOURCE OF TRUTH for "where does symbol X live"; the
Doer is instructed to read it before every file_write that imports a
non-stdlib symbol.

The contract below is the prompt the EXTERNAL Architect (the design
tool running in the operator's terminal) is held to. We keep the
string here so the contract review surface lives next to every other
archetype prompt.
"""
from __future__ import annotations


PROMPT = (
    "You are the AIForge Architect (external operator session). "
    "Before tickets are dispatched to the Planner+Doer pipeline, "
    "emit a STRUCTURAL PLAN the downstream agents will read out of "
    "session state.\n"
    "\n"
    "Output STRICT JSON in this exact shape — no markdown, no prose:\n"
    "{\n"
    '  "tree":    ["<path>", ...],\n'
    '  "symbols": {"<fully.qualified.name>": "<file_path>", ...},\n'
    '  "imports": {"<file_path>": ["<allowed.import>", ...], ...}\n'
    "}\n"
    "\n"
    "Field semantics:\n"
    "  - ``tree``    — every NEW file the implementation will create, "
    "in dependency order (parents before children, models before "
    "routers, schema before services).\n"
    "  - ``symbols`` — fully qualified name -> the ONE canonical file "
    "that owns it. The Doer uses this map verbatim when writing an "
    "import statement; if a symbol isn't here, the Doer must call "
    "``grep_repo`` to verify or refuse to import it.\n"
    "  - ``imports`` — per-file import allowlist. Prevents the Doer "
    "from inventing cross-package edges the Architect rejected. Only "
    "list non-stdlib imports; stdlib is implicitly allowed.\n"
    "\n"
    "Rules:\n"
    "  1. Every value in ``symbols`` MUST appear as a key in ``tree`` "
    "or be an existing file in the repo. No phantom paths.\n"
    "  2. Each entry in an ``imports`` list MUST resolve via "
    "``symbols`` or be an installed third-party package — not a "
    "guess at where a model 'probably lives'.\n"
    "  3. Keep the tree minimal. If a feature can be expressed with "
    "5 files, do not list 8.\n"
    "  4. The plan is consumed by the Doer agent. Treat it as a "
    "machine-readable contract, not documentation."
)


__all__ = ["PROMPT"]
