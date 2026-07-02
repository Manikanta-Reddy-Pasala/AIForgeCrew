"""Fix M3 — the condense LLM call must not hang the Doer.

On the Doer path ``session_id is None`` → ``_complete_cancellable`` runs the
summariser INLINE with no timeout. A wedged endpoint hangs the whole turn on a
condense. ``_llm_summarize_middle`` now bounds it with a wall-clock timeout and
falls back to the non-LLM breadcrumb.
"""
from __future__ import annotations

import time

from aiforge_core.runtime import chat_agent as ca


def test_summarize_times_out_and_falls_back(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONDENSE_TIMEOUT_S", "0.3")

    def _blocking(role, convo):
        time.sleep(30)                 # wedged endpoint
        return "SHOULD NOT APPEAR"

    middle = [{"role": "user", "content": "do a thing"},
              {"role": "assistant", "content": "ACTION: grep\nARGS_JSON: {}"}]
    t0 = time.monotonic()
    out = ca._llm_summarize_middle(middle, _blocking, session_id=None)
    elapsed = time.monotonic() - t0
    assert out == ""                   # fell back (empty → heuristic breadcrumb)
    assert elapsed < 3.0               # returned promptly, did not hang


def test_summarize_returns_result_when_fast(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONDENSE_TIMEOUT_S", "5")

    def _fast(role, convo):
        return "- did X\n- fixed Y"

    middle = [{"role": "user", "content": "hello"},
              {"role": "assistant", "content": "world"}]
    out = ca._llm_summarize_middle(middle, _fast, session_id=None)
    assert "did X" in out


def test_condense_timeout_env_default():
    import os
    os.environ.pop("AIFORGE_CONDENSE_TIMEOUT_S", None)
    assert ca._condense_timeout_s() == 30.0
