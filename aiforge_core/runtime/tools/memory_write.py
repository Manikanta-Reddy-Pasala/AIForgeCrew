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

import contextlib
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
    scope: str = "",
) -> "dict[str, Any]":
    """Public entry — delegates to the real writer, then mirrors the WRITE
    to Langfuse (env-gated, soft-fail) so memory writes are observable next
    to the recalls and LLM calls they feed."""
    res = _memory_write_impl(text=text, kind=kind, tags=tags,
                             media_refs=media_refs, decision=decision,
                             repo=repo, source=source, embed_vec=embed_vec,
                             scope=scope)
    try:
        import json as _json

        from aiforge_core.integrations import langfuse_adapter as _lf
        if _lf.enabled():
            _lf.record_generation(
                role="memory.write",
                model="decision" if decision else (kind or "note"),
                messages=[{"role": "user", "content": (text or "")[:2000]}],
                output=_json.dumps({k: res.get(k) for k in
                                    ("ok", "id", "label", "error")
                                    if k in res}),
                metadata={"path": "memory", "repo": repo or "",
                          "scope": scope or "", "source": source,
                          "tags": list(tags or [])[:8]})
    except Exception:  # noqa: BLE001 — tracing never breaks a write
        pass
    return res


def _resolve_scope(scope: str, repo: str | None) -> tuple[str | None, str | None]:
    """``(repo, error)``.

    GLOBAL memory: an explicit ``scope="global"`` writes a repo-less fact (repo
    IS NULL) that recall UNIONS into EVERY scope — so a lesson learned on one
    ticket is available across all tickets/pages/repos. Without this a repo was
    mandatory, so global memory could never be written. Any other scope keeps
    the per-context key (a Jira ticket, page, or repo).
    """
    if (scope or "").lower() in ("global", "all"):
        return None, None
    repo = repo or _infer_repo()
    return (repo, None) if repo else (None, "no_repo_in_env")


def _write_tags(tags: list | None, source: str) -> list:
    """The caller's tags plus the self-write marker and the agent attribution.

    ATTRIBUTing the write to the agent that made it keeps memory filterable by
    role. The active request-context role wins; the writer's ``source`` label is
    the fallback. One shared tag scheme across every agent's writes.
    """
    out = list(tags or [])
    out.append("doer-self-write")
    try:
        from aiforge_core.runtime import request_context as _rc
        role = _rc.get_role() or source
    except Exception:  # noqa: BLE001
        role = source
    if role:
        out.append(f"agent:{role}")
    return out


def _feed_brief(kind: str, text: str, repo: str | None, tags: list,
                source: str) -> None:
    """UNIFIED compaction feed: every durable memory write — whatever the
    backend, whatever the scope (a repo, a Jira ticket, a Confluence page, or
    global) — folds itself into the compacted brief HERE, in one place, so a new
    caller or a new scope is handled automatically with no extra wiring.

    Skips bulk ingest (chunk floods), the md-mirror path (source "md:*"), AND a
    compacted-brief RE-INGEST (source "compacted:*" / "agent:*") — otherwise
    re-ingesting a topic brief would spawn a fresh note-unit per brief (the
    "agent:compacted:compacted-X" sprawl). capture already maintains the
    topic-aware brief for a genuine write.
    """
    if source == "ingest" or source.startswith(("md:", "compacted:", "agent:")):
        return
    try:
        # Route through the OKR library (capture) so EVERY agent's write is a
        # topic-organized, TAGGED unit (carrying the agent:<role> tag) that flows
        # into the topic + repo briefs — not a bare repo-only bullet.
        # ingest=False: the backend already holds this write; capture only
        # maintains the OKR md side.
        from aiforge_core.memory import md_store
        md_store.capture(kind, text, repo=(repo or "shared"), tags=tags,
                         source=f"agent:{source}", ingest=False)
    except Exception:  # noqa: BLE001 — brief upkeep never breaks a write
        pass


def _write_sqlite(text: str, kind: str, decision: bool, source: str,
                  tags: list, media_refs, repo) -> dict[str, Any]:
    """Embedded (zero-infra) path — persist to the SQLite memory store instead
    of Neo4j/AFM."""
    try:
        from aiforge_core.memory import sqlite_memory as _sqlmem
        rid = _sqlmem.write_unit(
            text=text, kind=("decision" if decision else kind), source=source,
            tags=tags, metadata={"media_refs": media_refs or []}, repo=repo)
    except Exception as exc:  # noqa: BLE001
        log.warning("memory_write[sqlite] failed: %s", exc)
        return {"ok": False, "error": f"sqlite: {exc}"}
    _feed_brief(kind, text, repo, tags, source)
    return {"ok": True, "id": rid,
            "label": "Decision_v2" if decision else "Observation_v2",
            "deduped": rid == 0}


def _upsert_fns():
    """``(upsert_decision, upsert_observation)`` or None — the module moved in
    a AiForgeMemory refactor, so both locations are supported."""
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
            return None
    return upsert_decision, upsert_observation


def _neo4j_driver_or_error():
    """``(driver, error)`` — the driver, or the reason it is unavailable."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return None, "neo4j_driver_missing"
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD",
                        os.environ.get("NEO4J_PASSWORD", "password"))
    try:
        return GraphDatabase.driver(uri, auth=(user, pw)), None
    except Exception as exc:  # noqa: BLE001
        return None, f"neo4j_connect: {exc}"


def _embedded_vector(text: str, embed_vec):
    """F5: embed so ingested chunks are vector-recallable (mirrors
    failure_memory / learner_persist). Soft-fail: sidecar down → write without a
    vector rather than crash the agent loop. A caller (batch ingest) may pass a
    PRECOMPUTED vector to skip the per-write round-trip — a single-doc embed is
    ~2s on CPU, so batching the whole file at the ingest layer is ~orders faster
    for a big repo."""
    if embed_vec is not None:
        return embed_vec
    try:
        from aiforge_core.memory.embed import embed as _embed
        return _embed(text)
    except Exception:  # noqa: BLE001
        return None


def _write_neo4j(text: str, kind: str, decision: bool, source: str, tags: list,
                 media_refs, repo, embed_vec) -> dict[str, Any]:
    fns = _upsert_fns()
    if fns is None:
        return {"ok": False, "error": "aiforge_memory_not_installed"}
    upsert_decision, upsert_observation = fns
    drv, err = _neo4j_driver_or_error()
    if drv is None:
        return {"ok": False, "error": err}
    try:
        if decision:
            out = upsert_decision(drv, repo=repo, title=text[:120] or "decision",
                                  body=text, rationale="doer-self-write",
                                  author=source, tags=tags)
            label, deduped = "Decision_v2", False
        else:
            out = upsert_observation(
                drv, repo=repo, text=text, kind=kind, author=source, tags=tags,
                media_refs=media_refs or [],
                embed_vec=_embedded_vector(text, embed_vec))
            label, deduped = "Observation_v2", bool(out.get("deduped"))
    except Exception as exc:  # noqa: BLE001
        log.warning("memory_write failed: %s", exc)
        return {"ok": False, "error": f"upsert_failed: {exc}"}
    finally:
        with contextlib.suppress(Exception):
            drv.close()
    _feed_brief(kind, text, repo, tags, source)
    return {"ok": True, "id": out.get("id"), "label": label, "deduped": deduped}


def _memory_write_impl(
    text: str,
    kind: str = "gotcha",
    tags: list[str] | None = None,
    media_refs: list[str] | None = None,
    decision: bool = False,
    repo: str | None = None,
    source: str = "doer",
    embed_vec: "list[float] | None" = None,
    scope: str = "",
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
    repo, err = _resolve_scope(scope, repo)
    if err:
        return {"ok": False, "error": err}
    tags = _write_tags(tags, source)

    from aiforge_core.memory import backend_select as _bsel
    if _bsel.embedded():
        return _write_sqlite(text, kind, decision, source, tags, media_refs, repo)
    return _write_neo4j(text, kind, decision, source, tags, media_refs, repo,
                        embed_vec)


__all__ = ["memory_write"]
