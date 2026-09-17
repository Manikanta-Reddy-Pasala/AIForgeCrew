"""Every import of an aiforge_core module resolves — including the lazy ones.

A function-local import inside ``try/except Exception`` fails silently: when
``repo_standards`` moved from ``runtime`` to ``config``, REPO_NOTES.md quietly
lost its build commands for months. This walks the source (no imports run)
and checks each ``aiforge_core`` import names a module that exists and a name
that module defines.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "aiforge_core"


def _module_file(dotted: str) -> Path | None:
    base = ROOT.joinpath(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _defined(path: Path) -> tuple[set[str], bool]:
    """Names bound at module level, and whether the module is open-ended
    (``__getattr__`` or a star import)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    open_ended = False
    for node in ast.walk(tree):
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
    return names, open_ended


def _absolute(node: ast.ImportFrom, path: Path) -> str | None:
    if not node.level:
        return node.module
    pkg = path.parent.relative_to(ROOT).parts
    if node.level > 1:
        pkg = pkg[:-(node.level - 1)]
    return ".".join([*pkg, *([node.module] if node.module else [])])


def _problems(path: Path) -> list[str]:
    out = []
    rel = path.relative_to(ROOT)
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
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
