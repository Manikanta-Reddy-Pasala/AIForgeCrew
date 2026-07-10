"""Langfuse mirror — env-gated, SDK-free REST ingestion, soft-fail, never
touches the call result."""
from __future__ import annotations

from aiforge_core.integrations import langfuse_adapter as lf


def test_disabled_without_keys(monkeypatch):
    for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert lf.enabled() is False


def test_kill_switch_wins(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3005")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("AIFORGE_LANGFUSE_DISABLE", "1")
    assert lf.enabled() is False


def test_complete_unaffected_when_tracing_off(monkeypatch):
    for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    from aiforge_core.llm import client
    monkeypatch.setattr(client, "_complete_impl",
                        lambda role, messages, **k2: "the answer")
    assert client.complete("chat", [{"role": "user", "content": "q"}]) \
        == "the answer"


def test_trace_crash_never_breaks_turn(monkeypatch):
    from aiforge_core.llm import client

    def boom(**k):
        raise RuntimeError("langfuse down")

    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3005")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.delenv("AIFORGE_LANGFUSE_DISABLE", raising=False)
    monkeypatch.setattr(lf, "record_generation", boom)
    monkeypatch.setattr(client, "_complete_impl",
                        lambda role, messages, **k2: "still fine")
    assert client.complete("chat", [{"role": "user", "content": "q"}]) \
        == "still fine"


def test_pipeline_mirror_extracts_adk_shapes(monkeypatch):
    """EscalatingLlm path (pipeline agents) mirrors too — chat goes through
    client.complete, ADK agents through _mirror_to_langfuse. Regression for
    'only simple chat shows up in langfuse'."""
    from types import SimpleNamespace as NS

    from aiforge_core.runtime import escalating_llm as esc
    seen = {}

    def fake_record(**kw):
        seen.update(kw)

    monkeypatch.setattr(
        "aiforge_core.integrations.langfuse_adapter.enabled", lambda: True)
    monkeypatch.setattr(
        "aiforge_core.integrations.langfuse_adapter.record_generation",
        fake_record)
    req = NS(config=NS(system_instruction="you are the doer"),
             contents=[NS(role="user", parts=[NS(text="fix the bug")])])
    resp = [NS(content=NS(parts=[NS(text="FINAL: fixed")]))]
    esc._mirror_to_langfuse("doer", req, resp, "qwen-local", 456)
    assert seen["role"] == "doer" and seen["model"] == "qwen-local"
    assert seen["messages"][0] == {"role": "system",
                                   "content": "you are the doer"}
    assert seen["messages"][1]["content"] == "fix the bug"
    assert seen["output"] == "FINAL: fixed"
    assert seen["metadata"] == {"path": "pipeline"}


def test_ingestion_payload_shape(monkeypatch):
    """SDK-free path: one trace-create + one generation-create per call,
    payload capped, sent via _send (stubbed — no network)."""
    sent: list[dict] = []
    monkeypatch.setattr(lf, "_send", lambda payload: sent.append(payload))
    # make the fire-and-forget thread synchronous for the assertion
    import threading

    class _SyncThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._t, self._a = target, args

        def start(self):
            self._t(*self._a)

    monkeypatch.setattr(threading, "Thread", _SyncThread)
    lf.record_generation(role="grader", model="qwen",
                         messages=[{"role": "user", "content": "x" * 20000}],
                         output="ok", latency_ms=123, error="")
    assert len(sent) == 1
    batch = sent[0]["batch"]
    kinds = [e["type"] for e in batch]
    assert kinds == ["trace-create", "generation-create"]
    gen = batch[1]["body"]
    assert gen["name"] == "llm:grader" and gen["model"] == "qwen"
    assert len(gen["input"][0]["content"]) <= 8000       # capped
    assert gen["output"] == "ok"
    assert gen["traceId"] == batch[0]["body"]["id"]      # linked


def test_memory_recall_and_write_mirrored(monkeypatch, tmp_path):
    """Memory layer observability: unified_query recalls and memory writes
    each produce a langfuse record (env-gated, soft-fail)."""
    seen: list[dict] = []
    monkeypatch.setattr(lf, "enabled", lambda: True)
    monkeypatch.setattr(lf, "record_generation",
                        lambda **kw: seen.append(kw))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_UQ_CACHE_TTL_S", "0")

    from aiforge_core.runtime.tools.memory_write import memory_write
    r = memory_write(text="sync retries use exponential backoff",
                     kind="gotcha", repo="demo")
    assert r.get("ok") is not None
    writes = [e for e in seen if e["role"] == "memory.write"]
    assert writes and writes[0]["metadata"]["path"] == "memory"

    from aiforge_core.memory import unified_query
    res = unified_query.query("how do sync retries work?", repo="demo")
    assert isinstance(res.get("hits"), list)
    recalls = [e for e in seen if e["role"] == "memory.recall"]
    assert recalls and recalls[0]["metadata"]["hits"] == len(res["hits"])
    assert "sync retries" in recalls[0]["messages"][0]["content"]


def test_score_payload_shape(monkeypatch):
    """record_score → one trace-create + one score-create, score linked to the
    trace and both tagged with the session so the Scores view populates."""
    import threading
    sent: list[dict] = []
    monkeypatch.setattr(lf, "_send", lambda payload: sent.append(payload))

    class _SyncThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._t, self._a = target, args

        def start(self):
            self._t(*self._a)

    monkeypatch.setattr(threading, "Thread", _SyncThread)
    lf.record_score(name="turn_completed", value=1.0, session_id=42,
                    comment="completed", metadata={"mode": "team"})
    assert len(sent) == 1
    batch = sent[0]["batch"]
    assert [e["type"] for e in batch] == ["trace-create", "score-create"]
    trace, score = batch[0]["body"], batch[1]["body"]
    assert trace["sessionId"] == "42"
    assert score["name"] == "turn_completed" and score["value"] == 1.0
    assert score["dataType"] == "NUMERIC"
    assert score["traceId"] == trace["id"]                # linked
    assert score["comment"] == "completed"


def test_session_id_threaded_into_generation(monkeypatch):
    """A completion made inside a bound session tags its Langfuse generation
    with that session id — regression for 'Sessions view empty'."""
    from aiforge_core.llm import client
    from aiforge_core.runtime import request_context
    seen: dict = {}
    monkeypatch.setattr(lf, "enabled", lambda: True)
    monkeypatch.setattr(lf, "record_generation", lambda **kw: seen.update(kw))
    monkeypatch.setattr(client, "_complete_impl",
                        lambda role, messages, **k2: "answer")
    tok = request_context.set_session_id(77)
    try:
        assert client.complete("chat", [{"role": "user", "content": "q"}]) \
            == "answer"
    finally:
        request_context.reset_session_id(tok)
    assert seen.get("session_id") == "77"
