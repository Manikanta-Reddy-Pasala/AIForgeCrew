"""Session nodes and solution records."""
from __future__ import annotations

from . import graph as _graph
from . import store as _store
from ._author_okr import (
    _dedup_key,
)


def write_session_node(*, title: str, body: str,
                       linked_krs: list[str] | None = None,
                       repo: str | None = None) -> dict:
    """Write a ``session`` node (chronological log) from a run's steps, linked to
    the KRs it advanced (defaults to the active KR). A ``repo`` scopes the
    session into ``projects/<repo>/`` (it's that repo's activity). Soft-fail."""
    krs = list(linked_krs or [])
    if not krs:
        act = _graph.get_active()
        if act:
            krs = [act]
    import datetime as _dt
    meta = {"date": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d"),
            "linked_krs": krs, "title": title}
    if repo:
        meta["workspace"] = repo
    return _store.save_node("session", None, meta, body)


def _existing_solution(ticket: str, kind: str, norm: str) -> dict | None:
    """A solution node already recorded for this work.

    DEDUP: never write a second solution node for the same fix. Matches on
    (ticket + kind) or a normalized summary already recorded — so re-runs of
    the learner on the same work don't pile up duplicate S-NN nodes.
    """
    for d in _store.load_all():
        if d.get("type") != "solution":
            continue
        m = d.get("meta") or {}
        by_ticket = ticket and m.get("ticket") == ticket and m.get("kind") == kind
        by_summary = norm and _dedup_key(
            m.get("description") or m.get("title") or "") == norm
        if by_ticket or by_summary:
            return d
    return None


def _clean_list(values, limit: int | None = None) -> list[str]:
    out = [str(v).strip() for v in (values or []) if str(v).strip()]
    return out[:limit] if limit else out


def _solution_meta(*, kind: str, title: str, summary: str, workspace: str,
                   topic: str, tables, services, files, about, ticket: str,
                   date: str) -> dict:
    meta: dict = {"kind": kind, "title": title,
                  "description": (summary or "").strip()[:200]}
    if workspace:
        meta["workspace"] = workspace
        meta["resource"] = f"repo:{workspace}"     # OKF `resource` URI
    for key, value in (("topic", topic), ("ticket", ticket),
                       ("timestamp", date)):
        if value:
            meta[key] = value
    for key, values, limit in (("tables", tables, None),
                               ("services", services, None),
                               ("files", files, 20)):
        cleaned = _clean_list(values, limit)
        if cleaned:
            meta[key] = cleaned
    # about → OKF links (the symbols/paths/tickets this solution relates to)
    meta["about"] = list(about or [])
    return meta


def _append_solution_log(*, kind: str, title: str, node_id, workspace: str,
                         tables, services, date: str) -> None:
    """The dated audit trail (reserved OKF log.md, newest-first)."""
    try:
        import os as _os

        from aiforge_core.memory import okf
        extra = []
        if workspace:
            extra.append(f"workspace:{workspace}")
        if tables:
            extra.append(f"tables:{','.join(tables[:6])}")
        if services:
            extra.append(f"services:{','.join(services[:6])}")
        entry = (f"[{kind}] {title}"
                 + (f" ({'; '.join(extra)})" if extra else "")
                 + f" · {node_id}")
        okf.append_log(_os.path.join(_store.okf_root(), "log.md"),
                       entry, date=date)
    except Exception:  # noqa: BLE001 — log is best-effort
        pass


def record_solution(*, kind: str, summary: str, workspace: str = "",
                    topic: str = "", tables: "list[str] | None" = None,
                    services: "list[str] | None" = None,
                    files: "list[str] | None" = None,
                    about: "list[str] | None" = None,
                    ticket: str = "", body: str = "", date: str = "") -> dict:
    """Record ONE completed feature or bug fix as an OKF ``solution`` node AND a
    dated ``log.md`` entry — so the OKR bundle is a queryable changelog of what
    was solved, mapped to the workspace/repo it touched, the topic, and the DB
    tables + connected services involved.

    ``kind`` is 'feature' or 'fix'. ``date`` (ISO YYYY-MM-DD) is passed in by the
    caller (no clock here — keeps it reproducible/testable). Soft-fail: never
    raises into the persistence path."""
    try:
        kind = "fix" if str(kind).lower().startswith(("fix", "bug")) else "feature"
        title = (summary or "").strip().split("\n", 1)[0][:90] or f"{kind}"
        dup = _existing_solution(ticket, kind, _dedup_key(summary))
        if dup is not None:
            return {"ok": True, "id": dup.get("id"), "path": dup.get("path"),
                    "deduped": True}
        meta = _solution_meta(kind=kind, title=title, summary=summary,
                              workspace=workspace, topic=topic, tables=tables,
                              services=services, files=files, about=about,
                              ticket=ticket, date=date)
        r = _store.save_node("solution", None, meta,
                             body or (summary or "").strip())
        if date and r.get("ok"):
            _append_solution_log(kind=kind, title=title, node_id=r.get("id"),
                                 workspace=workspace, tables=tables,
                                 services=services, date=date)
        return r
    except Exception as exc:  # noqa: BLE001 — never break the learner path
        return {"ok": False, "error": str(exc)}
