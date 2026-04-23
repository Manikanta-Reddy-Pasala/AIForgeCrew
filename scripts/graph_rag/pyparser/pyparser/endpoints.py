"""REST endpoint detection for FastAPI, Flask, aiohttp, Django view decorators."""
from __future__ import annotations

import ast
import re


FASTAPI_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
# Decorator names that take (path, ...) positionally
FLASK_ROUTE = {"route", "get", "post", "put", "patch", "delete"}


def _decorator_name(dec: ast.AST) -> str:
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Attribute):
        return dec.attr
    if isinstance(dec, ast.Name):
        return dec.id
    return ""


def _first_str_arg(dec: ast.Call) -> str | None:
    if not dec.args:
        return None
    a0 = dec.args[0]
    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
        return a0.value
    return None


def _http_method_from_route(dec: ast.Call) -> str | None:
    for kw in dec.keywords or []:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            for v in kw.value.elts:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    return v.value.upper()
    return None


def detect_endpoints(tree: ast.Module, pkg: str):
    endpoints: list[dict] = []
    external: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            name = _decorator_name(dec)
            path = _first_str_arg(dec)
            if not path:
                continue
            if name in FASTAPI_METHODS:
                endpoints.append({
                    "http": name.upper(),
                    "path": path,
                    "framework": "fastapi",
                    "handler": node.name,
                    "package": pkg,
                    "line": node.lineno,
                })
            elif name == "route":
                m = _http_method_from_route(dec) or "GET"
                endpoints.append({
                    "http": m,
                    "path": path,
                    "framework": "flask",
                    "handler": node.name,
                    "package": pkg,
                    "line": node.lineno,
                })
            elif name in FLASK_ROUTE and name != "route":
                endpoints.append({
                    "http": name.upper(),
                    "path": path,
                    "framework": "flask",
                    "handler": node.name,
                    "package": pkg,
                    "line": node.lineno,
                })

    # External calls: httpx/requests URL literals
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        fn_name = ""
        if isinstance(call.func, ast.Attribute):
            fn_name = call.func.attr
            root = call.func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            root_name = root.id if isinstance(root, ast.Name) else ""
            if root_name not in {"httpx", "requests", "aiohttp", "urllib"}:
                continue
        elif isinstance(call.func, ast.Name):
            fn_name = call.func.id
        if fn_name not in {"get", "post", "put", "patch", "delete", "request", "head"}:
            continue
        if not call.args:
            continue
        a0 = call.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            url = a0.value
            if url.startswith(("http://", "https://", "/v1/", "/api/")):
                external.append({"url": url, "via": fn_name, "line": call.lineno})

    return endpoints, external
