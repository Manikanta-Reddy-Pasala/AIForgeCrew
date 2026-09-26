"""``pr_reviewer._llm_review`` waits for a model that is down.

It calls litellm directly, so it never got the shared wait every other send
has: a review that hit a restarting LM Studio just returned "no verdict". It now
goes through :func:`model_wait.call_with_wait` — probe the endpoint, back off,
re-send once it answers — and a Stop / ticket cancel / shutdown ends the wait.

The endpoint here is a real HTTP server (``GET /models`` answers 503 while
"down", 200 once "up"); only the litellm send is faked.
"""
from __future__ import annotations

import http.server
import sys
import threading
import types

import pytest

from aiforge_core.llm import model_wait
from aiforge_core.runtime import pr_reviewer


class _Box:
    """A fake model server: down until ``is_up`` is set — by hand, or by
    itself once it has been probed ``up_after`` times (a time-based switch
    races the first test's imports)."""

    def __init__(self) -> None:
        self.is_up = threading.Event()
        self.probes = 0
        self.up_after = 0
        box = self

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — stdlib name
                box.probes += 1
                if box.up_after and box.probes >= box.up_after:
                    box.is_up.set()
                code = 200 if box.is_up.is_set() else 503
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data": []}')

            def log_message(self, *a):  # quiet
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def box(monkeypatch, tmp_path):
    b = _Box()
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    import os
    for k in list(os.environ):
        if k.startswith("AIFORGE_") and k.endswith(
                ("_MODEL", "_PROVIDER", "_BASE_URL", "_API_KEY")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("AIFORGE_LLM_MAX_RPM", raising=False)
    # Forever (the product default) — the suite lowers it to "do not wait".
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0")
    monkeypatch.setattr(model_wait, "_DEFAULT_MAX_S", 0.0)

    def _fast(cap=None):
        while True:
            yield 0.05
    monkeypatch.setattr(model_wait, "delays", _fast)
    model_wait._reset_for_tests()
    from aiforge_core.config import _filecache, agent_config
    from aiforge_core.llm import rate_limiter as rl
    _filecache.clear()
    rl.reset_global()
    agent_config.set_role("doer", "openai_compatible", "cfg-model",
                          base_url=b.base, api_key="k")
    yield b
    b.close()
    model_wait._reset_for_tests()
    rl.reset_global()


def _litellm(monkeypatch, box: _Box, *, down_error=None):
    """litellm.completion: refused while the box is down, a verdict once up."""
    sends: list = []

    def _completion(**kw):
        sends.append(kw)
        if not box.is_up.is_set():
            raise (down_error or ConnectionRefusedError(
                111, "Connection refused"))
        return {"choices": [{"message": {"content": '{"verdict": "approve"}'}}]}

    mod = types.ModuleType("litellm")
    mod.completion = _completion
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return sends


def test_a_down_model_is_waited_for_and_the_review_resent(monkeypatch, box):
    sends = _litellm(monkeypatch, box)
    box.up_after = 3
    statuses: list = []

    with model_wait.status_sink(statuses.append):
        out = pr_reviewer._llm_review("review this")

    assert out == {"verdict": "approve"}
    assert len(sends) == 2, "one refused send, one re-send after it came back"
    assert box.probes == 3, "it re-sent before the endpoint answered"
    states = [s["state"] for s in statuses]
    assert states[0] == "waiting" and states[-1] == "back"


def test_every_attempt_is_charged_to_the_ceiling(monkeypatch, box):
    """A re-send after the outage is a real send — it takes its own slot."""
    from aiforge_core.llm import rate_limiter as rl
    monkeypatch.setenv("AIFORGE_LLM_MAX_RPM", "30")
    sends = _litellm(monkeypatch, box)
    box.up_after = 2
    before = rl.global_used()

    assert pr_reviewer._llm_review("review this") == {"verdict": "approve"}
    assert len(sends) == 2
    assert rl.global_used() == before + len(sends)


def test_a_ticket_cancel_ends_the_wait(monkeypatch, box):
    """The box never comes back; the ticket is cancelled → no verdict, and no
    further sends after the cancel."""
    sends = _litellm(monkeypatch, box)
    lost = threading.Event()
    threading.Timer(0.3, lost.set).start()

    with model_wait.scope(lost, "ticket cancelled"):
        out = pr_reviewer._llm_review("review this")

    assert out == {}
    assert len(sends) == 1


def test_shutdown_ends_the_wait(monkeypatch, box):
    _litellm(monkeypatch, box)
    threading.Timer(0.3, model_wait.shutdown).start()
    assert pr_reviewer._llm_review("review this") == {}


def test_a_config_error_is_not_waited_for(monkeypatch, box):
    """A 401 is the operator's to fix — waiting would hang the PR forever."""
    class AuthenticationError(Exception):
        status_code = 401

    sends = _litellm(monkeypatch, box, down_error=AuthenticationError("bad key"))
    assert pr_reviewer._llm_review("review this") == {}
    assert len(sends) == 1
    assert box.probes == 0
