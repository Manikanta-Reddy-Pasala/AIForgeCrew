"""Real detectors for the failure taxonomy.

What makes this stack smarter than smolagents / aider / generic agents
is that we don't just observe failures — we BLOCK them at the source
using the AiForgeMemory graph as ground truth.

Detectors:
    LoopDetector              F-004, F-007, F-008, F-010
        — same output 3x in a row = stuck. Hash-based.
    HallucinatedImportDetector F-001
        — query AiForgeMemory IMPORTS graph + package manifest;
          import not found in either = hallucination.
    HallucinatedSymbolDetector F-002
        — query Symbol_v2 graph; symbol not found = hallucination.
    DiffContextHashDetector    F-003
        — udiff context lines must hash-match the target file.
    DepthDetector              F-006
        — plan.steps > 7 → reject; force task split.
    TokenBudgetDetector        F-009
        — actual_tokens > 2x expected → trip.

Each detector returns None on clean, FailureMatch on dirty.
"""
from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from aiforge_core.aiforge_agents.runtime import failure_taxonomy as ft


# ─────────── F-004 / F-007 / F-008 / F-010 — loop detector ─────────────

class LoopDetector:
    """Track last N outputs (hashes); trip if same hash 3x consecutive.

    Use one instance per (ticket, step_kind) — e.g. one for test runs,
    one for lint runs, one for tool-call format errors.
    """
    def __init__(self, *, window: int = 3, mode_id: str = "F-004") -> None:
        self.window = window
        self.mode_id = mode_id
        self._buf: deque[str] = deque(maxlen=window)

    @staticmethod
    def _hash(s: str) -> str:
        return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()[:16]

    def record(self, output: str) -> ft.FailureMatch | None:
        h = self._hash(output)
        self._buf.append(h)
        if len(self._buf) == self.window and len(set(self._buf)) == 1:
            return ft.record(
                self.mode_id,
                evidence=output[:200],
                ctx={"window": self.window, "hash": h},
            )
        return None

    def reset(self) -> None:
        self._buf.clear()


# ─────────── F-001 — hallucinated-import detector ──────────────────────

# Java/Kotlin: `import com.foo.Bar;`
# Python:  `from foo import bar` / `import foo`
# TS:      `import { x } from "./foo"` / `import foo from "foo"`
_IMPORT_PATTERNS = {
    "java":       re.compile(r"^\s*import\s+([\w.]+)(?:\.\*)?;", re.MULTILINE),
    "kotlin":     re.compile(r"^\s*import\s+([\w.]+)(?:\.\*)?", re.MULTILINE),
    "python_imp": re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE),
    "python_frm": re.compile(r"^\s*from\s+([\w.]+)\s+import", re.MULTILINE),
    "typescript": re.compile(r"^\s*import\s+(?:[^\"']+\s+from\s+)?[\"']([^\"']+)[\"']",
                             re.MULTILINE),
}


def extract_imports(text: str) -> set[str]:
    """All import targets found in the diff/file content.

    Tolerates udiff line prefixes (`+ `, `- `, ` `) by stripping them
    before regex match.
    """
    # Strip udiff prefix (single +/-/space at line start) so the
    # `^\s*import` patterns match diff-added lines.
    cleaned = re.sub(r"(?m)^[+\- ]", "", text)
    out: set[str] = set()
    for pat in _IMPORT_PATTERNS.values():
        for m in pat.finditer(cleaned):
            out.add(m.group(1))
    return out


@dataclass
class HallucinatedImportDetector:
    """Imports must resolve in AiForgeMemory IMPORTS graph OR be in
    a known package manifest (pom/maven, package.json, requirements.txt).
    Otherwise = hallucination = block before applying.
    """
    repo: str = ""
    driver: Any = None      # neo4j driver, can be None for unit tests
    known_packages: set[str] = field(default_factory=set)

    def check(self, diff_text: str) -> list[ft.FailureMatch]:
        imports = extract_imports(diff_text)
        if not imports:
            return []
        bad: list[str] = []
        for imp in imports:
            if self._is_known(imp):
                continue
            bad.append(imp)
        return [
            ft.record(
                "F-001",
                evidence=imp,
                ctx={"import": imp, "repo": self.repo},
            )
            for imp in bad
        ]

    def _is_known(self, imp: str) -> bool:
        # Standard library & built-ins always pass.
        if imp.startswith(("java.", "javax.", "jakarta.", "kotlin.")):
            return True
        if imp.startswith("std::") or imp in {"os", "sys", "json", "re", "time"}:
            return True
        # Operator-supplied manifest hits
        for pkg in self.known_packages:
            if imp == pkg or imp.startswith(pkg + ".") or imp.startswith(pkg + "/"):
                return True
        # Graph-resolved
        if self.driver is None:
            return False
        with self.driver.session() as s:
            row = s.run(
                "MATCH (f:File_v2 {repo:$repo})-[:IMPORTS]->(g:File_v2) "
                "WHERE g.path CONTAINS $needle RETURN g.path LIMIT 1",
                repo=self.repo, needle=imp.replace(".", "/"),
            ).single()
        return row is not None


# ─────────── F-002 — hallucinated-symbol detector ──────────────────────

@dataclass
class HallucinatedSymbolDetector:
    """Each symbol referenced in a diff (Class.method, package.Class)
    must exist in Symbol_v2 of this repo OR be a JDK / stdlib element.
    Caller passes the list of symbols found in the diff (LSP / grep).
    """
    repo: str = ""
    driver: Any = None
    stdlib_prefixes: tuple[str, ...] = (
        "java.", "javax.", "jakarta.", "kotlin.", "org.springframework.",
        "com.fasterxml.", "lombok.", "reactor.", "org.slf4j.",
    )

    def check(self, symbols: list[str]) -> list[ft.FailureMatch]:
        if not symbols or self.driver is None:
            return []
        bad: list[str] = []
        with self.driver.session() as s:
            for sym in symbols:
                if any(sym.startswith(p) for p in self.stdlib_prefixes):
                    continue
                row = s.run(
                    "MATCH (sym:Symbol_v2 {repo:$repo}) "
                    "WHERE sym.fqname ENDS WITH $needle RETURN sym LIMIT 1",
                    repo=self.repo, needle="::" + sym.rsplit(".", 1)[-1],
                ).single()
                if row is None:
                    bad.append(sym)
        return [
            ft.record("F-002", evidence=sym, ctx={"symbol": sym, "repo": self.repo})
            for sym in bad
        ]


# ─────────── F-003 — diff context hash mismatch ────────────────────────

class DiffContextHashDetector:
    """Unified-diff context lines (the unchanged ones starting with ' ')
    must match the actual file contents at the target hunk position.
    Hash mismatch = diff stale/hallucinated.
    """

    _HUNK_RE = re.compile(
        r"@@ -(\d+),?(\d*) \+\d+,?\d* @@"
    )

    @staticmethod
    def _file_lines(path_text: str) -> list[str]:
        return path_text.splitlines()

    @classmethod
    def check(cls, *, udiff: str, file_text: str) -> ft.FailureMatch | None:
        """Compare each hunk's context lines to file_text."""
        lines = cls._file_lines(file_text)
        for m in cls._HUNK_RE.finditer(udiff):
            start = int(m.group(1)) - 1
            chunk_start = m.end()
            chunk_end = udiff.find("@@", chunk_start)
            if chunk_end == -1:
                chunk_end = len(udiff)
            chunk = udiff[chunk_start:chunk_end]
            offset = 0
            for ln in chunk.splitlines():
                if not ln:
                    continue
                tag, body = ln[:1], ln[1:]
                if tag == " ":
                    actual = lines[start + offset] if start + offset < len(lines) else ""
                    if actual != body:
                        return ft.record(
                            "F-003",
                            evidence=f"expected={body!r} actual={actual!r}",
                            ctx={"hunk_line": start + offset + 1},
                        )
                    offset += 1
                elif tag == "-":
                    offset += 1
        return None


# ─────────── F-006 — plan depth ───────────────────────────────────────

def check_plan_depth(plan: dict[str, Any], *, max_depth: int = 7) -> ft.FailureMatch | None:
    steps = plan.get("steps") or []
    if len(steps) > max_depth:
        return ft.record(
            "F-006",
            evidence=f"steps={len(steps)} > max={max_depth}",
            ctx={"steps": len(steps), "max": max_depth},
        )
    return None


# ─────────── F-009 — token budget ─────────────────────────────────────

def check_token_budget(used: int, *, expected: int,
                       multiplier: float = 2.0) -> ft.FailureMatch | None:
    if expected <= 0:
        return None
    if used > multiplier * expected:
        return ft.record(
            "F-009",
            evidence=f"used={used} expected={expected} mult={multiplier}",
            ctx={"used": used, "expected": expected},
        )
    return None
