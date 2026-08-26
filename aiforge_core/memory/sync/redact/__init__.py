"""The outbound filter: what this machine will let leave it.

Runs on the CLIENT, in the push path, before anything is advertised — so a
blocked node never appears in an offer and the admin never learns it exists.
That ordering is the point: "we chose not to send it" and "we told them about it
and then declined" are different guarantees. Re-run on the admin in
``inbox.accept`` as defence in depth, so a client build that predates this
package cannot leak into a group.

A package rather than a process. A filter that can be *down* is a filter that
stops sync, and this one has to be able to fail without taking the daemon with
it; it also has to be cheap enough to run on every node of every cycle. Every
rule is deterministic — no LLM in the sync path, so the filter costs nothing,
never rate-limits and cannot wedge a cycle when a model is unreachable.

Three stages, first refusal wins:

* ``secrets``  — credentials. The half that must not be wrong.
* ``private``  — somebody's own machine, not the fleet's knowledge.
* ``noise``    — the idle-search class: the user asked what the capital of
                 France is, it became a capture, the capture became a node.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from aiforge_core.memory.sync.redact import noise, private, secrets

_log = logging.getLogger("aiforge.sync")


@dataclass(frozen=True)
class Verdict:
    """Why a node may or may not leave. ``rule``/``reason`` are "" when it may."""
    send: bool
    rule: str = ""
    reason: str = ""


_STAGES = (("secrets", secrets.check), ("private", private.check),
           ("noise", noise.check))


def review(node: dict) -> Verdict:
    """Whether ``node`` may leave this machine.

    **Fails CLOSED.** A rule that raises means we do not know whether the node is
    safe, and the whole point of the stage is that we do not send what we cannot
    vouch for. The node is simply re-offered next cycle, so a bug here costs a
    delay rather than a leak.
    """
    for name, check in _STAGES:
        try:
            rule, reason = check(node)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            _log.warning("sync: filter stage %s failed, holding the node back: %s",
                         name, exc)
            return Verdict(False, f"{name}.error",
                           "the filter could not judge this note")
        if rule:
            return Verdict(False, rule, reason)
    return Verdict(True)


def explain() -> list[dict]:
    """The stages, in the order they are applied, for the settings screen."""
    return [{"stage": name,
             "doc": (check.__doc__ or "").strip().splitlines()[0]}
            for name, check in _STAGES]


__all__ = ["Verdict", "review", "explain"]
