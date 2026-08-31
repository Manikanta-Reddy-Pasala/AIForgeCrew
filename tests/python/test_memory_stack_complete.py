"""Guardrail: the memory stack's deps must stay DECLARED + importable so they
can't silently vanish on `uv sync` again (the tree-sitter/croniter class of
bug). If this fails, a memory bit was dropped from pyproject."""
from __future__ import annotations

import pathlib
import tomllib


def _deps() -> list[str]:
    pp = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
    return tomllib.loads(pp.read_text())["project"]["dependencies"]


def test_tree_sitter_deps_declared():
    joined = " ".join(_deps())
    for pkg in ("tree-sitter", "tree-sitter-java", "tree-sitter-python"):
        assert pkg in joined, f"{pkg} missing from pyproject dependencies"


def test_multilang_symbol_deps_declared():
    # The generic tag-query engine behind _parse_via_tags (kotlin/python/react/
    # c/cpp symbol ingest). Must stay declared so `uv sync` can't prune them.
    joined = " ".join(_deps())
    for pkg in ("grep-ast", "tree-sitter-language-pack", "networkx",
                "diskcache", "pygments"):
        assert pkg in joined, f"{pkg} missing from pyproject dependencies"


def test_aider_chat_is_not_a_dependency():
    """The RepoMap is vendored (aiforge_core/indexing/repomap/); the package is
    not a dep. Re-adding it drags 107 transitive packages and its exact pins on
    litellm/urllib3/pillow/requests, which is what the vendoring undid."""
    assert "aider-chat" not in " ".join(_deps())


def test_core_memory_deps_declared():
    joined = " ".join(_deps())
    for pkg in ("aiforge-memory", "croniter", "numpy"):
        assert pkg in joined, f"{pkg} missing from pyproject dependencies"


def test_tree_sitter_importable_and_api_compatible():
    import tree_sitter_java as j
    import tree_sitter_python as p
    from tree_sitter import Language
    # single-arg Language(capsule) is the >=0.22 API the RepoMap uses
    Language(j.language())
    Language(p.language())
