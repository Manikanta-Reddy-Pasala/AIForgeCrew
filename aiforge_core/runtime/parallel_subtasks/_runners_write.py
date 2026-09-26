"""What a subtask may write: target paths, file blocks in the reply, the prompt
rules, and writing the files."""
from __future__ import annotations

import os
import re as _re


def _enforce_target_path(worktree: str, path: str) -> None:
    """If ``path`` doesn't exist in ``worktree`` but a file with the same
    basename was created elsewhere (a re-cased/renamed dir), move it to the
    exact ``path`` and prune the now-empty variant dir. Best-effort."""
    import shutil
    target = os.path.join(worktree, path)
    if os.path.exists(target):
        return
    base = os.path.basename(path)
    for root, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in (".git", ".aiforge-worktrees")]
        if base in files:
            src = os.path.join(root, base)
            if os.path.abspath(src) == os.path.abspath(target):
                return
            try:
                os.makedirs(os.path.dirname(target) or worktree, exist_ok=True)
                shutil.move(src, target)
                log.info("path-enforce: moved %s -> %s", src, target)
                # prune an emptied variant dir (e.g. miniLang/ after its one file)
                vdir = os.path.dirname(src)
                if vdir and vdir != worktree and not os.listdir(vdir):
                    os.rmdir(vdir)
            except Exception as exc:  # noqa: BLE001 — enforcement is best-effort
                log.debug("path-enforce move failed %s->%s: %s", src, target, exc)
            return


_FILE_BLOCK_RE = None  # lazy-compiled in _parse_file_blocks


def _marker_path(line: str) -> str | None:
    """The path in a ``=== path ===`` marker line, or None for anything else.

    Split out of :func:`_marker_sections`: deciding whether a line IS a marker
    is a separate question from accumulating bodies, and three levels of `if`
    inside the loop meant a reader had to hold both at once.
    """
    stripped = line.strip()
    if not (stripped.startswith("===") and stripped.endswith("===")
            and len(stripped) > 6):
        return None
    inner = stripped[3:-3].strip().strip("`")
    # An "=" inside the marker means this is a line of content that happens to
    # be wrapped in ===, not a path.
    return inner if inner and "=" not in inner else None


def _marker_sections(text: str) -> list[tuple[str, str]]:
    """``(path, body)`` for every ``=== path ===`` block in ``text``.

    A line scan: a marker is a whole line, so nothing here needs a quantifier
    over the model's entire reply.
    """
    sections: list[tuple[str, str]] = []
    path: str | None = None
    body: list[str] = []
    for line in (text or "").split("\n"):
        marker = _marker_path(line)
        if marker is not None:
            if path is not None:
                sections.append((path, "\n".join(body)))
            path, body = marker, []
        elif path is not None:
            body.append(line)
    if path is not None:
        sections.append((path, "\n".join(body)))
    return sections


def _parse_file_blocks(text: str) -> dict:
    """Parse ``=== path/to/file ===\\n<content>`` blocks (also fenced ``)."""
    blocks: dict = {}
    # === path === markers. Scanned line by line rather than matched with a
    # lazy quantifier + lookahead over the whole reply: that shape is what a
    # scanner flags as a denial-of-service risk, and the markers are lines.
    for path, body in _marker_sections(text):
        # strip a leading ```lang and trailing ``` fence if present
        body_lines = body.split("\n")
        if body_lines and body_lines[0].startswith("```"):
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        if body_lines and body_lines[-1].strip() == "```":
            body_lines.pop()
        body = "\n".join(body_lines).strip()
        if path and body:
            blocks[path] = body + "\n"
    return blocks


def _in_scope(rel: str, globs: list[str]) -> bool:
    """True if relative path ``rel`` matches any allowlist glob.

    Fix 2: delegate to the ONE shared, robust matcher
    (``scope_guard._matches_any``) so parallel and single-doer mode enforce
    IDENTICAL scope semantics (directory globs, ``**``, normalization).
    Soft-fail to allow so a matcher slip never silently drops a legit write.
    """
    try:
        from aiforge_core.runtime import scope_guard as _sg
        return _sg._matches_any(rel, globs)
    except Exception:  # noqa: BLE001 — never crash the parallel runner
        return True

# Test-helper / framework / builtin names that are NOT part of the impl's API —
# a `.assertEqual(...)` or `.push_back(...)` on a stdlib type must not be demanded
# of the unit under test.
_TEST_CALL_NOISE = frozenset({
    # test-framework assertions / lifecycle (called on self / the test object)
    "assert", "asserttrue", "assertfalse", "assertequal", "assertnotequal",
    "assertraises", "assertisnone", "assertisnotnone", "assertin", "assertnotin",
    "assertis", "assertalmostequal", "assertgreater", "assertless", "assertthat",
    "expect", "should", "setup", "teardown", "before", "after", "beforeeach",
    "aftereach", "fail",
    # output / language builtins (never the unit's own API)
    "print", "println", "printf", "format", "fmt",
    # Object/base methods every class already inherits — don't demand them
    "tostring", "hashcode", "equals", "clone", "getclass",
})


def _required_api_from_tests(tests_src: str) -> list:
    """Method/function names the TEST source CALLS — the exact surface the impl
    must expose so the test compiles. Language-agnostic: pulls ``.method(`` calls
    and bare ``Name(`` calls, drops the test-framework/builtin noise. Best-effort;
    caps the list so the prompt stays small."""
    if not tests_src:
        return []
    names: list[str] = []
    seen: set = set()
    # dotted method calls (obj.method(...)) — the class's own API
    for m in _re.finditer(r"\.\s*([A-Za-z_]\w*)\s*\(", tests_src):
        nm = m.group(1)
        if nm.lower() not in _TEST_CALL_NOISE and nm not in seen:
            seen.add(nm)
            names.append(nm)
    return names[:24]


_CPP_EXTS_R = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h", ".c++")


def _lang_rules(path: str) -> str:
    """Language-specific coding rules for the target file that a local model
    reliably gets wrong (injected into the impl prompt). C++ TEMPLATES are the
    big one — a template body in a .cpp causes redefinition / undefined-reference
    link errors (observed: DynamicArray<T> split .h + .cpp → won't build)."""
    pl = (path or "").lower()
    if pl.endswith(_CPP_EXTS_R):
        return ("C++ RULE — any TEMPLATE class/function MUST be fully defined in a "
                "HEADER (declaration + method bodies together in the .hpp/.h); do "
                "NOT put template method bodies in a .cpp (it causes redefinition / "
                "undefined-reference link errors). A single self-contained header "
                "for a templated type is correct.\n\n")
    return ""


_TEST_IS_SPEC = (
    "CRITICAL PRINCIPLE — THE TEST IS THE SPECIFICATION.\n"
    "The test file below is the ABSOLUTE GROUND TRUTH for method/attribute "
    "NAMES (incl. leading underscores like `_is_valid_position`), signatures, "
    "return types, exact VALUES and math. Your code MUST make EVERY assertion "
    "pass. Even if a rule looks unconventional (e.g. the test says an O-piece "
    "is 'cyan' not 'yellow', or score == (level+1)*10), match it EXACTLY — "
    "NEVER 'correct'/'standardize' a value the test asserts. If the test calls "
    "`x._foo(a, b)` you define `_foo(self, a, b)`; if it asserts "
    "`grid[0][3] == COLORS['cyan']` your code must produce cyan.\n\n")

_CONTRACT_RULES = (
    "CONTRACT: expose the PUBLIC API listed for your file in the spec's "
    "'API contract' section EXACTLY (same names, signatures, constants), and "
    "when you import/call another file, use the EXACT names it exposes there. "
    "Do not invent variant names — the other files are written to this same "
    "contract.\n\n"
    "Output ONLY the file(s), each as:\n=== relative/path.ext ===\n"
    "<full file content>\n\nNo prose, no explanation.")


def _tests_block(tests_src: str) -> str:
    """The ground-truth tests, plus the API the test CALLS made explicit.

    Test-first divergence guard: the impl gets the test as ground truth, but a
    local model still MISSES a method the test calls (observed: a Java Stack
    test called a method the impl never defined → uncompilable test).
    """
    if not tests_src:
        return ""
    req = _required_api_from_tests(tests_src)
    req_block = (("REQUIRED API — the test CALLS every one of these; your code "
                  "MUST define ALL of them with matching names/arity (missing "
                  "one = the test won't compile):\n" + ", ".join(req) + "\n\n")
                 if req else "")
    return (_TEST_IS_SPEC + f"TESTS (ground truth):\n{tests_src[:6000]}\n\n"
            + req_block + "---\n\n")


def _subtask_prompt(subtask: dict, spec_md: str, path: str, goal: str) -> str:
    retry_err = str(subtask.get("_retry_error") or "").strip()
    existing = subtask.get("_existing_files")
    return (
        (f"⚠ YOUR PREVIOUS ATTEMPT FAILED with:\n{retry_err[:800]}\nFix that this "
         f"time — re-read the SPEC, emit correct, complete code.\n\n---\n\n"
         if retry_err else "")
        + _tests_block(str(subtask.get("_tests") or "").strip())
        # Language rules for the target file (e.g. C++ templates are header-only).
        + _lang_rules(path)
        + (f"EXISTING PROJECT FILES already on disk (REAL, committed — import from "
           f"these using their EXACT class/function/constant names + signatures; do "
           f"NOT guess or invent variant names):\n{existing}\n\n---\n\n"
           if existing else "")
        + (f"PROJECT SPEC (shared — build YOUR slice to fit it; use the EXACT "
           f"file/dir paths it lists):\n{spec_md.strip()[:5000]}\n\n---\n\n"
           if spec_md and spec_md.strip() else "")
        + "Implement this subtask as COMPLETE, runnable file(s) in the language "
          "the target path implies (.py→Python, .java→Java, .go→Go, .ts→"
          "TypeScript, .c/.cpp→C/C++, .rs→Rust, .sh→shell, …).\n\n"
        + (f"TARGET FILE (emit EXACTLY this path, verbatim — do not re-case or "
           f"rename the directory): {path}\n\n" if path else "")
        + f"SUBTASK: {goal}\n\n" + _CONTRACT_RULES)


def _remap_to_canonical(files: dict, path: str) -> dict:
    """This subtask owns exactly ONE file (``path``), so ignore whatever dir the
    model labelled and force the content to the exact target — the block whose
    basename matches, else the first/only block."""
    base = os.path.basename(path)
    chosen = next((c for p, c in files.items()
                   if os.path.basename(p.strip().lstrip("/")) == base), None)
    return {path: chosen if chosen is not None else next(iter(files.values()))}


def _syntax_rejection(rel: str, content: str) -> str | None:
    """Syntax gate (LANGUAGE-AGNOSTIC): lightweight writes files DIRECTLY (no
    file_write / no syntax_guard), and an isolated subtask worktree has no build
    marker so build-validation is skipped — a truncated/broken file (any
    language) would sail through per-subtask and only blow up at the post-merge
    build/test. The guard must never crash the runner."""
    try:
        from aiforge_core.runtime.syntax_guard import validate_syntax
        ok, err = validate_syntax(rel, content)
        return None if ok else f"{rel}: {err}"
    except Exception:  # noqa: BLE001
        return None


def _inside(worktree: str, rel: str) -> bool:
    """Does ``rel`` actually land inside the worktree?

    Deleting ".." from the string is not containment: "/../etc/passwd.py"
    becomes "/etc/passwd.py", which ``os.path.join`` returns UNCHANGED because
    it is absolute — so a path the MODEL labelled its block with could
    overwrite a file outside the subtask's worktree entirely. Check where it
    lands, not how it looks.
    """
    try:
        root = os.path.realpath(worktree)
        dest = os.path.realpath(os.path.join(worktree, rel))
        return os.path.commonpath([root, dest]) == root
    except (ValueError, OSError):  # different drives / unresolvable
        return False


def _write_subtask_files(files: dict, worktree: str, scope: list):
    """``(written_files, rejected, syntax_error)``.

    A scope allowlist REJECTS writes whose relative path matches no glob, so
    out-of-scope files never land; no allowlist preserves current behaviour.
    """
    written_files: list[str] = []
    rejected: list[str] = []
    for rel, content in files.items():
        rel = rel.lstrip("/").replace("..", "")
        if not _inside(worktree, rel):
            rejected.append(rel)
            continue
        if scope and not _in_scope(rel, scope):
            rejected.append(rel)
            continue
        from ._protected import is_protected, refusal
        if is_protected(worktree, rel):
            log.info("subtask write refused: %s", refusal(rel))
            rejected.append(rel)
            continue
        bad = _syntax_rejection(rel, content)
        if bad:
            return written_files, rejected, bad
        dest = os.path.join(worktree, rel)
        try:
            os.makedirs(os.path.dirname(dest) or worktree, exist_ok=True)
            with open(dest, "w") as f:
                f.write(content)
            written_files.append(rel)
        except OSError:
            continue
    return written_files, rejected, None


# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._worktree import log
