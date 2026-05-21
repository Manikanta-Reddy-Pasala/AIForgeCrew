"""Unified budget tracker (sub #9).

One in-memory accounting layer for per-call cost + tokens shared by
EscalatingLlm, loop_budget, and ADK. Operators query rollups via the
``tracker`` module singleton (or build a private :class:`BudgetTracker`
for tests).

KISS: bounded ring (default 1000 entries) so a runaway loop can't
explode memory. Thread-safe via a single lock; cheap enough at LLM
call cadence.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from .tools._trace import emit


_DEFAULT_CAP = 1000


@dataclass(frozen=True)
class Spend:
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ts: float


class BudgetTracker:
    """Cap-bounded, thread-safe accounting ring."""

    def __init__(self, cap: int = _DEFAULT_CAP) -> None:
        self._ring: deque[Spend] = deque(maxlen=cap)
        self._lock = threading.Lock()

    def record(
        self, role: str, model: str,
        input_tokens: int = 0, output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> Spend:
        spend = Spend(
            role=role, model=model,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cost_usd=float(cost_usd),
            ts=time.time(),
        )
        with self._lock:
            self._ring.append(spend)
        emit("Cost", {"role": role, "model": model,
                      "in": spend.input_tokens, "out": spend.output_tokens,
                      "usd": round(spend.cost_usd, 6)})
        return spend

    def total(self) -> dict[str, Any]:
        with self._lock:
            in_t = sum(s.input_tokens for s in self._ring)
            out_t = sum(s.output_tokens for s in self._ring)
            usd = sum(s.cost_usd for s in self._ring)
            calls = len(self._ring)
        return {
            "input_tokens": in_t,
            "output_tokens": out_t,
            "cost_usd": round(usd, 6),
            "calls": calls,
        }

    def _group_by(self, key: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for s in self._ring:
                k = getattr(s, key)
                bucket = out.setdefault(k, {
                    "input_tokens": 0, "output_tokens": 0,
                    "cost_usd": 0.0, "calls": 0,
                })
                bucket["input_tokens"] += s.input_tokens
                bucket["output_tokens"] += s.output_tokens
                bucket["cost_usd"] += s.cost_usd
                bucket["calls"] += 1
        for bucket in out.values():
            bucket["cost_usd"] = round(bucket["cost_usd"], 6)
        return out

    def by_role(self) -> dict[str, dict[str, Any]]:
        return self._group_by("role")

    def by_model(self) -> dict[str, dict[str, Any]]:
        return self._group_by("model")

    def reset(self) -> None:
        with self._lock:
            self._ring.clear()

    def to_json(self) -> str:
        with self._lock:
            return json.dumps([asdict(s) for s in self._ring])


# Module-level singleton — the EscalatingLlm, ADK, etc. import this name.
tracker = BudgetTracker()


__all__ = ["Spend", "BudgetTracker", "tracker"]
