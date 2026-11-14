"""Telling topic names apart: merge ratios, shared prefixes, one-edit typos."""
from __future__ import annotations

import os


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


# ── same subject by NAME: stems, abbreviations, separators, suffix families ──
#: common developer abbreviations, expanded before comparing names
_ABBREV = {
    "venv": "virtual-environment", "virtualenv": "virtual-environment",
    "e2e": "end-to-end", "env": "environment", "db": "database",
    "k8s": "kubernetes", "auth": "authentication", "config": "configuration",
    "cfg": "configuration", "repo": "repository", "repos": "repository",
    "doc": "documentation", "docs": "documentation", "deps": "dependencies",
    "py": "python", "js": "javascript", "ts": "typescript", "msg": "message",
}
_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ence", "ent",
             "ies", "ers", "er", "ate", "es", "ed", "s")


def _stem(tok: str) -> str:
    """A crude stem: testing/tests -> test, persistence/persistent -> persist,
    validation/validate -> valid. Only strips when 3+ letters stay."""
    t = tok.lower()
    for suf in _SUFFIXES:
        keep = 5 if suf in ("ence", "ent", "er", "ers", "ate") else 3   # client != cli
        if t.endswith(suf) and len(t) - len(suf) >= keep:
            return t[:-len(suf)]
    return t


def _name_tokens(key: str) -> list[str]:
    out: list[str] = []
    for t in _toks(key.lower()):
        out.extend(_toks(_ABBREV.get(t, t)))
    return [_stem(t) for t in out]


def _name_key(key: str) -> str:
    """The subject a topic name names, spelling aside: stems joined with no
    separator, so ai-forge-memory == aiforgememory, venv-setup ==
    virtual-environment-setup, persistence == persistent. A bare number does
    not name a subject (ai-forge-memory-2), unless the name is only numbers."""
    toks = _name_tokens(key)
    words = [t for t in toks if not t.isdigit()]
    return "".join(words or toks)


def _same_name(a: str, b: str) -> bool:
    """Same subject by name: equal name keys, or a SUFFIX family — the shorter
    is one word (4+ letters) that the longer ends with (validation /
    input-validation / assertion-validation; test / end-to-end-testing)."""
    if _name_key(a) == _name_key(b):
        return True
    ta, tb = _name_tokens(a), _name_tokens(b)
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return (len(short) == 1 and len(long_) > 1 and len(short[0]) >= 4
            and long_[-1] == short[0]
            and long_[0] not in _MODIFIERS)         # in-memory is not the memory topic


#: a leading word that changes what the rest means (in-memory, non-blocking)
_MODIFIERS = frozenset({"in", "on", "of", "no", "non", "un", "pre", "post", "anti",
                        "multi", "self", "sub", "over", "under"})
