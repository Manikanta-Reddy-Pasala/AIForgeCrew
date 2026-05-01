"""Learner — online (after each step) + offline (weekly).

Online (this module):
    record_step_trace(...)        → step_traces table
    record_episodic(...)          → episodic_outcomes table
    update_procedural(...)        → procedural_patterns table

Offline (separate cron):
    cluster_failures()
    distil_skills()
    promote_via_eval_gate()
    resolve_contradictions()
"""
from __future__ import annotations
