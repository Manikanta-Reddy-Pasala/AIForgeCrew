"""OKF memory — Open Knowledge Format (OKF v0.1) bundle: Markdown nodes with
typed frontmatter (``type:`` required), Markdown-link edges, and the reserved
``index.md`` / ``log.md`` navigation + audit files.

Two public surfaces are re-exported here so callers use
``from aiforge_core.memory import okf``:
  - the NODE STORE (author/read/query the on-disk bundle), and
  - the OKF SPEC helpers (``OKF_RULES`` prompt block, ``okf_frontmatter``,
    ``append_log``, ``render_index``, ``validate_file``) from :mod:`.spec`.

See docs/OKF.md and docs/OKR_MEMORY.md.
"""
from __future__ import annotations

from .nodes import (
    NODE_TYPES,
    edges_of,
    parse_node,
    render_node,
    validate,
)
from .author import extract_and_save, migrate_from_briefs, write_session_node
from .graph import build, get_active, set_active
from .retrieve import compile_prompt, context_block, retrieve
from .store import (
    load_all,
    next_id,
    okf_root,
    read_node,
    save_node,
    type_dir,
)
from .spec import (
    OKF_RULES,
    RECOMMENDED_FIELDS,
    append_log,
    okf_frontmatter,
    render_index,
    validate_file,
)

__all__ = [
    "NODE_TYPES", "render_node", "parse_node", "edges_of", "validate",
    "okf_root", "type_dir", "next_id", "save_node", "read_node", "load_all",
    "build", "set_active", "get_active",
    "retrieve", "compile_prompt", "context_block",
    "extract_and_save", "write_session_node", "migrate_from_briefs",
    # OKF spec helpers
    "OKF_RULES", "RECOMMENDED_FIELDS", "okf_frontmatter", "append_log",
    "render_index", "validate_file",
]
