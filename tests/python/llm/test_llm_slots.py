"""How many requests the model server serves at once (``llm/slots.py``).

The probe runs against real HTTP servers on loopback that answer the way
LM Studio, a vLLM-like batching server, llama.cpp, and an unknown
OpenAI-compatible server do. Model ids are placeholders, never real names.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiforge_core.llm import _slots_probe, slots

M_WIDE = "pub/model-wide"
M_NARROW = "pub/model-narrow"


def _lmstudio_routes() -> dict:
    def _inst(mid, parallel):
        return {"id": mid, "config": {"context_length": 8192,
                                      "parallel": parallel}}
    return {
        "/v1/models": {"data": [{"id": M_WIDE, "owned_by": "organization_owner"},
                                {"id": M_NARROW,
                                 "owned_by": "organization_owner"}]},
        "/api/v1/models": {"models": [
            {"key": M_WIDE, "selected_variant": M_WIDE + "@4bit",
             "loaded_instances": [_inst(M_WIDE, 4)]},
            {"key": M_NARROW, "loaded_instances": [_inst(M_NARROW, 1)]},
            {"key": "pub/model-idle", "loaded_instances": []},
        ]},
    }


@pytest.fixture
def server():
    started = []

    def _start(routes: dict):
        hits: list[str] = []

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                body = routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}/v1", hits

    yield _start
    for srv in started:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for var in ("AIFORGE_LLM_PARALLEL", "AIFORGE_LLM_PARALLEL_MULTI",
                "AIFORGE_LLM_PARALLEL_TTL_S"):
        monkeypatch.delenv(var, raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    slots.reset()
    yield
    slots.reset()


# ── the probe, per server kind ─────────────────────────────────────────────

def test_lmstudio_reports_each_loaded_instances_parallel(server):
    base, _ = server(_lmstudio_routes())
    assert _slots_probe.probe(base, M_WIDE) == 4
    assert _slots_probe.probe(base, "openai/" + M_WIDE + "@4bit") == 4
    assert _slots_probe.probe(base, M_NARROW) == 1
    # Listed but not loaded: nothing says it has slots.
    assert _slots_probe.probe(base, "pub/model-idle") == 1


def test_vllm_like_server_is_multi_slot(server, monkeypatch):
    base, _ = server({"/v1/models": {"data": [
        {"id": "m", "owned_by": "vllm", "max_model_len": 32768}]}})
    assert _slots_probe.probe(base, "m") == 4
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL_MULTI", "16")
    assert _slots_probe.probe(base, "m") == 16


def test_llamacpp_props_total_slots(server):
    base, _ = server({"/v1/models": {"data": [{"id": "m",
                                               "owned_by": "llamacpp"}]},
                      "/props": {"total_slots": 3}})
    assert _slots_probe.probe(base, "m") == 3


def test_unknown_server_is_one_slot(server):
    base, _ = server({"/v1/models": {"data": [{"id": "m",
                                               "owned_by": "someone"}]}})
    assert _slots_probe.probe(base, "m") == 1


def test_unreachable_server_is_one_slot():
    assert _slots_probe.probe("http://127.0.0.1:9/v1", "m") == 1
    assert _slots_probe.probe("", "m") == 1


def test_https_public_host_counts_as_hosted():
    assert _slots_probe._hosted("https://api.example.com/v1")
    assert not _slots_probe._hosted("http://api.example.com/v1")
    assert not _slots_probe._hosted("https://192.168.1.5:1234/v1")
    assert not _slots_probe._hosted("https://box.local/v1")
    assert not _slots_probe._hosted("https://localhost/v1")


# ── resolution: registry > setting > probe, cached ─────────────────────────

def _point(monkeypatch, table: dict):
    """role -> (base_url, model)."""
    monkeypatch.setattr(slots, "_endpoint",
                        lambda role: (*table.get(role, ("", "")), ""))


def test_auto_probes_once_and_caches(server, monkeypatch):
    base, hits = server(_lmstudio_routes())
    _point(monkeypatch, {"chat": (base, M_WIDE), "learner": (base, M_NARROW)})
    assert slots.llm_slots("chat") == 4
    n = len(hits)
    assert slots.llm_slots("chat") == 4
    assert len(hits) == n                        # cached: no second probe
    assert slots.llm_slots("learner") == 1       # its own model, its own answer


def test_setting_overrides_the_probe(server, monkeypatch):
    base, hits = server(_lmstudio_routes())
    _point(monkeypatch, {"chat": (base, M_WIDE)})
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "1")
    assert slots.llm_slots("chat") == 1
    assert hits == []
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "auto")
    assert slots.llm_slots("chat") == 4
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_parallel": 6})
    assert slots.llm_slots("chat") == 6


def test_registry_parallel_wins(monkeypatch):
    from aiforge_core.config import model_registry as mr
    row = mr.add_model(label="x", model="pub/model-x",
                       base_url="http://127.0.0.1:9/v1")
    _point(monkeypatch, {"chat": ("http://127.0.0.1:9/v1", "pub/model-x")})
    assert slots.llm_slots("chat") == 1           # unreachable, no override
    mr.update_model(row["id"], parallel=3)
    assert slots.llm_slots("chat") == 3
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "8")
    assert slots.llm_slots("chat") == 3           # per model beats global


def test_no_endpoint_is_one_slot(monkeypatch):
    _point(monkeypatch, {})
    assert slots.llm_slots("chat") == 1


def test_parallel_ok_groups_roles_by_server(server, monkeypatch):
    base, _ = server(_lmstudio_routes())
    other, _ = server({"/v1/models": {"data": []}})
    _point(monkeypatch, {"chat": (base, M_WIDE), "triage": (base, M_WIDE),
                         "narrow": (base, M_NARROW), "far": (other, "m")})
    assert slots.parallel_ok("chat", "triage")         # 4 slots, 2 callers
    assert not slots.parallel_ok("chat", "narrow")     # shares a 1-slot model
    assert slots.parallel_ok("narrow", "far")          # different servers
    assert not slots.parallel_ok("x", "y")             # unknown: sequential
