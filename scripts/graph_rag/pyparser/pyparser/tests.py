"""pytest test discovery: functions named test_* or methods on Test* classes."""
from __future__ import annotations

import ast


def detect_tests(tree: ast.Module) -> list[dict]:
    tests: list[dict] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            tests.append({"kind": "test", "name": node.name, "line": node.lineno})
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for it in node.body:
                if isinstance(it, (ast.FunctionDef, ast.AsyncFunctionDef)) and it.name.startswith("test_"):
                    tests.append({
                        "kind": "test",
                        "name": f"{node.name}.{it.name}",
                        "line": it.lineno,
                    })
    return tests
