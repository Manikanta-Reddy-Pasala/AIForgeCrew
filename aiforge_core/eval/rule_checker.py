"""Harness pre-flight rule checker (enforcement Layer C).

Walks the recorded trace events from a finished agent run and asserts
that the agent honored its ``AgentContract``: no forbidden tool name was
called, turn count stayed within budget, wall clock stayed within
budget. This is a backstop for the structural ADK filter (Layer A) and
the GA ``tool_before_callback`` reject (Layer B).

Trace event shapes accepted (any one is enough; they are merged):

* ``{"tool_calls": [{"name": ...}, ...]}`` — ADK / OpenAI-style
* ``{"tool_name": "..."}`` — GA-style flattened event
* ``{"name": "...", "type": "tool_call"}`` — fallback shape
* ``{"raw": "...marker text..."}`` — for harness records that only kept
  the GA stdout markers; we re-scan with the same regex used by
  ``run_genericagent_eval._compute_metrics``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aiforge_core.agents import AgentContract


_TOOL_MARKER_RX = re.compile(r"🛠️\s*Tool:\s*`([a-z_]+)`")
_TOOL_COMPACT_RX = re.compile(r"🛠️\s+([a-z_]+)\(")


@dataclass
class RuleCheckResult:
    passed: bool
    violations: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _extract_tool_names(events: list[dict]) -> list[str]:
    names: list[str] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        calls = ev.get("tool_calls")
        if isinstance(calls, list):
            for c in calls:
                if isinstance(c, dict) and isinstance(c.get("name"), str):
                    names.append(c["name"])
        if isinstance(ev.get("tool_name"), str):
            names.append(ev["tool_name"])
        if ev.get("type") == "tool_call" and isinstance(ev.get("name"), str):
            names.append(ev["name"])
        raw = ev.get("raw")
        if isinstance(raw, str) and "🛠️" in raw:
            for m in _TOOL_MARKER_RX.finditer(raw):
                names.append(m.group(1))
            for m in _TOOL_COMPACT_RX.finditer(raw):
                names.append(m.group(1))
    return names


def check_run(
    role: str,
    trace_events: list[dict],
    contract: AgentContract,
    *,
    wall_clock_s: float | None = None,
    turn_count: int | None = None,
) -> RuleCheckResult:
    """Verify a finished agent run against its contract.

    Parameters
    ----------
    role:
        The role string the run was executed under. Used for error
        messages; the actual contract comes from *contract*.
    trace_events:
        Recorded trace events. Must be a list (may be empty).
    contract:
        The ``AgentContract`` the run was launched with.
    wall_clock_s:
        Observed wall clock. If ``None``, the wall-clock check is
        skipped — callers that don't track wall time pass ``None``.
    turn_count:
        Observed turn count. If ``None``, the harness derives a count
        from the number of trace events as a fallback.
    """
    violations: list[str] = []
    if role != contract.role:
        violations.append(
            f"role mismatch: trace says {role!r}, contract says "
            f"{contract.role!r}"
        )

    tool_names = _extract_tool_names(trace_events)
    tool_dist: dict[str, int] = {}
    for n in tool_names:
        tool_dist[n] = tool_dist.get(n, 0) + 1

    if contract.tools.forbidden_is_all and tool_names:
        violations.append(
            f"forbidden=ALL but {len(tool_names)} tool call(s) recorded: "
            f"{sorted(set(tool_names))}"
        )
    else:
        forbidden_set = set(contract.tools.forbidden)
        called_forbidden = sorted({n for n in tool_names if n in forbidden_set})
        for n in called_forbidden:
            violations.append(
                f"forbidden tool {n!r} was called {tool_dist[n]} time(s)"
            )
        if contract.tools.allowed:
            allowed_set = set(contract.tools.allowed)
            unknown = sorted({
                n for n in tool_names
                if n not in allowed_set and n not in forbidden_set
            })
            for n in unknown:
                violations.append(
                    f"tool {n!r} called but not in allowed list "
                    f"(called {tool_dist[n]} time(s))"
                )

    effective_turns = (
        turn_count if turn_count is not None else len(trace_events)
    )
    if effective_turns > contract.contract.max_turns:
        violations.append(
            f"turn count {effective_turns} exceeds max_turns "
            f"{contract.contract.max_turns}"
        )

    if wall_clock_s is not None and wall_clock_s > contract.contract.max_wall_s:
        violations.append(
            f"wall clock {wall_clock_s:.1f}s exceeds max_wall_s "
            f"{contract.contract.max_wall_s}"
        )

    stats = {
        "role": role,
        "tool_calls_total": sum(tool_dist.values()),
        "tool_call_distribution": dict(sorted(
            tool_dist.items(), key=lambda kv: -kv[1]
        )),
        "turn_count": effective_turns,
        "max_turns": contract.contract.max_turns,
        "wall_clock_s": wall_clock_s,
        "max_wall_s": contract.contract.max_wall_s,
        "forbidden_is_all": contract.tools.forbidden_is_all,
    }
    return RuleCheckResult(
        passed=not violations,
        violations=violations,
        stats=stats,
    )


__all__ = ["RuleCheckResult", "check_run"]
