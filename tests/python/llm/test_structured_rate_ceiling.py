"""The structured/instructor path is under the ceiling and on the meter.

The bug: `structured_complete` reaches the endpoint through the OpenAI SDK, not
through `llm.client._post`, so NOTHING enforced or counted at the wire applied
to it. Every caller of it is memory-side (compaction folds, scope
classification, graph reconcile, OKF authoring, the session ledger) — the
`learner` role, the busiest unattended sender in the system. An operator who
set `llm_max_rpm` under their provider's limit still collected rate-limit
rejections, from traffic the toolbar meter reported as zero.

These pin the mirror at the TRANSPORT, because instructor retries INSIDE
`create()`: a gate at the function entry counts one request and sends three.
"""
import httpx
import pytest

from aiforge_core.integrations import instructor_adapter as ia
from aiforge_core.llm import call_meter, rate_limiter as rl


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    from aiforge_core.config import _filecache
    _filecache.clear()
    rl.reset_global()
    call_meter.reset_all()
    yield
    rl.reset_global()
    call_meter.reset_all()


def _client(monkeypatch, handler, *, role="learner", model="m",
            provider="openai_compatible"):
    """A real httpx.Client wired with our hooks over a mock transport — the
    hooks must work through httpx's own machinery, not a hand-called stub."""
    pending: list = []
    hooks = ia._event_hooks(role, model, provider, pending)
    return httpx.Client(transport=httpx.MockTransport(handler),
                        event_hooks=hooks, base_url="http://x"), pending


def test_every_send_is_charged_to_the_ceiling(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 50})
    cli, _p = _client(monkeypatch,
                      lambda req: httpx.Response(200, json={"ok": 1}))
    with cli:
        for _ in range(3):
            cli.post("/chat/completions", json={})
    assert rl.global_used() == 3, "the structured path skipped the ceiling"


def test_instructor_internal_retries_each_cost_one(monkeypatch):
    """The reason the hook is at the transport. instructor reasks inside one
    create() call; a gate at the entry would have counted 1 of 3."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 50})
    n = {"i": 0}

    def _h(req):
        n["i"] += 1
        return httpx.Response(200, json={"ok": n["i"]})

    cli, _p = _client(monkeypatch, _h)
    with cli:
        for _ in range(3):          # stands in for instructor's reask loop
            cli.post("/chat/completions", json={})
    assert n["i"] == 3
    assert rl.global_used() == 3


def test_every_send_is_on_the_meter(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 0})          # no ceiling: metering is separate
    cli, _p = _client(monkeypatch,
                      lambda req: httpx.Response(200, json={"ok": 1}))
    with cli:
        cli.post("/chat/completions", json={})
        cli.post("/chat/completions", json={})
    snap = call_meter.global_snapshot(series=False)
    assert snap["per_minute"] == 2
    assert snap["by_role"].get("learner") == 2


def test_a_failed_send_is_counted_as_a_failure_not_a_success(monkeypatch):
    cli, _p = _client(monkeypatch,
                      lambda req: httpx.Response(500, text="boom"))
    with cli:
        cli.post("/chat/completions", json={})
    snap = call_meter.global_snapshot(series=False)
    assert snap["per_minute"] == 1
    assert snap["failed_per_minute"] == 1
    assert "http_500" in snap["by_fail_reason"]


def test_a_success_is_not_counted_as_a_failure(monkeypatch):
    cli, _p = _client(monkeypatch,
                      lambda req: httpx.Response(200, json={"ok": 1}))
    with cli:
        cli.post("/chat/completions", json={})
    snap = call_meter.global_snapshot(series=False)
    assert snap["per_minute"] == 1
    assert snap["failed_per_minute"] == 0


def test_a_400_that_is_really_a_rate_limit_makes_the_process_hold(monkeypatch):
    """The observed gateway says 400, not 429:
    "You've used 20 requests with this model in the last minute, exceeding
    your limit of 20 requests per minute."
    A status-code-only reading calls that a permanent bad request and sends
    the next call straight into the same wall."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 12})
    body = {"detail": "You've used 20 requests with this model in the last "
                      "minute, exceeding your limit of 20 requests per minute."}
    cli, _p = _client(monkeypatch, lambda req: httpx.Response(400, json=body))
    with cli:
        cli.post("/chat/completions", json={})
    assert rl.held_for("openai_compatible") > 0, "the rejection did not hold"


def test_an_ordinary_400_does_not_stall_the_box(monkeypatch):
    """The other half of that judgement: a real bad-request must NOT be read
    as a rate limit, or one malformed payload idles every caller for a
    minute."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 12})
    cli, _p = _client(monkeypatch, lambda req: httpx.Response(
        400, json={"detail": "unknown parameter: top_q"}))
    with cli:
        cli.post("/chat/completions", json={})
    assert rl.global_used() == 1
    assert rl.held_for("openai_compatible") == 0, \
        "one bad payload must not idle the whole box"


def test_a_transport_error_does_not_read_as_a_success(monkeypatch):
    """A connection reset never reaches the response hook, so its request
    would sit in the meter as a send that worked — the reading that hides a
    retry storm."""
    def _h(req):
        raise httpx.ConnectError("reset", request=req)

    cli, pending = _client(monkeypatch, _h)
    with cli:
        with pytest.raises(httpx.ConnectError):
            cli.post("/chat/completions", json={})
    assert pending, "no unsettled send was tracked"
    ia._settle_pending(pending, "transport_ConnectError")
    snap = call_meter.global_snapshot(series=False)
    assert snap["per_minute"] == 1
    assert snap["failed_per_minute"] == 1
    assert not pending


def test_structured_complete_hands_the_role_down(monkeypatch):
    """The role is what puts these calls on the meter under `learner` instead
    of unattributed — and it travels from `structured_complete`, which is the
    only layer that knows it."""
    from aiforge_core.llm import structured as st
    seen: dict = {}

    class _Ep:
        base_url, api_key, model = "http://x/v1", "", "m"
        provider = "openai_compatible"

    monkeypatch.setattr(st, "_mode", lambda: "instructor")
    monkeypatch.setattr(ia, "available", lambda: True)
    monkeypatch.setattr("aiforge_core.llm.client.resolve", lambda role: _Ep())

    class _Out:
        def model_dump_json(self):
            return "{}"

    def _fake(**kw):
        seen.update(kw)
        return _Out()

    monkeypatch.setattr(ia, "structured", _fake)
    st.structured_complete("learner", [{"role": "user", "content": "x"}],
                           _Out)  # type: ignore[arg-type]
    assert seen.get("role") == "learner"
    assert seen.get("provider") == "openai_compatible"


class _UnreadTransport(httpx.BaseTransport):
    """A transport that returns a response whose body is NOT yet read — what
    every real call looks like. httpx.MockTransport pre-buffers, so it is the
    one shape the hook is never exercised against in tests."""

    def __init__(self, status, payload: bytes):
        self._status, self._payload = status, payload

    def handle_request(self, request):
        return httpx.Response(self._status,
                              stream=httpx._content.IteratorByteStream(
                                  iter([self._payload])),
                              request=request)


def test_the_rate_limit_body_is_read_in_the_shape_production_actually_sees():
    """httpx fires response hooks BEFORE reading the body. `response.text`
    there raises ResponseNotRead on every real call — only MockTransport makes
    the other order look right, so the detection was only ever proven against a
    shape that does not occur."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 12})
    pending: list = []
    hooks = ia._event_hooks("learner", "m", "openai_compatible", pending)
    body = (b'{"detail":"You\'ve used 20 requests with this model in the last '
            b'minute, exceeding your limit of 20 requests per minute."}')
    cli = httpx.Client(transport=_UnreadTransport(400, body),
                       event_hooks=hooks, base_url="http://x")
    with cli:
        r = cli.post("/chat/completions", json={})
        assert r.status_code == 400
    assert rl.held_for("openai_compatible") > 0


def test_an_sdk_internal_retry_that_recovers_still_records_its_failures():
    """The OpenAI SDK retries APIConnectionError itself and returns normally
    from the attempt that worked. Those sends never reach the response hook —
    and discarding them left the meter reading "3 sends, 0 failed" for a call
    that failed twice."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 0})
    n = {"i": 0}

    def _h(req):
        n["i"] += 1
        if n["i"] < 3:
            raise httpx.ConnectError("reset", request=req)
        return httpx.Response(200, json={"ok": 1})

    pending: list = []
    hooks = ia._event_hooks("learner", "m", "openai_compatible", pending)
    cli = httpx.Client(transport=httpx.MockTransport(_h), event_hooks=hooks,
                       base_url="http://x")
    with cli:
        for _ in range(3):                 # stands in for the SDK's own retries
            try:
                cli.post("/chat/completions", json={})
            except httpx.ConnectError:
                pass
    assert len(pending) == 2               # two sends never got a response
    ia._settle_pending(pending, "no_response")
    snap = call_meter.global_snapshot(series=False)
    assert snap["per_minute"] == 3
    assert snap["failed_per_minute"] == 2


# --- structured() itself, not just its hooks ---------------------------------
# `instructor` is an optional extra and is not installed in this environment, so
# the SDK layer is faked. What is faked is only the third-party surface
# (`instructor.from_openai`, `openai.OpenAI`); everything under test — the hook
# wiring, the client kwargs, the settle path, the no-httpx fallback — is ours.

import sys
import types

from pydantic import BaseModel


class _Ok(BaseModel):
    ok: int = 1


def _fake_sdk(monkeypatch, handler, *, capture: dict):
    """Install fake `instructor` / `openai` modules whose create() really POSTs
    through whatever http_client the adapter handed OpenAI — so a hook that was
    never wired in shows up as a missing count, not as a passing test."""
    def _OpenAI(**kw):
        capture.update(kw)
        return types.SimpleNamespace(**kw)

    def _from_openai(client, mode=None):
        capture["mode"] = mode

        def _create(*, model, messages, response_model, max_retries, **kwargs):
            capture["instructor_max_retries"] = max_retries
            http = getattr(client, "http_client", None)
            if http is None:
                raise AssertionError("no http_client handed to OpenAI")
            for _ in range(handler.get("sends", 1)):
                http.post("/chat/completions", json={})
            if handler.get("raise"):
                raise handler["raise"]
            return _Ok()

        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(
                create=_create)))

    monkeypatch.setitem(sys.modules, "openai",
                        types.SimpleNamespace(OpenAI=_OpenAI))
    monkeypatch.setitem(sys.modules, "instructor", types.SimpleNamespace(
        from_openai=_from_openai,
        Mode=types.SimpleNamespace(MD_JSON="md_json")))


def _mock_httpx_client(monkeypatch, responder):
    """Make the adapter's own httpx.Client talk to a mock transport."""
    real = httpx.Client

    def _mk(**kw):
        kw.pop("verify", None)
        return real(transport=httpx.MockTransport(responder),
                    base_url="http://x", **kw)

    monkeypatch.setattr(httpx, "Client", _mk)


def test_structured_wires_the_hooks_into_the_client_the_sdk_uses(monkeypatch):
    """The hooks are only worth anything if they reach the client the SDK
    actually posts through. Every other test here calls _event_hooks directly
    and so cannot see a wiring mistake."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 50})
    cap: dict = {}
    _fake_sdk(monkeypatch, {"sends": 3}, capture=cap)
    _mock_httpx_client(monkeypatch,
                       lambda req: httpx.Response(200, json={"ok": 1}))
    out = ia.structured(base_url="http://x/v1", api_key="", model="m",
                        messages=[{"role": "user", "content": "x"}],
                        response_model=_Ok, role="learner")
    assert isinstance(out, _Ok)
    assert rl.global_used() == 3, "the SDK's client was not the hooked one"
    snap = call_meter.global_snapshot(series=False)
    assert snap["per_minute"] == 3
    assert snap["by_role"].get("learner") == 3


def test_structured_does_not_let_the_sdk_add_a_third_retry_layer(monkeypatch):
    """instructor reasks, and structured_complete's fallback loop retries after
    that. The SDK retrying on its own (default 2) multiplies both — up to nine
    sends for one extraction, each taking a ceiling slot."""
    cap: dict = {}
    _fake_sdk(monkeypatch, {"sends": 1}, capture=cap)
    _mock_httpx_client(monkeypatch,
                       lambda req: httpx.Response(200, json={"ok": 1}))
    ia.structured(base_url="http://x/v1", api_key="", model="m",
                  messages=[], response_model=_Ok, role="learner")
    # The SDK client's own retry budget…
    assert cap.get("max_retries") == 0
    # …while instructor keeps its reasks, which are a different thing: a reask
    # is a new PROMPT after a validation failure, not a re-send of the same one.
    assert cap.get("instructor_max_retries") == 2


def test_structured_settles_its_sends_when_the_call_raises(monkeypatch):
    """The raise path of _settle_pending, end to end rather than hand-called."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 0})
    boom = RuntimeError("instructor gave up")
    _fake_sdk(monkeypatch, {"sends": 2, "raise": boom}, capture={})

    def _resp(req):
        raise httpx.ConnectError("reset", request=req)

    _mock_httpx_client(monkeypatch, _resp)
    with pytest.raises(httpx.ConnectError):
        ia.structured(base_url="http://x/v1", api_key="", model="m",
                      messages=[], response_model=_Ok, role="learner")
    snap = call_meter.global_snapshot(series=False)
    assert snap["per_minute"] == 1
    assert snap["failed_per_minute"] == 1, "an unanswered send read as a success"


def test_the_no_httpx_fallback_still_takes_a_ceiling_slot(monkeypatch):
    """If the hooked client cannot be built, the SDK uses its own — with no
    hooks at all. Charging one slot up front is a smaller error than exempting
    the whole path, which is the bug this file exists to fix."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 50})
    cap: dict = {}

    def _create_no_client(*, model, messages, response_model, max_retries,
                          **kwargs):
        return _Ok()

    _fake_sdk(monkeypatch, {"sends": 1}, capture=cap)
    monkeypatch.setitem(sys.modules, "instructor", types.SimpleNamespace(
        from_openai=lambda client, mode=None: types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(
                create=_create_no_client))),
        Mode=types.SimpleNamespace(MD_JSON="md_json")))
    # The httpx client cannot be built at all.
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no")))
    ia.structured(base_url="http://x/v1", api_key="", model="m",
                  messages=[], response_model=_Ok, role="learner")
    assert rl.global_used() == 1
    assert "http_client" not in cap
    snap = call_meter.global_snapshot(series=False)
    assert snap["per_minute"] == 1
