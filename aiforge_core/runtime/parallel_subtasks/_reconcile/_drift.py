"""Deterministic dead-import pruning and cross-module symbol-drift report.

Split from ``parallel_subtasks._reconcile`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os

from .._contracts import _blackboard_from_contracts


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
