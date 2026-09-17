"""Archiving the originals, restoring them, and applying a merge."""
from __future__ import annotations

import datetime as _dt
import shutil
from pathlib import Path

from aiforge_core.config.paths import config_dir

from ._artifact_cluster import (
    _Item,
)


def _pkg():
    """``artifact_merge``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``artifact_merge``; patch any other
    name on this module."""
    import aiforge_core.runtime.artifact_merge as package
    return package


# ── archive + apply ─────────────────────────────────────────────────────────

def archive_dir(kind: str) -> Path:
    return Path(str(config_dir())) / _pkg()._ARCHIVE_DIR / kind


def archive(item: _Item) -> str:
    """Copy an artifact's file aside before it is removed. Returns the archive
    path, or "" when there was nothing on disk to keep."""
    src = Path(item.source)
    if not src.is_file():
        return ""
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")
    slug = src.stem if src.name.lower() not in ("skill.md", "workflow.md") \
        else src.parent.name
    dest = archive_dir(item.kind) / f"{slug}-{stamp}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return str(dest)


def _provenance(kind: str, merged_body: str, cluster: list[_Item]) -> str:
    names = ", ".join(sorted(i.name for i in cluster))
    return (merged_body.rstrip() + "\n\n<!-- merged from " + kind + ": "
            + names + " -->\n")


def _merged_extra(cluster: list[_Item]) -> tuple[tuple, ...]:
    """The kind-specific metadata the merged artifact inherits.

    For rules that is SCOPE, and it has to be the union: the merged rule must
    apply wherever any member applied, or the merge silently narrows what the
    agent is told. ``alwaysApply`` wins if ANY member had it (an always-on rule
    folded into a glob-scoped one would otherwise stop firing), and the globs
    are unioned for the same reason in the other direction — an empty glob list
    on an alwaysApply=False rule means it applies NOWHERE.
    """
    extras = [dict(i.extra or ()) for i in cluster if i.extra]
    if not extras:
        # The SAME shape, empty — a kind that carries no extra metadata (skills,
        # workflows) reads the pair list and finds nothing, rather than reading
        # a different type on alternate calls.
        return (("globs", ()), ("always", False))
    globs: list[str] = []
    for e in extras:
        for g in e.get("globs") or ():
            if g not in globs:
                globs.append(g)
    return (("globs", tuple(globs)),
            ("always", any(bool(e.get("always")) for e in extras)))


def _restore(pairs: list[tuple[str, str]]) -> int:
    """Copy archived members back to where they came from. The sweep deletes
    BEFORE it writes (a merged artifact often reuses a member's name, and the
    writer would otherwise be undone by the delete), which means a failed write
    is the one moment the library is missing them. Restoring is what keeps that
    window from being data loss."""
    back = 0
    for src, dest in pairs:
        if not dest or not src:
            continue
        try:
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, src)
            back += 1
        except OSError:
            # exception(), not error(): a restore that failed is the one place
            # in this module where the traceback is the whole story — the file
            # is in the archive and NOT back where it belongs.
            _pkg().log.exception("artifact_merge: could not restore %s from %s",
                          src, dest)
    return back


def _apply(kind: str, merged, cluster: list[_Item]) -> dict:
    """Archive every member, remove them, then write the merged artifact.

    Order matters: a member whose name the merged artifact reuses must be gone
    (and archived) before the write, or the writer's overwrite would race the
    delete and take the merged file back out. A write that then fails rolls the
    members back from the archive.
    """
    pkg = _pkg()
    pairs = [(i.source, archive(i)) for i in cluster]
    archived = [dest for _src, dest in pairs if dest]
    for item in cluster:
        try:
            pkg._delete(item.kind, item.name)
        except Exception as exc:  # noqa: BLE001 — one bad unlink is not the run
            pkg.log.warning("artifact_merge: delete %s/%s failed: %s",
                        item.kind, item.name, exc)
    item = _Item(kind, merged.name.strip(),
                 (getattr(merged, "description", "") or "").strip(),
                 tuple(t.strip().lower()
                       for t in (getattr(merged, "triggers", []) or []) if t),
                 _provenance(kind, merged.body, cluster), "",
                 _merged_extra(cluster))
    links = [f"{kind[:-1]}:{i.name}" for i in cluster]
    try:
        res = pkg._WRITERS[kind](item, links)
    except Exception as exc:  # noqa: BLE001 — a writer raising is still a loss
        res = {"ok": False, "error": str(exc)[:200]}
    if not res.get("ok"):
        restored = _restore(pairs)
        pkg.log.error("artifact_merge: %s write failed (%s) — restored %d member(s)",
                  kind, res.get("error"), restored)
        return {"ok": False, "path": "", "error": res.get("error", "write failed"),
                "archived": archived, "restored": restored}
    return {"ok": True, "path": res.get("path", ""), "error": "",
            "archived": archived}
