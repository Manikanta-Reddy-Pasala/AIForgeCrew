"""Failure taxonomy — F-001..F-012 hardcoded + extensible via YAML.

Per spec §5.4 + my Gap #3: F-013+ user-defined detectors load from
.aiforge/failure_taxonomy.yaml.

Public:
    failure_taxonomy.load_all()  -> list[FailureMode]
    failure_taxonomy.match(model_output, ctx) -> FailureMatch | None
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore


@dataclass
class FailureMode:
    id: str                                # "F-001", etc.
    name: str
    severity: str = "warn"                 # "warn" | "halt" | "retry"
    detector: str = ""                     # "regex" | "import_check" | "symbol_check" | etc.
    pattern: str = ""                      # detector-specific
    message: str = ""
    source: str = "builtin"                # "builtin" | "user"


# Built-in F-001..F-012 — short stubs; real detectors live with the
# code they monitor (e.g. F-001 hooks into the udiff applier).
_BUILTIN: list[FailureMode] = [
    FailureMode("F-001", "Hallucinated import",  detector="import_check"),
    FailureMode("F-002", "Hallucinated symbol",  detector="symbol_check"),
    FailureMode("F-003", "Diff context mismatch", detector="diff_hash"),
    FailureMode("F-004", "Test loop without progress", detector="loop_3x"),
    FailureMode("F-005", "Plan with unreachable step", detector="grounder_fail"),
    FailureMode("F-006", "Plan exceeds depth limit",   detector="depth_gt_7"),
    FailureMode("F-007", "Lint loop",                  detector="loop_3x"),
    FailureMode("F-008", "Type-check loop",            detector="loop_3x"),
    FailureMode("F-009", "Token budget overrun",       detector="budget_2x"),
    FailureMode("F-010", "Tool-call format error loop", detector="loop_3x"),
    FailureMode("F-011", "Skill misapplication",       detector="postcond_fail"),
    FailureMode("F-012", "Memory contradiction",       detector="contradiction"),
]


def _user_yaml_path() -> Path:
    return Path(os.environ.get(
        "AIFORGE_FAILURE_TAXONOMY",
        os.path.expanduser("~/.aiforge/failure_taxonomy.yaml"),
    ))


def load_all() -> list[FailureMode]:
    """Built-ins + user-defined modes from YAML (F-013+).

    YAML schema:
        modes:
          - id: F-013
            name: SQL injection candidate
            severity: halt
            detector: regex
            pattern: "(?i)select.*from.*where.*\\$\\{"
            message: "User input concatenated into SQL"
    """
    out = list(_BUILTIN)
    if yaml is None:
        return out
    p = _user_yaml_path()
    if not p.is_file():
        return out
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError:
        return out
    for m in data.get("modes") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        out.append(FailureMode(
            id=str(m["id"]),
            name=str(m.get("name", "")),
            severity=str(m.get("severity", "warn")),
            detector=str(m.get("detector", "regex")),
            pattern=str(m.get("pattern", "")),
            message=str(m.get("message", "")),
            source="user",
        ))
    return out


@dataclass
class FailureMatch:
    mode: FailureMode
    evidence: str = ""
    ctx: dict[str, Any] = field(default_factory=dict)


def match(text: str, *, ctx: dict[str, Any] | None = None) -> FailureMatch | None:
    """Match `text` against regex-based modes only. Other detectors
    (import_check, diff_hash, loop_3x) are wired at their respective
    runtime sites and call this module to record the match.
    """
    ctx = ctx or {}
    for m in load_all():
        if m.detector != "regex" or not m.pattern:
            continue
        try:
            if re.search(m.pattern, text):
                return FailureMatch(mode=m, evidence=text[:200], ctx=ctx)
        except re.error:
            continue
    return None


def record(mode_id: str, evidence: str, ctx: dict[str, Any] | None = None) -> FailureMatch:
    """Site-specific detectors call this when they trip a mode."""
    by_id = {m.id: m for m in load_all()}
    if mode_id not in by_id:
        raise KeyError(f"unknown failure mode: {mode_id}")
    return FailureMatch(mode=by_id[mode_id], evidence=evidence, ctx=ctx or {})
