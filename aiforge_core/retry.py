"""Retry + circuit-breaker machinery per DESIGN §10.

Two enforcement surfaces:

1. `enforce_loop_caps()` — before a transition is committed, check how many
   dev↔tester and dev↔architect loops the ticket has already gone through.
   Exceeding the cap auto-routes the ticket into the `escalated` state and
   records an escalation audit event.

2. `CircuitBreaker` — consecutive-failure tracker per (role, ticket). Trips
   after N failures; tripped breaker refuses `record_failure()` / mandates a
   human-driven reset. Stored in the audit table (event='breaker_trip').
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import PaperclipConfig
from .lifecycle import LifecycleError
from .observe import ticket_report
from .store import Store


class RetryExceeded(LifecycleError):
    """Loop cap hit — ticket must escalate."""


class BreakerTripped(RuntimeError):
    """Consecutive-failure breaker is open. Needs human reset."""


def enforce_loop_caps(
    store: Store,
    cfg: PaperclipConfig,
    ticket_id: str,
    from_state: str,
    to_state: str,
) -> None:
    """Raise RetryExceeded if this transition would push loop counts past their caps."""
    report = ticket_report(store, ticket_id)
    if report is None:
        return

    loops = report.loops  # already counted via audit
    # Simulate the current transition's contribution so we catch the last-hop breach.
    prospective = {
        "dev_tester": loops["dev_tester"] + (1 if from_state == "verifying" and to_state == "coding" else 0),
        "dev_architect": loops["dev_architect"] + (1 if from_state == "reviewing" and to_state == "coding" else 0),
    }
    if prospective["dev_tester"] > cfg.retry_rules.dev_tester_loops_max:
        store.audit_event(ticket_id, "escalate", "system",
                          {"reason": "dev_tester_loops_exceeded", "count": prospective["dev_tester"]})
        raise RetryExceeded(
            f"dev↔tester loop cap ({cfg.retry_rules.dev_tester_loops_max}) exceeded — escalate required"
        )
    if prospective["dev_architect"] > cfg.retry_rules.dev_architect_loops_max:
        store.audit_event(ticket_id, "escalate", "system",
                          {"reason": "dev_architect_loops_exceeded", "count": prospective["dev_architect"]})
        raise RetryExceeded(
            f"dev↔architect loop cap ({cfg.retry_rules.dev_architect_loops_max}) exceeded — escalate required"
        )


# --------- coverage gate (§10 "Unit test requirement: MR blocked if coverage < 80%") ---------

def require_coverage_for_mr(store: Store, ticket_id: str, *, min_pct: float = 80.0) -> None:
    """Inspect audit for latest `coverage` event; raise if below threshold."""
    events = [e for e in store.list_audit(ticket_id) if e["event"] == "coverage"]
    if not events:
        raise LifecycleError(
            f"no coverage report on {ticket_id}; architect must see ≥{min_pct:.0f}% before MR"
        )
    latest = events[-1]
    pct = float(latest["data"].get("pct") or 0)
    if pct < min_pct:
        raise LifecycleError(
            f"coverage {pct:.1f}% below minimum {min_pct:.0f}% — MR blocked"
        )


# --------- circuit breaker ---------

@dataclass
class CircuitBreaker:
    """Per (role, ticket) consecutive-failure breaker.

    Persisted in audit table: event='breaker_failure' / 'breaker_trip' / 'breaker_reset'.
    """

    store: Store
    threshold: int = 3

    def _consecutive_failures(self, ticket_id: str, role: str) -> int:
        """Count consecutive breaker_failure for `role`, unwinding on reset/success.

        Resets may be recorded by any actor (usually 'human') with data.for=role,
        so we filter by *event semantics*, not by the audit's actor field.
        """
        events = self.store.list_audit(ticket_id)
        count = 0
        for e in reversed(events):
            evt = e["event"]
            if evt == "breaker_reset" and e["data"].get("for") == role:
                break
            if evt == "breaker_success" and e["actor"] == role:
                break
            if evt == "breaker_failure" and e["actor"] == role:
                count += 1
        return count

    def record_failure(self, ticket_id: str, role: str, reason: str) -> None:
        n = self._consecutive_failures(ticket_id, role)
        if n >= self.threshold:
            raise BreakerTripped(f"breaker already open for {role} on {ticket_id}")
        self.store.audit_event(ticket_id, "breaker_failure", role, {"reason": reason})
        if n + 1 >= self.threshold:
            self.store.audit_event(ticket_id, "breaker_trip", role,
                                   {"consecutive_failures": n + 1})
            raise BreakerTripped(
                f"{role} circuit breaker tripped on {ticket_id} after {n + 1} failures"
            )

    def record_success(self, ticket_id: str, role: str) -> None:
        self.store.audit_event(ticket_id, "breaker_success", role, {})

    def reset(self, ticket_id: str, role: str, actor: str = "human") -> None:
        self.store.audit_event(ticket_id, "breaker_reset", actor, {"for": role})
