from __future__ import annotations

import os

from .._shell import _workspace_root
from .._tools import _SKIP_DIRS
from ._window import _window_scaled


def _repomap_max_chars() -> int:
    """Char cap for the repo-map block. An explicit ``AIFORGE_REPOMAP_MAX_CHARS``
    wins verbatim (0 disables); otherwise window-relative (A2): floor 6000,
    growing at ~2% of the window."""
    env = os.environ.get("AIFORGE_REPOMAP_MAX_CHARS")
    if env is not None:
        try:
            return max(0, int(env))
        except (TypeError, ValueError):
            pass
    return _window_scaled(6000, 0.02)


_SYM_PATTERNS = {
    ".py": r"^\s*(?:async\s+)?(?:class|def)\s+(\w+)",
    ".java": r"^\s*(?:@\w+\s*)*(?:public|private|protected|static|final|abstract|\s)*"
             r"(?:class|interface|enum|record)\s+(\w+)"
             r"|^\s*(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s.]+?\s+(\w+)\s*\(",
    ".go": r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)|^\s*type\s+(\w+)\s",
    ".ts": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|const|enum)\s+(\w+)",
    ".tsx": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|const)\s+(\w+)",
    ".js": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|const)\s+(\w+)",
    ".rb": r"^\s*(?:class|module|def)\s+([\w.]+)",
    ".rs": r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|impl)\s+(\w+)",
    ".c": r"^\s*[\w\*\s]+?\s+(\w+)\s*\([^;]*\)\s*\{",
    ".cpp": r"^\s*(?:class|struct)\s+(\w+)|^\s*[\w:<>\*&\s]+?\s+(\w+)\s*\([^;]*\)\s*\{",
    ".cs": r"^\s*(?:public|private|protected|internal|static|\s)*(?:class|interface|struct|enum)\s+(\w+)",
    ".kt": r"^\s*(?:fun|class|interface|object)\s+(\w+)",
    ".php": r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|function)\s+(\w+)",
}


def _build_symbol_map(cwd: str, max_files: int = 200, max_syms: int = 12) -> str:
    """A lightweight, dependency-free repo map: each source file → its top-level
    symbols (classes/functions/methods) via regex. Fast (no tree-sitter/aider),
    language-agnostic, so the agent navigates by SYMBOLS not blind `find`."""
    import re as _re
    base = str(_workspace_root() or cwd)
    compiled = {ext: _re.compile(pat, _re.MULTILINE)
                for ext, pat in _SYM_PATTERNS.items()}
    rows: list[tuple[str, list[str]]] = []
    seen = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext not in compiled:
                continue
            if seen >= max_files:
                return _fmt_symbol_rows(base, rows, truncated=True)
            fp = os.path.join(root, f)
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    src = fh.read(200_000)
            except Exception:  # noqa: BLE001
                continue
            syms: list[str] = []
            for m in compiled[ext].finditer(src):
                nm = next((g for g in m.groups() if g), None)
                if nm and nm not in syms and nm not in ("if", "for", "while",
                                                        "switch", "catch", "return"):
                    syms.append(nm)
                if len(syms) >= max_syms:
                    break
            if syms:
                rows.append((os.path.relpath(fp, base), syms))
                seen += 1
    return _fmt_symbol_rows(base, rows, truncated=False)


def _fmt_symbol_rows(base: str, rows: list, truncated: bool) -> str:
    if not rows:
        return ""
    cap = _repomap_max_chars()
    out: list[str] = []
    total = 0
    for rel, syms in rows:
        line = f"{rel}: {', '.join(syms)}"
        if cap and total + len(line) > cap:
            truncated = True
            break
        out.append(line)
        total += len(line) + 1
    body = "\n".join(out)
    tail = "\n… (more — grep/find for the rest)" if truncated else ""
    return body + tail


def _build_repo_map(cwd: str, max_entries: int = 160, max_depth: int = 3) -> str:
    """Repo map for the system prompt so the agent navigates by SYMBOLS, not blind
    `find`. Prefers the tree-sitter + PageRank Aider RepoMap (ranked functions/
    classes per file) — critical on big repos where a bare file tree is useless;
    falls back to a compact directory tree. Best-effort, char-capped."""
    base = str(_workspace_root() or cwd)
    if not os.path.isdir(base):
        return f"WORKING DIRECTORY: {base} (not a directory)"
    # 1. Tree-sitter Aider RepoMap — ranked symbols (the good map for analysis).
    #    TIME-BOUNDED: the first parse of a big repo can be slow, so run it in a
    #    thread with a short budget (AIFORGE_REPOMAP_BUDGET_S, default 6s). If it
    #    doesn't finish in time, fall through to the instant dir tree — the cached
    #    Aider map then serves later turns. Never blocks the turn.
    if os.environ.get("AIFORGE_CHAT_AIDER_MAP", "1") not in ("0", "false"):
        try:
            budget = float(os.environ.get("AIFORGE_REPOMAP_BUDGET_S", "30"))
        except ValueError:
            budget = 6.0
        _out: dict = {}

        def _work():
            try:
                from aiforge_core.memory.code_context import aider_digest
                _out["d"] = aider_digest(base, [])
            except Exception:  # noqa: BLE001
                _out["d"] = ""
        import threading as _th
        _t = _th.Thread(target=_work, daemon=True)
        _t.start()
        _t.join(budget)
        digest = _out.get("d") or ""
        if digest.strip():
            cap = _repomap_max_chars()
            if cap and len(digest) > cap:
                digest = digest[:cap] + "\n… (truncated — grep/find/list_dir for more)"
            return ("REPO MAP (ranked symbols via tree-sitter — the key functions/"
                    "classes per file; navigate by these, don't blind-`find`):\n"
                    f"WORKING DIRECTORY: {base}\n{digest}")
        # else: aider absent / timed out / empty → lightweight regex symbol map.

    # 2. Lightweight regex symbol map (no deps, fast) — file → its symbols.
    if os.environ.get("AIFORGE_CHAT_SYMBOL_MAP", "1") not in ("0", "false"):
        try:
            symmap = _build_symbol_map(base)
            if symmap and symmap.strip():
                return ("REPO MAP (each file → its top-level classes/functions; "
                        "navigate by these symbols, don't blind-`find`):\n"
                        f"WORKING DIRECTORY: {base}\n{symmap}")
        except Exception:  # noqa: BLE001
            pass
    lines: list[str] = []
    base_depth = base.rstrip(os.sep).count(os.sep)
    try:
        for root, dirs, files in os.walk(base):
            depth = root.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS
                             and not d.startswith("."))
            rel = os.path.relpath(root, base)
            indent = "" if rel == "." else "  " * depth
            if rel != ".":
                lines.append(f"{indent}{os.path.basename(root)}/")
            for f in sorted(files)[:40]:
                if not f.startswith("."):
                    lines.append(f"{indent}  {f}")
            if len(lines) >= max_entries:
                lines.append("  … (truncated — use find/grep/list_dir for more)")
                break
    except Exception:  # noqa: BLE001
        pass
    tree = "\n".join(lines) or "(empty)"
    # Char cap — the line/depth caps above bound entries but a wide tree can
    # still be huge. Window-relative (A2): floor 6000 on a 32K window, grows
    # with a bigger window so a 256K box shows a fuller map.
    cap = _repomap_max_chars()
    if cap and len(tree) > cap:
        tree = tree[:cap] + "\n… (truncated to fit context — use find/grep/list_dir)"
    return ("REPO MAP of the working directory (already known — do NOT "
            f"re-list directories you can see here):\nWORKING DIRECTORY: {base}\n"
            f"{tree}")


def _repo_name(cwd: str) -> str:
    # Canonical resolver (git-toplevel) — was workspace-dir basename, which
    # drifted from the recall key. Delegates now.
    from aiforge_core.runtime import repo_ident as _ri
    return _ri.repo_name(cwd, sentinel="repo")
