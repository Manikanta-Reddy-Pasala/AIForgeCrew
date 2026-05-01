"""Circuit breakers — halt agent when limits trip.

Per spec §5.5 (no sandbox row in this version).
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class BreakerState:
    tripped: bool = False
    reason: str = ""
    tripped_at: float = 0.0


class CircuitBreakers:
    """Per-ticket breaker state. Caller decides what to do on trip
    (pause / re-plan / escalate) — this just records."""

    def __init__(
        self,
        *,
        wall_clock_max_secs_doer: int = 1800,        # 30 min
        wall_clock_max_secs_other: int = 600,        # 10 min
        retries_per_step_max: int = 5,
        token_budget_multiplier: float = 2.0,
        ticket_wall_clock_max_secs: int = 14_400,    # 4 hr
        audit_failure_max: int = 1,
    ) -> None:
        self.wall_clock_max_secs_doer = wall_clock_max_secs_doer
        self.wall_clock_max_secs_other = wall_clock_max_secs_other
        self.retries_per_step_max = retries_per_step_max
        self.token_budget_multiplier = token_budget_multiplier
        self.ticket_wall_clock_max_secs = ticket_wall_clock_max_secs
        self.audit_failure_max = audit_failure_max

        self._ticket_started_at = time.time()
        self._agent_started_at: dict[str, float] = {}
        self._retries: dict[str, int] = {}
        self._audit_failures = 0
        self.state = BreakerState()

    # ---- per-agent wall clock --------------------------------------------

    def begin_agent(self, role: str) -> None:
        self._agent_started_at[role] = time.time()

    def check_agent(self, role: str) -> None:
        start = self._agent_started_at.get(role)
        if start is None:
            return
        cap = (self.wall_clock_max_secs_doer if role == "doer"
               else self.wall_clock_max_secs_other)
        if time.time() - start > cap:
            self.trip(f"{role}_wall_clock", f"{role} exceeded {cap}s")

    # ---- retries per step ------------------------------------------------

    def record_retry(self, step_key: str) -> None:
        self._retries[step_key] = self._retries.get(step_key, 0) + 1
        if self._retries[step_key] > self.retries_per_step_max:
            self.trip("retry_per_step",
                      f"step {step_key} > {self.retries_per_step_max} retries")

    # ---- token budget ----------------------------------------------------

    def check_token_budget(self, step_key: str, used: int, expected: int) -> None:
        if expected <= 0:
            return
        if used > self.token_budget_multiplier * expected:
            self.trip("token_budget",
                      f"step {step_key} used {used} vs expected {expected}")

    # ---- audit failure ---------------------------------------------------

    def record_audit_failure(self) -> None:
        self._audit_failures += 1
        if self._audit_failures >= self.audit_failure_max:
            self.trip("audit_failure", "audit pipeline failed")

    # ---- ticket wall ----------------------------------------------------

    def check_ticket_wall(self) -> None:
        if time.time() - self._ticket_started_at > self.ticket_wall_clock_max_secs:
            self.trip("ticket_wall_clock",
                      f"ticket exceeded {self.ticket_wall_clock_max_secs}s")

    # ---- core ------------------------------------------------------------

    def trip(self, reason_id: str, msg: str) -> None:
        if self.state.tripped:
            return
        self.state.tripped = True
        self.state.reason = f"{reason_id}: {msg}"
        self.state.tripped_at = time.time()

    @property
    def tripped(self) -> bool:
        return self.state.tripped
