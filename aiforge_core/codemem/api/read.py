"""Public read helper — open driver + run bundle.query() + render.

The intended caller is UnifiedContext, which today wires 8 sources by
hand. This wrapper lets it consume codemem with a single import and a
single call.

    from aiforge_core.codemem.api.read import context_bundle_for

    rendered = context_bundle_for("fix payment", repo="PosClientBackend")
"""
from __future__ import annotations

import os

from aiforge_core.codemem.query import bundle


def context_bundle_for(
    text: str,
    *,
    repo: str,
    role: str = "doer",
    token_budget: int = 4000,
) -> str:
    """Best-effort: any backend failure returns ''.

    Caller (UnifiedContext) decides whether to fall back to its
    legacy 8-source aggregation when this returns ''.
    """
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return ""
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception:
        return ""
    try:
        b = bundle.query(text, repo=repo, driver=drv,
                         role=role, token_budget=token_budget)
        return b.render()
    except Exception:
        return ""
    finally:
        try:
            drv.close()
        except Exception:
            pass


def context_bundle_object(
    text: str,
    *,
    repo: str,
    role: str = "doer",
    token_budget: int = 4000,
) -> bundle.ContextBundle | None:
    """Same as context_bundle_for but returns the structured bundle."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return None
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception:
        return None
    try:
        return bundle.query(text, repo=repo, driver=drv,
                            role=role, token_budget=token_budget)
    except Exception:
        return None
    finally:
        try:
            drv.close()
        except Exception:
            pass
