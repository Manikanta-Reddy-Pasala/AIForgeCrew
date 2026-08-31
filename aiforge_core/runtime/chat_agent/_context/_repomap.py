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


_SYM_NOISE = frozenset({"if", "for", "while", "switch", "catch", "return"})


def _file_symbols(path: str, pattern, max_syms: int) -> list[str]:
    """The file's top-level symbol names, capped. [] when it can't be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read(200_000)
    except Exception:  # noqa: BLE001
        return []
    syms: list[str] = []
    for m in pattern.finditer(src):
        nm = next((g for g in m.groups() if g), None)
        if nm and nm not in syms and nm not in _SYM_NOISE:
            syms.append(nm)
        if len(syms) >= max_syms:
            break
    return syms


def _build_symbol_map(cwd: str, max_files: int = 200, max_syms: int = 12) -> str:
    """A lightweight, dependency-free repo map: each source file → its top-level
    symbols (classes/functions/methods) via regex. Fast (no tree-sitter),
    language-agnostic, so the agent navigates by SYMBOLS not blind `find`."""
    import re as _re
    base = str(_workspace_root() or cwd)
    compiled = {ext: _re.compile(pat, _re.MULTILINE)
                for ext, pat in _SYM_PATTERNS.items()}
    rows: list[tuple[str, list[str]]] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            pattern = compiled.get(os.path.splitext(f)[1].lower())
            if pattern is None:
                continue
            if len(rows) >= max_files:
                return _fmt_symbol_rows(base, rows, truncated=True)
            fp = os.path.join(root, f)
            syms = _file_symbols(fp, pattern, max_syms)
            if syms:
                rows.append((os.path.relpath(fp, base), syms))
    return _fmt_symbol_rows(base, rows, truncated=False)


def _fmt_symbol_rows(_base: str, rows: list, truncated: bool) -> str:
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


def _aider_digest_bounded(base: str) -> str:
    """The tree-sitter + PageRank Aider digest, TIME-BOUNDED.

    The first parse of a big repo can be slow, so it runs in a thread with a
    short budget (AIFORGE_REPOMAP_BUDGET_S). If it doesn't finish in time we
    fall through to the instant dir tree — the cached Aider map then serves
    later turns. Never blocks the turn.
    """
    import threading as _th
    try:
        budget = float(os.environ.get("AIFORGE_REPOMAP_BUDGET_S", "30"))
    except ValueError:
        budget = 6.0
    out: dict = {}

    def _work():
        try:
            from aiforge_core.memory.code_context import aider_digest
            out["d"] = aider_digest(base, [])
        except Exception:  # noqa: BLE001
            out["d"] = ""

    t = _th.Thread(target=_work, daemon=True)
    t.start()
    t.join(budget)
    return out.get("d") or ""


def _capped(text: str, note: str) -> str:
    """Char cap — the line/depth caps bound entries but a wide tree can still be
    huge. Window-relative (A2): floor 6000 on a 32K window, grows with a bigger
    window so a 256K box shows a fuller map."""
    cap = _repomap_max_chars()
    return text[:cap] + note if (cap and len(text) > cap) else text


def _ranked_symbol_map(base: str) -> str:
    """Section 1: the ranked-symbol map (the good map for analysis)."""
    if os.environ.get("AIFORGE_CHAT_AIDER_MAP", "1") in ("0", "false"):
        return ""
    try:
        digest = _aider_digest_bounded(base)
    except Exception:  # noqa: BLE001
        return ""
    if not digest.strip():
        return ""       # RepoMap absent / timed out / empty → next strategy
    digest = _capped(digest, "\n… (truncated — grep/find/list_dir for more)")
    return ("REPO MAP (ranked symbols via tree-sitter — the key functions/"
            "classes per file; navigate by these, don't blind-`find`):\n"
            f"WORKING DIRECTORY: {base}\n{digest}")


def _regex_symbol_map(base: str) -> str:
    """Section 2: lightweight regex symbol map (no deps, fast) — file → its
    symbols."""
    if os.environ.get("AIFORGE_CHAT_SYMBOL_MAP", "1") in ("0", "false"):
        return ""
    try:
        symmap = _build_symbol_map(base)
    except Exception:  # noqa: BLE001
        return ""
    if not (symmap and symmap.strip()):
        return ""
    return ("REPO MAP (each file → its top-level classes/functions; "
            "navigate by these symbols, don't blind-`find`):\n"
            f"WORKING DIRECTORY: {base}\n{symmap}")


def _tree_rows(root: str, _dirs: list, files: list, base: str,
               depth: int) -> list[str]:
    """One directory's lines: its own name (except at the root) then its files."""
    rel = os.path.relpath(root, base)
    indent = "" if rel == "." else "  " * depth
    rows = [] if rel == "." else [f"{indent}{os.path.basename(root)}/"]
    return rows + [f"{indent}  {f}" for f in sorted(files)[:40]
                   if not f.startswith(".")]


def _dir_tree(base: str, max_entries: int, max_depth: int) -> str:
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
            lines += _tree_rows(root, dirs, files, base, depth)
            if len(lines) >= max_entries:
                lines.append("  … (truncated — use find/grep/list_dir for more)")
                break
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines) or "(empty)"


def _tree_map(base: str, max_entries: int, max_depth: int) -> str:
    """Section 3: the compact directory tree — the always-available fallback."""
    tree = _capped(_dir_tree(base, max_entries, max_depth),
                   "\n… (truncated to fit context — use find/grep/list_dir)")
    return ("REPO MAP of the working directory (already known — do NOT "
            f"re-list directories you can see here):\nWORKING DIRECTORY: {base}\n"
            f"{tree}")


def _build_repo_map(cwd: str, max_entries: int = 160, max_depth: int = 3) -> str:
    """Repo map for the system prompt so the agent navigates by SYMBOLS, not blind
    `find`. Prefers the tree-sitter + PageRank Aider RepoMap (ranked functions/
    classes per file) — critical on big repos where a bare file tree is useless;
    falls back to a regex symbol map, then a compact directory tree. Best-effort,
    char-capped."""
    base = str(_workspace_root() or cwd)
    if not os.path.isdir(base):
        return f"WORKING DIRECTORY: {base} (not a directory)"
    return (_ranked_symbol_map(base) or _regex_symbol_map(base)
            or _tree_map(base, max_entries, max_depth))


def _repo_name(cwd: str) -> str:
    # Canonical resolver (git-toplevel) — was workspace-dir basename, which
    # drifted from the recall key. Delegates now.
    from aiforge_core.runtime import repo_ident as _ri
    return _ri.repo_name(cwd, sentinel="repo")
