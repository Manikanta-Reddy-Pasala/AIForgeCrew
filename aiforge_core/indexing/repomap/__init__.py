"""Ranked repo map (tree-sitter tags + PageRank), vendored from aider-chat.

See ``_vendor_repomap`` for why this lives in-tree rather than as a
dependency. Import ``RepoMap`` from here; nothing else should reach into
the vendored modules.
"""
from ._vendor_repomap import RepoMap, Tag, find_src_files, get_scm_fname

__all__ = ["RepoMap", "Tag", "find_src_files", "get_scm_fname"]
