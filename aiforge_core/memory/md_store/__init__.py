"""Markdown-file memory: human-readable notes on the filesystem that are
ALSO ingested into the searchable memory backend and shown in the UI.

Why: the app's knowledge memory (SQLite/Neo4j) is opaque — you can't `cat`
it or diff it in git. This keeps a plain ``.md`` file per memory under a
directory (``AIFORGE_MEMORY_MD_DIR``, default ``~/.aiforge/memory``) as the
human-facing source of truth, and mirrors each into the memory backend
(via the embedded SQLite store or AFM/Neo4j) so search + stats + the
Memory tab pick it up. Drop a ``.md`` file in the dir by hand and call
:func:`ingest_dir` to pull it in.

Each file carries YAML-ish frontmatter (name/kind/tags/source/created)
followed by the markdown body.

This module was split (grouped by concern) into ``_base`` / ``_render`` /
``_ingest`` / ``_scope`` / ``_capture`` / ``_compact`` / ``_graph``
submodules; this package re-exports the full former top-level surface so
``from aiforge_core.memory import md_store`` and every ``md_store.<name>``
attribute access (public AND underscore-private) is unchanged.
"""
from __future__ import annotations

from ._base import (
    _CAPTURE_SIG_RE,
    _COMPACT_LOCK,
    _FM_RE,
    _WRITE_LOCK,
    _all_md_files,
    _brief_part_paths,
    _brief_title,
    _capture_md_files,
    _find_by_source,
    _log,
    _md_path_for_stem,
    _now_iso,
    _parse,
    _resolve_md,
    _slug,
    brief_path,
    briefs_dir,
    captures_dir,
    iter_briefs,
    list_files,
    memory_dir,
    migrate_briefs_to_folder,
    migrate_captures_to_folder,
    read_file,
)
from ._capture import _CAPTURE_KINDS, capture
from ._compact import (
    _COMPACT_BODY_CAP,
    _CRYPTIC_KEY_RE,
    _SUMMARY_INPUT_CAP,
    _SUMMARY_SYS,
    _brief_parts,
    _consolidate_brief_content,
    _consolidate_brief_sections,
    _demote_headings,
    _group_key,
    _summarize_block,
    _summarize_notes,
    _topic_labels,
    _topic_split_cap,
    _union_back,
    archive_covered_captures,
    cleanup_legacy_compacted,
    compact,
    sweep_empty_briefs,
    sweep_stale_captures,
)
from ._graph import (
    _CONTRADICT_SYS,
    _MAP_SYS,
    _RECONCILE_SYS,
    _REL_DEFAULT,
    _REL_INVERSE,
    _REL_TYPES,
    _brief_file_of_source,
    _briefs_modified_within,
    _live_briefs,
    _order_briefs_by_similarity,
    _remove_facts_locked,
    _topic_clusters,
    cleanup_reheal,
    dedupe_global_copies,
    expand_links,
    finalize_briefs,
    lint_graph,
    map_scopes,
    merge_similar_topics,
    reconcile_briefs,
    reheal_scopes,
    resolve_contradictions,
    fold_kind_briefs,
    tidy_briefs,
)
from ._ingest import (
    _ingest_unit,
    append_bullet,
    delete_file,
    ingest_dir,
    upsert_section,
    write,
)
from ._render import (
    _BRIEF_CAP,
    _BRIEF_OBJECTIVE,
    _KEY_DENY,
    _KEY_PREFIX_RE,
    _LEGACY_RECENT_RE,
    _TICKET_DENY,
    _TICKET_RE,
    _brief_upsert,
    _fact_body,
    _parse_brief,
    _reconcile_dropped_index,
    _render_brief,
    brief_index,
    brief_source_stems,
    migrate_to_okr,
    seed_memory_block,
)
from ._scope import _SCOPE_SYS, _snap_topic, classify_scope

__all__ = [
    "append_bullet",
    "archive_covered_captures",
    "brief_index",
    "brief_source_stems",
    "brief_path",
    "briefs_dir",
    "capture",
    "captures_dir",
    "classify_scope",
    "cleanup_legacy_compacted",
    "cleanup_reheal",
    "compact",
    "dedupe_global_copies",
    "delete_file",
    "expand_links",
    "finalize_briefs",
    "ingest_dir",
    "iter_briefs",
    "lint_graph",
    "list_files",
    "map_scopes",
    "memory_dir",
    "merge_similar_topics",
    "fold_kind_briefs",
    "tidy_briefs",
    "migrate_briefs_to_folder",
    "migrate_captures_to_folder",
    "migrate_to_okr",
    "read_file",
    "reconcile_briefs",
    "reheal_scopes",
    "resolve_contradictions",
    "seed_memory_block",
    "sweep_empty_briefs",
    "sweep_stale_captures",
    "upsert_section",
    "write",
]
