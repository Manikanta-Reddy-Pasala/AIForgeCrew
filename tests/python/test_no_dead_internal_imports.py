"""Every import of an aiforge_core module resolves — including the lazy ones.

A function-local import inside ``try/except Exception`` fails silently: when
``repo_standards`` moved from ``runtime`` to ``config``, REPO_NOTES.md quietly
lost its build commands for months. This walks the source (no imports run)
and checks each ``aiforge_core`` import names a module that exists and a name
that module defines, and that ``getattr(mod, "x")`` / ``hasattr(mod, "x")``
probes on an imported aiforge_core module name something it defines — a
guarded probe of a missing name hid the email tools from the chat model.
"""
from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "aiforge_core"


def _module_file(dotted: str) -> Path | None:
    base = ROOT.joinpath(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _module_level(tree: ast.Module):
    """Module-level statements and what they bind, without descending into
    function or class bodies (their locals are not module attributes)."""
    todo = list(tree.body)
    while todo:
        node = todo.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        todo.extend(ast.iter_child_nodes(node))


@cache
def _defined(path: Path) -> tuple[frozenset[str], bool]:
    """Names bound at module level, and whether the module is open-ended
    (``__getattr__`` or a star import)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    open_ended = False
    for node in _module_level(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            open_ended |= node.name == "__getattr__"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name == "*":
                    open_ended = True
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return frozenset(names), open_ended


def _absolute(node: ast.ImportFrom, path: Path) -> str | None:
    if not node.level:
        return node.module
    pkg = path.parent.relative_to(ROOT).parts
    if node.level > 1:
        pkg = pkg[:-(node.level - 1)]
    return ".".join([*pkg, *([node.module] if node.module else [])])


def _module_aliases(scope: ast.AST, path: Path) -> dict[str, str]:
    """Local names bound to an aiforge_core MODULE by imports directly in
    ``scope`` (not in nested functions, so one alias reused in two functions
    stays two separate bindings)."""
    out: dict[str, str] = {}
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        todo.extend(ast.iter_child_nodes(node))
        if isinstance(node, ast.Import):
            for a in node.names:
                if (a.asname and a.name.startswith("aiforge_core.")
                        and _module_file(a.name) is not None):
                    out[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom):
            mod = _absolute(node, path)
            if mod and mod.startswith("aiforge_core"):
                for a in node.names:
                    if _module_file(f"{mod}.{a.name}") is not None:
                        out[a.asname or a.name] = f"{mod}.{a.name}"
    return out


def _own_nodes(scope: ast.AST):
    """Nodes of ``scope`` itself, not of functions/classes nested in it."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(node))


def _rebound(scope: ast.AST) -> set[str]:
    """Names a function scope binds itself (parameters, assignments)."""
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    out = {a.arg for a in ast.walk(scope.args) if isinstance(a, ast.arg)}
    out |= {n.id for n in _own_nodes(scope)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    return out


def _probe_problems(tree: ast.AST, path: Path) -> list[str]:
    out = []
    rel = path.relative_to(ROOT)
    module_aliases = _module_aliases(tree, path)
    scopes = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        own = _module_aliases(scope, path) if scope is not tree else {}
        rebound = _rebound(scope) - set(own)
        aliases = {k: v for k, v in module_aliases.items() if k not in rebound}
        aliases.update(own)
        for node in _own_nodes(scope):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("getattr", "hasattr") and len(node.args) >= 2):
                continue
            obj, attr = node.args[0], node.args[1]
            if not (isinstance(obj, ast.Name) and obj.id in aliases
                    and isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
                continue
            mod = aliases[obj.id]
            names, open_ended = _defined(_module_file(mod))
            if not open_ended and attr.value not in names \
                    and _module_file(f"{mod}.{attr.value}") is None:
                out.append(f"{rel}:{node.lineno} {node.func.id}({mod}, {attr.value!r})")
    return out


def _problems(path: Path) -> list[str]:
    out = []
    rel = path.relative_to(ROOT)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out += _probe_problems(tree, path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("aiforge_core.") and _module_file(a.name) is None:
                    out.append(f"{rel}:{node.lineno} import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = _absolute(node, path)
            if not mod or not mod.startswith("aiforge_core"):
                continue
            target = _module_file(mod)
            if target is None:
                out.append(f"{rel}:{node.lineno} from {mod}")
                continue
            names, open_ended = _defined(target)
            for a in node.names:
                if a.name == "*" or open_ended or a.name in names:
                    continue
                if _module_file(f"{mod}.{a.name}") is None:
                    out.append(f"{rel}:{node.lineno} from {mod} import {a.name}")
    return out


def test_every_internal_import_resolves():
    problems = [p for f in sorted(PKG.rglob("*.py")) for p in _problems(f)]
    assert not problems, "imports of aiforge_core names that do not exist:\n" + "\n".join(problems)
