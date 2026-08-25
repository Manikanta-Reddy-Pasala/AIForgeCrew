"""API-contract sidecars, blackboard, test matching, aggregation merge.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import subprocess
import threading

from pydantic import BaseModel

from aiforge_core.runtime import review_gates
from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS, ensure_artifact_gitignore

_CONTRACT_DIR = ".aiforge-contracts"


def _path_to_module(path: str) -> str:
    p = str(path or "").lstrip("/")
    for ext in (".py", ".java", ".go", ".ts", ".tsx", ".js", ".rs", ".rb"):
        if p.endswith(ext):
            p = p[: -len(ext)]
            break
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".").strip(".")


def _first_json_object(blob: str):
    """The first COMPLETE ``{...}`` object in ``blob``, brace-balanced so
    trailing prose after the declaration does not break the parse. None when
    there is no balanced object or it does not parse."""
    import json as _json
    depth = 0
    for i, ch in enumerate(blob):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return _json.loads(blob[:i + 1])
                except Exception:  # noqa: BLE001
                    return None
    return None


def _write_contract_sidecar(worktree: str, subtask: dict, out: str) -> None:
    """Parse the worker's ``===CONTRACT=== {json}`` interface declaration and
    persist it under ``.aiforge-contracts/`` so the merger has a language-agnostic,
    worker-declared blackboard (exposes/consumes) to reconcile over."""
    import json as _json
    import re as _re
    m = _re.search(r"===CONTRACT===\s*(\{.*)", out, _re.DOTALL)
    if not m:
        return
    obj = _first_json_object(m.group(1))
    if obj is None:
        return
    path = str(subtask.get("path") or "")
    slug = str(subtask.get("slug") or _path_to_module(path) or "sub")
    rec = {"module": _path_to_module(path), "path": path,
           "exposes": obj.get("exposes") or [], "consumes": obj.get("consumes") or {}}
    # Write to the shared PROJECT ROOT (not the isolated worktree) so all workers'
    # contracts land in one place for the merger — no commit/merge, no tree
    # pollution. Concurrent workers use distinct filenames (per slug).
    marker = os.sep + ".aiforge-worktrees" + os.sep
    root = worktree.split(marker)[0] if marker in worktree else worktree
    d = os.path.join(root, _CONTRACT_DIR)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, _slugify(slug) + ".json"), "w", encoding="utf-8") as fh:
        fh.write(_json.dumps(rec))


_DECL_KEYWORDS = frozenset({
    "class", "def", "func", "function", "const", "let", "var", "public",
    "private", "protected", "static", "final", "void", "struct", "type",
    "interface", "fn", "val", "enum", "abstract", "async", "export", "default",
})


def _clean_symbol(s: str) -> str:
    """The declared NAME from an api entry ('class Board' → 'Board',
    'def drop(x)' → 'drop', 'COLORS: dict' → 'COLORS')."""
    import re as _re
    for t in _re.findall(r"[A-Za-z_]\w*", str(s)):
        if t not in _DECL_KEYWORDS:
            return t
    return ""


def _read_contract_files(cdir: str) -> list[dict]:
    """Every readable contract sidecar in ``cdir``."""
    import json as _json
    out = []
    for f in os.listdir(cdir):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(cdir, f), encoding="utf-8",
                      errors="replace") as fh:
                out.append(_json.load(fh))
        except Exception:  # noqa: BLE001
            continue
    return out


def _declared_exposes(records: list[dict]) -> dict[str, set]:
    exposes: dict[str, set] = {}
    for rec in records:
        mod = rec.get("module") or ""
        if not mod:
            continue
        exposes[mod] = {n for n in (_clean_symbol(e)
                                    for e in (rec.get("exposes") or [])) if n}
    return exposes


def _declared_consumes(records: list[dict], mods: set) -> list[tuple]:
    """``(consumer, target, name)`` for each declared import, with the target
    matched loosely by last segment when the full name is not known."""
    consumes: list[tuple[str, str, str]] = []
    for rec in records:
        cons = rec.get("module") or ""
        for tgtmod, names in (rec.get("consumes") or {}).items():
            last = str(tgtmod).split(".")[-1]
            tgt = (tgtmod if tgtmod in mods
                   else next((m for m in mods if m.split(".")[-1] == last), None))
            if not tgt:
                continue
            consumes.extend((cons, tgt, n) for n in
                            (_clean_symbol(x) for x in (names or [])) if n)
    return consumes


def _blackboard_from_contracts(cwd: str):
    """Read the declared contract sidecars → (exposes{mod:set}, consumes[(cons,tgt,name)]).
    Returns None when no contracts were declared (→ AST fallback)."""
    cdir = os.path.join(cwd, _CONTRACT_DIR)
    if not os.path.isdir(cdir):
        return None
    records = _read_contract_files(cdir)
    exposes = _declared_exposes(records)
    if not exposes:
        return None
    return exposes, _declared_consumes(records, set(exposes))


def _is_test_subtask(s: dict) -> bool:
    """True when this subtask produces a TEST file (built first, in test-first)."""
    p = (str(s.get("path") or "") + " " + str(s.get("slug") or "")).lower()
    base = os.path.basename(str(s.get("path") or "")).lower()
    return ("test" in base or "/test" in p or "/spec" in p
            or base.startswith("test") or base.endswith(("_test.py", "_test.go",
            ".test.js", ".test.ts", ".spec.ts", ".spec.js"))
            or "test-" in p or "-test" in p)


def _matching_tests_for(cwd: str, impl_path: str) -> str:
    """Read the test file(s) whose module name matches ``impl_path`` (board.py →
    test_board.py / board_test.* / test/…/board.*), so the impl is built to PASS
    them. Returns concatenated test source (capped)."""
    if not impl_path:
        return ""
    stem = os.path.splitext(os.path.basename(impl_path))[0].lower()
    if not stem:
        return ""
    out: list[str] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".aiforge-venv", ".venv", "__pycache__")]
        for f in files:
            fl = f.lower()
            is_test = ("test" in fl or ".spec." in fl)
            if is_test and stem in fl:
                try:
                    with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                        rel = os.path.relpath(os.path.join(root, f), cwd)
                        out.append(f"=== {rel} ===\n{fh.read()[:4000]}")
                except Exception:  # noqa: BLE001
                    pass
    return "\n\n".join(out)[:8000]


def _merge_aggs(a: dict, b: dict) -> dict:
    """Combine two run_parallel aggregates (test phase + impl phase)."""
    a, b = a or {}, b or {}

    def _sum(key: str) -> int:
        return (a.get(key) or 0) + (b.get(key) or 0)

    return {
        "ok": bool(a.get("ok", True)) and bool(b.get("ok", True)),
        "total": _sum("total"), "done": _sum("done"),
        "failed": _sum("failed"), "validated": _sum("validated"),
        "merged": _sum("merged"),
        "conflicts": (a.get("conflicts") or []) + (b.get("conflicts") or []),
        "review": b.get("review") or a.get("review") or "done",
    }

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._worktree import _slugify
