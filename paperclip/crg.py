"""code-review-graph (DESIGN.md §5.1, §7).

Builds a static call graph over Python files in the repo. Query shapes:
  - blast_radius(target, max_depth=3): files reachable upstream (callers of callers).
  - dependency_chain(target): downstream (callees) + upstream (callers).

`target` is one of:
  - "path/to/file.py"                           → any symbol in that file
  - "path/to/file.py::function_name"            → specific function
  - "path/to/file.py::ClassName.method"         → class method

Graph is cached at `.aiforge/crg/graph.json`; rebuild with `rebuild()`.
Non-Python files are ignored (extension-based). Expand with tree-sitter later.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SymbolInfo:
    qualified: str           # "path/to/file.py::Foo.bar"
    defined_in: str          # "path/to/file.py"
    calls: list[str] = field(default_factory=list)    # unqualified target names


@dataclass
class CallGraph:
    repo_root: Path
    # qualified-name → SymbolInfo
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)
    # unqualified name → list of qualified definitions
    by_short_name: dict[str, list[str]] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "symbols": {k: v.__dict__ for k, v in self.symbols.items()},
            "by_short_name": self.by_short_name,
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, repo_root: Path, path: Path) -> "CallGraph":
        data = json.loads(path.read_text())
        g = cls(repo_root=repo_root)
        g.symbols = {k: SymbolInfo(**v) for k, v in data["symbols"].items()}
        g.by_short_name = data["by_short_name"]
        return g


class _CollectCalls(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.calls.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.calls.append(node.func.attr)
        self.generic_visit(node)


def _extract_from_file(path: Path, rel: str) -> list[SymbolInfo]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except SyntaxError:
        return []
    out: list[SymbolInfo] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parents = [n.name for n in ast.walk(tree)
                       if isinstance(n, ast.ClassDef) and node in ast.walk(n)]
            owner = parents[0] + "." + node.name if parents else node.name
            q = f"{rel}::{owner}"
            coll = _CollectCalls()
            for child in node.body:
                coll.visit(child)
            out.append(SymbolInfo(qualified=q, defined_in=rel, calls=coll.calls))
    return out


def build_graph(repo_root: Path) -> CallGraph:
    g = CallGraph(repo_root=repo_root)
    exclude_dirs = {".venv", ".aiforge", ".paperclip", "node_modules", ".git", "__pycache__"}
    for p in repo_root.rglob("*.py"):
        if any(part in exclude_dirs for part in p.parts):
            continue
        rel = str(p.relative_to(repo_root))
        for sym in _extract_from_file(p, rel):
            g.symbols[sym.qualified] = sym
            short = sym.qualified.split("::", 1)[1].split(".")[-1]
            g.by_short_name.setdefault(short, []).append(sym.qualified)
    return g


def _resolve_target(g: CallGraph, target: str) -> list[str]:
    """Return list of qualified names that match `target`."""
    if "::" in target:
        return [target] if target in g.symbols else []
    # file path only → all symbols in that file
    file_part = target.rstrip("/")
    return [q for q in g.symbols if q.startswith(file_part + "::")]


def blast_radius(g: CallGraph, target: str, max_depth: int = 3) -> dict:
    """Return files that would be impacted if `target` changed (upstream callers)."""
    roots = _resolve_target(g, target)
    if not roots:
        return {"target": target, "files": [], "symbols": [], "note": "target not found"}

    # Find callers: any symbol whose .calls mentions the short name(s) of a root.
    short_roots = {q.split("::", 1)[1].split(".")[-1] for q in roots}
    seen_syms: set[str] = set(roots)
    frontier = list(roots)
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: list[str] = []
        short_frontier = {q.split("::", 1)[1].split(".")[-1] for q in frontier}
        for q, info in g.symbols.items():
            if q in seen_syms:
                continue
            if any(c in short_frontier for c in info.calls):
                seen_syms.add(q)
                next_frontier.append(q)
        frontier = next_frontier
        depth += 1
    files = sorted({g.symbols[q].defined_in for q in seen_syms})
    return {
        "target": target,
        "root_symbols": roots,
        "affected_symbols": sorted(seen_syms - set(roots)),
        "files": files,
        "max_depth_reached": depth,
    }


def dependency_chain(g: CallGraph, target: str) -> dict:
    roots = _resolve_target(g, target)
    if not roots:
        return {"target": target, "callees": [], "callers": [], "note": "target not found"}
    callees: list[str] = []
    for q in roots:
        info = g.symbols[q]
        for c in info.calls:
            for hit in g.by_short_name.get(c, []):
                callees.append(hit)
    up = blast_radius(g, target, max_depth=1)
    return {
        "target": target,
        "callees": sorted(set(callees)),
        "callers": sorted(up["affected_symbols"]),
    }
