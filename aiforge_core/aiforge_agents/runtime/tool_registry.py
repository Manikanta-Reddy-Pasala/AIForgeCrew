"""Tool registry — atomic primitives only. No sandbox.

Per spec §5.3 minus sandbox/exec_python. Higher-level capabilities
are skills (built atop these), not tools.

Tools (host-side, no Docker):
    shell.run            execute shell command in working tree
    fs.read/write/delete file ops
    git.diff/commit/apply_udiff/log
    http.get             external knowledge fetch (KGR only)
    code_review_graph.query   AiForgeMemory MCP/HTTP query
    memory.expand        dereference memory_id
    knowledge_gap_resolver.resolve  web/SO/self-distill
"""
from __future__ import annotations

from typing import Any, Callable

_TOOLS: dict[str, Callable[..., Any]] = {}


def register(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _TOOLS:
            raise ValueError(f"tool '{name}' already registered")
        _TOOLS[name] = fn
        return fn
    return _wrap


def get(name: str) -> Callable[..., Any]:
    return _TOOLS[name]


def known() -> list[str]:
    return sorted(_TOOLS)


def call(name: str, **kwargs) -> Any:
    return _TOOLS[name](**kwargs)
