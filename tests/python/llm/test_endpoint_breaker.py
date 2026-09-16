"""A model endpoint that cannot be connected to is skipped, briefly.

Every call probed its endpoint with an 8 s connect and retried three times, and
nothing told the NEXT call what happened. When the Mac Studio moved from .185 to
.167, every chat turn, learner fold and enhancer call paid up to 3 x 8 s against
the dead host before falling back — turns with no tool calls took 20-40 minutes.
"""
from __future__ import annotations

import errno
import socket
import time

import pytest

from aiforge_core.llm import client as c
from aiforge_core.llm import endpoint_breaker as br

DEAD = "http://127.0.0.1:1"          # nothing listens on port 1: refused at once


# ── the breaker itself ───────────────────────────────────────────────────────
def test_one_failure_does_not_open_it():
    br.record_failure(DEAD, "refused")
    assert br.is_open(DEAD) is None


def test_two_consecutive_failures_open_it():
    br.record_failure(DEAD, "refused")
    br.record_failure(DEAD, "refused")
    reason = br.is_open(DEAD)
    assert reason is not None
    assert "127.0.0.1:1" in reason
    assert "refused" in reason


def test_a_success_closes_it():
    br.record_failure(DEAD)
    br.record_failure(DEAD)
    br.record_success(DEAD)
    assert br.is_open(DEAD) is None


def test_it_half_opens_after_the_cooldown(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_BREAKER_COOLDOWN_S", "0.2")
    br.record_failure(DEAD)
    br.record_failure(DEAD)
    assert br.is_open(DEAD) is not None
    time.sleep(0.3)
    assert br.is_open(DEAD) is None                 # the next call may probe


def test_a_still_dead_host_reopens_after_one_failed_probe(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_BREAKER_COOLDOWN_S", "0.2")
    br.record_failure(DEAD)
    br.record_failure(DEAD)
    time.sleep(0.3)
    br.record_failure(DEAD)                          # the half-open probe fails
    assert br.is_open(DEAD) is not None


def test_zero_cooldown_disables_it(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_BREAKER_COOLDOWN_S", "0")
    for _ in range(5):
        br.record_failure(DEAD)
    assert br.is_open(DEAD) is None


def test_endpoints_are_tracked_separately():
    br.record_failure(DEAD)
    br.record_failure(DEAD)
    assert br.is_open("http://127.0.0.1:2") is None
    assert br.is_open("http://127.0.0.1:1/v1") is not None   # same host:port


# ── what counts as "could not reach it" ──────────────────────────────────────
@pytest.mark.parametrize("exc", [
    ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
    OSError(errno.EHOSTUNREACH, "No route to host"),
    RuntimeError("litellm.APIConnectionError: [Errno 111] Connection refused"),
    ConnectionError("LLM endpoint unreachable (x:1) within 8s connect budget"),
])
def test_connect_failures_count(exc):
    assert br.is_connect_error(exc)


@pytest.mark.parametrize("exc", [
    TimeoutError("The read operation timed out"),
    RuntimeError("litellm.Timeout: Read timed out after 600s"),
    RuntimeError("500 Internal Server Error"),
    ValueError("model returned malformed JSON"),
    RuntimeError("litellm.APIConnectionError: remote end closed connection"),
])
def test_a_reachable_server_does_not_count(exc):
    """The server answered, or at least accepted the connection. Skipping it
    would hide a real, reachable model."""
    assert not br.is_connect_error(exc)


def test_a_wrapped_connect_failure_is_found_through_the_cause_chain():
    try:
        try:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        except ConnectionRefusedError as inner:
            raise RuntimeError("upstream call failed") from inner
    except RuntimeError as outer:
        assert br.is_connect_error(outer)


# ── the chat / memory path ───────────────────────────────────────────────────
def test_preflight_skips_a_known_dead_endpoint_without_waiting(monkeypatch):
    # TEST-NET-1 never answers, so a real probe costs the whole connect budget.
    unroutable = "http://192.0.2.1:9"
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "1")
    for _ in range(2):
        with pytest.raises(ConnectionError):
            c._preflight(unroutable)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError, match="skipping it"):
        c._preflight(unroutable)
    assert time.monotonic() - t0 < 0.1, "a known-dead endpoint still cost a network wait"


def test_preflight_counts_a_connect_timeout(monkeypatch):
    """A sleeping host produces a bare TimeoutError. The preflight is
    connect-only, so it must count that, even though the generic classifier
    cannot tell it from a read timeout."""
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "0.5")
    for _ in range(2):
        with pytest.raises(ConnectionError):
            c._preflight("http://192.0.2.1:9")
    assert br.is_open("http://192.0.2.1:9") is not None


def test_preflight_closes_it_again_once_the_host_answers(monkeypatch):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    url = f"http://127.0.0.1:{srv.getsockname()[1]}"
    try:
        monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "2")
        br.record_failure(url)                       # one earlier blip
        c._preflight(url)                            # reachable
        br.record_failure(url)                       # a single new blip…
        assert br.is_open(url) is None               # …does not open it
    finally:
        srv.close()


def test_the_disabled_preflight_never_consults_the_breaker(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "0")
    br.record_failure(DEAD)
    br.record_failure(DEAD)
    c._preflight(DEAD)                               # disabled means no raise


# ── the ADK team-pipeline path ───────────────────────────────────────────────
def test_the_pipeline_skips_a_dead_candidate_and_moves_on(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from aiforge_core.runtime.escalating_llm import _wrapper as w

    attempted: list[str] = []

    class _Fake:
        role = "planner"

        async def _attempt(self, model, req, label, target, meter):
            attempted.append(label)
            return [SimpleNamespace(content="ok")]

    fake = _Fake()
    fake._stamp_request = lambda req, model: req
    fake._note_success = lambda label: None
    fake._record_spend = lambda *a, **k: None
    monkeypatch.setattr(w, "_api_base_of", lambda m: m.base)
    monkeypatch.setattr(w, "_mirror_to_langfuse", lambda *a, **k: None)
    monkeypatch.setattr(w, "_is_empty", lambda r: False)

    dead = SimpleNamespace(model="m", base="http://192.0.2.1:9/v1")
    br.record_failure(dead.base)
    br.record_failure(dead.base)

    async def run(label, model):
        state = {"exc": None, "done": False}
        out = [r async for r in w.EscalatingLlm._try_candidate(
            fake, label, model, object(), time.monotonic(), state)]
        return out, state

    out, state = asyncio.run(run("primary", dead))
    assert out == []
    assert attempted == [], "a known-dead endpoint was still attempted"
    assert "unreachable" in str(state["exc"])
    assert state["done"] is False                    # the chain continues
