"""Extract Python source to JSONL for Neo4j ingestion.

Usage:
    aiforge-pyparser --repo ~/code/PosPythonBackend --out /tmp/ppb.py.jsonl
    aiforge-pyparser --repo <path> --files a.py b.py --out out.jsonl
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any

from .endpoints import detect_endpoints
from .integrations import detect_integrations
from .tests import detect_tests


EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "dist", "build", ".aiforge", ".mypy_cache", ".pytest_cache",
}


def iter_py_files(root: Path, only: list[str] | None):
    if only:
        for f in only:
            p = Path(f)
            if p.is_absolute():
                yield p
            else:
                yield root / p
        return
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(".py"):
                yield Path(dp) / fn


def extract_ast(src: str, rel: str, repo: str) -> dict:
    tree = ast.parse(src)
    pkg = rel.replace("/", ".").rsplit(".py", 1)[0]

    classes: list[dict] = []
    functions: list[dict] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cls = {
                "kind": "class",
                "simple": node.name,
                "fqn": f"{pkg}.{node.name}",
                "start_line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "annotations": [ast.unparse(d) for d in node.decorator_list],
                "methods": [],
                "javadoc": ast.get_docstring(node) or "",
            }
            for it in node.body:
                if isinstance(it, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m = {
                        "kind": "method",
                        "name": it.name,
                        "fqn": f"{pkg}.{node.name}.{it.name}",
                        "sig": _signature(it),
                        "return_type": ast.unparse(it.returns) if it.returns else "",
                        "annotations": [ast.unparse(d) for d in it.decorator_list],
                        "start_line": it.lineno,
                        "end_line": getattr(it, "end_lineno", it.lineno),
                        "body_snippet": ast.unparse(it)[:4096],
                        "is_async": isinstance(it, ast.AsyncFunctionDef),
                        "javadoc": ast.get_docstring(it) or "",
                    }
                    cls["methods"].append(m)
            classes.append(cls)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "kind": "function",
                "name": node.name,
                "fqn": f"{pkg}.{node.name}",
                "sig": _signature(node),
                "return_type": ast.unparse(node.returns) if node.returns else "",
                "annotations": [ast.unparse(d) for d in node.decorator_list],
                "start_line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "body_snippet": ast.unparse(node)[:4096],
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "javadoc": ast.get_docstring(node) or "",
            })

    imports = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            imports.append(n.module)
        elif isinstance(n, ast.Import):
            for a in n.names:
                imports.append(a.name)

    endpoints, external = detect_endpoints(tree, pkg)
    integrations = detect_integrations(src)
    tests = detect_tests(tree) if ("test_" in rel or rel.endswith("_test.py")) else []

    return {
        "lang": "python",
        "repo": repo,
        "file": rel,
        "kind": "file",
        "package": pkg,
        "imports": imports,
        "classes": classes,
        "functions": functions,
        "endpoints": endpoints,
        "externalEndpoints": external,
        "integrations": integrations,
        "tests": tests,
    }


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(fn.args)
    except Exception:
        args = ""
    return f"{fn.name}({args})"[:200]


def main() -> int:
    ap = argparse.ArgumentParser(prog="aiforge-pyparser")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--files", nargs="*")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    repo_name = root.name
    count = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for path in iter_py_files(root, args.files):
            try:
                src = path.read_text(encoding="utf-8", errors="ignore")
                rel = str(path.relative_to(root))
                rec = extract_ast(src, rel, repo_name)
                fh.write(json.dumps(rec))
                fh.write("\n")
                count += 1
            except SyntaxError:
                continue
            except Exception as exc:
                print(f"  skip {path}: {exc}", file=sys.stderr)
    print(f"extracted {count} files from {repo_name} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
