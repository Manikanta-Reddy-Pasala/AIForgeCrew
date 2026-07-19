"""Test/build execution, output collection, steering, and failure classification.

Split from ``parallel_subtasks._reconcile`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os
import re
import subprocess

from pydantic import BaseModel


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
    build puts the compile error under stdout/stderr, not just output/error.

    CRITICAL for compiled languages: ``project()`` buries each command's real
    output under ``results[].output`` (a Maven/Gradle/Go/Rust compile error is
    there, NOT at the top level), so without digging into ``results`` the
    reconcile sees an EMPTY error and can't fix a javac/rustc/go error it never
    saw. Pull both the top-level keys AND every failing sub-result's output."""
    parts = [str(res.get(k) or "") for k in
             ("error", "output", "stdout", "stderr", "logs", "details", "message")]
    for r in (res.get("results") or []):
        if isinstance(r, dict):
            parts.append(str(r.get("output") or r.get("error") or ""))
    return "\n".join(p for p in parts if p).strip()


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
