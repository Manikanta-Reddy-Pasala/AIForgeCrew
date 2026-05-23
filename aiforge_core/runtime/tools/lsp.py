"""LSP integration (standards gap C8).

KISS thin wrapper around ``multilspy`` exposing ``go-to-def``,
``find-refs``, ``hover`` as Doer tools. Falls back to a clear error
when the LSP server (pyright/gopls/tsserver/jedi) isn't installed —
the operator decides whether to install on the runner host.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiforge_core.runtime.sandbox import root

log = logging.getLogger("aiforge.tools.lsp")

_SUFFIX_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "typescript", ".jsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
}


def _detect(path: str) -> str | None:
    return _SUFFIX_LANG.get(Path(path).suffix.lower())


def _build_server(language: str, repo_root: Path):
    """Return a started ``multilspy`` server context manager or None."""
    try:
        from multilspy import SyncLanguageServer
        from multilspy.multilspy_config import MultilspyConfig
    except ImportError:
        return None
    try:
        config = MultilspyConfig.from_dict({
            "code_language": language,
            "trace_lsp_communication": False,
        })
        return SyncLanguageServer.create(config, None, str(repo_root))
    except Exception as exc:  # noqa: BLE001
        log.debug("lsp build failed: %s", exc)
        return None


def lsp(
    command: str,
    path: str = "",
    line: int = 0,
    character: int = 0,
) -> dict[str, Any]:
    """Run an LSP query against ``path``.

    Args:
        command: ``goto_definition`` | ``find_references`` | ``hover``.
        path: file path relative to ``AIFORGE_REPO_ROOT``.
        line: 0-indexed line.
        character: 0-indexed column.

    Returns ``{ok, results}`` on success, ``{ok: False, error: ...}``
    otherwise. ``results`` is the list multilspy returned.
    """
    cmd = (command or "").strip().lower()
    if cmd not in {"goto_definition", "find_references", "hover"}:
        return {"ok": False, "error": "unknown_command",
                "supported": ["goto_definition", "find_references", "hover"]}
    if not path:
        return {"ok": False, "error": "empty_path"}
    language = _detect(path)
    if language is None:
        return {"ok": False, "error": "unsupported_language",
                "path": path}
    repo = root()
    server = _build_server(language, repo)
    if server is None:
        return {"ok": False, "error": "missing_multilspy_or_server",
                "hint": "pip install multilspy"}
    try:
        with server.start_server():
            if cmd == "goto_definition":
                res = server.request_definition(path, line, character)
            elif cmd == "find_references":
                res = server.request_references(path, line, character)
            else:
                res = server.request_hover(path, line, character)
        return {"ok": True, "command": cmd, "results": res}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"lsp_call_failed: {exc}"}


__all__ = ["lsp"]
