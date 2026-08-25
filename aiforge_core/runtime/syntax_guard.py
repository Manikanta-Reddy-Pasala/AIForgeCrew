"""Cheap, LANGUAGE-AGNOSTIC syntax sniff used before a generated file hits disk.

Goal: catch the model's most common failure mode — half-truncated output — for
whatever language the file is in, so a broken draft never lands and only blows
up much later at the post-merge build/test. Order of preference per file:

1. Python → in-process ``compile()`` (authoritative, no I/O).
2. A language with a cheap NON-EXECUTING syntax checker on PATH — shell
   (``bash -n``), C (``gcc -fsyntax-only``), C++ (``g++ -fsyntax-only``), Java
   (``javac``), Go (``gofmt -e``), JS (``node --check``), Ruby (``ruby -c``),
   PHP (``php -l``). None of these RUN the code; they only parse it.
3. Anything else (or a checker not installed) → the brace-balance truncation
   heuristic (+ a Java/Kotlin Python-kwarg sniff).

Isolation-aware: a subtask builds ONE file, so cross-file references (a missing
sibling header / symbol / package) can't resolve yet — those errors are treated
as PASS; only true SYNTAX errors fail. The real cross-file build runs post-merge.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from aiforge_core.config import languages as _languages

_PAIRS: tuple[tuple[str, str], ...] = (("{", "}"), ("(", ")"), ("[", "]"))
_KWARG_PATTERN = re.compile(r"\b\w+\s*=\s*\w+[\s,]")
_ANNOTATION_PATTERN = re.compile(r"@\w+\s*\(")

# stderr fragments that mean "a reference to another file couldn't resolve"
# (an isolation artifact when validating a single subtask's file), NOT a syntax
# error — treat these as PASS.
_ISOLATION_MARKERS = (
    "no such file", "file not found", "cannot find symbol",
    "does not exist", "undeclared", "unknown type name", "was not declared",
    "has not been declared", "package ", "fatal error:", "undefined reference",
    "cannot find module", "no such module", "expected class or module",
)

# ext → (binary, argv builder). Each command is a SYNTAX-ONLY / parse check that
# never executes the file. ``-x`` forces the language for header files.
#
# Primary per-extension entries are SOURCED from the language registry
# (aiforge_core/config/languages) — shell (bash -n), C (gcc -fsyntax-only),
# C++ (g++ -fsyntax-only), Java (javac -d). Two kinds of entry can't be
# expressed by a profile's single ``syntax_check`` and stay as literals below:
#   1. Header files (.h / .hpp) force their language via ``-x`` (the profile's
#      generic gcc/g++ form is overridden here).
#   2. Languages not modelled first-class in the registry — Go, JS, Ruby, PHP.
# Rust / Python / Kotlin expose no external checker (syntax_check is None →
# skipped), so they fall back to compile() / the brace-balance heuristic.
_CHECKERS: dict[str, tuple[str, object]] = {}
for _prof in _languages.all_profiles():
    if _prof.syntax_check:
        for _ext in _prof.extensions:
            _CHECKERS[_ext] = _prof.syntax_check
_CHECKERS.update({
    ".h":   ("gcc",   lambda b, f: [b, "-fsyntax-only", "-x", "c", f]),
    ".hpp": ("g++",   lambda b, f: [b, "-fsyntax-only", "-x", "c++", f]),
    ".go":  ("gofmt", lambda b, f: [b, "-e", f]),
    ".js":  ("node",  lambda b, f: [b, "--check", f]),
    ".mjs": ("node",  lambda b, f: [b, "--check", f]),
    ".rb":  ("ruby",  lambda b, f: [b, "-c", f]),
    ".php": ("php",   lambda b, f: [b, "-l", f]),
})


def _last_err_line(err: str) -> str:
    lines = [ln for ln in err.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "syntax error"


def _external_syntax(path: str, content: str) -> tuple[bool, str] | None:
    """Run the language's non-executing syntax checker. Returns ``(ok, err)``,
    or ``None`` when no checker applies / the tool isn't installed (→ caller
    falls back to the brace-balance heuristic)."""
    ext = os.path.splitext(path)[1].lower()
    spec = _CHECKERS.get(ext)
    if not spec:
        return None
    binary, argfn = spec
    if not shutil.which(binary):
        return None
    base = os.path.basename(path) or ("f" + ext)
    d = tempfile.mkdtemp(prefix="synchk-")
    try:
        fp = os.path.join(d, base)      # keep basename: javac needs ClassName.java
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write(content)
        try:
            proc = subprocess.run(argfn(binary, fp), capture_output=True,
                                  text=True, timeout=25)
        except Exception:  # noqa: BLE001
            return None
        if proc.returncode == 0:
            return True, ""
        err = (proc.stderr or proc.stdout or "")
        low = err.lower()
        if any(m in low for m in _ISOLATION_MARKERS):
            return True, ""             # cross-file ref, not a syntax error
        return False, f"{ext.lstrip('.')} syntax: {_last_err_line(err)}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _fallback_syntax_check(path, content):
    """Brace-balance truncation heuristic (+ a Java/Kotlin python-kwargs sniff) for languages with no installed syntax checker. Returns (ok, error_msg)."""
    # 3. Fallback — brace-balance truncation heuristic (+ Java/Kotlin kwarg sniff)
    #    for languages with no installed checker.
    for opener, closer in _PAIRS:
        n_open = content.count(opener)
        n_close = content.count(closer)
        if n_open != n_close:
            return False, f"unbalanced {opener}{closer} ({n_open} vs {n_close})"

    if path.endswith((".java", ".kt")):
        if _KWARG_PATTERN.search(content) and "(" in content:
            if not _ANNOTATION_PATTERN.search(content):
                return False, "java/kotlin: looks like Python-style kwargs in call"

    return True, ""


def validate_syntax(path: str, content: str) -> tuple[bool, str]:
    """Return ``(ok, error_msg)`` — empty error on the happy path. Language-aware
    (see module docstring). Best-effort: a missing toolchain degrades to the
    brace-balance truncation heuristic, never a false reject."""
    if not content or not content.strip():
        return False, "empty file content"

    # 1. Python — in-process compile() is authoritative and needs no toolchain.
    #    (Don't brace-count Python: delimiters inside string literals/comments —
    #    common in lexer/parser tests like ``assert scan("(")`` — aren't real.)
    if path.endswith(".py") or path.endswith(".pyi"):
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            return False, f"python syntax: {exc.msg} at line {exc.lineno}"
        return True, ""

    # 2. A real per-language syntax checker (shell/C/C++/Java/Go/JS/Ruby/PHP).
    ext_res = _external_syntax(path, content)
    if ext_res is not None:
        return ext_res

    return _fallback_syntax_check(path, content)


# Code-file extensions, sourced from the language registry (DRY — same set the
# syntax checkers cover), plus the compile()/heuristic-only languages that expose
# no external checker but are still code.
_CODE_EXTS = frozenset(
    [ext for _p in _languages.all_profiles() for ext in _p.extensions]
    + [".py", ".rs", ".kt", ".ts", ".tsx", ".jsx", ".vue", ".go", ".rb", ".php"])


def _line_cap() -> int:
    """~500-line hard cap on a single file (env AIFORGE_MAX_FILE_LINES; <=0
    disables the nudge)."""
    try:
        return int(os.environ.get("AIFORGE_MAX_FILE_LINES", "500"))
    except (TypeError, ValueError):
        return 500


def oversize_warning(path: str, content: str) -> str:
    """A soft, NON-BLOCKING nudge when a written CODE file exceeds the line cap —
    the standing DRY / KISS / separation-of-concerns rule: split a growing file
    into concern-grouped modules. Returns '' when within the cap, for a non-code
    file, or when disabled. Never raises (a nudge must not break a write)."""
    try:
        cap = _line_cap()
        if cap <= 0:
            return ""
        if os.path.splitext(path)[1].lower() not in _CODE_EXTS:
            return ""
        n = (content or "").count("\n") + 1
        if n <= cap:
            return ""
        return (f"file is {n} lines (over the {cap}-line cap) — split into "
                f"concern-grouped modules (DRY / KISS / separation of concerns)")
    except Exception:  # noqa: BLE001
        return ""


__all__ = ["validate_syntax", "oversize_warning"]
