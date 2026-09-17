"""Merging similar topics: which topics are the same subject, clustering them,
and folding their briefs into one."""
from __future__ import annotations

import os
import re

from .._base import (
    _CAPTURE_SIG_RE,
    _WRITE_LOCK,
    _log,
    _slug,
    brief_path,
    iter_briefs,
)
from .._render import _fact_body, _parse_brief, _reconcile_dropped_index, _render_brief
from ._reconcile_names import (
    _common_token_prefix,
    _merge_families,
    _shared_prefix,
    _topic_merge_ratio,
    _typo_sibling,
)


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.md_store._graph._reconcile as package
    return package


def _same_subject(a: str, b: str, cache: dict) -> bool:
    """True when two topic slugs cover the SAME subject by content similarity.

    Complements the lexical rules: those catch spelling families
    (``gpsd``/``gpsd-config``), this catches synonym families
    (``calc``/``calculator``/``math-expression-engine``) that share no
    characters. Cutoff is deliberately high (env
    ``AIFORGE_TOPIC_MERGE_COSINE``, default 0.86) — a wrong merge silently
    fuses two subjects, which is worse than leaving two files. Returns False
    whenever embedding is unavailable, so the lexical behaviour is unchanged
    on a box with no embed backend.
    """
    import os
    try:
        cut = float(os.environ.get("AIFORGE_TOPIC_MERGE_COSINE", "0.86"))
    except (TypeError, ValueError):
        cut = 0.86
    if cut > 1:                       # operator disabled it
        return False
    try:
        from ... import local_embed
        from .. import _topics
        if not _topics.semantic_ready():
            return False
        for k in (a, b):
            if k not in cache:
                cache[k] = _topics._vec(_topics._topic_text(k))
        va, vb = cache.get(a), cache.get(b)
        if va is None or vb is None:
            return False
        return float(local_embed.cosine(va, vb)) >= cut
    except Exception:  # noqa: BLE001
        return False


def _uf_find(parent, x):
    """Union-find root of x with path compression."""
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _uf_union(parent, a, b):
    """Union two topic keys; canonical root = the SHORTER (broader) name."""
    ra, rb = _uf_find(parent, a), _uf_find(parent, b)
    if ra != rb:
        if len(rb) < len(ra) or (len(rb) == len(ra) and rb < ra):
            ra, rb = rb, ra
        parent[rb] = ra


def _same_topic_family(a, b, families, ratio, sim):
    """True when two topic keys name the SAME subject: prefix family, plural,
    shared prefix words, typo sibling, high lexical ratio, or embedding-near."""
    import difflib
    shared = _shared_prefix(a, b)
    return (a.startswith(b + "-") or b.startswith(a + "-")   # prefix family
            or a == b + "s" or b == a + "s"                  # plural (note/notes)
            or shared >= 2                                   # wifi-device-* siblings
            or (families and shared >= 1)                    # windows-* whole family
            or _typo_sibling(a, b)                           # windows-ntp / -npt
            or difflib.SequenceMatcher(None, a, b).ratio() >= ratio
            or _same_subject(a, b, sim))                     # calc / calculator


def _topic_clusters(keys: list[str]) -> list[list[str]]:
    """Group topic keys that are the SAME subject: prefix-family (one extends
    another at a word boundary — gpsd / gpsd-config / gpsd-configuration) OR
    fuzzy-near-identical (note / notes). Union-find over both signals. Returns
    only clusters with >1 member (the ones worth merging)."""
    parent = {k: k for k in keys}
    ratio = _topic_merge_ratio()
    families = _merge_families()
    # Similarity over each topic's slug + brief head — the signal no amount of
    # string distance can supply. `calc` / `math-expression-engine` share no
    # characters yet cover one subject; embeddings union them, lexical rules
    # never will. Cache the vectors: one embed per topic, not one per pair.
    sim: dict = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if _same_topic_family(a, b, families, ratio, sim):
                _uf_union(parent, a, b)
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(_uf_find(parent, k), []).append(k)
    return [sorted(v, key=len) for v in groups.values() if len(v) > 1]


_EMPTY_BRIEF = {"facts": [], "learnings": [], "links": [], "key_results": [],
                "body": "", "title": "", "sources": [], "tags": []}


def _protected_topics() -> set:
    """Names that must never fold into another brief: the global ``shared``
    brief and every discovered repo's brief."""
    protected = {"shared"}
    try:
        from aiforge_core.memory.migrations import _discover_repos
        protected |= {_slug(r) for r in (_discover_repos() or [])}
    except Exception:  # noqa: BLE001
        pass
    return protected


def _mergeable_topic_keys(protected: set) -> list[str]:
    """Topic names eligible for clustering.

    Split OVERFLOW parts (compacted-<topic>-2.md …) are pages of ONE brief, not
    separate topics. They look like a prefix family to the clusterer, so without
    excluding them the merge folds a split topic back into a single oversized
    file — undoing the very compaction that split it.
    """
    part = re.compile(r"-\d+$")
    return [p.stem[len("compacted-"):] for p in iter_briefs()
            if not _CAPTURE_SIG_RE.search(p.name)
            and not part.search(p.stem)
            and p.stem[len("compacted-"):] not in protected]


def _canonical_name(cluster: list[str], protected: set) -> str:
    """The family's COMMON WORD PREFIX when there is one (windows-ntp +
    windows-cpu-mode → "windows"), so a family folds into one broad topic rather
    than into whichever member happened to be shortest. Fuzzy/typo clusters with
    no shared first word fall back to the shortest member. Never a protected
    name."""
    prefix = _common_token_prefix(cluster)
    if prefix and prefix not in protected and _mintable(prefix):
        return prefix
    return cluster[0]


def _mintable(name: str) -> bool:
    """Whether a NEW brief may be created under this name.

    The family prefix is minted as a file, so it has to clear the same
    admission control every other topic does: ``api`` + ``api-gateway`` sharing
    a first word is not a reason to create ``compacted-api.md``, which is
    precisely the generic magnet the topic vocabulary exists to keep out."""
    from .. import _topics
    try:
        return bool(_topics.topic_ok(name))
    except Exception:  # noqa: BLE001 — unknown ⇒ don't invent a new name
        return False


def _load_brief(path) -> dict | None:
    """A parsed brief, an empty one when the file does not exist yet, or None
    when it cannot be read."""
    if not path.exists():
        return dict(_EMPTY_BRIEF)
    try:
        return _parse_brief(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _absorb(into: dict, other: dict) -> None:
    """Union ``other``'s sections into ``into``. Facts dedupe on their BODY, so
    the same fact recorded with different metadata is not carried twice."""
    bodies = {_fact_body(x) for x in into["facts"]}
    for f in other["facts"]:
        if f not in into["facts"] and _fact_body(f) not in bodies:
            into["facts"].append(f)
            bodies.add(_fact_body(f))
    for section in ("learnings", "links", "key_results"):
        for item in (other.get(section) or []):
            if item not in into[section]:
                into[section].append(item)
    if other["body"] and other["body"] not in into["body"]:
        into["body"] = ((into["body"] + "\n\n" + other["body"]).strip()
                        if into["body"] else other["body"])


def _drop_brief(path, topic: str, facts: list) -> None:
    """Retire a folded duplicate: its index rows, its vectors, then the file
    itself — ARCHIVED, not deleted. Its content now lives in the canonical
    brief, but a merge that picked the wrong cluster is otherwise unrecoverable,
    so the file moves to ``archive/`` the way every other retirement does."""
    _reconcile_dropped_index(facts, topic)
    try:
        from aiforge_core.memory import backend_select, sqlite_memory
        if backend_select.embedded():
            sqlite_memory.delete_by_source(f"compacted:compacted-{topic}")
            sqlite_memory.delete_by_source(f"md:compacted-{topic}")
    except Exception:  # noqa: BLE001
        pass
    _archive_file(path)


def _archive_file(path) -> None:
    """Move one brief into ``archive/<stamp>/``; unlink only if that fails."""
    import shutil

    from .._base import _now_iso, memory_dir
    try:
        dst = memory_dir() / "archive" / _now_iso().replace(":", "")
        dst.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dst / path.name))
        return
    except OSError:          # shutil.Error is an OSError subclass
        pass
    try:
        path.unlink()
    except OSError:
        pass


def _write_brief_file(path, name: str, acc: dict) -> bool:
    """Render + atomically write one merged brief. False when it did not land —
    the caller must then keep the members it was about to retire."""
    from aiforge_core.config import _atomic
    try:
        _atomic.write_text(str(path), _render_brief(
            name, facts=acc["facts"], body_md=acc["body"],
            learnings=acc["learnings"], title=acc["title"] or name,
            key_results=acc["key_results"], links=acc["links"],
            sources=acc.get("sources") or None, tags=acc.get("tags") or None))
        return True
    except OSError as exc:
        _log.warning("topic merge: %s not written (%s) — members kept", path, exc)
        return False


def _merge_cluster(cluster: list[str], protected: set) -> int:
    """Fold one cluster into its canonical brief; returns how many were folded."""
    canonical = _canonical_name(cluster, protected)
    cpath = brief_path(canonical)
    # Seed from the canonical's existing brief, or from empty when the canonical
    # is a NEW broader name no member currently owns.
    acc = _load_brief(cpath)
    if acc is None:
        return 0
    acc = {**acc, "facts": list(acc["facts"]), "learnings": list(acc["learnings"]),
           "links": list(acc.get("links") or []),
           "key_results": list(acc["key_results"]),
           "sources": list(acc.get("sources") or []),
           "tags": list(acc.get("tags") or [])}
    absorbed = []
    for other in [m for m in cluster if m != canonical]:
        opath = brief_path(other)
        if not opath.exists():
            continue
        ob = _load_brief(opath)
        if ob is None:
            continue
        _absorb(acc, ob)
        absorbed.append((opath, other, ob["facts"]))
    if not absorbed:
        return 0
    # WRITE FIRST, retire second. Deleting the members before the merged file
    # landed meant one failed write destroyed every one of their facts.
    if not _pkg()._write_brief_file(cpath, canonical, acc):
        return 0
    for opath, other, facts in absorbed:
        _drop_brief(opath, other, facts)
    return len(absorbed)


def merge_similar_topics() -> dict:
    """Consolidate near-duplicate TOPIC briefs into ONE — kills the
    ``gpsd`` / ``gpsd-config`` / ``gpsd-configuration`` (and ``note`` / ``notes``)
    sprawl that made the Memory page a junk drawer. For each cluster the SHORTER
    (broader) name is canonical; the others' Facts / Learnings / Links / Key
    Results are unioned into it and the duplicate briefs deleted (index rows
    reconciled). Deterministic (no LLM). Repo briefs (discovered repo names) and
    the global ``shared`` brief are PROTECTED from merging. Default on
    (``AIFORGE_OKR_TOPIC_MERGE``); never raises. Returns ``{merged, groups}``."""
    if os.environ.get("AIFORGE_OKR_TOPIC_MERGE", "1") == "0":
        return {"merged": 0, "skipped": "disabled"}
    protected = _protected_topics()
    clusters = _topic_clusters(_mergeable_topic_keys(protected))
    merged = 0
    done: list[list[str]] = []
    with _WRITE_LOCK:
        for cluster in clusters:
            n = _merge_cluster(cluster, protected)
            if n:
                merged += n
                done.append(cluster)
    return {"merged": merged, "groups": done}


def _fold_kind_briefs(*, dry_run: bool) -> int:
    """Fold every KIND-named junk brief (compacted-learning.md, compacted-user-
    comment.md, …) INTO the global shared brief, then delete it. These have a
    note's KIND as their name (minted by the old _group_key kind-fallback), not
    a topic — their content is untopic'd knowledge that belongs in global.
    Returns the count folded; dry_run only counts. Protects shared + repo briefs
    (only _KIND_BRIEF_STEMS are touched)."""
    kind_paths = [p for p in iter_briefs()
                  if not _CAPTURE_SIG_RE.search(p.name)
                  and p.stem[len("compacted-"):] in _pkg()._KIND_BRIEF_STEMS]
    if dry_run:
        return len(kind_paths)
    sp = brief_path("shared")
    acc = _load_brief(sp)
    if acc is None:
        return 0
    acc = {**acc, "facts": list(acc["facts"]), "learnings": list(acc["learnings"]),
           "links": list(acc.get("links") or []),
           "key_results": list(acc["key_results"]),
           "sources": list(acc.get("sources") or []),
           "tags": list(acc.get("tags") or []),
           "title": acc["title"] or "shared"}
    absorbed = []
    for p in kind_paths:
        kb = _load_brief(p)
        if kb is None:
            continue
        _absorb(acc, kb)
        absorbed.append((p, p.stem[len("compacted-"):], kb["facts"]))
    if not absorbed or not _pkg()._write_brief_file(sp, "shared", acc):
        return 0                      # write first — see _merge_cluster
    for p, name, facts in absorbed:
        _drop_brief(p, name, facts)
    return len(absorbed)


def fold_kind_briefs() -> dict:
    """Fold KIND-named junk briefs into the global shared brief + delete them
    (public step for the recompact pipeline). Returns ``{folded}``."""
    with _WRITE_LOCK:
        try:
            return {"folded": _fold_kind_briefs(dry_run=False)}
        except Exception as exc:  # noqa: BLE001
            _log.debug("fold_kind_briefs failed: %s", exc)
            return {"folded": 0}


def tidy_briefs(*, dry_run: bool = False) -> dict:
    """One-shot Memory-folder tidy so briefs are PROPER-named, canonical, and
    free of cross-scope duplicate content:
      1. fold KIND-named junk briefs into the global shared brief (delete them),
      2. merge near-duplicate TOPIC briefs into one canonical file,
      3. drop project/topic facts already present in the global shared brief.
    ``dry_run`` reports counts without touching disk. Never raises."""
    with _WRITE_LOCK:
        try:
            folded = _fold_kind_briefs(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            _log.debug("tidy_briefs: fold_kind failed: %s", exc)
            folded = 0
    if dry_run:
        # count-only estimate for the other two passes
        try:
            keys = [p.stem[len("compacted-"):] for p in iter_briefs()
                    if not _CAPTURE_SIG_RE.search(p.name)]
            clusters = _topic_clusters(keys)
            would_merge = sum(len(c) - 1 for c in clusters)
        except Exception:  # noqa: BLE001
            would_merge = 0
        return {"ok": True, "dry_run": True, "folded_kind": folded,
                "merged": would_merge, "deduped": 0}
    try:
        merged = merge_similar_topics().get("merged", 0)
    except Exception as exc:  # noqa: BLE001
        _log.debug("tidy_briefs: merge_similar_topics failed: %s", exc)
        merged = 0
    try:
        deduped = _pkg().dedupe_global_copies().get("removed", 0)
    except Exception as exc:  # noqa: BLE001
        _log.debug("tidy_briefs: dedupe_global_copies failed: %s", exc)
        deduped = 0
    return {"ok": True, "dry_run": False, "folded_kind": folded,
            "merged": merged, "deduped": deduped}
