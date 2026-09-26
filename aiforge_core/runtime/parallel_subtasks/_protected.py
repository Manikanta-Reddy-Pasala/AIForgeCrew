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

_NEG = r"(?:do\s*n[o']?t|don'?t|never|without|must\s+not|should\s+not|no)"
_VERB = (r"(?:edit|editing|modify|modifying|change|changing|touch|touching|"
         r"alter|altering|rewrite|rewriting|update|updating|weaken|weakening|"
         r"delete|deleting|remove|removing|create|rename)")
_TESTS_WORD = r"(?:(?:the|any|existing|these|those|my)\s+)*(?:unit\s+)?tests?(?:\s+files?)?\b"
_TESTS_RULES = (
    re.compile(_NEG + r"\s+(?:\w+\s+){0,2}?" + _VERB
               + r"(?:\s*(?:,|or|and|/)\s*" + _VERB + r")*\s+(?:any\s+of\s+)?"
               + _TESTS_WORD, re.I),
    re.compile(r"\b(?:leave|keep)\s+(?:the\s+|all\s+)?tests?\s+"
               r"(?:alone|as\s+(?:is|they\s+are)|unchanged|untouched)", re.I),
    re.compile(r"\btests?(?:\s+files?)?\s+(?:are|is|stay|remain)\s+"
               r"(?:\w+\s+)?read[- ]only", re.I),
    re.compile(r"\b(?:tests?|test\s+files?)\s+(?:must|should)\s+not\s+be\s+"
               r"(?:edited|modified|changed|touched)", re.I),
)
# A backticked path is protected only as the OBJECT of the refusal: "do not
# modify `pyproject.toml`, `conftest.py`" or "`tests/` are read-only" — never
# "do not change the behaviour of `money.py`" or "fixed by modifying `money.py`".
_TICK = re.compile(r"`([^`\n]{1,200})`")
_NEG_VERB = re.compile(_NEG + r"\s+(?:\w+\s+){0,2}?" + _VERB
                       + r"(?:\s*(?:,|or|and|/)\s*" + _VERB + r")*\s+", re.I)
_FILLER = re.compile(r"(?:(?:the|any|file|files|in|under|inside|folder|"
                     r"directory|of\s+the\s+files?\s+in)\s+){0,3}", re.I)
_LIST_SEP = re.compile(r"\s*(?:,\s*(?:or\s+|and\s+)?|\s+or\s+|\s+and\s+)")
_READ_ONLY = re.compile(r"read[- ]only|must\s+not\s+be\s+(?:edited|modified|"
                        r"changed|touched)", re.I)
_ONLY_LINE = re.compile(
    r"\bonly\s+files?\s+(?:that\s+)?(?:may|can|should|is\s+allowed\s+to)\s+be\s+"
    r"(?:created\s+or\s+)?(?:modified|changed|edited)", re.I)


def _objects_after(clause: str, pos: int) -> list[str]:
    """Backticked paths that directly follow ``pos`` (a list of them)."""
    out: list[str] = []
    m = _FILLER.match(clause, pos)
    pos = m.end() if m else pos
    while True:
        t = _TICK.match(clause, pos)
        if not t:
            return out
        out.append(t.group(1))
        sep = _LIST_SEP.match(clause, t.end())
        if not sep:
            return out
        pos = sep.end()


def _forbidden_paths(text: str) -> list[str]:
    out: list[str] = []
    for clause in re.split(r"[.;\n]\s", str(text or "") + " "):
        toks: list[str] = []
        if _READ_ONLY.search(clause):
            toks += _TICK.findall(clause)
        for m in _NEG_VERB.finditer(clause):
            toks += _objects_after(clause, m.end())
        for tok in toks:
            p = _clean_path(tok)
            if p and p not in out:
                out.append(p)
    return out


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


def from_texts(texts) -> list[str]:
    """Protected patterns stated in the user's own words."""
    out: list[str] = []
    for t in texts or ():
        t = str(t or "")
        if any(r.search(t) for r in _TESTS_RULES) and TESTS not in out:
            out.append(TESTS)
        out += [p for p in _forbidden_paths(t) if p not in out]
    return out


def from_spec(spec_md: str) -> tuple[list[str], list[str]]:
    """``(protected, only_writable)`` stated by the SPEC's scope lines."""
    protected: list[str] = []
    only: list[str] = []
    for line in str(spec_md or "").splitlines():
        if _ONLY_LINE.search(line):
            only += [p for p in map(_clean_path, _TICK.findall(line)) if p]
            continue
        if any(r.search(line) for r in _TESTS_RULES) and TESTS not in protected:
            protected.append(TESTS)
        protected += [p for p in _forbidden_paths(line) if p not in protected]
    return protected, only


def _root(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:  # noqa: BLE001
        return str(path)


def register(root: str, patterns=(), only=()) -> None:
    """Add ``patterns`` (and an allow-only list) for the run rooted at
    ``root``. A file named in the allow-only list is never protected."""
    if not patterns and not only:
        return
    k = _root(root)
    with _LOCK:
        cur = _REG.setdefault(k, {"patterns": [], "only": []})
        for p in patterns or ():
            if p and p not in cur["patterns"]:
                cur["patterns"].append(p)
        for p in only or ():
            if p and p not in cur["only"]:
                cur["only"].append(p)


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
    only = rules.get("only") or []
    if only and any(_matches(rel, o) for o in only):
        return False
    return any(_matches(rel, p) for p in rules.get("patterns") or [])


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
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=60)


def revert(cwd: str, base: str = "HEAD") -> list[str]:
    """Put every protected file in ``cwd`` back to ``base``: a changed one is
    checked out again, a new one removed. Returns the paths reverted."""
    if not rules_for(cwd):
        return []
    try:
        changed = _git(["diff", "--name-only", base, "--"], cwd).stdout
        new = _git(["ls-files", "--others", "--exclude-standard"], cwd).stdout
    except Exception:  # noqa: BLE001
        return []
    done: list[str] = []
    for rel in [*changed.splitlines(), *new.splitlines()]:
        rel = rel.strip()
        if not rel or not is_protected(cwd, rel):
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
           "is_protected", "refusal", "register", "revert", "rules_for"]
