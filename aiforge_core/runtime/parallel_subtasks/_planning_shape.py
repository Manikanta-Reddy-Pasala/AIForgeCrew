"""How an architect plan is shaped: module caps, merging coupled modules, and
the checks a plan must pass."""
from __future__ import annotations

import os
import re

_PLAN_CODE_EXTS = {"py", "java", "js", "ts", "tsx", "go", "rs", "kt", "rb",
                   "c", "cpp", "cs", "php"}


# Compiled languages — their cross-module contracts must line up to COMPILE, so
# fragmentation is far more fragile than in Python/JS. Tighter cap for these.
_COMPILED_CODE_EXTS = {"java", "go", "rs", "kt", "c", "cpp", "cc", "cs"}


def _max_compiled_modules() -> int:
    """Tighter module cap for a COMPILED-language plan (default 3). Java/Go/Rust
    isolated agents diverge on the exact type contracts needed to compile;
    keeping a coupled subsystem in ONE module avoids the mismatch. Raise
    AIFORGE_ARCHITECT_MAX_MODULES_COMPILED for a genuinely large compiled build."""
    try:
        return max(1, int(os.environ.get(
            "AIFORGE_ARCHITECT_MAX_MODULES_COMPILED", "2")))
    except ValueError:
        return 2


def _max_code_modules() -> int:
    """Cap on NON-TEST code modules in one plan. Finer decompose is WORSE on a
    local model — it over-splits one responsibility (a single queue into
    ``core.py`` + ``queue_ordering.py`` + ``worker_retry.py``) and the isolated
    workers then diverge on names/imports the reconcile can't stitch. A tight cap
    forces the architect to consolidate coupled logic into cohesive modules
    (coarser = safer). Raise AIFORGE_ARCHITECT_MAX_MODULES for a genuinely large
    build. Tests + manifests don't count — only implementation modules."""
    try:
        return max(1, int(os.environ.get("AIFORGE_ARCHITECT_MAX_MODULES", "4")))
    except ValueError:
        return 4


def _module_cap_for(paths: list[str]) -> tuple[list[str], int]:
    """NON-test code modules in ``paths`` + the applicable cap. Compiled languages
    punish fragmentation HARDER (cross-module type contracts — generics, nested-
    type constructors, signatures — must line up to even COMPILE), so a compiled
    plan gets the tighter :func:`_max_compiled_modules` cap."""
    code = [p for p in paths
            if "." in p and p.rsplit(".", 1)[-1].lower() in _PLAN_CODE_EXTS
            and "test" not in p.lower()]
    compiled = any(p.rsplit(".", 1)[-1].lower() in _COMPILED_CODE_EXTS
                   for p in code)
    cap = (min(_max_code_modules(), _max_compiled_modules()) if compiled
           else _max_code_modules())
    return code, cap


_SYMBOL_DECL_RE = re.compile(
    r"\b(?:class|struct|def|fn|func|type|enum|interface|trait)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)")


def _plan_path(f: dict) -> str:
    return str(f.get("path") or "").strip().lstrip("/")


def _api_symbols(module: dict) -> list[str]:
    """Bare identifiers declared in a module's api, so we can tell WHICH module
    references another's type."""
    names = []
    for a in (module.get("api") or []):
        m = _SYMBOL_DECL_RE.search(str(a))
        if m:
            names.append(m.group(1))
    return names


def _user_of(helper: dict, mods: list[dict]) -> dict | None:
    """The module whose api text NAMES one of ``helper``'s declared types."""
    symbols = _api_symbols(helper)
    if not symbols:
        return None
    for u in mods:
        if u is helper:
            continue
        utext = " ".join(str(x) for x in u["api"])
        if any(re.search(r"\b" + re.escape(s) + r"\b", utext) for s in symbols):
            return u
    return None


def _coupled_pair(mods: list[dict]) -> tuple | None:
    """``(helper, user)`` — the helper's type NAME appears in the user's api
    text, so the helper folds INTO the user. Smallest helper first."""
    best = None
    for h in mods:
        u = _user_of(h, mods)
        if u is not None and (best is None or len(h["api"]) < len(best[0]["api"])):
            best = (h, u)
    return best


def _fold_into(helper: dict, user: dict) -> None:
    for a in helper["api"]:
        if a not in user["api"]:
            user["api"].append(a)
    if helper["purpose"]:
        user["purpose"] = (user["purpose"] + "; "
                           + helper["purpose"]).strip("; ")[:250]


def _merge_modules(mods: list[dict], cap: int, compiled: bool) -> list[dict]:
    """Fold modules until the cap is met (and, for compiled languages, until no
    coupled pair remains). No coupling → fold smallest into largest."""
    while len(mods) > 1:
        pair = _coupled_pair(mods)
        over = len(mods) > cap
        if not over and not (compiled and pair):
            break
        if pair is None:
            order = sorted(mods, key=lambda m: len(m["api"]))
            pair = (order[0], order[-1])
        helper, user = pair
        _fold_into(helper, user)
        mods.remove(helper)
    return mods


def _coalesce_code_modules(files: list[dict]) -> tuple[list[dict], int]:
    """HARD-enforce the module cap the architect keeps IGNORING in its re-ask:
    deterministically merge excess NON-test code modules down to the cap, at PLAN
    time (before any code is written, so it's safe). Symbols are PRESERVED (union
    of every merged module's ``api``) — they just live in fewer files; the module
    contract + SPEC api-contract carry the merged mapping, so a test importing a
    moved symbol still resolves. Tests / manifests / config files are untouched.
    Returns ``(new_files, n_modules_removed)`` (0 when already within the cap).

    COUPLING-AWARE: a HELPER (a module whose type is REFERENCED in another
    module's api — e.g. LRUCache's api names DoublyLinkedList/Node) folds INTO
    its user, so the helper lands in the file that uses it → no cross-module
    constructor/type mismatch. For COMPILED languages this runs even WITHIN the
    count cap (a coupled pair at exactly the cap is the exact failure mode); for
    looser languages it only fires to hit the count cap.
    """
    code_paths, cap = _module_cap_for([_plan_path(f) for f in files])
    compiled = any(p.rsplit(".", 1)[-1].lower() in _COMPILED_CODE_EXTS
                   for p in code_paths)
    code_set = set(code_paths)
    code = [f for f in files if _plan_path(f) in code_set]
    others = [f for f in files if _plan_path(f) not in code_set]
    if len(code) <= cap and not compiled:
        return files, 0
    mods = _merge_modules(
        [{"path": _plan_path(f), "purpose": str(f.get("purpose") or ""),
          "api": list(f.get("api") or [])} for f in code],
        cap, compiled)
    merged = [{"path": m["path"], "purpose": m["purpose"] or "combined module",
               "api": m["api"]} for m in mods]
    return others + merged, len(code) - len(merged)


def _importable_py_path(p: str, issues: list[str]) -> str:
    """HYPHEN sanitize: a Python module file with a hyphen in its stem
    (`task-queue.py`) is UNIMPORTABLE — `import task-queue` is a syntax error —
    so an isolated worker writes it and every `from .task-queue import …` fails.
    The stem's hyphens become underscores (dir parts + extension untouched); the
    architect's api/imports reference the module NAME, which the doer derives
    from this path."""
    if p.rsplit(".", 1)[-1].lower() != "py" or "-" not in os.path.basename(p):
        return p
    d, b = os.path.split(p)
    stem, _dot, ext = b.rpartition(".")
    fixed = os.path.join(d, stem.replace("-", "_") + "." + ext)
    issues.append(f"invalid python module name {p!r} → {fixed!r} "
                  "(hyphens aren't importable)")
    return fixed


def _sanitized_files(files: list[dict], issues: list[str]) -> list[dict]:
    """Drop escaping paths and duplicates, fix un-importable module names."""
    seen: set[str] = set()
    clean: list[dict] = []
    for f in files:
        p = str(f.get("path") or "").strip().lstrip("/")
        if not p:
            continue
        if p.startswith("..") or "/../" in f"/{p}/":
            issues.append(f"path escapes the workspace: {p!r} (dropped)")
            continue
        p = _importable_py_path(p, issues)
        if p in seen:
            issues.append(f"duplicate path: {p!r} (deduped)")
            continue
        seen.add(p)
        clean.append({**f, "path": p})
    return clean


def _plan_shape_issues(paths: list[str]) -> list[str]:
    """The soft defects a semantic re-ask should fix."""
    issues: list[str] = []
    exts = {p.rsplit(".", 1)[-1].lower() for p in paths if "." in p}
    code_exts = exts & _PLAN_CODE_EXTS
    if len(paths) > 40:
        issues.append(f"{len(paths)} files is a dump, not a plan — collapse "
                      "coupled concerns (aim well under 40)")
    # Over-fragmentation gate: too many NON-TEST code modules → the architect
    # atomised a coupled subsystem. Re-ask to consolidate (coarser = safer on a
    # local model; finer split diverges and won't reconcile).
    code_modules, cap = _module_cap_for(paths)
    if len(code_modules) > cap:
        issues.append(
            f"{len(code_modules)} code modules is over-fragmented for one build "
            f"— CONSOLIDATE coupled logic into at most {cap} cohesive modules "
            "(e.g. ONE queue.py, not core.py + queue_ordering.py + worker_retry.py). "
            "Give a separate file only to a genuinely DECOUPLED concern "
            "(persistence, CLI/entrypoint). Keep every test + the manifest.")
    if code_exts and not any("test" in p.lower() for p in paths):
        issues.append("plan has code modules but NO test files — every code "
                      "module needs a test file in the SAME plan")
    if len(code_exts - {"js", "ts", "tsx"}) > 2:
        issues.append(f"plan mixes {sorted(code_exts)} languages — a single "
                      "build uses the spec's one stack")
    return issues


def _validate_plan(files: list[dict]) -> tuple[list[dict], list[str]]:
    """Deterministic sanity gate on the architect's file plan — the plan is a
    single point of failure (every subtask builds against it), so structural
    defects must be caught BEFORE the fan-out, not discovered by 10 workers.
    Returns ``(sanitized_files, issues)``: hard defects (dupes, escaping
    paths) are FIXED in the sanitized list; soft defects (no tests, language
    soup, absurd size) are reported for a semantic reask."""
    issues: list[str] = []
    clean = _sanitized_files(files, issues)
    return clean, issues + _plan_shape_issues([f["path"] for f in clean])
