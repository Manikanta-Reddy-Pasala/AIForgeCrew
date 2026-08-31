"""Postgres/Mongo/Neo4j drivers stay OUT of the default install.

This build is SQLite-only: run.sh strips AIFORGE_NEO4J_* / AIFORGE_PG_* from
the environment on boot, and api.py lists those keys in _RUNTIME_ENV_DB_KEYS so
a stale .env cannot restore them. pymongo was declared as a core dependency and
imported by nothing at all.

These tests pin that, so a driver cannot drift back in as a transitive of
something else and quietly re-add a database to a database-free deployment.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


_PKG = _REPO / "packages" / "aiforge_memory" / "aiforge_memory"


@pytest.mark.parametrize("mod", ["pymongo", "psycopg", "psycopg2", "asyncpg"])
def test_db_driver_is_not_installed(mod):
    assert importlib.util.find_spec(mod) is None, (
        f"{mod} is installed — this build is SQLite-only. If something now "
        f"needs it, say so in pyproject rather than letting it arrive as a "
        f"transitive.")


def test_neo4j_is_not_in_the_default_install():
    """It is available as the `graph` extra, never by default."""
    assert importlib.util.find_spec("neo4j") is None


def test_the_neo4j_layer_is_gone_from_the_memory_package():
    """Not just the driver — the whole Cypher layer was deleted: core.neo4j,
    every feature store, the graph-backed commands and the CLI that wired
    them. These two modules were the entry points into it."""
    # Check the FILES, not import machinery: an emptied directory still
    # resolves as a namespace package, so find_spec would say it is still there.
    for rel in ("core/neo4j.py", "api", "features/link/store.py",
                "features/memory/store", "query/bundle/_builder.py"):
        assert not (_PKG / rel).exists(), f"{rel} is back in the package"


def test_the_memory_package_still_imports_cleanly():
    """What survives is the library the app actually uses (chunk/embed for the
    chonkie path, the extractors). Every module must still import — a package
    left half-deleted is worse than either state."""
    import pkgutil

    import aiforge_memory
    broken = []
    for m in pkgutil.walk_packages(aiforge_memory.__path__, "aiforge_memory."):
        try:
            importlib.import_module(m.name)
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{m.name}: {type(exc).__name__}: {exc}")
    assert not broken, "surviving modules fail to import:\n" + "\n".join(broken)


def test_the_chonkie_path_the_app_uses_survived():
    """features/chunk is the one part aiforge_core still reaches (via the
    chonkie text adapter), so the deletion had to stop short of it."""
    from aiforge_memory.features.chunk import chonkie_adapter, embed  # noqa: F401


def test_core_no_longer_imports_the_memory_graph_package():
    """The Neo4j-backed sources were removed outright, so aiforge_core reaches
    into aiforge_memory nowhere at all — previously it did in four places, all
    of them dead (one imported a function that no longer existed)."""
    out = subprocess.run(
        ["grep", "-rn", "aiforge_memory", "--include=*.py",
         "aiforge_core", "scripts"],
        cwd=str(_REPO), capture_output=True, text=True)
    code = [ln for ln in out.stdout.splitlines()
            if re.search(r"^\S+:\d+:\s*(from|import)\s", ln)]
    assert not code, "aiforge_core imports aiforge_memory again:\n" + "\n".join(code)


def test_removed_recall_sources_are_gone():
    """afm_bundle and xrepo were the two Neo4j-backed recall sources."""
    from aiforge_core.memory.unified_query import _helpers, _sources
    for name in ("_afm_bundle", "_cross_repo_links", "_bundle_object"):
        assert not hasattr(_sources, name), f"{name} came back"
    for w in ("afm_bundle", "xrepo"):
        assert w not in _helpers._DEFAULT_WEIGHTS, f"{w} weight came back"


def test_no_source_file_imports_a_removed_driver():
    """Belt and braces: if someone adds `import pymongo` back, the dependency
    audit that justified removing it stops being true."""
    out = subprocess.run(
        ["grep", "-rEn", r"^\s*(import|from)\s+(pymongo|psycopg2?|asyncpg)\b",
         "aiforge_core", "scripts", "packages"],
        cwd=str(_REPO), capture_output=True, text=True)
    hits = [ln for ln in out.stdout.splitlines() if ln.strip()]
    assert not hits, "a removed driver is imported again:\n" + "\n".join(hits)


def test_pyproject_does_not_redeclare_the_removed_drivers():
    txt = (_REPO / "pyproject.toml").read_text()
    deps = txt.split("[project.optional-dependencies]")[0]
    for pkg in ("pymongo", "psycopg", "asyncpg", "neo4j"):
        assert not re.search(rf'^\s*"{pkg}[<>=~\[]', deps, re.M), \
            f"{pkg} is back in the core dependency list"
