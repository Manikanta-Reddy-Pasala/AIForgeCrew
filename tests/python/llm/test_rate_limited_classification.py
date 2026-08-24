"""A rate limit is transient however the gateway spells it.

The observed rejection arrives as HTTP 400 with
  {"detail": "You've used 20 requests with this model in the last minute,
   exceeding your limit of 20 requests per minute."}
A bare 400 is classified PERMANENT, so the retry loop skipped its backoff,
bubbled instantly, and the model chain spent another request on the next model
inside the same throttled minute — turning one rejection into several.
"""
import io
import urllib.error

import pytest

from aiforge_core.llm import rate_limiter as rl
from aiforge_core.llm.client import _errors as E


def _http_err(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/v1/chat/completions", code,
                                  "err", {}, io.BytesIO(body))


_RATE_400 = (b'{"detail":"You\'ve used 20 requests with this model in the '
             b'last minute, exceeding your limit of 20 requests per minute."}')


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    from aiforge_core.config import _filecache
    _filecache.clear()
    rl.reset_global()
    yield
    rl.reset_global()


def test_a_400_rate_limit_is_transient():
    retry, label = E._is_transient_exc(_http_err(400, _RATE_400))
    assert retry is True
    assert label == "rate_limited"


def test_a_429_carries_the_same_label():
    retry, label = E._is_transient_exc(_http_err(429, b'{"detail":"slow down"}'))
    assert retry is True
    assert label == "rate_limited"


def test_an_ordinary_400_stays_permanent():
    """The judgement has to cut both ways: a real bad request must still fail
    fast, or every malformed payload gets retried into a long stall."""
    retry, label = E._is_transient_exc(
        _http_err(400, b'{"detail":"unknown parameter: top_q"}'))
    assert retry is False
    assert label == "http_400"


def test_a_model_drop_still_wins_its_own_label():
    retry, label = E._is_transient_exc(
        _http_err(400, b'{"error":{"message":"model not loaded"}}'))
    assert retry is True
    assert label == "model_reloading_4xx"


def test_a_403_rate_limit_is_not_mistaken_for_the_nginx_blip():
    """401/403 are in _TRANSIENT_HTTP by default (the self-hosted proxy's
    intermittent "Authorization Required"). Checking that set FIRST meant a
    gateway answering with a 403 rate limit — Cloudflare and several API
    gateways do — got a 0.5s backoff and never told the ceiling."""
    retry, label = E._is_transient_exc(
        _http_err(403, b'{"detail":"rate limit exceeded for this key"}'))
    assert (retry, label) == (True, "rate_limited")


def test_an_exhausted_quota_is_not_a_rate_limit():
    """A per-day cap or an exhausted billing quota is permanent for HOURS —
    waiting a minute cannot fix it. Treated as a rate limit, a dead API key
    re-armed a process-wide hold on every attempt and again for every model in
    the chain: minutes of stall per call, where it used to fail fast."""
    for body in (b'{"error":{"message":"You exceeded your current quota",'
                 b'"type":"insufficient_quota"}}',
                 b'{"detail":"Quota exceeded for quota metric requests per day"}'):
        assert not E.is_rate_limited(_http_err(429, body)), body
        assert not E.is_rate_limited(_http_err(403, body)), body
        assert E.is_quota_exhausted(_http_err(403, body)), body
    # …and it does not arm a hold anywhere.
    retry, label = E._is_transient_exc(
        _http_err(403, b'{"detail":"quota exceeded for this key"}'))
    assert label != "rate_limited"


def test_a_pasted_error_in_an_echoed_request_does_not_freeze_the_box():
    """Gateways commonly ECHO the request in the error body. A user asking the
    agent about a rate-limit error they pasted in would otherwise arm a
    process-wide 60s hold from their own words."""
    body = (b'{"detail":"unknown parameter: top_q","request":'
            b'{"messages":[{"role":"user","content":"why do I get '
            b'rate limit exceeded, too many requests?"}]}}')
    assert not E.is_rate_limited(_http_err(400, body))
    assert E._is_transient_exc(_http_err(400, body)) == (False, "http_400")


def test_a_plaintext_gateway_page_is_still_matched():
    """A bare nginx/Cloudflare page has no field to narrow to, so the whole
    body stays in scope — narrowing must not become a way to MISS a real
    rejection."""
    assert E.is_rate_limited(
        _http_err(429, b"<html><body>429 Too Many Requests</body></html>"))


def test_a_plain_403_is_still_the_auth_blip():
    retry, label = E._is_transient_exc(_http_err(403, b"Authorization Required"))
    assert (retry, label) == (True, "http_403")


def test_classification_reads_the_WHOLE_body():
    """_http_err_body clips to 600 chars for the log line. A gateway that
    echoes the request before its verdict pushed the verdict past the clip, so
    the two classifiers in this file gave opposite answers on one exception."""
    padded = b'{"echo":"' + b'x' * 900 + b'","detail":"exceeding your limit of 20 requests per minute"}'
    exc = _http_err(400, padded)
    assert E.is_rate_limited(exc)
    assert E._is_transient_exc(exc) == (True, "rate_limited")


def test_is_rate_limited_helper():
    assert E.is_rate_limited(_http_err(400, _RATE_400))
    assert E.is_rate_limited(_http_err(429, b""))
    assert not E.is_rate_limited(_http_err(400, b'{"detail":"bad param"}'))
    assert not E.is_rate_limited(_http_err(500, _RATE_400))
    assert not E.is_rate_limited(ValueError("nope"))


def test_the_body_survives_two_readers():
    """exc.read() is ONE-SHOT. The classifier reads it to spot the rate-limit
    phrase; the transport_error log reads it again to show the operator what
    the server said. Without the stash, whichever ran second logged nothing."""
    exc = _http_err(400, _RATE_400)
    retry, label = E._is_transient_exc(exc)
    assert label == "rate_limited"
    assert "requests per minute" in E._http_err_body(exc)


def test_a_rate_limited_reply_backs_off_for_a_real_minute(monkeypatch):
    """A 0.5s backoff just re-earns the same rejection and pays a request to
    do it. And the rejection is the only ground truth we get about what the
    server counts, so it must reach the ceiling too."""
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    rs.set_many({"llm_max_rpm": 9})
    slept: list = []
    monkeypatch.setattr(_http.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(_http, "_post",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _http_err(400, _RATE_400)))
    ep = Endpoint(base_url="http://x/v1", api_key="", model="m",
                  provider="openai_compatible", role="learner", extras={})
    with pytest.raises(urllib.error.HTTPError):
        _http._post_with_retry(ep, b"{}", 600, role="learner", source="test")
    assert slept, "a rate limit was retried with no wait at all"
    assert max(slept) >= 20.0, slept
    # …and every other caller of THAT provider now holds.
    assert rl.held_for("openai_compatible") > 0


def test_retry_after_is_honoured_over_the_default(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    rs.set_many({"llm_max_rpm": 0})          # ceiling off: backoff is separate
    slept: list = []
    monkeypatch.setattr(_http.time, "sleep", lambda s: slept.append(s))
    err = _http_err(429, b'{"detail":"slow down"}')
    err.headers = {"Retry-After": "45"}
    monkeypatch.setattr(_http, "_post",
                        lambda *a, **k: (_ for _ in ()).throw(err))
    ep = Endpoint(base_url="http://x/v1", api_key="", model="m",
                  provider="openai_compatible", role="learner", extras={})
    with pytest.raises(urllib.error.HTTPError):
        _http._post_with_retry(ep, b"{}", 600, role="learner", source="test")
    assert slept, slept
    assert 45.0 <= max(slept) <= 45.3, slept


def _rate_limited_post(monkeypatch, _http, headers=None):
    err = _http_err(400, _RATE_400)
    if headers:
        err.headers = headers
    monkeypatch.setattr(_http, "_post",
                        lambda *a, **k: (_ for _ in ()).throw(err))
    return err


def test_a_classifier_still_gets_a_retry_inside_its_own_budget(monkeypatch):
    """C4. A flat 20s backoff plus timeout_s never fits a caller with a 15-30s
    budget, so the retry was refused and the wait computed for it was never
    slept — the routers and classifiers behaved exactly as before this existed.
    The wait is clamped to the room actually left instead."""
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    rs.set_many({"llm_max_rpm": 0})
    slept: list = []
    monkeypatch.setattr(_http.time, "sleep", lambda s: slept.append(s))
    _rate_limited_post(monkeypatch, _http)
    ep = Endpoint(base_url="http://x/v1", api_key="", model="m",
                  provider="openai_compatible", role="triage", extras={})
    with pytest.raises(urllib.error.HTTPError):
        _http._post_with_retry(ep, b"{}", 20, role="triage", source="test")
    assert slept, "a 20s classifier got no wait and no retry at all"
    # …and it stayed inside its own declared budget (20*1.5 = 30s).
    assert sum(slept) <= 30.0, slept


def test_a_hostile_retry_after_cannot_own_the_process(monkeypatch):
    """C3. Retry-After: 3600 with AIFORGE_LLM_RETRY_BUDGET=0 (a documented
    knob that removes the deadline) was two hours of blocking sleep on the
    caller's thread, from one header."""
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    rs.set_many({"llm_max_rpm": 0})
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BUDGET", "0")
    slept: list = []
    monkeypatch.setattr(_http.time, "sleep", lambda s: slept.append(s))
    _rate_limited_post(monkeypatch, _http, headers={"Retry-After": "3600"})
    ep = Endpoint(base_url="http://x/v1", api_key="", model="m",
                  provider="openai_compatible", role="learner", extras={})
    with pytest.raises(urllib.error.HTTPError):
        _http._post_with_retry(ep, b"{}", 600, role="learner", source="test")
    assert slept
    assert max(slept) <= 60.3, slept          # AIFORGE_LLM_RATE_LIMIT_CAP_S
    assert sum(slept) <= 121.0, slept


def test_the_ceiling_is_told_even_when_this_caller_cannot_wait(monkeypatch):
    """The half that has to survive a short budget: this classifier bubbles
    immediately, but the model chain's next attempt now queues on the hold
    instead of spending another request discovering the same wall."""
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    rs.set_many({"llm_max_rpm": 0})
    monkeypatch.setattr(_http.time, "sleep", lambda s: None)
    _rate_limited_post(monkeypatch, _http)
    ep = Endpoint(base_url="http://x/v1", api_key="", model="m",
                  provider="openai_compatible", role="triage", extras={})
    with pytest.raises(urllib.error.HTTPError):
        _http._post_with_retry(ep, b"{}", 15, role="triage", source="test")
    assert rl.held_for("openai_compatible") > 0
    # …and a DIFFERENT provider is untouched: the local mlx server declares no
    # rate limit and did not reject anything.
    assert rl.held_for("mlx_lm") == 0.0


def test_the_clamp_still_leaves_a_real_wait_and_a_real_retry(monkeypatch):
    """The subtlest line in the change, at its actual operating point.

    Production's short callers are turn_router (20s), task_router (15s) and the
    30s API route — not the 600s of the tests above. Unclamped, 20s of backoff
    plus a full attempt never fit their budget, so the retry was refused and
    nothing was slept: identical to the pre-fix behaviour. Clamped, they get a
    shorter real wait AND a second attempt."""
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    rs.set_many({"llm_max_rpm": 0})
    attempts: list = []
    slept: list = []
    monkeypatch.setattr(_http.time, "sleep", lambda s: slept.append(s))

    def _boom(*a, **k):
        attempts.append(1)
        raise _http_err(400, _RATE_400)

    monkeypatch.setattr(_http, "_post", _boom)
    ep = Endpoint(base_url="http://x/v1", api_key="", model="m",
                  provider="openai_compatible", role="triage", extras={})
    with pytest.raises(urllib.error.HTTPError):
        _http._post_with_retry(ep, b"{}", 20, role="triage", source="test")
    assert len(attempts) >= 2, "the classifier got no retry at all"
    assert slept, slept
    assert 1.0 < max(slept) < 20.0, slept
