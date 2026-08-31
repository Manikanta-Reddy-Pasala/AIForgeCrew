"""Postgres/Mongo/Neo4j drivers stay OUT of the default install.

This build is SQLite-only: run.sh strips AIFORGE_NEO4J_* / AIFORGE_PG_* from
the environment on boot, and api.py lists those keys in _RUNTIME_ENV_DB_KEYS so
a stale .env cannot restore them. pymongo was declared as a core dependency and
imported by nothing at all.

These tests pin that, so a driver cannot drift back in as a transitive of
something else and quietly re-add a database to a database-free deployment.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mod", ["pymongo", "psycopg", "psycopg2", "asyncpg"])
def test_db_driver_is_not_installed(mod):
    assert importlib.util.find_spec(mod) is None, (
        f"{mod} is installed — this build is SQLite-only. If something now "
        f"needs it, say so in pyproject rather than letting it arrive as a "
        f"transitive.")


def test_neo4j_is_not_in_the_default_install():
    """It is available as the `graph` extra, never by default."""
    assert importlib.util.find_spec("neo4j") is None


def test_neo4j_schema_module_still_imports_without_the_driver():
    """Dropping the driver must not break the package: every
    `from neo4j import GraphDatabase` is inside a function, and the Cypher
    schema is plain strings."""
    from aiforge_memory.core import neo4j as schema
    assert hasattr(schema, "open_driver")


def test_open_driver_raises_rather_than_silently_misbehaving():
    from aiforge_memory.core import neo4j as schema
    with pytest.raises(ImportError):
        schema.open_driver()


def test_cross_repo_links_degrades_to_empty_without_a_driver():
    """The one live caller. It must return [], not raise — the same thing it
    did before the driver was dropped, since there was never a server here."""
    from aiforge_core.memory.unified_query._sources import _cross_repo_links
    assert _cross_repo_links("anything", repo="AIForgeCrew") == []


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
