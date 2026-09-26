"""Paths a team run must not write: the user said so, or the SPEC says so.

A live run on "fix money.py so every test in tests/ passes … Do not edit the
tests" wrote a SPEC that called the tests read-only — and then planned a
``test-money`` subtask, merged a rewritten ``tests/test_money.py`` and let the
repair engine patch it again. Asking in a prompt is not enough for a local
model, so this is a hard guard every writer consults:

* :func:`from_texts` / :func:`from_spec` read the constraint ("do not edit the
  tests", "``tests/`` are read-only", "do not modify ``pyproject.toml``", "the
  only file that may be modified is ``money.py``");
* :func:`register` keeps them per run root (a subtask worktree under that root
  inherits them);
* :func:`is_protected` answers for one relative path — subtask writes, the
  repair engine's patches and the test reviewer refuse such a write with
  :func:`refusal`;
* :func:`filter_subtasks` drops planned subtasks that own a protected file;
* :func:`revert` puts back any protected file a writer changed anyway before
  it is committed or merged.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import threading

_LOCK = threading.Lock()
_REG: dict[str, dict] = {}

TESTS = "@tests"          # every test file (gaming_check.is_test_path)

# Only a prohibition makes a file read-only: don't / do not / never / must not
# / should not (or "without editing") + an EDIT verb + its object. A DELETE or
# RENAME prohibition keeps the file in place but lets it be edited, and a
# word like "no" is not a prohibition ("no update tests needed").
_NEG = (r"\b(?:do\s+not|don[’']?t|never|must\s+not|mustn[’']?t|should\s+not|"
        r"shouldn[’']?t|without)")
_EDIT_V = (r"(?:edit(?:ing)?|modify(?:ing)?|chang(?:e|ing)|touch(?:ing)?|"
           r"alter(?:ing)?|rewrit(?:e|ing)|updat(?:e|ing)|weaken(?:ing)?)")
_DEL_V = r"(?:delet(?:e|ing)|remov(?:e|ing)|renam(?:e|ing))"
_ANY_V = rf"(?:{_EDIT_V}|{_DEL_V})"
_TESTS_WORD = (r"(?:(?:the|any|all|existing|these|those|my|of\s+the)\s+)*"
               r"(?:unit\s+)?tests?(?:\s+files?)?\b(?![/\w.])")
_NEG_VERB = re.compile(_NEG + r"\s+(?:\w+\s+){0,2}?(" + _ANY_V
                       + r"(?:\s*(?:,|or|and|/)\s*" + _ANY_V + r")*)\b\s*", re.I)
_EDIT_RE = re.compile(r"\b" + _EDIT_V + r"\b", re.I)
_TESTS_OBJ = re.compile(r"(?:any\s+of\s+)?" + _TESTS_WORD, re.I)
_TESTS_RO_RULES = (
    re.compile(r"\b(?:leave|keep)\s+(?:the\s+|all\s+)?tests?\s+"
               r"(?:alone|as\s+(?:is|they\s+are)|unchanged|untouched)", re.I),
    re.compile(r"\btests?(?:\s+files?)?\s+(?:are|is|stay|remain)\s+"
               r"(?:\w+\s+)?read[- ]only", re.I),
    re.compile(r"\b(?:tests?|test\s+files?)\s+(?:must|should)\s+not\s+be\s+"
               r"(?:edited|modified|changed|touched)", re.I),
)
# An explicit request to change something lifts an earlier protection of it
# ("now update the tests"), and a file the user asks to create or change is
# never read-only ("add a test in tests/test_money.py").
_POS_VERB = re.compile(r"\b(?:add|create|write|edit|update|modify|change|fix|"
                       r"patch|rewrite|implement|extend|touch|adjust)\s+", re.I)
# ... unless it is negated or hedged: "no update tests needed" asks for nothing.
_NEG_BEFORE = re.compile(r"(?:\bno|\bnot|n[’']t|\bnever|\bwithout|\bnor)\s+"
                         r"(?:\w+\s+){0,2}$", re.I)
_POS_TESTS = re.compile(r"\b(?:update|edit|modify|change|fix|rewrite|touch|"
                        r"adjust)\s+" + _TESTS_WORD, re.I)
_POS_FILLER = re.compile(r"(?:(?:a|an|the|new|one|more|unit|test|tests|file|"
                         r"files|in|into|to|at|under|inside|for|called|named)"
                         r"\s+){0,5}", re.I)
# A backticked (or path-shaped) object is protected only as the OBJECT of the
# refusal: "do not modify `pyproject.toml`, `conftest.py`" or "`tests/` are
# read-only" — never "do not change the behaviour of `money.py`" or "fixed by
# modifying `money.py`".
_TICK = re.compile(r"`([^`\n]{1,200})`")
_BARE = re.compile(r"([\w.-]+(?:/[\w.-]+)*/?)(?=[\s,;:)]|$)")
_FILLER = re.compile(r"(?:(?:the|any|file|files|in|under|inside|folder|"
                     r"directory|existing|of\s+the\s+files?\s+in)\s+){0,3}", re.I)
_LIST_SEP = re.compile(r"\s*(?:,\s*(?:or\s+|and\s+)?|\s+or\s+|\s+and\s+)")
_READ_ONLY = re.compile(r"read[- ]only|must\s+not\s+be\s+(?:edited|modified|"
                        r"changed|touched)", re.I)
_ONLY_LINE = re.compile(
    r"\bonly\s+files?\s+(?:that\s+)?(?:may|can|should|is\s+allowed\s+to)\s+be\s+"
    r"(?:created\s+or\s+)?(?:modified|changed|edited)", re.I)


def _object_at(clause: str, pos: int) -> tuple[str, int] | None:
    t = _TICK.match(clause, pos)
    if t:
        return t.group(1), t.end()
    b = _BARE.match(clause, pos)
    if b and ("/" in b.group(1) or re.search(r"\.[A-Za-z0-9]{1,8}$",
                                              b.group(1).rstrip("."))):
        return b.group(1).rstrip("."), b.end()
    return None


def _objects_after(clause: str, pos: int, filler=_FILLER) -> list[str]:
    """Paths (backticked or path-shaped) that directly follow ``pos``."""
    out: list[str] = []
    m = filler.match(clause, pos)
    pos = m.end() if m else pos
    while True:
        hit = _object_at(clause, pos)
        if not hit:
            return out
        out.append(hit[0])
        sep = _LIST_SEP.match(clause, hit[1])
        if not sep:
            return out
        pos = sep.end()


def _clause_events(clause: str, positives: bool) -> list[tuple[int, str, str]]:
    """``(position, kind, target)`` in text order; kind is ``ro`` (read-only),
    ``keep`` (no delete / rename) or ``allow`` (asked to create/change)."""
    ev: list[tuple[int, str, str]] = []
    spans = []
    for m in _NEG_VERB.finditer(clause):
        spans.append((m.start(), m.end()))
        kind = "ro" if _EDIT_RE.search(m.group(1)) else "keep"
        t = _TESTS_OBJ.match(clause, m.end())
        if t:
            ev.append((m.start(), kind, TESTS))
        ev += [(m.start(), kind, p) for p in _objects_after(clause, m.end())]
    for r in _TESTS_RO_RULES:
        ev += [(m.start(), "ro", TESTS) for m in r.finditer(clause)]
    if _READ_ONLY.search(clause):
        ev += [(0, "ro", p) for p in _TICK.findall(clause)]
    if positives:
        def _negated(i):
            return any(a <= i < b for a, b in spans) \
                or bool(_NEG_BEFORE.search(clause[:i]))
        for m in _POS_TESTS.finditer(clause):
            if not _negated(m.start()):
                ev.append((m.start(), "allow", TESTS))
        for m in _POS_VERB.finditer(clause):
            if not _negated(m.start()):
                ev += [(m.start(), "allow", p)
                       for p in _objects_after(clause, m.end(), _POS_FILLER)]
    return sorted(ev, key=lambda e: e[0])


def _clauses(text: str) -> list[str]:
    return re.split(r"[.;!?\n]\s", str(text or "") + " ")


def _apply(state: dict, kind: str, target: str) -> None:
    tgt = target if target == TESTS else _clean_path(target)
    if not tgt:
        return
    ro, keep, allow = state["patterns"], state["keep"], state["allow"]
    if kind == "allow":
        if tgt in ro:
            ro.remove(tgt)
        if tgt not in allow:
            allow.append(tgt)
        return
    if tgt in allow:
        allow.remove(tgt)
    bucket = ro if kind == "ro" else keep
    if tgt not in bucket:
        bucket.append(tgt)


def _forbidden_paths(text: str) -> list[str]:
    state = {"patterns": [], "keep": [], "allow": []}
    for clause in _clauses(text):
        for _pos, kind, tgt in _clause_events(clause, positives=False):
            if kind == "ro" and tgt != TESTS:
                _apply(state, kind, tgt)
    return state["patterns"]


def _norm(rel: str) -> str:
    rel = str(rel or "").replace(os.sep, "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def _clean_path(tok: str) -> str:
    tok = tok.strip().strip("'\"").rstrip(".,;:")
    if not tok or " " in tok or tok.startswith(("-", "$")):
        return ""
    if os.path.isabs(tok) or tok.startswith("~"):
        return ""                       # the target folder itself, not a file
    if not ("/" in tok or "." in tok):
        return ""
    return _norm(tok)


def rules_from_texts(texts) -> dict:
    """``{patterns, keep, allow}`` stated in the user's own words. ``texts``
    come newest first (team_target.user_texts); they are read oldest first
    so the latest message wins — "now update the tests" lifts an earlier
    "don't edit the tests"."""
    state: dict = {"patterns": [], "keep": [], "allow": []}
    for t in reversed(list(texts or ())):
        for clause in _clauses(t):
            for _pos, kind, tgt in _clause_events(clause, positives=True):
                _apply(state, kind, tgt)
    return state


def from_texts(texts) -> list[str]:
    """Read-only patterns stated in the user's own words."""
    return rules_from_texts(texts)["patterns"]


def from_spec(spec_md: str) -> tuple[list[str], list[str]]:
    """``(protected, only_writable)`` stated by the SPEC's scope lines."""
    protected: list[str] = []
    only: list[str] = []
    for line in str(spec_md or "").splitlines():
        if _ONLY_LINE.search(line):
            only += [p for p in map(_clean_path, _TICK.findall(line)) if p]
            continue
        for clause in _clauses(line):
            for _pos, kind, tgt in _clause_events(clause, positives=False):
                tgt = tgt if tgt == TESTS else _clean_path(tgt)
                if kind == "ro" and tgt and tgt not in protected:
                    protected.append(tgt)
    return protected, only


def _root(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:  # noqa: BLE001
        return str(path)


def register(root: str, patterns=(), only=(), keep=(), allow=()) -> None:
    """Add read-only ``patterns`` (and an allow-only list, no-delete ``keep``
    patterns and the files the user asked to change, ``allow``) for the run
    rooted at ``root``. A file in ``only`` or ``allow`` is never read-only."""
    if not (patterns or only or keep or allow):
        return
    k = _root(root)
    with _LOCK:
        cur = _REG.setdefault(k, {"patterns": [], "only": [], "keep": [],
                                  "allow": []})
        for key, vals in (("patterns", patterns), ("only", only),
                          ("keep", keep), ("allow", allow)):
            lst = cur.setdefault(key, [])
            for p in vals or ():
                if p and p not in lst:
                    lst.append(p)


def clear(root: str) -> None:
    with _LOCK:
        _REG.pop(_root(root), None)


def rules_for(path: str) -> dict:
    """The rules of the registered run ``path`` is inside (or equal to)."""
    if not path:
        return {}
    k = _root(path)
    with _LOCK:
        best = ""
        for root in _REG:
            if (k == root or k.startswith(root.rstrip(os.sep) + os.sep)) \
                    and len(root) > len(best):
                best = root
        return dict(_REG[best]) if best else {}


def _matches(rel: str, pattern: str) -> bool:
    if pattern == TESTS:
        from aiforge_core.runtime.gaming_check import is_test_path
        return is_test_path(rel)
    pat = _norm(pattern)
    if pat.endswith("/"):
        return rel.startswith(pat) or rel + "/" == pat
    if any(c in pat for c in "*?["):
        return fnmatch.fnmatch(rel, pat)
    return rel == pat or rel.startswith(pat + "/")


def is_protected(root: str, rel: str) -> bool:
    """True when the run owning ``root`` must not write ``rel``."""
    rules = rules_for(root)
    if not rules:
        return False
    rel = _norm(rel)
    if not rel:
        return False
    if any(_matches(rel, a) for a in rules.get("allow") or []):
        return False
    only = rules.get("only") or []
    if only and any(_matches(rel, o) for o in only):
        return False
    return any(_matches(rel, p) for p in rules.get("patterns") or [])


def must_keep(root: str, rel: str) -> bool:
    """True when ``rel`` may be edited but must not be deleted or renamed
    ("do not delete the existing tests") — or is read-only outright."""
    rules = rules_for(root)
    rel = _norm(rel)
    if not rules or not rel:
        return False
    return is_protected(root, rel) or any(
        _matches(rel, k) for k in rules.get("keep") or [])


def refusal(rel: str) -> str:
    return (f"{rel} is read-only for this run (the request or its SPEC says "
            "not to edit it) — write refused")


def filter_subtasks(subs: list, root: str) -> tuple[list, list]:
    """Drop subtasks whose file is protected. ``(kept, dropped_paths)``."""
    kept, dropped = [], []
    for s in subs or []:
        p = _norm((s or {}).get("path") or "")
        if p and is_protected(root, p):
            dropped.append(p)
        else:
            kept.append(s)
    return kept, dropped


def _git(args, cwd) -> subprocess.CompletedProcess:
    from ._worktree import _run_env
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=60, env=_run_env(cwd))


def revert(cwd: str, base: str = "HEAD") -> list[str]:
    """Put every protected file in ``cwd`` back to ``base``: a changed one is
    checked out again, a new one removed. Returns the paths reverted."""
    if not rules_for(cwd):
        return []
    try:
        changed = _git(["diff", "--name-only", base, "--"], cwd).stdout
        gone = set(_git(["diff", "--name-only", "--diff-filter=D", base, "--"],
                        cwd).stdout.split("\n"))
        new = _git(["ls-files", "--others", "--exclude-standard"], cwd).stdout
    except Exception:  # noqa: BLE001
        return []
    done: list[str] = []
    for rel in [*changed.splitlines(), *new.splitlines()]:
        rel = rel.strip()
        if not rel or not (is_protected(cwd, rel)
                           or (rel in gone and must_keep(cwd, rel))):
            continue
        in_base = _git(["cat-file", "-e", f"{base}:{rel}"], cwd).returncode == 0
        if in_base:
            _git(["checkout", base, "--", rel], cwd)
        else:
            try:
                os.remove(os.path.join(cwd, rel))
            except OSError:
                continue
            _git(["rm", "-q", "--cached", "--ignore-unmatch", "--", rel], cwd)
        done.append(rel)
    return done


__all__ = ["TESTS", "clear", "filter_subtasks", "from_spec", "from_texts",
           "is_protected", "must_keep", "refusal", "register", "revert",
           "rules_for", "rules_from_texts"]
