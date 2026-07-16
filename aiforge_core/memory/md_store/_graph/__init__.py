"""md_store internals: the cross-brief graph layer — link mapping / lint /
expansion, scope re-heal, cross-scope dedupe / reconcile / contradiction
resolution and topic merging. The top layer; builds on `_base`, `_render`,
`_scope`, `_capture` and `_compact`.

Split (grouped by concern) into ``_map`` / ``_reconcile`` / ``_lint`` /
``_finalize`` submodules; this package re-exports the full former top-level
surface so ``from ._graph import <name>`` and every ``_graph.<name>`` attribute
(public AND underscore-private) is unchanged."""
from __future__ import annotations

from ._finalize import _briefs_modified_within, finalize_briefs
from ._lint import (
    _remove_facts_locked,
    cleanup_reheal,
    lint_graph,
    reheal_scopes,
)
from ._map import (
    _MAP_SYS,
    _REL_DEFAULT,
    _REL_INVERSE,
    _REL_TYPES,
    _brief_file_of_source,
    _live_briefs,
    _order_briefs_by_similarity,
    expand_links,
    map_scopes,
)
from ._reconcile import (
    _CONTRADICT_SYS,
    _RECONCILE_SYS,
    _topic_clusters,
    dedupe_global_copies,
    merge_similar_topics,
    reconcile_briefs,
    resolve_contradictions,
)

__all__ = [
    "cleanup_reheal",
    "dedupe_global_copies",
    "expand_links",
    "finalize_briefs",
    "lint_graph",
    "map_scopes",
    "merge_similar_topics",
    "reconcile_briefs",
    "reheal_scopes",
    "resolve_contradictions",
]
