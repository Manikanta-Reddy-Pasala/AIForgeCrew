"""Local-model resilience: model-drop recovery (retry, not hard-fail) +
serial-endpoint concurrency (don't fan out to a single local model)."""
from __future__ import annotations

import pytest

from aiforge_core.llm import client as c


def test_model_drop_body_raises_transient():
    # A 200-OK error body naming a model drop → _ModelReloading (transient).
    with pytest.raises(c._ModelReloading):
        c._raise_if_model_dropped({"error": {"message": "Model unloaded"}})
    with pytest.raises(c._ModelReloading):
        c._raise_if_model_dropped({"error": "the model is loading, try again"})


def test_normal_body_does_not_raise():
    c._raise_if_model_dropped({"choices": [{"message": {"content": "hi"}}]})
    c._raise_if_model_dropped({"error": {"message": "invalid api key"}})  # not a drop
    c._raise_if_model_dropped("not a dict")


def test_model_reloading_is_transient():
    retry, label = c._is_transient_exc(c._ModelReloading("x"))
    assert retry is True and label == "model_reloading"


def test_escalating_markers_include_model_drop():
    from aiforge_core.runtime import escalating_llm as e
    joined = " ".join(e._TRANSIENT_MARKERS)
    for m in ("unloaded", "not loaded", "model not found", "model is loading"):
        assert m in joined


def test_max_workers_default_four_even_on_local(monkeypatch):
    # Operator decision 2026-07-09: default 4 everywhere (modern local servers
    # batch); a strictly serial endpoint is downgraded via MAX=1 explicitly.
    from aiforge_core.runtime import parallel_subtasks as pp
    monkeypatch.delenv("AIFORGE_PARALLEL_SUBTASKS_MAX", raising=False)
    monkeypatch.setattr("aiforge_core.llm.router.is_local_endpoint",
                        lambda role="doer": True)
    assert pp._max_workers() == 4


def test_max_workers_parallel_on_remote(monkeypatch):
    from aiforge_core.runtime import parallel_subtasks as pp
    monkeypatch.delenv("AIFORGE_PARALLEL_SUBTASKS_MAX", raising=False)
    monkeypatch.setattr("aiforge_core.llm.router.is_local_endpoint",
                        lambda role="doer": False)
    assert pp._max_workers() == 4


def test_max_workers_explicit_override_wins(monkeypatch):
    # A batching server operator sets it high even on localhost.
    from aiforge_core.runtime import parallel_subtasks as pp
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS_MAX", "6")
    monkeypatch.setattr("aiforge_core.llm.router.is_local_endpoint",
                        lambda role="doer": True)
    assert pp._max_workers() == 6


def test_is_local_endpoint_detects_loopback(monkeypatch):
    from aiforge_core.llm import router
    from aiforge_core.llm.types import Endpoint
    monkeypatch.setattr(router, "resolve", lambda role: Endpoint(
        base_url="http://127.0.0.1:1234/v1", api_key="x", model="m",
        provider="openai_compatible", role=role, extras={}))
    assert router.is_local_endpoint("doer") is True
    monkeypatch.setattr(router, "resolve", lambda role: Endpoint(
        base_url="https://api.example.com/v1", api_key="x", model="m",
        provider="openai_compatible", role=role, extras={}))
    assert router.is_local_endpoint("doer") is False
