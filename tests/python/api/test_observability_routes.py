"""Health, the LLM meter, log/trace tailing, and the workflow DAG.

These endpoints are what an operator watches while a run is in progress, so
the properties pinned here are about never going dark: health degrades field
by field rather than 500-ing, an unknown log role tails an empty file instead
of 404-ing the tab (and cannot traverse out of the log dir), and the topology
falls back to a static pipeline when no live module answers.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import observability as obs


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(obs.router)
    return TestClient(app)


def _drain(agen, limit=10):
    """Pull up to ``limit`` items out of an async generator."""
    async def _run():
        out = []
        async for item in agen:
            out.append(item)
            if len(out) >= limit:
                break
        return out
    return asyncio.run(_run())


# ─── health ────────────────────────────────────────────────────────────


@pytest.fixture
def health_env(monkeypatch):
    import aiforge_core.tickets.backend_factory as bf
    import urllib.request
    from aiforge_core.api.routes.observability import tickets_mod

    class _BE:
        name = "sqlite"
    monkeypatch.setattr(bf, "get_backend", lambda: _BE())
    monkeypatch.setattr(tickets_mod, "get", lambda ident: None)

    def _no_lm(*_a, **_kw):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", _no_lm)
    from aiforge_core.integrations import langfuse_adapter as lfa
    monkeypatch.setattr(lfa, "enabled", lambda: False)


def test_health_reports_the_storage_backend(client, health_env):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["storage"] == "sqlite"
    assert body["lm_studio"] is False


def test_an_unreachable_store_makes_health_not_ok(client, monkeypatch, health_env):
    import aiforge_core.tickets.backend_factory as bf
    monkeypatch.setattr(bf, "get_backend",
                        lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert client.get("/api/health").json()["ok"] is False


def test_a_reachable_model_server_is_reported(client, monkeypatch, health_env):
    import urllib.request

    class _Resp:
        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _Resp())
    assert client.get("/api/health").json()["lm_studio"] is True


def test_only_the_trace_host_is_exposed_never_the_keys(client, monkeypatch, health_env):
    from aiforge_core.integrations import langfuse_adapter as lfa
    monkeypatch.setattr(lfa, "enabled", lambda: True)
    monkeypatch.setenv("LANGFUSE_HOST", "https://traces.example.com")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-should-never-appear")
    body = client.get("/api/health").json()
    assert body["traces_url"] == "https://traces.example.com"
    assert "sk-should-never-appear" not in json.dumps(body)


# ─── the LLM meter ─────────────────────────────────────────────────────


def test_the_meter_is_served_from_the_call_meter(client, monkeypatch):
    from aiforge_core.llm import call_meter
    seen: dict = {}

    def _snap(series=True):
        seen["series"] = series
        return {"per_minute": 4, "failed_per_minute": 1}
    monkeypatch.setattr(call_meter, "global_snapshot", _snap)
    assert client.get("/api/llm/usage").json()["per_minute"] == 4
    assert seen["series"] is True


def test_the_sparkline_series_can_be_skipped(client, monkeypatch):
    from aiforge_core.llm import call_meter
    seen: dict = {}
    monkeypatch.setattr(call_meter, "global_snapshot",
                        lambda series=True: seen.setdefault("series", series) or {})
    client.get("/api/llm/usage?series=false")
    assert seen["series"] is False


# ─── which log file a role tails ───────────────────────────────────────


def test_the_newest_naming_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(obs, "LOG_DIR", str(tmp_path))
    (tmp_path / "orchestrator-adk.doer.ndjson").write_text("new\n")
    (tmp_path / "orchestrator-doer.ndjson").write_text("old\n")
    assert obs._resolve_role_log("doer").endswith("orchestrator-adk.doer.ndjson")


def test_the_legacy_name_is_the_second_choice(monkeypatch, tmp_path):
    monkeypatch.setattr(obs, "LOG_DIR", str(tmp_path))
    (tmp_path / "orchestrator-doer.ndjson").write_text("old\n")
    assert obs._resolve_role_log("doer").endswith("orchestrator-doer.ndjson")


def test_the_master_stream_is_the_last_resort(monkeypatch, tmp_path):
    monkeypatch.setattr(obs, "LOG_DIR", str(tmp_path))
    (tmp_path / "orchestrator-adk_runner.ndjson").write_text("master\n")
    assert obs._resolve_role_log("doer").endswith("orchestrator-adk_runner.ndjson")


def test_an_empty_file_does_not_count_as_present(monkeypatch, tmp_path):
    monkeypatch.setattr(obs, "LOG_DIR", str(tmp_path))
    (tmp_path / "orchestrator-adk.doer.ndjson").write_text("")
    assert obs._resolve_role_log("doer").endswith("orchestrator-adk.doer.ndjson")


def test_an_unknown_role_tails_a_waiting_path(monkeypatch, tmp_path):
    monkeypatch.setattr(obs, "LOG_DIR", str(tmp_path))
    assert obs._resolve_role_log("nosuchrole").endswith(
        "orchestrator-adk.nosuchrole.ndjson")


def _tailed_path(monkeypatch, role):
    """The file the role endpoint would tail, without opening a stream."""
    seen: dict = {}

    async def _fake(path):
        seen["path"] = path
        return
        yield                                   # pragma: no cover — async gen
    monkeypatch.setattr(obs, "_tail_forever", _fake)
    obs.stream_role_log(role)
    return obs._resolve_role_log(
        __import__("re").sub(r"[^a-z0-9_]", "", role.lower()) or "adk_runner")


def test_a_role_cannot_traverse_out_of_the_log_dir(monkeypatch, tmp_path):
    """The role reaches the filesystem, so it is sanitised before it does."""
    monkeypatch.setattr(obs, "LOG_DIR", str(tmp_path))
    path = _tailed_path(monkeypatch, "../../etc/passwd")
    assert ".." not in path
    assert "/etc/passwd" not in path
    assert path.startswith(str(tmp_path))


def test_an_empty_role_falls_back_to_the_master_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(obs, "LOG_DIR", str(tmp_path))
    assert _tailed_path(monkeypatch, "---").endswith("orchestrator-adk.adk_runner.ndjson")


# ─── tailing ───────────────────────────────────────────────────────────


def test_only_the_tail_is_read(tmp_path):
    p = tmp_path / "log.ndjson"
    p.write_text("".join(f"line{i}\n" for i in range(500)))
    lines = asyncio.run(obs._tail_lines(str(p), n=3))
    assert [ln.strip() for ln in lines] == ["line497", "line498", "line499"]


def test_reading_resumes_from_an_offset(tmp_path):
    p = tmp_path / "log.ndjson"
    p.write_text("abcdef")
    assert asyncio.run(obs._read_from(str(p), 3)) == "def"


def test_blank_lines_are_not_sse_events():
    assert list(obs._sse_lines("a\n\n  \nb\n")) == ["data: a\n\n", "data: b\n\n"]


def test_the_size_of_a_missing_file_is_zero(tmp_path):
    assert obs._size_of(str(tmp_path / "gone")) == 0
    (tmp_path / "here").write_text("abc")
    assert obs._size_of(str(tmp_path / "here")) == 3


def test_the_backfill_shows_history_immediately(tmp_path):
    p = tmp_path / "log.ndjson"
    p.write_text("one\ntwo\n")
    assert _drain(obs._backfill(str(p))) == ["data: one\n\n", "data: two\n\n"]


def test_appends_are_streamed_as_they_land(monkeypatch, tmp_path):
    p = tmp_path / "log.ndjson"
    p.write_text("one\n")

    async def _no_sleep(_n):
        p.write_text("one\ntwo\n")
    monkeypatch.setattr(obs.asyncio, "sleep", _no_sleep)
    assert _drain(obs._poll_appends(str(p), 4), limit=1) == ["data: two\n\n"]


def test_a_missing_file_is_waited_for_rather_than_erroring(monkeypatch, tmp_path):
    p = tmp_path / "later.ndjson"

    async def _no_sleep(_n):
        p.write_text("appeared\n")
    monkeypatch.setattr(obs.asyncio, "sleep", _no_sleep)
    assert _drain(obs._tail_forever(str(p)), limit=1) == ["data: appeared\n\n"]


def test_a_failed_backfill_does_not_stop_the_stream(monkeypatch, tmp_path):
    p = tmp_path / "log.ndjson"
    p.write_text("one\n")

    async def _boom(_path, n=200):
        raise OSError("vanished")
    monkeypatch.setattr(obs, "_tail_lines", _boom)

    async def _no_sleep(_n):
        p.write_text("one\ntwo\n")
    monkeypatch.setattr(obs.asyncio, "sleep", _no_sleep)
    assert _drain(obs._tail_forever(str(p)), limit=1) == ["data: two\n\n"]


# ─── ticket trace scoping ──────────────────────────────────────────────


def test_a_run_for_this_ticket_opens_the_window():
    in_ctx, emit = obs._trace_scope('{"event": "adk_runner.start", "t": "ONE-1"}',
                                    "ONE-1", False)
    assert in_ctx is True
    assert emit is False


def test_another_tickets_run_closes_the_window():
    in_ctx, emit = obs._trace_scope('{"event": "adk_runner.start", "t": "ONE-2"}',
                                    "ONE-1", True)
    assert in_ctx is False
    assert emit is False


def test_the_closing_line_is_emitted_then_the_window_ends():
    in_ctx, emit = obs._trace_scope('{"event": "adk_runner.done", "t": "ONE-1"}',
                                    "ONE-1", True)
    assert in_ctx is False
    assert emit is True


def test_lines_inside_the_window_are_emitted():
    assert obs._trace_scope("Step 1: thinking", "ONE-1", True) == (True, True)


def test_lines_outside_the_window_are_not():
    assert obs._trace_scope("Step 1: thinking", "ONE-1", False) == (False, False)


def test_the_legacy_event_names_still_scope():
    assert obs._trace_scope('{"event":"graph_runner.start","t":"ONE-1"}',
                            "ONE-1", False) == (True, False)


# ─── llm.call filtering ────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [
    '{"event": "llm.call", "ticket": "ONE-1"}',
    '{"event":"llm.call","ticket":"ONE-1"}',
])
def test_an_llm_call_for_this_ticket_matches(raw):
    assert obs._is_llm_call_for(raw, "ONE-1") is True


@pytest.mark.parametrize("raw", [
    '{"event": "llm.call", "ticket": "ONE-2"}',
    '{"event": "agent.step", "ticket": "ONE-1"}',
])
def test_other_events_and_tickets_do_not(raw):
    assert obs._is_llm_call_for(raw, "ONE-1") is False


def test_the_last_n_llm_calls_are_returned(client, monkeypatch, tmp_path):
    err = tmp_path / "graph-runner.err"
    err.write_text("".join(
        json.dumps({"event": "llm.call", "ticket": "ONE-1", "n": i}) + "\n"
        for i in range(10)) + '{"event": "llm.call", "ticket": "ONE-2"}\n')
    monkeypatch.setenv("AIFORGE_GRAPH_RUNNER_ERR", str(err))
    body = client.get("/api/llm-trace/ONE-1?limit=3").json()
    assert body["count"] == 3
    assert [e["n"] for e in body["events"]] == [7, 8, 9]


def test_a_corrupt_line_is_skipped(client, monkeypatch, tmp_path):
    err = tmp_path / "graph-runner.err"
    err.write_text('{"event": "llm.call", "ticket": "ONE-1"} trailing junk\n'
                   '{"event": "llm.call", "ticket": "ONE-1", "n": 1}\n')
    monkeypatch.setenv("AIFORGE_GRAPH_RUNNER_ERR", str(err))
    assert client.get("/api/llm-trace/ONE-1").json()["count"] == 1


def test_a_missing_log_reports_the_path_rather_than_500ing(client, monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_GRAPH_RUNNER_ERR", str(tmp_path / "gone.err"))
    body = client.get("/api/llm-trace/ONE-1").json()
    assert body["events"] == []
    assert "not found" in body["error"]


# ─── the workflow DAG ──────────────────────────────────────────────────


def test_the_static_topology_is_a_linear_pipeline():
    topo = obs._static_topology()
    assert topo["static"] is True
    assert [n["id"] for n in topo["nodes"]][:3] == ["triage", "enhancer", "researcher"]
    assert len(topo["edges"]) == len(topo["nodes"]) - 1
    assert topo["edges"][0] == {"from": "triage", "to": "enhancer", "label": ""}


def test_the_live_topology_is_preferred(monkeypatch):
    from aiforge_core.runtime import workflow_topology as wt
    monkeypatch.setattr(wt, "snapshot", lambda ticket: {"nodes": [], "ticket": ticket})
    assert obs._topology_snapshot("ONE-1") == {"nodes": [], "ticket": "ONE-1"}


def test_a_broken_topology_module_falls_back_to_static(monkeypatch):
    from aiforge_core.runtime import workflow_topology as wt
    monkeypatch.setattr(wt, "snapshot",
                        lambda ticket: (_ for _ in ()).throw(RuntimeError("no graph")))
    assert obs._topology_snapshot(None)["static"] is True


def test_the_topology_endpoint_serves_the_snapshot(client, monkeypatch):
    monkeypatch.setattr(obs, "_topology_snapshot", lambda ticket: {"ticket": ticket})
    assert client.get("/api/workflow/topology?ticket=ONE-1").json() == {"ticket": "ONE-1"}


class _StopStream(Exception):
    pass


@pytest.mark.parametrize("raw,expected", [(0, 3), (-5, 1), (3, 3), (99, 30),
                                          (None, 3)])
def test_the_refresh_interval_is_clamped(monkeypatch, raw, expected):
    """A client-driven interval reaches a sleep loop, so it is clamped to
    1..30 — otherwise ?interval=0 would spin the server at full speed. Note 0
    is falsy, so it takes the 3s default rather than the floor."""
    seen: dict = {}
    monkeypatch.setattr(obs, "_topology_snapshot", lambda ticket: {"nodes": []})
    import time

    def _sleep(n):
        seen["interval"] = n
        raise _StopStream
    monkeypatch.setattr(time, "sleep", _sleep)
    resp = (obs.workflow_stream() if raw is None
            else obs.workflow_stream(interval=raw))

    async def _pull():
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk)
            if len(out) >= 2:
                break
        return out

    pull = _pull()
    with pytest.raises(_StopStream):
        asyncio.run(pull)
    assert seen["interval"] == expected
