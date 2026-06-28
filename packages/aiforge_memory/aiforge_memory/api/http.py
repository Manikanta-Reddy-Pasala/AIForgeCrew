"""Public read helper — open driver + run bundle.query() + render.

The intended caller is UnifiedContext, which today wires 8 sources by
hand. This wrapper lets it consume codemem with a single import and a
single call.

    from aiforge_memory.api.http import context_bundle_for

    rendered = context_bundle_for("fix payment", repo="PosClientBackend")
"""
from __future__ import annotations

from aiforge_memory.query import bundle


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
    from aiforge_memory.core.neo4j import open_driver
    try:
        drv = open_driver()
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
    from aiforge_memory.core.neo4j import open_driver
    try:
        drv = open_driver()
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
