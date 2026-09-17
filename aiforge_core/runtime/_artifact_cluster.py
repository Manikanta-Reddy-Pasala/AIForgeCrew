"""Finding near-duplicate rules, skills and workflows, and merging a cluster with the model."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


def _pkg():
    """``artifact_merge``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``artifact_merge``; patch any other
    name on this module."""
    import aiforge_core.runtime.artifact_merge as package
    return package


# ── the three kinds, behind one shape ───────────────────────────────────────

@dataclass(frozen=True)
class _Item:
    """One library artifact, flattened so the sweep never branches on kind.

    ``extra`` carries what only one kind has — a rule's ``globs`` and
    ``alwaysApply``. Dropping those on the way through would turn a merge into
    a silent scope change: two rules scoped to ``*.py`` come back applying to
    every turn, which is a bigger behaviour change than the duplication was.
    """

    kind: str
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str
    source: str
    extra: tuple = ()

    def fingerprint(self) -> str:
        # usedforsecurity=False: this is an IDENTITY digest — "is this the same
        # artifact as last night" — never a signature or a credential. The flag
        # is how the stdlib lets a caller say which of the two it means.
        h = hashlib.sha1(
            (self.name + "\x00" + self.body).encode("utf-8", "replace"),
            usedforsecurity=False)
        return h.hexdigest()[:16]


# ── clustering (deterministic, no model) ────────────────────────────────────

def _tokens(item: _Item) -> set[str]:
    from aiforge_core.runtime import skills as _sk
    text = " ".join([item.name, item.description, " ".join(item.triggers),
                     item.body[:_pkg()._BODY_HEAD]])
    return _sk._tokens(text)


def item_from(kind: str, name: str, description: str, triggers, body: str,
              source: str = "") -> _Item:
    """A comparable artifact that is not on disk yet.

    So a writer can ask "do we already have this?" with the SAME similarity the
    sweep uses. Two definitions of similar — one for merging, one for
    admission — would drift, and the pair that drifted apart would be exactly
    the duplicate nobody catches."""
    return _Item(kind, (name or "").strip(), (description or "").strip(),
                 tuple(t.strip().lower() for t in (triggers or []) if t),
                 body or "", source, ())


def similarity(a: _Item, b: _Item) -> float:
    """0..1 overlap between two artifacts.

    Delegates the token scoring to ``skills._fuzzy_overlap`` so "deploy" still
    matches "deployment" and there is ONE definition of what similar means —
    two copies of that rule is how search and this sweep would drift apart and
    start disagreeing about the same pair.
    """
    from aiforge_core.runtime import skills as _sk
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    # SYMMETRIC on purpose. ``_fuzzy_overlap`` scores one token set AGAINST
    # another and is directional — it answers "how much of a is in b", which
    # for a long rule and a short one differs by a lot in each direction. Used
    # raw, the cluster you get depends on which member the loop happened to
    # reach first, so "a≈b" and "b≉a" could both be true in the same pass.
    # Average both directions, each normalised by its own source.
    fwd = _sk._fuzzy_overlap(ta, tb) / float(len(ta))
    rev = _sk._fuzzy_overlap(tb, ta) / float(len(tb))
    return min(1.0, (fwd + rev) / 2.0)


def _pairs_above(items: list[_Item], threshold: float) -> list[tuple[int, int]]:
    return [(i, j)
            for i in range(len(items))
            for j in range(i + 1, len(items))
            if similarity(items[i], items[j]) >= threshold]


def find_clusters(kind: str, items: list[_Item] | None = None,
                  threshold: float | None = None) -> list[list[_Item]]:
    """Groups of near-duplicate artifacts, largest first.

    Union-find over the pairs above the threshold: "a≈b" and "b≈c" put all
    three in one cluster even when a and c only meet through b, which is how
    three spellings of one instruction actually accumulate.
    """
    pkg = _pkg()
    pool = [i for i in (items if items is not None else pkg.load(kind))
            if pkg.mergeable(i)]
    if len(pool) < 2:
        return []
    thr = threshold if threshold is not None else pkg._env_float(
        "AIFORGE_MERGE_SIMILARITY", 0.72)
    parent = list(range(len(pool)))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in _pairs_above(pool, thr):
        ri, rj = _find(i), _find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[_Item]] = {}
    for idx, item in enumerate(pool):
        groups.setdefault(_find(idx), []).append(item)
    cap = pkg._env_int("AIFORGE_MERGE_MAX_CLUSTER", 6, low=2)
    out = [sorted(g, key=lambda i: i.name)[:cap]
           for g in groups.values() if len(g) > 1]
    return sorted(out, key=len, reverse=True)


def cluster_fingerprint(cluster: list[_Item]) -> str:
    """Identity of a cluster BY CONTENT, so an unchanged cluster is never sent
    to the model twice — and a cluster whose member was edited is."""
    joined = "|".join(sorted(i.fingerprint() for i in cluster))
    return hashlib.sha1(joined.encode(), usedforsecurity=False).hexdigest()[:16]


# ── the merge itself ────────────────────────────────────────────────────────

_SYSTEM = (
    "You consolidate duplicated agent instruction documents. You NEVER invent "
    "guidance and you NEVER drop a distinct instruction: the merged document "
    "must carry every rule, step, caveat and command that appears in any "
    "input. Where inputs conflict, keep the more specific one and say so in "
    "one clause. Output terse markdown — no preamble, no headings that only "
    "restate the title."
)


def _cross_kind_enabled() -> bool:
    """Whether the sweep also reconciles ACROSS kinds
    (``AIFORGE_MERGE_CROSS_KIND``, default on)."""
    return os.environ.get("AIFORGE_MERGE_CROSS_KIND", "1").strip().lower() \
        not in ("0", "false", "no", "off")


# Which kind a MIXED cluster collapses into: the most specific one present.
# A workflow spells out steps, a skill explains an approach, a rule only
# asserts — folding the specific into the general is the lossy direction.
_KIND_RANK = {"workflows": 3, "skills": 2, "rules": 1}


def target_kind(cluster: list[_Item]) -> str:
    """The kind a cluster should end up as (see :data:`_KIND_RANK`)."""
    return max((i.kind for i in cluster),
               key=lambda k: _KIND_RANK.get(k, 0), default="rules")


def cross_kind_clusters(threshold: float | None = None) -> list[list[_Item]]:
    """Near-duplicates that span KINDS — one instruction saved as a rule AND
    as a skill, which the per-kind sweep can never see because it only ever
    compares a kind with itself.

    Only genuinely mixed clusters are returned; a cluster wholly inside one
    kind is the per-kind pass's job and merging it twice would be waste."""
    pkg = _pkg()
    pool = [i for k in pkg.KINDS for i in pkg.load(k)]
    return [c for c in find_clusters("", items=pool, threshold=threshold)
            if len({i.kind for i in c}) > 1]


def _merge_prompt(kind: str, cluster: list[_Item]) -> str:
    kinds = {i.kind for i in cluster}
    what = kind if len(kinds) == 1 else "library artifacts"
    parts = [f"These {len(cluster)} {what} say substantially the same thing. "
             f"Produce ONE {kind[:-1]} that replaces all of them.\n"]
    if len(kinds) > 1:
        # The model must SEE the mix: a workflow's numbered steps and a rule's
        # one-line assertion are the same instruction at different resolutions,
        # and the merged artifact has to keep the steps.
        parts.append(
            f"They are currently a mix of {', '.join(sorted(kinds))}; keep "
            "every concrete step and condition from the most detailed one.\n")
    for n, item in enumerate(cluster, 1):
        label = item.kind[:-1] if len(kinds) > 1 else kind[:-1]
        parts.append(
            f"\n--- {label} {n}: {item.name} ---\n"
            f"description: {item.description}\n"
            f"triggers: {', '.join(item.triggers) or '(none)'}\n"
            f"{item.body.strip()}\n")
    parts.append(
        "\nReturn the merged name (prefer the clearest existing one), a "
        "one-line description, the union of the triggers, and the merged "
        "body.")
    return "".join(parts)


def _response_model():
    """Built lazily: pydantic is a heavy import for a module that is mostly
    file walking, and the tests exercise the clustering without it."""
    from pydantic import BaseModel, Field

    class MergedArtifact(BaseModel):
        name: str = Field(description="the merged artifact's name")
        description: str = Field(default="", description="one line")
        triggers: list[str] = Field(default_factory=list)
        body: str = Field(description="merged markdown body")

    return MergedArtifact


def _llm_merge(kind: str, cluster: list[_Item]):
    from aiforge_core.llm.structured import structured_complete
    return structured_complete(
        "learner",
        [{"role": "system", "content": _SYSTEM},
         {"role": "user", "content": _merge_prompt(kind, cluster)}],
        _response_model(), max_tokens=1600, temperature=0.0)


def _coverage(merged_body: str, cluster: list[_Item]) -> float:
    """Fraction of the members' body vocabulary the merged body still carries.

    The failure this exists for is not a wrong merge, it is a LOSSY one: the
    model writes a tidy summary of five rules, three instructions quietly
    disappear, and the archive is the only place they still exist. Cheap, but
    it catches the summary-instead-of-merge case every time.
    """
    from aiforge_core.runtime import skills as _sk
    want: set[str] = set()
    for item in cluster:
        want |= _sk._tokens(item.body)
    if not want:
        return 1.0
    have = _sk._tokens(merged_body)
    return len(want & have) / float(len(want))


def too_large(cluster: list[_Item]) -> str:
    """"" unless the cluster cannot be sent whole.

    Truncating the INPUTS would be the worst of both worlds: the model merges
    what it was shown, the coverage check then fails against what it was not,
    and the cluster is recorded as a refusal forever. Skip it instead and say
    so — an operator can raise the cap or split the artifacts by hand.
    """
    total = sum(len(i.body) for i in cluster)
    cap = _pkg()._env_int("AIFORGE_MERGE_MAX_CHARS", 12000, low=1000)
    if total > cap:
        return f"cluster is {total} chars, over the {cap}-char prompt cap"
    return ""


def validate_merge(merged, cluster: list[_Item]) -> str:
    """"" when the merge may be applied, else why it may not."""
    name = (getattr(merged, "name", "") or "").strip()
    body = (getattr(merged, "body", "") or "").strip()
    if not name or not body:
        return "model returned an empty name or body"
    # Against the LONGEST input, not the shortest. Merging a one-line rule with
    # a detailed one and keeping the one-liner IS the lossy case, and a floor
    # taken from the shortest member waves it straight through.
    longest = max(len(i.body.strip()) for i in cluster)
    if len(body) < longest * 0.8:
        return (f"merged body ({len(body)} chars) is shorter than the "
                f"longest input ({longest}) — a summary, not a merge")
    floor = _pkg()._env_float("AIFORGE_MERGE_MIN_COVERAGE", 0.6)
    cov = _coverage(body, cluster)
    if cov < floor:
        return f"merged body covers only {cov:.0%} of the inputs' wording"
    return ""
