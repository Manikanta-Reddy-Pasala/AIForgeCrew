"""Doer self-edit memory tool (gap-8).

Lets the Doer write an Observation_v2 mid-run instead of waiting for
the Learner to summarize at the end. Useful for "remember this
specific gotcha I just hit so the next ticket against the same repo
skips it" — a Letta-class ``core_memory_replace`` analogue.

Persisted via the same AFM ``upsert_observation`` path the Learner
uses, so dedupe + event_time + media_refs all work the same. Repo is
inferred from ``AIFORGE_REPO_ROOT`` (which adk_runner now pins per
ticket via ``ensure_branch_and_worktree``), so the fact lands on the
right project automatically.

Safety: writes are soft-fail. Any error logs + returns
``{"ok": False, "error": ...}`` — never raises into the agent loop.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("aiforge.memory_write")


def _infer_repo() -> str | None:
    """Pull the project name from ``AIFORGE_REPO_ROOT``.

    ``adk_runner._setup_ticket_workspace`` pins this to
    ``<root>/<project>/.aiforge-worktrees/<TICKET>`` so the second-
    -to-last path component is the project name (= AFM repo name).
    """
    from aiforge_core.runtime import request_context
    root = request_context.get_repo_root()
    if not root:
        return None
    parts = [p for p in root.split(os.sep) if p]
    if len(parts) >= 3 and parts[-2] == ".aiforge-worktrees":
        return parts[-3]
    # Fallback: single-repo layout where REPO_ROOT *is* the project.
    return parts[-1] if parts else None


def memory_write(
    text: str,
    kind: str = "gotcha",
    tags: list[str] | None = None,
    media_refs: list[str] | None = None,
    decision: bool = False,
    repo: str | None = None,
    source: str = "doer",
    embed_vec: "list[float] | None" = None,
) -> dict[str, Any]:
    """Persist a fact the Doer noticed during this run.

    Args:
        text: free-form one-paragraph fact. Required.
        kind: ``learning`` | ``bug`` | ``gotcha`` | ``feedback`` |
            ``note``. Defaults to ``gotcha`` because that's the
            dominant Doer-noticed pattern.
        tags: extra tags. ``"doer-self-write"`` is always added.
        media_refs: list of file paths the fact mentions (e.g.
            screenshots produced during the run).
        decision: when True, write as a ``Decision_v2`` instead of
            ``Observation_v2``. Use for "we decided to do X over Y";
            otherwise leave False.
        source: writer label recorded on the unit (SQLite ``source`` /
            Neo4j ``author``). Defaults to ``"doer"``; ingest passes
            ``"ingest"`` so chunks aren't mislabeled as Doer self-writes.

    Returns:
        ``{"ok": True, "id": str, "label": "Observation_v2" |
        "Decision_v2", "deduped": bool}`` on success;
        ``{"ok": False, "error": str}`` on any failure.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty_text"}

    repo = repo or _infer_repo()
    if not repo:
        return {"ok": False, "error": "no_repo_in_env"}

    tags = list(tags or [])
    tags.append("doer-self-write")

    # Embedded (zero-infra) path — persist the Doer's self-write to the
    # SQLite memory store instead of Neo4j/AFM.
    from aiforge_core.memory import backend_select as _bsel
    if _bsel.embedded():
        try:
            from aiforge_core.memory import sqlite_memory as _sqlmem
            rid = _sqlmem.write_unit(
                text=text, kind=("decision" if decision else kind),
                source=source, tags=tags,
                metadata={"media_refs": media_refs or []}, repo=repo,
            )
            return {"ok": True, "id": rid,
                    "label": "Decision_v2" if decision else "Observation_v2",
                    "deduped": rid == 0}
        except Exception as exc:  # noqa: BLE001
            log.warning("memory_write[sqlite] failed: %s", exc)
            return {"ok": False, "error": f"sqlite: {exc}"}

    try:
        from aiforge_memory.features.memory.store import (
            upsert_decision, upsert_observation,
        )
    except ImportError:
        try:
            from aiforge_memory.memory.store import (  # type: ignore
                upsert_decision, upsert_observation,
            )
        except ImportError:
            return {"ok": False, "error": "aiforge_memory_not_installed"}

    try:
        from neo4j import GraphDatabase
    except ImportError:
        return {"ok": False, "error": "neo4j_driver_missing"}

    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"neo4j_connect: {exc}"}

    try:
        if decision:
            title = text[:120] or "decision"
            out = upsert_decision(
                drv, repo=repo, title=title, body=text,
                rationale="doer-self-write",
                author=source,
                tags=tags,
            )
            return {"ok": True, "id": out.get("id"),
                    "label": "Decision_v2",
                    "deduped": False}
        else:
            # F5: embed so ingested chunks are vector-recallable (mirrors
            # failure_memory / learner_persist). Soft-fail: sidecar down ->
            # write without a vector rather than crash the agent loop. A caller
            # (batch ingest) may pass a PRECOMPUTED vector to skip the per-write
            # round-trip — a single-doc embed is ~2s on CPU, so batching the
            # whole file at the ingest layer is ~orders faster for a big repo.
            if embed_vec is None:
                try:
                    from aiforge_core.memory.embed import embed as _embed
                    embed_vec = _embed(text)
                except Exception:  # noqa: BLE001
                    embed_vec = None
            out = upsert_observation(
                drv, repo=repo, text=text, kind=kind,
                author=source, tags=tags,
                media_refs=media_refs or [],
                embed_vec=embed_vec,
            )
            return {"ok": True, "id": out.get("id"),
                    "label": "Observation_v2",
                    "deduped": bool(out.get("deduped"))}
    except Exception as exc:  # noqa: BLE001
        log.warning("memory_write failed: %s", exc)
        return {"ok": False, "error": f"upsert_failed: {exc}"}
    finally:
        try:
            drv.close()
        except Exception:
            pass


__all__ = ["memory_write"]
