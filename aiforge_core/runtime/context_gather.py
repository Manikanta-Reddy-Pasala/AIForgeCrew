"""Cross-entity context dossier — parallel gather, cache, timestamp refresh.

Asking about a Jira ticket (or a Confluence page) usually means you also want
what it LINKS to: a ticket's Confluence pages, a page's referenced tickets, and
every image on either. Those fetches are independent single-context reads, so we
fan them out in PARALLEL (a thread pool — I/O bound, no model needed), save each
piece as its own md file INSIDE the context folder (work/<kind>/<key>/), and
merge them into one ``dossier.md``.

It's cached: a re-request reads the stored dossier + its saved ``updated``
timestamp and only re-fetches when the live entity is newer — so repeat questions
about the same ticket are instant, and stay correct when something changed.

    work/jira/PROJ-42/
      dossier.md              ← merged, human-readable
      .dossier.json           ← {updated, links, gathered_at} for the cache
      ticket.md               ← the issue + comments + time
      confluence-<id>.md      ← each linked Confluence page
      attachments/…           ← images/docs (saved by the read tools)
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,20}-\d+)\b")
_CONF_ID_RE = re.compile(r"(?:/pages/|pageId=)(\d{4,})")
_MAX_LINKS = 12
_MAX_WORKERS = 6


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "")).strip("_.") or "x"


def _write(path: str, text: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text or "")
    except OSError:
        pass


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# ── primary-entity read + "updated" probe (cheap freshness check) ──

def _jira(args):
    from aiforge_core.runtime.tools import jira
    return jira, args


def _primary_updated(kind: str, key: str) -> str | None:
    """The live 'last updated' stamp of the primary entity — one light call so
    the cache can decide whether to re-fetch. None when unavailable."""
    import urllib.parse as _up
    qkey = _up.quote(str(key), safe="")
    try:
        if kind == "jira":
            from aiforge_core.runtime.tools import jira
            r = jira._request("GET", f"/rest/api/2/issue/{qkey}",
                              params={"fields": "updated"})
            if r.get("ok"):
                return (((r["data"] or {}).get("fields") or {}).get("updated"))
        elif kind == "confluence":
            from aiforge_core.runtime.tools import confluence
            r = confluence._request("GET", f"/rest/api/content/{qkey}",
                                    params={"expand": "version"})
            if r.get("ok"):
                return (((r["data"] or {}).get("version") or {}).get("when"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _read_jira(key: str, role: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_read({"key": key}, None)


def _read_confluence(pid: str, role: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_read({"id": pid}, None)


def _jira_links(key: str) -> list[dict]:
    from aiforge_core.runtime.tools import jira
    r = jira.jira_remote_links({"key": key}, None)
    return r.get("links") or [] if r.get("ok") else []


def _detect_confluence_ids(primary: dict, links: list[dict]) -> list[str]:
    ids: list[str] = []
    for lk in links:
        if lk.get("confluence_page_id"):
            ids.append(str(lk["confluence_page_id"]))
    # …and any /pages/<id> URL in the description/comments.
    blob = (primary.get("description") or "") + " " + " ".join(
        c.get("body", "") for c in (primary.get("comments") or []))
    ids += _CONF_ID_RE.findall(blob)
    # dedupe, preserve order, cap.
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out[:_MAX_LINKS]


def _detect_jira_keys(primary: dict, self_key: str) -> list[str]:
    blob = (primary.get("body") or "") + " " + (primary.get("title") or "")
    keys, seen = [], {self_key}
    for k in _JIRA_KEY_RE.findall(blob):
        if k not in seen:
            seen.add(k)
            keys.append(k)
    return keys[:_MAX_LINKS]


# "Acceptance criteria" block in a ticket description — jira wiki (h3.),
# markdown (###/**bold**) or a plain "Acceptance criteria:" label, followed by
# its lines up to the next blank line / heading. Best-effort, capped.
_AC_RE = re.compile(
    r"(?:^|\n)\s*(?:h[1-6]\.\s*|#{1,6}\s*|\*\*)?acceptance criteria\*{0,2}:?"
    r"\s*\n(.*?)(?=\n\s*\n|\n\s*(?:h[1-6]\.|#{1,6}\s)|\Z)",
    re.IGNORECASE | re.DOTALL)


def _acceptance_criteria(desc: str) -> list[str]:
    m = _AC_RE.search(desc or "")
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        item = re.sub(r"^[\s*#\-\d.)]+", "", ln).strip()
        if item:
            out.append(item)
    return out[:12]


def _note_for(kind: str, key: str, primary: dict,
              links: list[dict] | None = None) -> str:
    """Wrap a primary entity in the STANDARD managed-note envelope
    (work_notes.render_note): OKR sections up top for the curator/agent, the
    full legacy dossier text preserved as the body. Cross-entity references
    are emitted as wiki refs so notes link to each other, not to a base URL."""
    from aiforge_core.runtime import work_notes
    if kind == "jira":
        note_links = [primary.get("url") or ""]
        note_links += [lk.get("url") or "" for lk in (links or [])]
        note_links += [f"[[confluence/{cid}]]"
                       for cid in _detect_confluence_ids(primary, links or [])]
        return work_notes.render_note(
            "jira", key,
            title=f"{key} — {primary.get('summary') or ''}".strip(" —"),
            source_url=primary.get("url") or "",
            objective=primary.get("summary") or "",
            key_results=_acceptance_criteria(primary.get("description") or ""),
            facts=[f"{f}: {primary.get(f)}" for f
                   in ("status", "type", "assignee", "priority")
                   if primary.get(f)],
            links=note_links,
            body_md=_md_for(kind, primary))
    # confluence — link back to every ticket the page references.
    note_links = [primary.get("url") or ""]
    note_links += [f"[[jira/{jk}]]" for jk in _detect_jira_keys(primary, key)]
    return work_notes.render_note(
        "confluence", key,
        title=primary.get("title") or key,
        source_url=primary.get("url") or "",
        objective=primary.get("title") or "",
        facts=[f"{f}: {primary.get(f)}" for f in ("space", "version")
               if primary.get(f)],
        links=note_links,
        body_md=_md_for(kind, primary))


def _md_for(kind: str, ent: dict) -> str:
    if kind == "jira":
        lines = [f"# {ent.get('key','')} — {ent.get('summary','')}",
                 f"- status: {ent.get('status')}  type: {ent.get('type')}  "
                 f"assignee: {ent.get('assignee')}",
                 f"- url: {ent.get('url','')}", "",
                 (ent.get("description") or "")]
        for c in (ent.get("comments") or []):
            lines.append(f"\n> {c.get('author')}: {c.get('body','')[:1000]}")
        for a in (ent.get("attachments") or []):
            d = a.get("description") or a.get("error") or ""
            lines.append(f"\n[image/doc] {a.get('filename')} — {d}")
        return "\n".join(lines)
    # confluence
    lines = [f"# {ent.get('title','')}", f"- url: {ent.get('url','')}", "",
             (ent.get("body") or "")[:8000]]
    for a in (ent.get("attachments") or []):
        d = a.get("description") or a.get("error") or ""
        lines.append(f"\n[image/doc] {a.get('filename')} — {d}")
    return "\n".join(lines)


def gather(kind: str, key: str, *, force: bool = False,
           role: str = "chat") -> dict:
    """Assemble (and cache) the cross-entity dossier for ``(kind, key)``.

    Returns ``{ok, cached, refreshed, kind, key, dir, dossier, artifacts}``.
    Never raises — an unconfigured Jira/Confluence just yields a thin dossier.
    """
    from aiforge_core.runtime import work_context as wc
    if kind not in ("jira", "confluence"):
        return {"ok": False, "error": f"unsupported kind {kind!r}"}
    base = wc.context_dir(kind, key)
    dossier_md = os.path.join(base, "dossier.md")
    meta_path = os.path.join(base, ".dossier.json")

    # ── Cache + freshness: reuse the stored dossier unless the entity changed.
    meta = _load_json(meta_path)
    live_updated = _primary_updated(kind, key)
    # Serve the cached dossier unless we can PROVE it's stale: reuse it when the
    # entity is unchanged OR when the freshness probe was inconclusive
    # (live_updated is None — offline / not configured), rather than throwing a
    # good cache away and erroring on the re-fetch.
    if (not force) and meta and os.path.exists(dossier_md) \
            and (live_updated is None or meta.get("updated") == live_updated):
        try:
            with open(dossier_md, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            body = ""
        return {"ok": True, "cached": True, "refreshed": False,
                "kind": kind, "key": key, "dir": base, "dossier": body,
                "artifacts": meta.get("artifacts", [])}

    # ── Fresh gather. Phase 1: primary read (+ its links, in parallel).
    artifacts: list[str] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        if kind == "jira":
            f_primary = ex.submit(_read_jira, key, role)
            f_links = ex.submit(_jira_links, key)
            primary = f_primary.result()
            links = f_links.result()
        else:
            primary = _read_confluence(key, role)
            links = []

    if not primary.get("ok"):
        # e.g. jira_not_configured — record nothing, surface the reason.
        return {"ok": False, "error": primary.get("error", "read_failed"),
                "kind": kind, "key": key, "dir": base}

    _write(os.path.join(base, "ticket.md" if kind == "jira" else "page.md"),
           _note_for(kind, key, primary, links))
    artifacts.append("ticket.md" if kind == "jira" else "page.md")

    # ── Phase 2: fan out the cross-linked entities IN PARALLEL, then merge.
    secondaries: list[tuple[str, dict]] = []
    partial = False   # a linked fetch failed → don't stamp the cache as complete
    if kind == "jira":
        conf_ids = _detect_confluence_ids(primary, links)
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futs = {ex.submit(_read_confluence, cid, role): cid
                    for cid in conf_ids}
            for fut in as_completed(futs):
                cid = futs[fut]
                try:
                    ent = fut.result()
                except Exception:  # noqa: BLE001
                    partial = True
                    continue
                if ent.get("ok"):
                    secondaries.append(("confluence", ent))
                    fn = f"confluence-{_slug(cid)}.md"
                    _write(os.path.join(base, fn), _md_for("confluence", ent))
                    artifacts.append(fn)
                else:
                    partial = True
    else:
        jkeys = _detect_jira_keys(primary, key)
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futs = {ex.submit(_read_jira, jk, role): jk for jk in jkeys}
            for fut in as_completed(futs):
                jk = futs[fut]
                try:
                    ent = fut.result()
                except Exception:  # noqa: BLE001
                    partial = True
                    continue
                if ent.get("ok"):
                    secondaries.append(("jira", ent))
                    fn = f"jira-{_slug(jk)}.md"
                    _write(os.path.join(base, fn), _md_for("jira", ent))
                    artifacts.append(fn)
                else:
                    partial = True

    # ── Merge into one dossier + stamp the cache. The merged text is the
    # BODY of a standard managed note (same envelope as ticket.md/page.md) so
    # the dossier carries machine-readable identity + links up top.
    parts = [f"_gathered {len(secondaries)} linked item(s)_", "",
             _md_for(kind, primary)]
    for skind, ent in secondaries:
        parts.append("\n\n---\n")
        parts.append(f"## Linked {skind}\n")
        parts.append(_md_for(skind, ent))
    from aiforge_core.runtime import work_notes
    dossier = work_notes.render_note(
        kind, key, title=f"Dossier — {kind}:{key}",
        source_url=primary.get("url") or "",
        objective=primary.get("summary") or primary.get("title") or "",
        facts=[f"linked items: {len(secondaries)}"],
        links=([primary.get("url") or ""]
               + [f"[[{sk}/{ent.get('key') or ent.get('id')}]]"
                  for sk, ent in secondaries
                  if ent.get("key") or ent.get("id")]),
        body_md="\n".join(parts))
    _write(dossier_md, dossier)
    # Stamp the cache with the live `updated` ONLY on a complete gather. If a
    # linked fetch failed, leave `updated` null so the next request re-gathers
    # (the incomplete dossier is still written + usable now, just not cached as
    # authoritative).
    _write(meta_path, json.dumps({
        "updated": None if partial else live_updated,
        "artifacts": artifacts, "links": len(secondaries), "partial": partial,
    }))

    # ── Memory via md_store.capture: writes a per-context md note (repo=key)
    # AND mirrors to the DB — so it flows through the SAME repo-axis compaction /
    # write-time brief as every other memory (compacted-<key>.md), instead of a
    # DB-only row. Recalled next time under this ticket/page key.
    try:
        from aiforge_core.memory import md_store
        md_store.capture(
            "project_learning",
            f"DOSSIER {kind}:{key} — {primary.get('summary') or primary.get('title') or ''}"
            f" (+{len(secondaries)} linked item(s); files in {base}).",
            repo=key, topic=f"{kind}-dossier")
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, "cached": False, "refreshed": bool(meta),
            "kind": kind, "key": key, "dir": base, "dossier": dossier,
            "artifacts": artifacts, "linked": len(secondaries)}


__all__ = ["gather"]
