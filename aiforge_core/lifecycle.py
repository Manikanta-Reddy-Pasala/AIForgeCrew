"""Lifecycle v4.1 — parent + child state machines."""
from __future__ import annotations


class LifecycleError(RuntimeError):
    """Invalid transition."""


parent_transitions: dict[str, list[str]] = {
    "created":    ["planning"],
    "planning":   ["splitting", "escalated"],
    "splitting":  ["spawned", "escalated"],
    "spawned":    ["reflection", "escalated"],   # advances when all children merged
    "reflection": ["closed"],
    "closed":     [],
    "escalated":  [],
}

child_transitions: dict[str, list[str]] = {
    "created":    ["coding"],
    "coding":     ["reviewing", "escalated"],
    "reviewing":  ["mr_created", "coding", "escalated"],
    "mr_created": ["merged", "escalated"],
    "merged":     [],
    "escalated":  [],
}


def parent_allowed_next(current: str) -> list[str]:
    if current not in parent_transitions:
        raise LifecycleError(f"unknown parent state: {current}")
    return list(parent_transitions[current])


def child_allowed_next(current: str) -> list[str]:
    if current not in child_transitions:
        raise LifecycleError(f"unknown child state: {current}")
    return list(child_transitions[current])
