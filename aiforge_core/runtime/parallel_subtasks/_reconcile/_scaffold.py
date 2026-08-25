"""Scaffold stubs, disjoint-file ownership, and decomposition consistency.

Split from ``parallel_subtasks._reconcile`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os

from .._contracts import _clean_symbol, _is_test_subtask


_SCAFFOLD_MARK = "AIFORGE_SCAFFOLD_STUB"   # sentinel — a still-unimplemented stub

_COMMENT_PREFIX = {
    ".py": "#", ".sh": "#", ".rb": "#", ".yaml": "#", ".yml": "#", ".toml": "#",
    ".java": "//", ".go": "//", ".js": "//", ".mjs": "//", ".ts": "//",
    ".tsx": "//", ".c": "//", ".cc": "//", ".cpp": "//", ".rs": "//", ".php": "//",
}


def _stub_content(path: str, api: list, is_test: bool) -> str:
    """A SCAFFOLD stub: the file at its canonical path carrying the target public
    API as a header, so parallel workers implement INTO a fixed structure (no
    chaotic dir trees, no path drift) and to the exact contract. Language-agnostic
    — a comment header for every language; Python code files also get real
    signature stubs so sibling imports resolve during parallel work."""
    ext = os.path.splitext(path)[1].lower()
    # build/markup files: leave empty, the owning worker writes the whole thing.
    if ext in (".xml", ".html", ".json", ".cfg", ".properties", ".txt", ".md", ""):
        return ""
    cmt = _COMMENT_PREFIX.get(ext, "#")
    if is_test:
        return f"{cmt} Tests — implement per SPEC.md. {_SCAFFOLD_MARK}\n"
    if ext == ".py":
        return _python_stub(api)
    hdr = [f"{cmt} STUB {_SCAFFOLD_MARK} — implement this file per SPEC.md, "
           "keeping the public API:"]
    for a in api:
        hdr.append(f"{cmt}   {a}")
    if not api:
        hdr.append(f"{cmt}   (see SPEC.md)")
    return "\n".join(hdr) + "\n"


def _stub_line(a: str) -> "str | None":
    """One scaffold line for an API contract entry: a class/def keeps its
    signature with a stub body; a `CONST: type` or anything else becomes a
    module-level `name = None`. None when no symbol could be cleaned."""
    base = a.rstrip(":")
    if base.startswith(("class ", "async def ", "def ")):
        body = "    ..." if base.startswith("class ") else "    raise NotImplementedError"
        return f"\n\n{base}:\n{body}"
    nm = _clean_symbol(a)
    return f"\n\n{nm} = None" if nm else None


def _python_stub(api: list) -> str:
    """Real Python signature stubs from the API contract — keeps sibling imports
    resolvable while workers fill in bodies. Conservative: only clear top-level
    class/def/const forms; anything ambiguous becomes a module-level name = None."""
    lines = [f'"""Stub {_SCAFFOLD_MARK} — implement the bodies; keep this exact '
             'public API."""']
    for a in [x.strip() for x in api if x and x.strip()]:
        line = _stub_line(a)
        if line:
            lines.append(line)
    return "\n".join(lines) + "\n"


_NON_MODULE_TEST_STEMS = frozenset({
    "integration", "e2e", "end_to_end", "endtoend", "main", "app", "cli",
    "smoke", "full", "all", "system", "suite", "acceptance", "functional",
    "application",
})


def _impl_path_for_test(test_path: str, name: str, ext: str,
                        impl_dirs: list) -> str:
    """Where the impl module for a test should live. Java: mirror src/test→
    src/main. Else: alongside existing impls, or the test's parent (minus a
    tests/ dir)."""
    d = os.path.dirname(test_path)
    if ext.lower() == ".java":
        if "/test/" in test_path:
            return test_path.replace("/test/", "/main/").rsplit("/", 1)[0] + f"/{name}{ext}"
        if impl_dirs:
            return impl_dirs[0] + f"/{name}{ext}"
        return f"{name}{ext}"
    if impl_dirs:
        return f"{impl_dirs[0]}/{name}{ext}".lstrip("/")
    # strip a trailing tests/ segment
    parts = [p for p in d.split("/") if p and p.lower() not in ("tests", "test")]
    base = "/".join(parts)
    return (f"{base}/{name}{ext}" if base else f"{name}{ext}")


def _enforce_disjoint_files(subs: list) -> tuple[list, int]:
    """Mechanically enforce disjoint file ownership across parallel subtasks —
    the plan is NOT trusted, it's checked. Each subtask's primary ``path`` is its
    owned file; if two subtasks claim the same path, the second is FOLDED into the
    first (its goal appended) so exactly one agent authors each file. Returns
    ``(subs, folded_count)``. KISS: path-level, no globs."""
    owner_by_path: dict = {}
    out: list = []
    folded = 0
    for s in subs:
        path = (s.get("path") or "").strip().lstrip("./")
        if path and path in owner_by_path:
            owner = owner_by_path[path]
            extra = (s.get("goal") or "").strip()
            if extra and extra not in (owner.get("goal") or ""):
                owner["goal"] = ((owner.get("goal") or "").rstrip()
                                 + "\n- also: " + extra)
            folded += 1
            continue
        if path:
            owner_by_path[path] = s
        out.append(s)
    return out, folded


_TEST_MODULE_PATTERNS = (
    r"(?i)test_(.+)$", r"(?i)(.+)_tests?$",
    r"(.+)Tests?$",           # XTest / XTests (plural)
    r"(.+)IT(?:Case)?$",      # Java integration tests
    r"(?i)(.+)\.test$", r"(?i)(.+)\.spec$",
)


def _test_target_module(stem: str) -> "str | None":
    """The impl module a test stem targets (test_board→board, BookServiceTest→
    BookService, board.test→board), or None for a non-module test name."""
    import re as _re
    for pat in _TEST_MODULE_PATTERNS:
        m = _re.match(pat, stem)
        if m:
            return m.group(1)
    return None


def _partition_impl_tests(subs: list) -> "tuple[set, list, list]":
    """Split ``subs`` into (impl_stems, impl_dirs, tests) — impl stems lowercased,
    impl dirs (excluding .xml) in order, tests as ``(sub, path, stem, ext)``."""
    impl_stems: set = set()
    impl_dirs: list = []
    tests: list = []
    for s in subs:
        p = str(s.get("path") or "")
        if not p:
            continue
        stem, ext = os.path.splitext(os.path.basename(p))
        if _is_test_subtask(s):
            tests.append((s, p, stem, ext))
        else:
            impl_stems.add(stem.lower())
            d = os.path.dirname(p)
            if d and d not in impl_dirs and ext.lower() != ".xml":
                impl_dirs.append(d)
    return impl_stems, impl_dirs, tests


def _ensure_impl_modules(subs: list) -> list:
    """DECOMPOSITION CONSISTENCY (inverse of the off-plan pruner): every test that
    targets a module (test_board→board, BookServiceTest→BookService, board.test→
    board) MUST have a matching impl file in the plan. When the architect collapses
    all impl into one file but writes per-module tests, those tests can't import
    their modules → collection errors no reconcile fixes. Adds the missing impl
    subtasks. Language-agnostic; skips non-module test names (integration/e2e/…)."""
    impl_stems, impl_dirs, tests = _partition_impl_tests(subs)
    added: list = []
    for s, p, stem, ext in tests:
        name = _test_target_module(stem)
        if name is None:
            continue
        if name.lower() in _NON_MODULE_TEST_STEMS or name.lower() in impl_stems:
            continue
        impl_path = _impl_path_for_test(p, name, ext, impl_dirs).lstrip("/")
        added.append({"slug": name.lower(), "path": impl_path,
                      "goal": f"Implement {name} to satisfy its tests "
                              f"({os.path.basename(p)}).", "api": []})
        impl_stems.add(name.lower())
    return subs + added


def _scaffold_stubs(cwd: str, subs: list) -> list:
    """Deterministically create every declared file at its canonical path (with a
    stub header) BEFORE any parallel worker runs. Gives the local models a fixed
    track: the tree + paths exist, so isolated workers can't invent divergent
    directory layouts and merges stay clean. Returns the paths scaffolded."""
    written: list = []
    for s in subs:
        path = str(s.get("path") or "").lstrip("/").replace("..", "")
        if not path:
            continue
        dest = os.path.join(cwd, path)
        if os.path.exists(dest):
            continue
        try:
            os.makedirs(os.path.dirname(dest) or cwd, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(_stub_content(path, s.get("api") or [], _is_test_subtask(s)))
            written.append(path)
        except Exception:  # noqa: BLE001
            pass
    return written
