"""Graph layer — cross-scope dedupe / reconcile / contradiction resolution and
topic merging. Removes redundant/stale/contradictory facts scattered across
scope briefs and folds near-duplicate topic briefs into one. Part of the
``_graph`` package (split from the former flat ``_graph``)."""
from __future__ import annotations

import os

from .._base import (
    _CAPTURE_SIG_RE,
    _WRITE_LOCK,
    _log,
    _slug,
    brief_path,
    iter_briefs,
)
from .._render import _fact_body, _parse_brief, _reconcile_dropped_index, _render_brief


def _topic_merge_ratio() -> float:
    """Fuzzy cutoff for clustering near-duplicate topic slugs. 0.85 catches
    near-typos like gps/gpst (ratio ~0.857) while staying above the point where
    distinct short slugs start colliding. Env AIFORGE_TOPIC_MERGE_RATIO."""
    try:
        return float(os.environ.get("AIFORGE_TOPIC_MERGE_RATIO", "0.85"))
    except (TypeError, ValueError):
        return 0.85


def _merge_families() -> bool:
    """Whether to collapse a whole SINGLE-WORD family into one topic — the
    aggressive tier that turns ``windows-ntp`` / ``windows-cpu-mode`` /
    ``windows-time-verify`` into one ``windows`` topic. It fuses distinct
    subtopics on purpose (fewer, broader briefs), so it is a policy choice:
    on by default here, off via AIFORGE_TOPIC_MERGE_FAMILIES=0. The 2-word
    prefix and typo tiers below are always on — those are near-certain dupes."""
    return os.environ.get("AIFORGE_TOPIC_MERGE_FAMILIES", "1") != "0"


def _toks(key: str) -> list[str]:
    return [t for t in key.split("-") if t]


def _shared_prefix(a: str, b: str) -> int:
    """How many leading WORDS two slugs share (wifi-device-x / wifi-device-y = 2)."""
    n = 0
    for x, y in zip(_toks(a), _toks(b), strict=False):
        if x != y:
            break
        n += 1
    return n


def _lev1(a: str, b: str) -> bool:
    """True if a and b are within edit-distance 1 (one insert/delete/substitute)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:                      # one substitution
        return sum(x != y for x, y in zip(a, b)) == 1
    # one insert/delete: the shorter is the longer with one char removed
    s, t = (a, b) if la < lb else (b, a)
    for i in range(len(t)):
        if s == t[:i] + t[i + 1:]:
            return True
    return False


def _transpose1(a: str, b: str) -> bool:
    """One adjacent transposition apart — ntp↔npt (Levenshtein 2, but a single
    typo). ``_lev1`` alone misses it because it is two substitutions."""
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return (len(diff) == 2 and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])


def _typo_sibling(a: str, b: str) -> bool:
    """Same words except ONE token that is a one-typo variant of the other —
    ``windows-ntp`` / ``windows-npt`` (ntp↔npt, a transposition), or an
    edit-distance-1 slip. Guarded to tokens of length ≥3 so genuinely distinct
    short words (``in``/``on``) don't collapse."""
    ta, tb = _toks(a), _toks(b)
    if len(ta) != len(tb):
        return False
    diffs = [(x, y) for x, y in zip(ta, tb, strict=False) if x != y]
    if len(diffs) != 1:
        return False
    x, y = diffs[0]
    return min(len(x), len(y)) >= 3 and (_lev1(x, y) or _transpose1(x, y))


def _common_token_prefix(keys: list[str]) -> str:
    """The longest leading run of WORDS every key shares, as a slug.

    ``[windows-ntp, windows-cpu-mode]`` → ``windows``;
    ``[wifi-device-access, wifi-device-connection]`` → ``wifi-device``;
    a cluster with no shared first word → ``""`` (caller keeps the shortest)."""
    cols = zip(*(_toks(k) for k in keys), strict=False)
    out: list[str] = []
    for col in cols:
        if len(set(col)) == 1:
            out.append(col[0])
        else:
            break
    return "-".join(out)


# Brief stems that are a note's KIND, not a topic — these were minted by the old
# kind-fallback in _group_key and are junk (compacted-learning.md etc.). tidy_
# briefs folds them into the global shared brief.
_KIND_BRIEF_STEMS = frozenset({
    "learning", "topic-learning", "user-comment", "rule", "session", "note",
    "project-learning", "topic-suggestion", "skills", "task-history",
    "project", "repo",
})


_RECONCILE_SYS = (
    "You clean a set of knowledge-memory facts drawn from several scope briefs "
    "(each line: 'SCOPE :: fact'). Find DUPLICATES (same information, paraphrased "
    "across briefs) and CONTRADICTIONS (one fact supersedes another — a changed "
    "value / status / decision). For every REDUNDANT or STALE fact, output an "
    "item {scope, fact} to REMOVE, keeping the single best/newest version in ONE "
    "scope (prefer the broadest: shared > a topic > a project). Copy the fact "
    "text VERBATIM as given. Only remove genuine redundancy/contradiction — when "
    "unsure, keep it. Most facts are unique and stay."
)


def reconcile_briefs(*, role: str = "learner", max_facts: int = 400) -> dict:
    """CROSS-brief semantic cleanup: an LLM finds duplicate/contradictory facts
    that scattered across different scope briefs (the compaction consolidate only
    dedupes WITHIN a brief) and removes the redundant/stale copies, keeping one
    canonical version in the broadest scope. Feasible only at a bounded fact
    count (skips above ``max_facts`` so it stays a single call). Gated on
    ``AIFORGE_OKR_SCOPE_LLM``; ``AIFORGE_OKR_RECONCILE=0`` disables. Never raises."""
    # OPT-IN (default OFF): an LLM removing facts across briefs unsupervised can
    # be over-aggressive (it dropped ~24% on a stress run) and is inconsistent —
    # too risky for the automatic pipeline. Enable AIFORGE_OKR_RECONCILE=1 to run
    # it (manually or in recompact) when you want an aggressive cross-scope pass.
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0" \
            or os.environ.get("AIFORGE_OKR_RECONCILE", "0") != "1":
        return {"removed": 0, "skipped": "disabled"}
    from aiforge_core.runtime import work_notes
    briefs: dict = {}          # key -> [facts]
    total = 0
    for p in iter_briefs():
        if _CAPTURE_SIG_RE.search(p.name):
            continue
        key = p.stem[len("compacted-"):]
        try:
            facts = work_notes.parse_note(
                p.read_text(encoding="utf-8"))["sections"].get("facts") or []
        except OSError:
            continue
        if facts:
            briefs[key] = facts
            total += len(facts)
    if total < 2 or total > max_facts:
        return {"removed": 0, "skipped": f"facts={total}"}

    listing = "\n".join(f"{k} :: {_fact_body(f)}"
                        for k, fs in briefs.items() for f in fs)
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class _Rm(BaseModel):
            scope: str = ""
            fact: str = ""

        class _Removes(BaseModel):
            removes: list[_Rm] = []

        res = structured_complete(
            role,
            [{"role": "system", "content": _RECONCILE_SYS},
             {"role": "user", "content": listing[:24000]}],
            _Removes, max_tokens=2000, max_retries=1, temperature=0.0)
        removes = getattr(res, "removes", None) or []
    except Exception as exc:  # noqa: BLE001
        _log.debug("reconcile_briefs LLM failed: %s", exc)
        return {"removed": 0, "error": "llm_unreachable"}

    # group removals by scope key → ci-keys to drop
    drop: dict = {}
    for r in removes:
        k = (getattr(r, "scope", "") or "").strip()
        f = (getattr(r, "fact", "") or "").strip()
        if k in briefs and f:
            drop.setdefault(k, set()).add(work_notes._ci_key(f))
    removed = 0
    with _WRITE_LOCK:
        for k, dks in drop.items():
            p = brief_path(k)
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            kept = [f for f in facts
                    if work_notes._ci_key(_fact_body(f)) not in dks]
            if len(kept) != len(facts):
                removed += len(facts) - len(kept)
                work_notes.update_note(str(p), facts=kept, kind="knowledge", key=k)
    return {"removed": removed, "scopes": len(drop)}


_CONTRADICT_SYS = (
    "You are given knowledge facts from several scope briefs — each line is "
    "'SCOPE :: fact' (SCOPE is a project name, a topic, or 'shared' = global). "
    "Recall UNIONS a project's brief with the global 'shared' brief, so a fact "
    "in one scope that CONTRADICTS a fact in another is surfaced together and "
    "misleads. Find ONLY DIRECT CONTRADICTIONS: two facts about the SAME specific "
    "subject asserting MUTUALLY EXCLUSIVE values — a changed deploy method, port, "
    "runtime/version, status, owner, or decision (e.g. 'deploy via docker' vs "
    "'deploy via systemctl restart'; 'runtime python3.11' vs 'runtime python3.12' "
    "for the SAME thing). For each contradiction, output ONE item {scope, fact} "
    "naming the STALE/outdated fact to REMOVE, keeping the current one.\n"
    "STRICT RULES: (1) ONLY genuine contradictions — NOT duplicates, NOT "
    "paraphrases, NOT merely related facts. (2) Different subjects that share a "
    "word are NOT a contradiction (service A's port vs service B's port; repo X's "
    "runtime vs repo Y's runtime). (3) RECENCY = TRUTH: each scope shows its "
    "'updated' date; when two facts contradict, REMOVE the one from the scope "
    "with the OLDER 'updated' date (it is stale) and keep the newer. If an "
    "explicit correction says 'now X, NOT Y', the 'Y' fact is the stale one. "
    "(4) When in ANY doubt, output NOTHING. Copy the stale fact text VERBATIM. "
    "Most facts have no contradiction — an empty list is the common, correct answer."
)


def resolve_contradictions(*, role: str = "learner", max_facts: int = 400) -> dict:
    """CONTRADICTION-only cross-scope resolver — REPLACE outdated facts.

    A new fact that contradicts what a repo brief OR the global 'shared' brief
    already holds must supersede it (the video's "overwrite outdated facts, don't
    append" rule), because recall unions repo ∪ shared and would otherwise
    surface both. Unlike :func:`reconcile_briefs` (which also removes DUPLICATES
    and was too aggressive → off by default), this pass touches ONLY genuine
    contradictions with a strict prompt, so it is safe to run automatically
    (default ON; ``AIFORGE_OKR_CONTRADICT=0`` disables). Bounded to a single LLM
    call (skips above ``max_facts``). Gated on ``AIFORGE_OKR_SCOPE_LLM``. Never
    raises. Returns ``{removed, scopes}``."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0" \
            or os.environ.get("AIFORGE_OKR_CONTRADICT", "1") == "0":
        return {"removed": 0, "skipped": "disabled"}
    from aiforge_core.runtime import work_notes
    briefs: dict = {}          # key -> [facts]
    updated: dict = {}         # key -> updated_at (recency tiebreaker)
    total = 0
    for p in iter_briefs():
        if _CAPTURE_SIG_RE.search(p.name):
            continue
        key = p.stem[len("compacted-"):]
        try:
            parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            facts = parsed["sections"].get("facts") or []
        except OSError:
            continue
        if facts:
            briefs[key] = facts
            updated[key] = (parsed.get("frontmatter") or {}).get("updated_at") or ""
            total += len(facts)
    if total < 2 or total > max_facts:
        return {"removed": 0, "skipped": f"facts={total}"}

    # scope label carries the brief's updated date so the model picks the STALE
    # (older) side of a contradiction — recency = truth.
    listing = "\n".join(f"{k} (updated {updated.get(k) or '?'}) :: {_fact_body(f)}"
                        for k, fs in briefs.items() for f in fs)
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class _Rm(BaseModel):
            scope: str = ""
            fact: str = ""

        class _Removes(BaseModel):
            removes: list[_Rm] = []

        res = structured_complete(
            role,
            [{"role": "system", "content": _CONTRADICT_SYS},
             {"role": "user", "content": listing[:24000]}],
            _Removes, max_tokens=2000, max_retries=1, temperature=0.0)
        removes = getattr(res, "removes", None) or []
    except Exception as exc:  # noqa: BLE001
        _log.debug("resolve_contradictions LLM failed: %s", exc)
        return {"removed": 0, "error": "llm_unreachable"}

    drop: dict = {}
    for r in removes:
        k = (getattr(r, "scope", "") or "").strip()
        f = (getattr(r, "fact", "") or "").strip()
        if k in briefs and f:
            drop.setdefault(k, set()).add(work_notes._ci_key(f))
    removed = 0
    with _WRITE_LOCK:
        for k, dks in drop.items():
            p = brief_path(k)
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            kept = [f for f in facts
                    if work_notes._ci_key(_fact_body(f)) not in dks]
            if len(kept) != len(facts):
                # reconcile the search index too, so the stale contradicted fact
                # stops surfacing before the next full reingest.
                _reconcile_dropped_index([f for f in facts if f not in kept], k)
                removed += len(facts) - len(kept)
                work_notes.update_note(str(p), facts=kept, kind="knowledge", key=k)
    return {"removed": removed, "scopes": len(drop)}


def dedupe_global_copies() -> dict:
    """Remove facts from project/topic briefs when the SAME fact (case-insensitive)
    already lives in the global ``compacted-shared.md`` brief. Recall always
    unions the global brief for every scope, so those copies are pure redundancy
    — dropping them de-duplicates the md layer without any recall loss. Fresh
    read-modify-write under ``_WRITE_LOCK``. Never raises."""
    from aiforge_core.runtime import work_notes
    shared = brief_path("shared")
    if not shared.is_file():
        return {"removed": 0, "briefs": 0}
    try:
        gfacts = work_notes.parse_note(
            shared.read_text(encoding="utf-8"))["sections"].get("facts") or []
    except OSError:
        return {"removed": 0, "briefs": 0}
    gkeys = {work_notes._ci_key(_fact_body(f)) for f in gfacts}
    if not gkeys:
        return {"removed": 0, "briefs": 0}
    removed = 0
    touched = 0
    with _WRITE_LOCK:
        for p in iter_briefs():
            if p.name == "compacted-shared.md" or _CAPTURE_SIG_RE.search(p.name):
                continue
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            kept = [f for f in facts
                    if work_notes._ci_key(_fact_body(f)) not in gkeys]
            if len(kept) != len(facts):
                removed += len(facts) - len(kept)
                touched += 1
                work_notes.update_note(str(p), facts=kept, kind="knowledge",
                                       key=p.stem[len("compacted-"):])
    return {"removed": removed, "briefs": touched}


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
        from .. import _topics
        from ... import local_embed
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


def _topic_clusters(keys: list[str]) -> list[list[str]]:
    """Group topic keys that are the SAME subject: prefix-family (one extends
    another at a word boundary — gpsd / gpsd-config / gpsd-configuration) OR
    fuzzy-near-identical (note / notes). Union-find over both signals. Returns
    only clusters with >1 member (the ones worth merging)."""
    import difflib
    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # canonical root = the SHORTER (broader) name
            if len(rb) < len(ra) or (len(rb) == len(ra) and rb < ra):
                ra, rb = rb, ra
            parent[rb] = ra

    ratio = _topic_merge_ratio()
    families = _merge_families()
    # Similarity over each topic's slug + brief head — the signal no amount of
    # string distance can supply. `calc` / `math-expression-engine` share no
    # characters yet cover one subject; embeddings union them, lexical rules
    # never will. Cache the vectors: one embed per topic, not one per pair.
    sim: dict = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            shared = _shared_prefix(a, b)
            if (a.startswith(b + "-") or b.startswith(a + "-")   # prefix family
                    or a == b + "s" or b == a + "s"              # plural (note/notes)
                    or shared >= 2                               # wifi-device-* siblings
                    or (families and shared >= 1)                # windows-* whole family
                    or _typo_sibling(a, b)                       # windows-ntp / -npt
                    or difflib.SequenceMatcher(None, a, b).ratio() >= ratio
                    or _same_subject(a, b, sim)):                # calc / calculator
                union(a, b)
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return [sorted(v, key=len) for v in groups.values() if len(v) > 1]


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
    # protect repo briefs: a discovered repo's brief must never fold into another
    protected = {"shared"}
    try:
        from aiforge_core.memory.migrations import _discover_repos
        protected |= {_slug(r) for r in (_discover_repos() or [])}
    except Exception:  # noqa: BLE001
        pass
    # Split OVERFLOW parts (compacted-<topic>-2.md …) are pages of ONE brief,
    # not separate topics. They look like a prefix family to the clusterer, so
    # without this the merge folds a split topic back into a single oversized
    # file the very compaction that split it.
    import re as _re
    _part = _re.compile(r"-\d+$")
    keys = [p.stem[len("compacted-"):] for p in iter_briefs()
            if not _CAPTURE_SIG_RE.search(p.name)
            and not _part.search(p.stem)
            and p.stem[len("compacted-"):] not in protected]
    clusters = _topic_clusters(keys)
    merged = 0
    done: list[list[str]] = []
    with _WRITE_LOCK:
        for cluster in clusters:
            # The canonical name is the family's COMMON WORD PREFIX when there is
            # one (windows-ntp + windows-cpu-mode → "windows"), so a family folds
            # into one broad topic rather than into whichever member happened to
            # be shortest. Fuzzy/typo clusters with no shared first word fall back
            # to the shortest member. Never collapse onto a protected name.
            prefix = _common_token_prefix(cluster)
            canonical = prefix if (prefix and prefix not in protected) else cluster[0]
            cpath = brief_path(canonical)
            # Seed from the canonical's existing brief, or from empty when the
            # canonical is a NEW broader name no member currently owns.
            if cpath.exists():
                try:
                    cb = _parse_brief(cpath.read_text(encoding="utf-8"))
                except OSError:
                    continue
            else:
                cb = {"facts": [], "learnings": [], "links": [], "key_results": [],
                      "body": "", "title": ""}
            facts = list(cb["facts"]); learns = list(cb["learnings"])
            links = list(cb.get("links") or []); krs = list(cb["key_results"])
            body = cb["body"]; title = cb["title"]
            moved_any = False
            for other in [m for m in cluster if m != canonical]:
                op = brief_path(other)
                if not op.exists():
                    continue
                try:
                    ob = _parse_brief(op.read_text(encoding="utf-8"))
                except OSError:
                    continue
                for f in ob["facts"]:
                    if f not in facts and _fact_body(f) not in {_fact_body(x) for x in facts}:
                        facts.append(f)
                for l in ob["learnings"]:
                    if l not in learns:
                        learns.append(l)
                for lk in (ob.get("links") or []):
                    if lk not in links:
                        links.append(lk)
                for kr in ob["key_results"]:
                    if kr not in krs:
                        krs.append(kr)
                if ob["body"] and ob["body"] not in body:
                    body = (body + "\n\n" + ob["body"]).strip() if body else ob["body"]
                # remove the duplicate brief + its index rows
                _reconcile_dropped_index(ob["facts"], other)
                try:
                    from aiforge_core.memory import backend_select, sqlite_memory
                    if backend_select.embedded():
                        sqlite_memory.delete_by_source(f"compacted:compacted-{other}")
                        sqlite_memory.delete_by_source(f"md:compacted-{other}")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    op.unlink()
                except OSError:
                    pass
                moved_any = True
                merged += 1
            if moved_any:
                cpath.write_text(
                    _render_brief(canonical, facts=facts, body_md=body,
                                  learnings=learns, title=title, key_results=krs,
                                  links=links),
                    encoding="utf-8")
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
                  and p.stem[len("compacted-"):] in _KIND_BRIEF_STEMS]
    if dry_run:
        return len(kind_paths)
    sp = brief_path("shared")
    try:
        sb = _parse_brief(sp.read_text(encoding="utf-8")) if sp.exists() else {
            "facts": [], "learnings": [], "links": [], "key_results": [],
            "body": "", "title": ""}
    except OSError:
        return 0
    facts = list(sb["facts"]); learns = list(sb["learnings"])
    links = list(sb.get("links") or []); krs = list(sb["key_results"])
    body = sb["body"]; title = sb["title"] or "shared"
    folded = 0
    for p in kind_paths:
        try:
            kb = _parse_brief(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        seen = {_fact_body(x) for x in facts}
        for f in kb["facts"]:
            if f not in facts and _fact_body(f) not in seen:
                facts.append(f); seen.add(_fact_body(f))
        for l in kb["learnings"]:
            if l not in learns:
                learns.append(l)
        for lk in (kb.get("links") or []):
            if lk not in links:
                links.append(lk)
        for kr in kb["key_results"]:
            if kr not in krs:
                krs.append(kr)
        if kb["body"] and kb["body"] not in body:
            body = (body + "\n\n" + kb["body"]).strip() if body else kb["body"]
        _reconcile_dropped_index(kb["facts"], p.stem[len("compacted-"):])
        try:
            from aiforge_core.memory import backend_select, sqlite_memory
            if backend_select.embedded():
                sqlite_memory.delete_by_source(f"compacted:{p.stem}")
                sqlite_memory.delete_by_source(f"md:{p.stem}")
        except Exception:  # noqa: BLE001
            pass
        try:
            p.unlink()
        except OSError:
            pass
        folded += 1
    if folded:
        sp.write_text(
            _render_brief("shared", facts=facts, body_md=body, learnings=learns,
                          title=title, key_results=krs, links=links),
            encoding="utf-8")
    return folded


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
        deduped = dedupe_global_copies().get("removed", 0)
    except Exception as exc:  # noqa: BLE001
        _log.debug("tidy_briefs: dedupe_global_copies failed: %s", exc)
        deduped = 0
    return {"ok": True, "dry_run": False, "folded_kind": folded,
            "merged": merged, "deduped": deduped}
