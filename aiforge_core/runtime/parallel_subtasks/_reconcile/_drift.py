"""Deterministic dead-import pruning and cross-module symbol-drift report.

Split from ``parallel_subtasks._reconcile`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os

from .._contracts import _blackboard_from_contracts

_IMPORT = ' import '


_SKIP_DIRS = (".git", ".aiforge-worktrees", ".aiforge-venv", ".venv",
              "__pycache__", "node_modules", ".pytest_cache")


def _rel_to_mod(rel: str) -> str:
    """``pkg/sub/__init__.py`` → ``pkg.sub``; ``pkg/mod.py`` → ``pkg.mod``."""
    rel = rel[:-3] if rel.endswith(".py") else rel
    rel = rel[:-9] if rel.endswith("/__init__") else rel
    return rel.replace(os.sep, ".").strip(".")


def _read_python_files(cwd: str) -> dict[str, str]:
    """``{relpath: source}`` for every .py under ``cwd``, vendor dirs skipped."""
    out: dict[str, str] = {}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(root, f)
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    out[os.path.relpath(p, cwd)] = fh.read()
            except Exception:  # noqa: BLE001 — unreadable file is not our problem
                pass
    return out


def _module_level_symbols(tree) -> set:
    """Names a module EXPOSES: its top-level defs, classes, assignments, imports.

    Deliberately module level only (``tree.body``, not ``ast.walk``) — a method
    named ``X`` does not make ``from mod import X`` work, and mistaking the two
    is exactly the bug this file exists to catch.
    """
    import ast
    syms: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            syms.add(node.name)
        elif isinstance(node, ast.Assign):
            syms.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            syms.update(a.asname or a.name.split(".")[0] for a in node.names)
    return syms


def _resolve_module(key: str, modsyms: dict[str, set]) -> str | None:
    """The local module ``key`` refers to — exact name, else basename match."""
    if key in modsyms:
        return key
    return next((m for m in modsyms if m.endswith("." + key) or m == key), None)


def _dead_imported_names(tree, modsyms: dict[str, set]) -> set:
    """Names imported from a LOCAL module that the module does not define."""
    import ast
    dead: set = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.module):
            continue
        target = _resolve_module(node.module, modsyms)
        if target is None:
            continue          # third-party / stdlib — not ours to judge
        have = modsyms.get(target, set())
        dead.update(a.name for a in node.names
                    if a.name != "*" and a.name not in have)
    return dead


def _strip_import_line(line: str, dead: set) -> str | None:
    """``line`` with the dead names removed, or None if nothing survives."""
    head, names = line.split(_IMPORT, 1)
    kept = [n.strip() for n in names.split(",")
            if n.strip() and n.strip().split(" as ")[0].strip() not in dead]
    return head + _IMPORT + ", ".join(kept) if kept else None


def _is_dead_all_entry(stripped: str, dead: set) -> bool:
    """A lone ``"name",`` line inside an ``__all__`` list, for a dead name."""
    bare = stripped.rstrip(",")
    return any(f"'{d}'" == bare or f'"{d}"' == bare for d in dead)


def _without_dead_names(src: str, dead: set) -> str:
    out = []
    for line in src.splitlines():
        ls = line.strip()
        if ls.startswith("from ") and _IMPORT in ls:
            kept = _strip_import_line(line, dead)
            if kept is not None:
                out.append(kept)
            continue
        if _is_dead_all_entry(ls, dead):
            continue
        out.append(line)
    return "\n".join(out)


def _rewrite_if_valid(cwd: str, rel: str, new_src: str) -> bool:
    """Write ``new_src`` only if it still parses — a pruner must not break code."""
    try:
        compile(new_src, rel, "exec")
    except SyntaxError:
        return False
    with open(os.path.join(cwd, rel), "w", encoding="utf-8") as fh:
        fh.write(new_src if new_src.endswith("\n") else new_src + "\n")
    return True


def _parsed(src: str):
    import ast
    try:
        return ast.parse(src)
    except SyntaxError:
        return None


def _prune_dead_python_imports(cwd: str) -> list[str]:
    """DETERMINISTIC pre-fix (general Python): remove `from <local_mod> import X`
    names — and matching `__all__` entries — where X isn't defined at MODULE
    level in <local_mod>. This kills the single most common cross-file break: a
    package __init__ re-exporting a name that's actually a class method / typo /
    missing, which fails ALL imports. No LLM, no task-specific logic.

    Two passes over the same files: build the symbol map first, because a dead
    name can only be judged against a module that has already been read.
    """
    pyfiles = _read_python_files(cwd)
    modsyms: dict[str, set] = {}
    for rel, src in pyfiles.items():
        tree = _parsed(src)
        if tree is not None:
            modsyms[_rel_to_mod(rel)] = _module_level_symbols(tree)

    changed: list[str] = []
    for rel, src in pyfiles.items():
        tree = _parsed(src)
        if tree is None:
            continue
        dead = _dead_imported_names(tree, modsyms)
        if not dead:
            continue
        new_src = _without_dead_names(src, dead)
        if new_src != src and _rewrite_if_valid(cwd, rel, new_src):
            changed.append(rel)
    return changed


def _drift_rows(exposes: dict[str, set],
                consumes: list[tuple[str, str, str]]) -> list[dict]:
    """One row per consumed name its target module does not expose.

    Both blackboard sources (declared contracts, Python AST) reduce to the same
    (exposes, consumes) pair, so the comparison is written once.
    """
    import difflib
    drift: list[dict] = []
    for cons, tgt, name in consumes:
        have = exposes.get(tgt, set())
        if name in have:
            continue
        close = difflib.get_close_matches(name, list(have), n=1, cutoff=0.6)
        drift.append({"consumer": cons, "target": tgt, "name": name,
                      "target_exposes": sorted(have)[:15],
                      "suggest": close[0] if close else None})
    return drift


def _defined_symbols(tree) -> set:
    """What a module DEFINES itself — unlike :func:`_module_level_symbols`, its
    re-exported imports do not count as things it exposes to a drift report."""
    import ast
    syms: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            syms.add(node.name)
        elif isinstance(node, ast.Assign):
            syms.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return syms


def _drift_target(module: str, mods: set) -> str | None:
    """The local module an ``import from`` refers to, matched loosely.

    Looser than :func:`_resolve_module` on purpose: a report only suggests, so a
    last-segment match is worth a hint, whereas the pruner DELETES code and must
    not act on a guess.
    """
    if module in mods:
        return module
    last = module.split(".")[-1]
    return next((m for m in mods
                 if m.endswith("." + module) or m.split(".")[-1] == last), None)


def _python_blackboard(cwd: str) -> tuple[dict[str, set], list[tuple[str, str, str]]]:
    """AST fallback: ``(exposes, consumes)`` for every Python module under cwd."""
    import ast
    pyfiles = _read_python_files(cwd)
    trees = {}
    for rel, src in pyfiles.items():
        try:
            trees[rel] = ast.parse(src)
        except SyntaxError:
            continue

    exposes = {_rel_to_mod(rel): _defined_symbols(t) for rel, t in trees.items()}
    mods = set(exposes)
    consumes: list[tuple[str, str, str]] = []
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.module):
                continue
            tgt = _drift_target(node.module, mods)
            if not tgt:
                continue
            consumes.extend((_rel_to_mod(rel), tgt, a.name)
                            for a in node.names if a.name != "*")
    return exposes, consumes


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
    declared = _blackboard_from_contracts(cwd)
    exposes, consumes = declared if declared is not None else _python_blackboard(cwd)
    return _drift_rows(exposes, consumes)
