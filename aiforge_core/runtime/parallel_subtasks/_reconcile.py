"""Reconcile/rewrite/self-heal: steering, patches, stubs, drift, integration.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import subprocess
import threading

from pydantic import BaseModel

from aiforge_core.runtime import review_gates
from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS, ensure_artifact_gitignore

def _reconcile_rounds() -> int:
    try:
        return max(0, min(16, int(os.environ.get("AIFORGE_RECONCILE_ROUNDS", "12"))))
    except ValueError:
        return 12


def _escalation_model() -> "str | None":
    """The model to hand a STUCK residual to (F: escalation ladder). Priority:
    1. AIFORGE_ESCALATION_MODEL (explicit),
    2. a stronger reasoning role's configured model (reasoner/reviewer/critic/
       architect) when it DIFFERS from the reconcile default — so a deploy that
       assigns a bigger model to reasoning roles auto-escalates without extra
       env. Returns None when no distinct stronger model exists (nothing to
       escalate to — don't pretend). Config-driven, no hardcoded model id."""
    env = os.environ.get("AIFORGE_ESCALATION_MODEL", "").strip()
    if env:
        return env
    try:
        from aiforge_core.config import agent_config as _ac
        cfg = _ac.load_all() or {}
        base = ((cfg.get("doer") or {}).get("model")
                or (cfg.get("_default") or {}).get("model") or "").strip()
        for role in ("reasoner", "reviewer", "critic", "architect", "planner"):
            m = ((cfg.get(role) or {}).get("model") or "").strip()
            if m and m != base:
                return m
    except Exception:  # noqa: BLE001
        pass
    return None


def _collect_run_output(res: dict) -> str:
    """Gather the run output from ANY key project_runner might use — a compiled
    build puts the compile error under stdout/stderr, not just output/error."""
    return "\n".join(str(res.get(k) or "") for k in
                     ("error", "output", "stdout", "stderr", "logs", "details",
                      "message")).strip()


def _raw_build_test_output(cwd: str, stacks: list) -> str:
    """Fallback: run the toolchain test/build command directly, capturing COMBINED
    stdout+stderr, so a maven/gradle compile error is never lost to the reconciler."""
    if os.path.exists(os.path.join(cwd, "pom.xml")):
        cmd = ["mvn", "-q", "test"]
    elif (os.path.exists(os.path.join(cwd, "build.gradle"))
          or os.path.exists(os.path.join(cwd, "build.gradle.kts"))):
        cmd = ["gradle", "test", "-q", "--console=plain"]
    else:
        return ""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=300)
        return f"{p.stdout or ''}\n{p.stderr or ''}".strip()
    except Exception:  # noqa: BLE001
        return ""


def _project_test_output(cwd: str) -> tuple[bool, str]:
    """Run the project's tests and return ``(ok, raw_output)`` — the RAW build/
    test output (not the formatted report), so the reconciler sees exact errors.
    ``ok`` True when there's no project / no tests (nothing to reconcile)."""
    try:
        from aiforge_core.runtime.integration_report import run_bare_python_tests
        # PREFER the managed-venv pytest for any Python tree with tests: it
        # pip-installs the third-party deps (pygame, numpy, …) that a plain
        # project(action=test) misses — otherwise pytest fails to import and the
        # captured output is EMPTY, so the reconciler gets no errors to act on.
        bare = run_bare_python_tests(cwd)
        if bare is not None:
            return bare
        from aiforge_core.runtime.tools.project_runner import (
            _has_tests, detect, project,
        )
        stacks = (detect(cwd) or {}).get("stacks") or []
        if not stacks:
            return True, ""
        action = "test" if _has_tests(cwd, stacks) else "build"
        res = project(action=action, cwd=cwd) or {}
        out = _collect_run_output(res)
        # A compiled-language build (maven/gradle) can fail with the error under
        # stdout/stderr, not `output`/`error` — if we lost it, re-run the raw
        # command capturing combined output so the reconciler sees the compile
        # error (else it thinks 0 are failing and gives up).
        if not res.get("ok") and not out.strip():
            out = _raw_build_test_output(cwd, stacks) or out
        _ok = bool(res.get("ok"))
        # LINT/TYPECHECK gate (multi-language, native tools) — when the build+test
        # otherwise passes, run each stack's own static checker (tsc/go vet/clippy/
        # ruff) for real bugs the tests may miss. Compiled langs are already
        # typechecked by their build, so this mostly covers the loose/interpreted
        # ones. Any missing tool is skipped.
        if _ok:
            try:
                from aiforge_core.runtime.integration_report import run_static_checks
                _lok, _lout = run_static_checks(cwd)
                if not _lok:
                    _ok = False
                    out = (out + _lout)
            except Exception:  # noqa: BLE001
                pass
        return _ok, out
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _route_steering(txt: str, subs: list) -> dict:
    """Classify a mid-run user comment against the running subtasks. Returns
    ``{"target": <slug|"global"|"new">, "note": <=15-word plan}``. LLM-classified
    with a keyword-overlap fallback, so a comment about one file lands on that
    subtask, a whole-build change goes global, and a brand-new ask is flagged new."""
    slugs = {s.get("slug") for s in subs if s.get("slug")}
    idx = "\n".join(f"- {s.get('slug')}: {s.get('path','')} — "
                    f"{(s.get('goal') or '')[:70]}" for s in subs)
    try:
        from aiforge_core.llm.structured import structured_complete

        class _SteerTarget(BaseModel):
            target: str
            note: str = ""

        prompt = (
            f"A build is running with these subtasks:\n{idx}\n\n"
            f"The user just commented mid-run:\n\"{txt[:500]}\"\n\n"
            f"Classify the comment. Reply ONE line of JSON only:\n"
            f'{{"target":"<a slug above, or global, or new>","note":"<what to do, <=15 words>"}}\n'
            f"Rules: target=<slug> if it's about that ONE subtask/file; "
            f"global if it changes the whole spec/all subtasks; "
            f"new if it's a requirement not covered by any subtask.")
        d = structured_complete(
            "chat", [{"role": "user", "content": prompt}], _SteerTarget,
            max_retries=1)
        target = (d.target or "").strip()
        note = (d.note or "").strip()[:120]
        if target in slugs or target in ("global", "new"):
            return {"target": target, "note": note}
    except Exception:  # noqa: BLE001
        pass
    # Fallback: match the comment to a subtask by path/goal token overlap.
    low = txt.lower()
    best, score = None, 0
    for s in subs:
        toks = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}",
                          f"{s.get('path','')} {s.get('goal','')}".lower())
        hits = sum(1 for t in set(toks) if t in low)
        if hits > score:
            best, score = s.get("slug"), hits
    return {"target": best if score >= 1 else "global", "note": ""}


def _is_hard_residual(output: str) -> bool:
    """True when the failing output is a CROSS-FILE structural mismatch (a bad
    import, a wrong signature, a missing attribute/name) — the class the reasoning
    model resolves better than repeated coder patches. Drives early escalation."""
    return bool(re.search(
        r"cannot import name|no module named|has no attribute|is not defined|"
        r"unexpected keyword argument|missing \d+ required (?:positional |keyword )?"
        r"argument|takes \d+ positional argument|"
        r"ImportError|ModuleNotFoundError|AttributeError|NameError|"
        r"cannot find symbol|package .* does not exist",  # java
        output or "", re.I))


def _directed_hints(output: str) -> list[str]:
    """Turn common cross-file link errors — in ANY language — into CONCRETE,
    actionable fixes. The difference between the reconciler editing files vs
    narrating a plan. Covers Python / Java / Kotlin / Go / C / C++ / Node / Rust /
    TS — whichever produced the failing build/test output."""
    hints: list[str] = []

    def add(h: str) -> None:
        hints.append(h)

    # ── Python ──────────────────────────────────────────────────────────
    for name, mod in re.findall(r"cannot import name ['\"](\w+)['\"] from ['\"]([\w.]+)['\"]", output):
        add(f"`{mod}` is missing `{name}`: open it, see what it ACTUALLY defines, "
            f"then add/rename to `{name}` there OR fix the import to the real name.")
    for mod in re.findall(r"No module named ['\"]([\w.]+)['\"]", output):
        add(f"module `{mod}` is imported but missing — create it or fix the path.")
    for mod, attr in re.findall(r"module ['\"]?([\w.]+)['\"]? has no attribute ['\"](\w+)['\"]", output):
        add(f"`{mod}` has no `{attr}` — define it there or fix the caller.")
    for name in re.findall(r"NameError: name ['\"](\w+)['\"] is not defined", output):
        add(f"`{name}` is used but never defined/imported — add it.")
    # ── Java / Kotlin ───────────────────────────────────────────────────
    for sym in re.findall(r"cannot find symbol[^\n]*?symbol:\s*\w+\s+(\w+)", output):
        add(f"Java: symbol `{sym}` not found — it's referenced but not "
            f"defined/imported with that exact name; reconcile the two sides.")
    for pkg in re.findall(r"package ([\w.]+) does not exist", output):
        if pkg.startswith("javax."):
            add(f"Java: `{pkg}` not found — this project is on Spring Boot 3 / "
                f"Jakarta, so change EVERY `javax.` import to `jakarta.` "
                f"(e.g. javax.persistence→jakarta.persistence, "
                f"javax.validation→jakarta.validation) across all files. Keep the "
                f"pom's Spring Boot version and the imports consistent.")
        elif pkg.startswith("jakarta."):
            add(f"Java: `{pkg}` not found — add the matching starter dependency to "
                f"the build file (e.g. spring-boot-starter-data-jpa / "
                f"-validation), or the code/pom versions disagree — align them.")
        else:
            add(f"Java: package `{pkg}` doesn't exist — fix the import to the real "
                f"package, or add its dependency to the build file.")
    # ── Go ──────────────────────────────────────────────────────────────
    for sym in re.findall(r"undefined:\s*([\w.]+)", output):
        add(f"Go: `{sym}` is undefined — define it or fix the reference to the "
            f"real identifier (one canonical name across files).")
    # ── C / C++ ─────────────────────────────────────────────────────────
    for sym in re.findall(r"undefined reference to [`']?([\w:]+)", output):
        add(f"C/C++: undefined reference to `{sym}` — the declaration and "
            f"definition disagree, or the defining file isn't linked/named right.")
    for sym in re.findall(r"['\"]?(\w+)['\"]? was not declared", output):
        add(f"C/C++: `{sym}` not declared — include the right header / fix the name.")
    for sym in re.findall(r"no member named ['\"](\w+)['\"]", output):
        add(f"C/C++: no member `{sym}` — align the struct/class with its usage.")
    # ── Node / JS / TS ──────────────────────────────────────────────────
    for mod in re.findall(r"Cannot find module ['\"]([^'\"]+)['\"]", output):
        add(f"JS/TS: cannot find module `{mod}` — fix the import path or create it.")
    for name in re.findall(r"(\w+) is not defined", output):
        add(f"JS: `{name}` is not defined — import/define it (one canonical name).")
    for name in re.findall(r"(\w+) is not a function", output):
        add(f"JS: `{name}` is not a function — the export/shape disagrees with the call.")
    # ── Rust ────────────────────────────────────────────────────────────
    for sym in re.findall(r"cannot find (?:value|function|type) `(\w+)` in", output):
        add(f"Rust: `{sym}` not found in scope — define it or fix the `use`/name.")
    for imp in re.findall(r"unresolved import `([\w:]+)`", output):
        add(f"Rust: unresolved import `{imp}` — fix the module path or add the item.")
    # ── missing attribute / method (Python) — incl. hasattr() assertions ─
    for cls, attr in re.findall(r"'(\w+)' object has no attribute '(\w+)'", output):
        add(f"class `{cls}` is missing `{attr}` — ADD that attribute/method to "
            f"`{cls}` (a caller/test needs it; check the test for the expected "
            f"type/behaviour).")
    for attr in re.findall(r"hasattr\([^,]+,\s*['\"](\w+)['\"]\)", output):
        add(f"a test asserts an object HAS `{attr}` but it doesn't — open the "
            f"test to see which class is built, then add attribute/method `{attr}` "
            f"to that class (initialise it in __init__ / implement the method).")
    for name, attr in re.findall(r"module '([\w.]+)' has no attribute '(\w+)'", output):
        add(f"`{name}` is missing top-level `{attr}` — define it there.")
    # ── shared state leaking across tests (in-memory store not reset) ───────
    if re.search(r"(ValueError|Exception|IntegrityError)[^\n]*already exists", output) \
            or re.search(r"errors?\b.*\n.*already exists", output, re.I) \
            or (re.search(r"\d+\s+errors?\b", output) and "already exists" in output):
        add("Tests ERROR with 'already exists' (not a plain assert-fail): the "
            "in-memory store/DB KEEPS STATE BETWEEN TESTS, so the 2nd test's "
            "setup collides with the 1st. Reset it between tests — add a pytest "
            "AUTOUSE fixture that calls the store's reset()/clear() (and the "
            "auth/token store) before each test, e.g. "
            "`@pytest.fixture(autouse=True)\\ndef _reset(): store.reset(); yield`. "
            "Put it in conftest.py or each test module. This fixes a whole BATCH "
            "of errors at once.")
    # ── response/dict missing an expected key ───────────────────────────────
    for key in re.findall(r"KeyError:\s*['\"](\w+)['\"]", output):
        add(f"KeyError `{key}`: the response/dict the test reads is MISSING key "
            f"`{key}` — the handler must include `{key}` in what it returns "
            f"(e.g. `return jsonify({{'{key}': ...}})`); read the test to see the "
            f"exact expected shape.")
    # ── HTTP / web-framework status mismatches (Flask/FastAPI/Django/etc.) ──
    # These fall through the generic "assert X==Y" rule below with no root-cause,
    # so a local model patches blindly. Name the ACTUAL cause per status code.
    if re.search(r"assert 308 ==|308 PERMANENT REDIRECT|\b308\b.*[Rr]edirect", output):
        add("HTTP 308 (permanent redirect) where the test expects a real status: "
            "the route's TRAILING SLASH doesn't match the request path. The app "
            "defines the route WITH a trailing slash (e.g. `/books/`) but the test "
            "calls it WITHOUT (`/books`), so the framework 308-redirects. FIX in "
            "the ROUTES: define the collection route to match the test exactly "
            "(drop the trailing slash), or set `strict_slashes=False` (Flask: on "
            "the blueprint/route; or `app.url_map.strict_slashes=False`). Do NOT "
            "change the tests — align the routes to the paths the tests call.")
    if re.search(r"assert 405 ==|405 METHOD NOT ALLOWED", output):
        add("HTTP 405 (method not allowed): the route exists but doesn't accept "
            "the HTTP method the test uses — add the method to the route's "
            "`methods=[...]` (e.g. POST/PUT/DELETE) on the correct path.")
    if re.search(r"assert 404 ==\s*[2344]\d\d|404 NOT FOUND", output):
        add("HTTP 404 where a real status is expected: the route the test calls "
            "isn't MATCHING. Check, in order: (1) ID-TYPE MISMATCH — if a route "
            "uses a typed converter like `<int:id>` but the store assigns a "
            "different type (e.g. `uuid.uuid4()` strings, or vice-versa), the URL "
            "never matches → 404. Make the id type CONSISTENT across models + "
            "store + route converter (all int, or use `<string:id>`/no converter). "
            "This is the #1 cause of 404 on nested resources like "
            "/projects/<pid>/tasks/<tid>. (2) a missing blueprint url_prefix or a "
            "singular/plural path mismatch. (3) the lookup returns None because "
            "the item was stored under a different key than it's fetched by.")
    if re.search(r"assert 5\d\d ==|500 INTERNAL SERVER ERROR", output):
        add("HTTP 5xx where a client status is expected: the handler raises before "
            "returning — read the traceback in the body/log, fix the unhandled "
            "error (often a missing key, None deref, or wrong return shape).")
    # ── assertion VALUE mismatches — a logic bug to fix in the impl ──────
    for got, exp in re.findall(r"assert (\S{1,40}) == (\S{1,40})", output):
        add(f"assertion `{got} == {exp}` failed — the impl returns the wrong "
            f"value; fix the logic so it produces what the test expects.")
    # ── generic call/type mismatches (any language) ─────────────────────
    for typ, msg in re.findall(r"(TypeError|AttributeError|incompatible types)[:\s]+([^\n]{0,110})", output):
        add(f"{typ}: {msg.strip()} — align the call site with the definition.")

    # de-dup, keep order
    seen: set = set()
    uniq = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq[:20]


_SRC_EXTS = (".py", ".java", ".kt", ".go", ".c", ".cc", ".cpp", ".cxx", ".h",
             ".hpp", ".js", ".mjs", ".ts", ".tsx", ".rs", ".rb", ".php", ".sh",
             ".toml", ".cfg", ".json")


def _gather_sources(cwd: str) -> list[tuple[str, str]]:
    """Every source file in the tree (ANY language), for the reconciler's
    rewrite context. Excludes deps/artifacts/venvs. Returns [(relpath, content)]."""
    out: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".aiforge-venv", ".venv", "venv",
            "__pycache__", "node_modules", "target", "build", "dist",
            ".pytest_cache", _CONTRACT_DIR)]
        for f in files:
            if f.endswith(_SRC_EXTS):
                p = os.path.join(root, f)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        out.append((os.path.relpath(p, cwd), fh.read()))
                except Exception:  # noqa: BLE001
                    pass
    return out


def _spec_goal(cwd: str) -> str:
    """The ORIGINAL GOAL from SPEC.md — re-stated at the top of the merger prompt
    to anchor the model's attention on the primary objective."""
    try:
        p = os.path.join(cwd, "SPEC.md")
        if os.path.isfile(p):
            import re as _re
            src = open(p, encoding="utf-8", errors="replace").read()
            m = _re.search(r"##\s*Goal\s*(.+?)(?:\n##\s|\Z)", src, _re.DOTALL)
            if m:
                return m.group(1).strip()[:2000]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _files_in_output(cwd: str, output: str) -> set:
    """Source files REFERENCED in the failing test/build output — minimal,
    targeted context for the resolver (not the whole tree)."""
    import re as _re
    hits: set = set()
    for m in _re.findall(r"([\w./\\-]+\.(?:py|java|go|js|mjs|ts|tsx|rs|c|cc|cpp|cxx|h|hpp|rb|php))", output):
        p = m.replace("\\", "/")
        if os.path.isabs(p) and p.startswith(cwd):
            p = os.path.relpath(p, cwd)
        p = p.lstrip("./")
        if os.path.isfile(os.path.join(cwd, p)):
            hits.add(p)
    return hits


def _py_local_imports(cwd: str, rel: str) -> set:
    """Local module files a Python file imports (1 hop) — so the resolver sees
    both sides of a cross-file mismatch, still minimal."""
    import ast as _ast
    out: set = set()
    try:
        with open(os.path.join(cwd, rel), encoding="utf-8", errors="replace") as fh:
            tree = _ast.parse(fh.read())
    except Exception:  # noqa: BLE001
        return out
    mods: set = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[-1])
        elif isinstance(node, _ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[-1])
    for base in mods:
        fn = base + ".py"
        for root, _d, files in os.walk(cwd):
            if fn in files:
                r = os.path.relpath(os.path.join(root, fn), cwd)
                if ".aiforge" not in r:
                    out.add(r)
    return out


def _relevant_files(cwd: str, output: str) -> list:
    """Files the resolver needs: the ones named in the errors + their direct
    local imports. Falls back to the whole tree only if nothing was parsed."""
    seed = _files_in_output(cwd, output)
    if not seed:
        return _gather_sources(cwd)
    # BFS the local-import graph up to 2 hops from the failing files (test →
    # module → its deps), capped, so the resolver sees the whole failing chain
    # but never the whole tree.
    picked = set(seed)
    frontier = set(seed)
    for _ in range(2):
        nxt: set = set()
        for rel in frontier:
            if rel.endswith(".py"):
                nxt |= _py_local_imports(cwd, rel)
        nxt -= picked
        if not nxt or len(picked) >= 15:
            break
        picked |= nxt
        frontier = nxt
    out = []
    for rel in sorted(picked):
        try:
            with open(os.path.join(cwd, rel), encoding="utf-8", errors="replace") as fh:
                out.append((rel, fh.read()))
        except Exception:  # noqa: BLE001
            pass
    return out


_PATCH_RE = re.compile(
    r"<<<<<<< SEARCH\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>> REPLACE", re.DOTALL)
_FILE_HDR_RE = re.compile(r"^###\s*FILE:\s*(.+?)\s*$", re.MULTILINE)


def _apply_patches(cwd: str, out: str) -> tuple[list, list]:
    """Deterministic Search-and-Replace applier (zero-LLM). Parses `### FILE:`
    headers + `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks, verifies each
    SEARCH matches the file character-for-character, swaps it, syntax-checks, and
    writes. Surgical: fixing one test can't rewrite an unrelated section. Returns
    (written_files, failures[(file, why)])."""
    written: list = []
    failures: list = []
    heads = [(m.start(), m.group(1).strip()) for m in _FILE_HDR_RE.finditer(out)]
    if not heads:
        return written, [("", "no ### FILE headers")]
    for i, (pos, rel) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(out)
        seg = out[pos:end]
        rel = rel.lstrip("/").replace("..", "")
        fp = os.path.join(cwd, rel)
        if not os.path.isfile(fp):
            failures.append((rel, "file not found"))
            continue
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception:  # noqa: BLE001
            continue
        orig = content
        for search, replace in _PATCH_RE.findall(seg):
            if search in content:
                content = content.replace(search, replace, 1)
            else:
                failures.append((rel, "SEARCH block not found (indent/char mismatch)"))
        if content == orig:
            continue
        try:
            from aiforge_core.runtime.syntax_guard import validate_syntax
            ok, _ = validate_syntax(rel, content)
            if not ok:
                failures.append((rel, "syntax broke after patch"))
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(fp, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(rel)
        except Exception:  # noqa: BLE001
            pass
    return written, failures


def _rewrite_fix(cwd: str, output: str, hints: list[str], *,
                 model: str | None = None, audit_tests: bool = False) -> list[str]:
    """Minimal-context PATCH resolver (Git-state model, NOT a whole-tree blackboard):
    feed ONLY the files referenced in the failing output + their direct imports,
    with the errors, and have the LLM OUTPUT the corrected files (=== path ===
    blocks). Syntax-check + write each. Returns paths written. Language/usecase-
    agnostic; no task-specific logic. Keeps context small so a local model
    doesn't blow its window / hallucinate."""
    from aiforge_core.llm.client import complete as _complete
    try:
        budget = int(os.environ.get("AIFORGE_RECONCILE_CTX_CHARS", "40000"))
    except ValueError:
        budget = 40000
    # Fence each file (### FILE: path + ```) so the model reads it as DATA, with
    # clear boundaries — no blurred walls of concatenated text.
    parts: list[str] = []
    total = 0
    for rel, content in _relevant_files(cwd, output):
        block = f"### FILE: {rel}\n```\n{content}\n```"
        if total + len(block) > budget:
            continue
        parts.append(block)
        total += len(block)
    hint_str = "\n".join(f"- {h}" for h in hints)
    goal = _spec_goal(cwd)
    # Aider tree-sitter REPO MAP — ranked symbols across the WHOLE repo, so the
    # fixer can locate a class/method/constant the failing test needs that isn't in
    # the failing-file 2-hop chain above (the #1 minimal-context gap: the wanted
    # symbol lives in a file the resolver didn't pull). Cached (persistent index)
    # so it's cheap. Bounded. Off with AIFORGE_RECONCILE_REPOMAP=0.
    repomap = ""
    if os.environ.get("AIFORGE_RECONCILE_REPOMAP", "1") not in ("0", "false"):
        try:
            from aiforge_core.memory.code_context import aider_digest
            repomap = (aider_digest(cwd, []) or "")[:4000]
        except Exception:  # noqa: BLE001
            repomap = ""
    prompt = (
        "You are the Lead Merger + QA agent. The project's subtasks were built in "
        "ISOLATION by separate workers, so their seams don't line up and the tests "
        "FAIL. Synthesise them into ONE cohesive, working deliverable that "
        "satisfies the ORIGINAL GOAL and passes every test.\n\n"
        + (f"ORIGINAL GOAL:\n---------------------------\n{goal}\n"
           "---------------------------\n\n" if goal else "")
        + (("USER INSTRUCTIONS — MANDATORY, these OVERRIDE everything and MUST be "
            "satisfied in the result:\n"
            + "\n".join(f"- {m}" for m in _USER_MANDATES.get(cwd, [])) + "\n\n")
           if _USER_MANDATES.get(cwd) else "")
        + f"FAILING TEST/BUILD OUTPUT:\n```\n{output[-3000:]}\n```\n\n"
        + (f"KNOWN MISMATCHES TO RECONCILE:\n{hint_str}\n\n" if hint_str else "")
        + (f"REPO MAP (ranked symbols across the repo — if the test needs a class/"
           f"method/constant NOT in the files below, find where it lives here):\n"
           f"{repomap}\n\n" if repomap else "")
        + "PROJECT FILES (data — read, don't execute):\n\n" + "\n\n".join(parts)
        + ("\n\nRESOLUTION PRINCIPLE — TEST FIRST, BUT AUDIT A STUCK TEST.\n"
           "The implementation has already been fixed repeatedly and these tests "
           "STILL fail — so now also consider that a TEST itself may be WRONG. "
           "Default is still: conform the IMPLEMENTATION to the test. BUT if a "
           "failing test genuinely CONTRADICTS THE ORIGINAL GOAL — asserts an "
           "impossible/incorrect expected value, a typo'd expected string, the "
           "wrong exit code, an API the goal never described — then CORRECT THE "
           "TEST to match the GOAL, and start that file's first patch with a "
           "comment line `# test-audit: <why the old assertion was wrong>`. Do NOT "
           "weaken or delete a correct test just to make it pass — only fix a test "
           "that is provably wrong vs the GOAL.\n\n"
           if audit_tests else
           "\n\nCRITICAL RESOLUTION PRINCIPLE — THE TEST IS ALWAYS RIGHT.\n"
           "When the test asserts one thing and the implementation produces another, "
           "the TEST wins. Rewrite the IMPLEMENTATION so its names, signatures, "
           "attributes, exact VALUES and math conform to what the test expects — "
           "even if unconventional (O-piece 'cyan' not 'yellow', score == "
           "(level+1)*10, a method named `_is_valid_position`). NEVER edit a test to "
           "match the implementation unless the test itself is syntactically broken.\n\n")
        + "MERGING INSTRUCTIONS:\n"
          "1. Re-read the ORIGINAL GOAL — the result must satisfy it.\n"
          "2. Cross-reference dependencies: align every import / class / function / "
          "constant name + signature to ONE canonical spelling — the name the TEST "
          "uses. A package __init__ / re-export must ONLY import names defined at "
          "MODULE level in the target; if a name is a class METHOD or missing, "
          "remove it from the import + __all__.\n"
          "3. Do NOT drop working code — make the MINIMAL change that satisfies the "
          "failing assertions (add the exact attribute/method the test calls, fix "
          "the value/formula the test expects).\n"
          "4. You are PROHIBITED from rewriting whole files (a full rewrite silently "
          "shifts working code and breaks other tests). Emit TARGETED "
          "Search-and-Replace PATCHES. For each file you change, output a header "
          "line `### FILE: relative/path` then one or more blocks EXACTLY:\n"
          "<<<<<<< SEARCH\n<the exact existing lines to change — character-for-"
          "character incl. indentation>\n=======\n<the corrected lines>\n"
          ">>>>>>> REPLACE\n"
          "The SEARCH text MUST appear verbatim in the current file. Keep each "
          "SEARCH block small (the few lines around the defect). Output ONLY the "
          "`### FILE:` headers + SEARCH/REPLACE blocks — no whole files, no ``` "
          "fences, no prose.")
    try:
        mt = max(4096, int(os.environ.get("AIFORGE_LLM_MAX_TOKENS", "8192")))
    except ValueError:
        mt = 8192
    # Model override (escalation): when the primary reconciler stalls, the caller
    # passes a different model (e.g. a reasoning model) for the residual failures
    # it can't crack. Delivered via `extras={"model": …}` which overrides the
    # role's default in the request body — general, no per-problem code.
    _extras = {"model": model} if model else None
    _temp = None
    _sys = ("You are a Targeted Code Patch Engine. Output ONLY ### FILE headers + "
            "<<<<<<< SEARCH/======= />>>>>>> REPLACE blocks, nothing else. Never "
            "rewrite a whole file.")
    if model:
        # Reasoning/escalation models can't reliably reproduce a char-perfect
        # SEARCH block (their patches get rejected). Have them output the whole
        # corrected file instead — they GENERATE better than they patch; the
        # regression guard keeps the rewrite only if it reduces failures.
        prompt += ("\n\nOVERRIDE — IGNORE the SEARCH/REPLACE format above. Output "
                   "each CHANGED file IN FULL, each as:\n=== relative/path ===\n"
                   "<the complete corrected file>\nNo SEARCH/REPLACE blocks, no ``` "
                   "fences, no prose. Fix the ROOT CAUSE of the failing tests.")
        _sys = ("You are a senior engineer fixing failing tests. Output ONLY the "
                "changed files, each as `=== path ===` then the full corrected "
                "file. No prose, no fences.")
    if model:
        # An escalation (reasoning) model may be loaded at a smaller context — cap
        # completion so prompt+completion fit; its fixes are targeted anyway.
        try:
            mt = min(mt, int(os.environ.get("AIFORGE_ESCALATION_MAX_TOKENS", "2560")))
        except ValueError:
            mt = 2560
        # Apply the ESCALATION model's own sampling params (the role's ep.model —
        # qwen — is overridden via extras, so the client's quirk lookup would use
        # the wrong model). Reasoning models want their pinned temperature.
        try:
            from aiforge_core.config import model_overrides as _mo
            _ov = _mo.lookup(model)
            if _ov and _ov.get("temperature") is not None:
                _temp = _ov["temperature"]
        except Exception:  # noqa: BLE001
            pass
    out = _complete("doer", [
        {"role": "system", "content": _sys},
        {"role": "user", "content": prompt}],
        max_tokens=mt, temperature=_temp, extras=_extras) or ""
    written, failures = _apply_patches(cwd, out)
    if not written and failures:
        # Fallback: the model may have ignored the patch format and emitted whole
        # `=== path ===` files — accept those (syntax-checked) so a round isn't lost.
        for rel, content in _parse_file_blocks(out).items():
            rel = rel.lstrip("/").replace("..", "")
            if not rel or not content.strip():
                continue
            try:
                from aiforge_core.runtime.syntax_guard import validate_syntax
                _ok, _ = validate_syntax(rel, content)
                if not _ok:
                    continue
            except Exception:  # noqa: BLE001
                pass
            dest = os.path.join(cwd, rel)
            try:
                os.makedirs(os.path.dirname(dest) or cwd, exist_ok=True)
                with open(dest, "w", encoding="utf-8") as fh:
                    fh.write(content)
                written.append(rel)
            except Exception:  # noqa: BLE001
                pass
    return written


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


def _python_stub(api: list) -> str:
    """Real Python signature stubs from the API contract — keeps sibling imports
    resolvable while workers fill in bodies. Conservative: only clear top-level
    class/def/const forms; anything ambiguous becomes a module-level name = None."""
    lines = [f'"""Stub {_SCAFFOLD_MARK} — implement the bodies; keep this exact '
             'public API."""']
    for a in [x.strip() for x in api if x and x.strip()]:
        base = a.rstrip(":")
        if base.startswith(("class ", "async def ", "def ")):
            body = "    ..." if base.startswith("class ") else "    raise NotImplementedError"
            lines.append(f"\n\n{base}:\n{body}")
        elif ":" in a and "=" not in a and "(" not in a:   # CONST: type
            nm = _clean_symbol(a)
            if nm:
                lines.append(f"\n\n{nm} = None")
        else:
            nm = _clean_symbol(a)
            if nm:
                lines.append(f"\n\n{nm} = None")
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
        return (test_path.replace("/test/", "/main/").rsplit("/", 1)[0]
                + f"/{name}{ext}") if "/test/" in test_path \
            else (impl_dirs[0] + f"/{name}{ext}" if impl_dirs else f"{name}{ext}")
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


def _ensure_impl_modules(subs: list) -> list:
    """DECOMPOSITION CONSISTENCY (inverse of the off-plan pruner): every test that
    targets a module (test_board→board, BookServiceTest→BookService, board.test→
    board) MUST have a matching impl file in the plan. When the architect collapses
    all impl into one file but writes per-module tests, those tests can't import
    their modules → collection errors no reconcile fixes. Adds the missing impl
    subtasks. Language-agnostic; skips non-module test names (integration/e2e/…)."""
    import re as _re
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
    added: list = []
    for s, p, stem, ext in tests:
        m = (_re.match(r"(?i)test_(.+)$", stem) or _re.match(r"(?i)(.+)_tests?$", stem)
             or _re.match(r"(.+)Tests?$", stem)          # XTest / XTests (plural)
             or _re.match(r"(.+)IT(?:Case)?$", stem)     # Java integration tests
             or _re.match(r"(?i)(.+)\.test$", stem) or _re.match(r"(?i)(.+)\.spec$", stem))
        if not m:
            continue
        name = m.group(1)
        if name.lower() in _NON_MODULE_TEST_STEMS or name.lower() in impl_stems:
            continue
        impl_path = _impl_path_for_test(p, name, ext, impl_dirs).lstrip("/")
        added.append({"slug": name.lower(), "path": impl_path,
                      "goal": f"Implement {name} to satisfy its tests ({os.path.basename(p)}).",
                      "api": []})
        impl_stems.add(name.lower())
    return subs + added


_BASELINE_FILE = ".aiforge-baseline"


def _snapshot_baseline(cwd: str) -> int:
    """Record the source files that EXISTED before this run (an existing repo vs a
    greenfield build) so the off-plan pruner NEVER deletes pre-existing code and
    the scaffold doesn't stub over it. Returns the pre-existing source count."""
    pre = [rel for rel, _c in _gather_sources(cwd)]
    try:
        with open(os.path.join(cwd, _BASELINE_FILE), "w", encoding="utf-8") as fh:
            fh.write("\n".join(pre))
    except Exception:  # noqa: BLE001
        pass
    return len(pre)


def _baseline_set(cwd: str) -> set:
    try:
        with open(os.path.join(cwd, _BASELINE_FILE), encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip()}
    except Exception:  # noqa: BLE001
        return set()


def _is_greenfield(cwd: str) -> bool:
    """True when the workspace has (almost) no pre-existing source — a NEW project.
    Greenfield-only steps (scaffold, off-plan prune, decompose-into-full-tree) are
    DESTRUCTIVE on an existing repo, so gate them on this."""
    try:
        n = len(_baseline_set(cwd)) if os.path.exists(os.path.join(cwd, _BASELINE_FILE)) \
            else len([1 for _ in _gather_sources(cwd)])
    except Exception:  # noqa: BLE001
        n = 0
    try:
        thresh = int(os.environ.get("AIFORGE_GREENFIELD_MAX_FILES", "8"))
    except ValueError:
        thresh = 8
    return n <= thresh


def _spec_declared_paths(subs: list) -> set:
    return {str(s.get("path") or "").lstrip("/").replace("..", "")
            for s in subs if s.get("path")}


def _prune_offplan_files(cwd: str, subs: list) -> list:
    """Delete source files NOT in the SPEC's declared list — a worker or the
    reconciler sometimes invents a phantom package (e.g. tetris/game.py alongside
    the planned tetris_game.py), producing DUPLICATE modules → import/collection
    errors no runner can fix. Keeps declared files + package glue (__init__/
    conftest) + non-source (build/config). Deterministic; the tree matches the
    plan. Returns removed paths."""
    declared = _spec_declared_paths(subs)
    if not declared:
        return []
    # NEVER touch an existing repo: on a non-greenfield workspace the pruner would
    # delete the whole codebase (everything not in this task's tiny plan). Bail.
    if not _is_greenfield(cwd):
        return []
    baseline = _baseline_set(cwd)                 # pre-existing files — never delete
    declared_bases = {os.path.basename(d) for d in declared}
    removed: list = []
    for rel, _content in _gather_sources(cwd):
        r = rel.lstrip("/")
        if r in declared or r in baseline:
            continue
        base = os.path.basename(r)
        if base in ("__init__.py", "conftest.py"):
            continue                              # package glue — harmless
        if not r.endswith(_SRC_EXTS):
            continue                              # keep build/config/markup
        # a source file that is neither declared by path NOR a unique new basename
        # the plan lacks → it's off-plan pollution (often a dup of a declared file).
        try:
            os.remove(os.path.join(cwd, r))
            removed.append(r)
        except Exception:  # noqa: BLE001
            pass
    return removed


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


def _fail_count(output: str) -> int:
    """Number of unhealthy tests from the build/test output (for the regression
    guard + reconcile progress signal). Sums pytest FAILED **and** ERRORS — a
    setup/fixture error (e.g. 'ValueError: Username already exists' from a store
    not reset between tests) shows as 'N errors', NOT 'failed', so counting only
    'failed' made the loop stop early thinking it was nearly done. 999 =
    couldn't-run/collection-error (worst); 0 = all green."""
    import re as _re
    out = output or ""
    failed = _re.search(r"(\d+)\s+failed", out)
    errored = _re.search(r"(\d+)\s+errors?\b", out)
    n = (int(failed.group(1)) if failed else 0) + (int(errored.group(1)) if errored else 0)
    if n:
        return n
    if failed or errored:
        return 0                      # explicit "0 failed" style — all green
    if _re.search(r"error|Error|Traceback|Interrupted", out):
        return 999                    # a raw error with no pytest counts → worst
    return 0


_SRC_EXTS = (".py", ".java", ".go", ".js", ".mjs", ".ts", ".tsx", ".c", ".cc",
             ".cpp", ".h", ".hpp", ".rs", ".rb", ".php", ".cs", ".kt", ".swift",
             ".scala", ".sh")


def _change_in_error(cwd: str, output: str) -> bool:
    """True if any source file THIS turn changed is named in the test/build
    error — i.e. the failure plausibly stems from the change (so repair it).
    False means the harness error references none of the changed files, so it's
    pre-existing/unrelated. Best-effort; True on any doubt (git unusable) so we
    keep the normal repair loop rather than wrongly skip a real regression."""
    import os as _os
    import subprocess as _sp
    out = output or ""
    try:
        r = _sp.run(["git", "-C", cwd, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return True                       # git unusable → don't skip
        changed = [ln[3:].strip() for ln in (r.stdout or "").splitlines()
                   if ln.strip() and ln[3:].strip().endswith(_SRC_EXTS)]
    except Exception:  # noqa: BLE001
        return True
    if not changed:
        return False                          # nothing source changed → not the cause
    for f in changed:
        f = f.strip().strip('"')
        if f and (f in out or _os.path.basename(f) in out
                  or _os.path.splitext(_os.path.basename(f))[0] in out):
            return True
    return False


def _prune_dead_python_imports(cwd: str) -> list[str]:
    """DETERMINISTIC pre-fix (general Python): remove `from <local_mod> import X`
    names — and matching `__all__` entries — where X isn't defined at MODULE
    level in <local_mod>. This kills the single most common cross-file break: a
    package __init__ re-exporting a name that's actually a class method / typo /
    missing, which fails ALL imports. No LLM, no task-specific logic."""
    import ast
    changed: list[str] = []
    # module dotted-name → set of module-level symbols it defines
    modsyms: dict[str, set] = {}

    def _rel_to_mod(rel: str) -> str:
        rel = rel[:-3] if rel.endswith(".py") else rel
        rel = rel[:-9] if rel.endswith("/__init__") else rel
        return rel.replace(os.sep, ".").strip(".")

    pyfiles: dict[str, str] = {}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".aiforge-venv", ".venv",
            "__pycache__", "node_modules")]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        pyfiles[os.path.relpath(p, cwd)] = fh.read()
                except Exception:  # noqa: BLE001
                    pass
    for rel, src in pyfiles.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        syms: set = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                syms.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        syms.add(t.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    syms.add(a.asname or a.name.split(".")[0])
        modsyms[_rel_to_mod(rel)] = syms

    for rel, src in pyfiles.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        dead: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                key = node.module
                # resolve against known local modules (exact or basename match)
                target = (key if key in modsyms
                          else next((m for m in modsyms if m.endswith("." + key)
                                     or m == key), None))
                if target is None:
                    continue
                have = modsyms.get(target, set())
                for a in node.names:
                    if a.name != "*" and a.name not in have:
                        dead.add(a.name)
        if not dead:
            continue
        # drop dead names from `from X import ...` lines + __all__ list entries
        new_lines = []
        for line in src.splitlines():
            ls = line.strip()
            if ls.startswith("from ") and " import " in ls:
                head, names = line.split(" import ", 1)
                kept = [n.strip() for n in names.split(",")
                        if n.strip() and n.strip().split(" as ")[0].strip() not in dead]
                if not kept:
                    continue  # whole import was dead → drop the line
                new_lines.append(head + " import " + ", ".join(kept))
                continue
            if any(f"'{d}'" == ls.rstrip(",") or f'"{d}"' == ls.rstrip(",") for d in dead):
                continue  # a dead __all__ entry on its own line
            new_lines.append(line)
        new_src = "\n".join(new_lines)
        if new_src != src:
            try:
                compile(new_src, rel, "exec")   # only write if still valid
                with open(os.path.join(cwd, rel), "w", encoding="utf-8") as fh:
                    fh.write(new_src + ("\n" if not new_src.endswith("\n") else ""))
                changed.append(rel)
            except SyntaxError:
                pass
    return changed


def _symbol_drift_report(cwd: str) -> list[dict]:
    """MAP-REDUCE blackboard: for every Python module collect the symbols it
    EXPOSES (module-level defs/classes/constants) and the symbols it CONSUMES
    (imports from sibling modules). Report each consumed name that its target
    module doesn't expose, with the closest real name as the canonical suggestion.

    This is the structured aggregation step — the merger reasons over this COMPACT
    blackboard (module → exposes/consumes + mismatches), not the whole codebase,
    so cross-file drift (Binary vs BinaryExpr, drop_piece method-vs-function) is
    caught at MERGE time, before any test runs. General; no per-error hardcoding.

    Prefers the workers' DECLARED contracts (.aiforge-contracts/, language-
    agnostic); falls back to Python AST extraction when none were declared."""
    import difflib
    _declared = _blackboard_from_contracts(cwd)
    if _declared is not None:
        exposes, consumes = _declared
        drift: list[dict] = []
        for cons, tgt, name in consumes:
            have = exposes.get(tgt, set())
            if name not in have:
                close = difflib.get_close_matches(name, list(have), n=1, cutoff=0.6)
                drift.append({"consumer": cons, "target": tgt, "name": name,
                              "target_exposes": sorted(have)[:15],
                              "suggest": close[0] if close else None})
        return drift

    import ast
    pyfiles: dict[str, str] = {}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".aiforge-venv", ".venv",
            "__pycache__", "node_modules", ".pytest_cache")]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        pyfiles[os.path.relpath(p, cwd)] = fh.read()
                except Exception:  # noqa: BLE001
                    pass

    def _mod(rel: str) -> str:
        rel = rel[:-3] if rel.endswith(".py") else rel
        rel = rel[:-9] if rel.endswith("/__init__") else rel
        return rel.replace(os.sep, ".").strip(".")

    exposes: dict[str, set] = {}
    consumes: list[tuple[str, str, str]] = []   # (consumer_mod, target_mod, name)
    for rel, src in pyfiles.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        syms: set = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                syms.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        syms.add(t.id)
        exposes[_mod(rel)] = syms

    mods = set(exposes)
    for rel, src in pyfiles.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                tgt = (node.module if node.module in mods
                       else next((m for m in mods if m.endswith("." + node.module)
                                  or m.split(".")[-1] == node.module.split(".")[-1]), None))
                if not tgt:
                    continue
                for a in node.names:
                    if a.name != "*":
                        consumes.append((_mod(rel), tgt, a.name))

    drift: list[dict] = []
    for cons, tgt, name in consumes:
        have = exposes.get(tgt, set())
        if name not in have:
            close = difflib.get_close_matches(name, list(have), n=1, cutoff=0.6)
            drift.append({"consumer": cons, "target": tgt, "name": name,
                          "target_exposes": sorted(have)[:15],
                          "suggest": close[0] if close else None})
    return drift


def _reconcile_integration(cwd: str, result: dict, should_cancel=None):
    """Build + test the merged tree; while it fails on cross-file drift, run a
    bounded Doer pass over the WHOLE workspace — fed the RAW test output + a
    CONCRETE directed fix-list — to fix the mismatches, re-testing each round.
    Yields SSE events; stores the final report in ``result['rep']``. Skippable
    via AIFORGE_RECONCILE_INTEGRATION=0. Halts on ``should_cancel()``."""
    from aiforge_core.runtime.integration_report import build_and_test_report
    if should_cancel is not None and should_cancel():
        result["rep"] = build_and_test_report(cwd)
        return
    # DETERMINISTIC pre-fix: prune dead package re-exports (the #1 cross-file
    # break the LLM won't fix) before spending an LLM round on it.
    try:
        _pruned = _prune_dead_python_imports(cwd)
        if _pruned:
            yield {"type": "tool", "role": "reconciler", "name": "pruned dead re-exports",
                   "args": {}, "result": {"files": _pruned}}
    except Exception:  # noqa: BLE001
        pass

    ok, output = _project_test_output(cwd)
    if ok or os.environ.get("AIFORGE_RECONCILE_INTEGRATION", "1") in ("0", "false"):
        result["rep"] = build_and_test_report(cwd)
        result["ok"] = ok            # authoritative (matches the test runner)
        return

    # PRE-EXISTING-FAILURE GATE: the harness ERRORED before any test ran
    # (0 parsed failures — a collection/import error), the config in THIS tree
    # parses fine, and NONE of this turn's changed files appear in the error.
    # That failure is pre-existing / environmental (e.g. an unrelated module the
    # repo can't import), NOT caused by the change — so the 12-round repair loop
    # would churn qwen against something it can't fix and that isn't the change's
    # fault. Stop: report ok=None (not a regression, not verified). Opt out with
    # AIFORGE_RECONCILE_SKIP_PREEXISTING=0.
    if os.environ.get("AIFORGE_RECONCILE_SKIP_PREEXISTING", "1") not in ("0", "false") \
            and _fail_count(output) in (0, 999) and not _broken_project_config(cwd) \
            and not _change_in_error(cwd, output):
        yield {"type": "thought", "role": "reconciler",
               "text": "⚠ tests don't collect on this repo independent of your "
                       "change (pre-existing import/config error, not referenced "
                       "by your edit) — skipping the repair loop; your change is "
                       "not the cause."}
        result["rep"] = build_and_test_report(cwd)
        result["ok"] = None          # pre-existing failure — not a regression
        return

    # CONFIG-VALIDITY GATE (live-e2e finding): ONE unterminated string in a
    # merged pyproject.toml made every pytest/pip run die at CONFIG PARSE —
    # exit != 0 with ZERO parsed failures — so reconcile burned all its
    # passes reporting "failed (0 failing)" while patching the wrong files.
    # Detect a broken config deterministically and point the fixer AT it.
    _cfg_err = _broken_project_config(cwd)
    if _cfg_err:
        yield {"type": "thought", "role": "reconciler",
               "text": f"⚠ project config invalid — {_cfg_err}. Fixing it "
                       "first; every test/build run is blocked by it."}
        output = (f"CONFIG ERROR — fix this FIRST, nothing can run until it "
                  f"parses: {_cfg_err}\n\n{output}")

    max_rounds = _reconcile_rounds()
    rounds = 0
    prev_fails = _fail_count(output)
    stalls = 0
    while not ok and rounds < max_rounds:
        if should_cancel is not None and should_cancel():
            break
        rounds += 1
        try:
            _prune_dead_python_imports(cwd)   # deterministic, before the LLM round
        except Exception:  # noqa: BLE001
            pass
        hints = _directed_hints(output)
        # ESCALATION: after the primary model stalls (no improvement for a couple
        # rounds), hand the residual failures it can't crack to a stronger/reasoning
        # model (AIFORGE_ESCALATION_MODEL, e.g. a 9B reasoning model). General —
        # only the STUCK residual escalates, not every round; no per-problem code.
        _esc_model = _escalation_model()
        # TRIAGE: a structurally-hard residual (cross-file import/signature/
        # attribute mismatch) that the coder+repo-map didn't crack in ONE round
        # escalates to the reasoning model early — don't burn a 2nd stall round on
        # it. A plain logic/value fail keeps the coder for 2 rounds first.
        _hard = _is_hard_residual(output)
        _use_esc = _esc_model and stalls >= (1 if _hard else 2)
        # TEST-AUDIT: after impl fixes stall (the impl was rewritten repeatedly and
        # the SAME tests still fail), a failing test may itself be WRONG — a local
        # model writes buggy tests too. Once stuck, let the fixer correct a test
        # that CONTRADICTS the goal (guarded: regression guard rolls back if net
        # fails rise; `# test-audit:` marker makes edits visible). Off with
        # AIFORGE_RECONCILE_TEST_AUDIT=0.
        _audit = (os.environ.get("AIFORGE_RECONCILE_TEST_AUDIT", "1")
                  not in ("0", "false") and stalls >= 2)
        # "0 failing" with a red run = the run ERRORED before tests executed
        # (config/collection) — say so instead of the contradictory count.
        _fail_desc = (f"{prev_fails} failing" if prev_fails
                      else "run ERRORED before tests executed — config/collection")
        yield {"type": "thought", "role": "reconciler",
               "text": f"Integration failed ({_fail_desc}) — pass "
                       f"{rounds}/{max_rounds}: "
                       + (f"escalating the residual to {_esc_model}…" if _use_esc
                          else "auditing whether a stuck test is itself wrong…"
                          if _audit else "patching the offending files…")}
        # Snapshot BEFORE the round so a round that makes things WORSE (a local
        # model's bad patch) can be rolled back — reconcile is then MONOTONIC:
        # it never regresses, only accepts rounds that reduce the failure count.
        snapshot = dict(_gather_sources(cwd))
        try:
            written = _rewrite_fix(cwd, output, hints,
                                   model=(_esc_model if _use_esc else None),
                                   audit_tests=_audit)
        except Exception as exc:  # noqa: BLE001 — a transient LLM error must NOT
            yield {"type": "thought", "role": "reconciler",
                   "text": f"reconcile pass hit a transient error, retrying: {str(exc)[:80]}"}
            written = []
        ok, output = _project_test_output(cwd)
        new_fails = _fail_count(output)
        if new_fails > prev_fails:
            # STRICT regression only → roll back (restore snapshot + drop any files
            # the bad round created). A LATERAL move (== fails) is KEPT — it lets
            # the model refactor toward the seam without penalty.
            _snap_keys = set(snapshot)
            for _rel, _c in _gather_sources(cwd):
                if _rel not in _snap_keys:
                    try:
                        os.remove(os.path.join(cwd, _rel))
                    except Exception:  # noqa: BLE001
                        pass
            for rel, content in snapshot.items():
                try:
                    with open(os.path.join(cwd, rel), "w", encoding="utf-8") as fh:
                        fh.write(content)
                except Exception:  # noqa: BLE001
                    pass
            ok, output = _project_test_output(cwd)
            new_fails = _fail_count(output)
            stalls += 1
            yield {"type": "thought", "role": "reconciler",
                   "text": f"pass {rounds} REGRESSED ({prev_fails}→? ) — rolled back to "
                           f"{prev_fails} failing. Trying a different angle…"}
            if stalls >= 4:
                break                          # 4 no-progress rounds → give up
        else:
            # improvement OR lateral (no regression) → KEEP.
            if new_fails < prev_fails:
                prev_fails = new_fails
                stalls = 0
            else:
                stalls += 1                     # lateral move — bounded
            _lbl = ("tests can't run (collection/build error)"
                    if new_fails >= 999 else f"{new_fails} failing")
            yield {"type": "tool", "role": "reconciler", "name": "patched files",
                   "args": {"pass": rounds, "status": _lbl},
                   "result": {"ok": new_fails < 999, "files": written,
                              "output": (output or "")[-1500:] if new_fails >= 999 else None}}
            if stalls >= 4:
                break

    result["rep"] = build_and_test_report(cwd)
    result["ok"] = ok                # authoritative final state (the test runner)
    if rounds and ok:
        yield {"type": "thought", "role": "reconciler",
               "text": f"Reconciliation green after {rounds} pass(es) ✅"}
    elif rounds:
        yield {"type": "thought", "role": "reconciler",
               "text": f"Reconciliation ran {rounds} pass(es) — some tests still "
                       "red; see the report + manual steps below."}


def _broken_project_config(cwd: str) -> str | None:
    """Deterministic parse check of the project's build/test config files —
    a syntactically broken one blocks EVERY test/build run with an error the
    fail-count parser reads as '0 failing'. Returns '<file>: <error>' or
    None."""
    import json as _json
    py = os.path.join(cwd, "pyproject.toml")
    if os.path.isfile(py):
        try:
            import tomllib
            with open(py, "rb") as fh:
                tomllib.load(fh)
        except Exception as exc:  # noqa: BLE001
            return f"pyproject.toml: {str(exc)[:200]}"
    pj = os.path.join(cwd, "package.json")
    if os.path.isfile(pj):
        try:
            with open(pj, encoding="utf-8") as fh:
                _json.load(fh)
        except Exception as exc:  # noqa: BLE001
            return f"package.json: {str(exc)[:200]}"
    return None


def _render_spec_md(prompt: str, subs: list[dict]) -> str:
    """The shared requirements/plan document written to SPEC.md before the run
    and re-read by the final verification pass."""
    lines = ["# Project Spec", "", "## Goal", "", prompt.strip(), ""]
    # Canonical file tree — the EXACT paths every subtask must use verbatim (no
    # re-casing/renaming the package dir), so isolated contexts don't split into
    # mini_lang/ + miniLang/ + minilang/.
    paths = [str(s.get("path") or "").strip().lstrip("/")
             for s in subs if s.get("path")]
    if paths:
        lines += ["## File tree (use these EXACT paths — verbatim)", ""]
        lines += [f"- `{p}`" for p in paths]
        lines += [""]
    # API CONTRACT — the shared source of truth. Every file MUST expose these
    # names/signatures verbatim, and MUST import other files' names EXACTLY as
    # listed here. This is what stops isolated workers drifting (Binary vs
    # BinaryExpr, COLORS vs COLOR_MAP) — reconcile at DESIGN time, not after.
    api_lines = []
    for s in subs:
        api = [str(a) for a in (s.get("api") or []) if a]
        if api and s.get("path"):
            api_lines.append(f"### `{s['path']}` exposes")
            api_lines += [f"- `{a}`" for a in api]
    if api_lines:
        lines += ["## API contract — expose/import these names EXACTLY (verbatim)", ""]
        lines += api_lines
        lines += [""]
    lines += [f"## Subtasks ({len(subs)})", ""]
    for i, s in enumerate(subs):
        slug = s.get("slug") or f"sub-{i+1}"
        goal = (s.get("goal") or "").strip()
        lines.append(f"{i+1}. **{slug}** — {goal}")
        for a in (s.get("acceptance") or []):
            lines.append(f"   - [ ] {a}")
    lines.append("")
    return "\n".join(lines)


def _verify_against_spec(cwd: str, spec_md: str) -> str:
    """Fresh-context check: given SPEC.md + a listing of the produced files,
    ask the model whether every requirement is addressed. Returns a short
    verdict string (or '' on any failure)."""
    from aiforge_core.llm.client import complete as _complete
    tree = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".venv", "__pycache__", "node_modules")]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), cwd)
            tree.append(rel)
        if len(tree) > 400:
            break
    listing = "\n".join(sorted(tree)[:400]) or "(no files)"
    convo = [
        {"role": "system", "content":
         "You are a delivery auditor. Given a project SPEC and the file tree "
         "that was produced, state briefly whether every spec item appears "
         "addressed. List any MISSING or clearly-incomplete items as a short "
         "bullet list. Be concise (<200 words). If everything is covered, say so."},
        {"role": "user", "content":
         f"SPEC.md:\n{spec_md[:6000]}\n\nPRODUCED FILES:\n{listing}"},
    ]
    try:
        out = _complete("verifier", convo)
        return (out or "").strip()
    except Exception:  # noqa: BLE001
        return ""

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._contracts import _CONTRACT_DIR, _blackboard_from_contracts, _clean_symbol, _is_test_subtask
from ._runners import _parse_file_blocks
from ._stream import _USER_MANDATES
