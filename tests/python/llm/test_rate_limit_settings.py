"""The rate-limit knobs are OPERATOR-SETTABLE, not env-only.

Two layers that have burned this codebase before, checked here because these
run everywhere (the HTTP round-trip in ``tests/api`` needs the whole app):

1. The route's pydantic body is the ONLY write path into the store. A bound
   there that disagrees with ``_BOUNDS`` makes the store's own bound
   unreachable — the UI offers a value, gets a 422 for it, and NOTHING in that
   body is written, not even the sibling field that was fine.
2. A knob read from ``os.environ`` alone is INERT however carefully the UI
   saves it. The store is what the UI writes, so the runtime must resolve
   stored -> env -> default like every other knob.
"""
import pytest

from aiforge_core.llm import rate_limiter as rl


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("AIFORGE_LLM_RATE_LIMIT_BACKOFF_S", "AIFORGE_LLM_RATE_LIMIT_CAP_S",
              "AIFORGE_LLM_MAX_RPM"):
        monkeypatch.delenv(k, raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    rl.reset_global()
    yield
    rl.reset_global()


def test_route_bounds_match_the_store():
    """Assert the two agree rather than trusting that they do."""
    from aiforge_core.api.routes.runtime import _RuntimeSettingsBody
    from aiforge_core.config.runtime_settings import _BOUNDS
    for name in ("llm_rate_limit_backoff_s", "llm_rate_limit_cap_s",
                 "llm_max_rpm"):
        lo, hi = _BOUNDS[name]
        got = {}
        for m in _RuntimeSettingsBody.model_fields[name].metadata:
            for attr, key in (("ge", "lo"), ("le", "hi")):
                if hasattr(m, attr):
                    got[key] = getattr(m, attr)
        assert got == {"lo": lo, "hi": hi}, (name, got, (lo, hi))


def test_every_new_knob_is_reachable_from_the_ui():
    """A knob in the store with no field in the route body can never be set by
    an operator, and one in the body with no store entry is silently dropped."""
    from aiforge_core.api.routes.runtime import _RuntimeSettingsBody
    from aiforge_core.config.runtime_settings import _SPEC
    for name in ("llm_rate_limit_backoff_s", "llm_rate_limit_cap_s"):
        assert name in _SPEC
        assert name in _RuntimeSettingsBody.model_fields


def test_the_limiter_reads_the_STORED_cap_not_just_the_env(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    assert rl._hold_cap() == 60.0                    # built-in default
    monkeypatch.setenv("AIFORGE_LLM_RATE_LIMIT_CAP_S", "45")
    from aiforge_core.config import _filecache
    _filecache.clear()
    assert rl._hold_cap() == 45.0                    # env overrides the default
    rs.set_many({"llm_rate_limit_cap_s": 90})
    assert rl._hold_cap() == 90.0                    # the STORE wins over env


def test_the_saved_cap_bounds_a_hostile_retry_after(monkeypatch):
    """End to end: the number in the Settings box is what bounds how long one
    server response can park the whole process."""
    from aiforge_core.config import runtime_settings as rs
    clock = {"t": 1000.0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: clock["t"])
    rs.set_many({"llm_rate_limit_cap_s": 10})
    rl.note_rate_limited(3600.0)
    assert abs(rl.held_for() - 10.0) < 0.01


def test_the_saved_backoff_is_what_the_transport_sleeps(monkeypatch):
    import io
    import urllib.error
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    rs.set_many({"llm_max_rpm": 0, "llm_rate_limit_backoff_s": 7,
                 "llm_rate_limit_cap_s": 600})
    slept: list = []
    monkeypatch.setattr(_http.time, "sleep", lambda s: slept.append(s))
    body = b'{"detail":"exceeding your limit of 20 requests per minute"}'
    monkeypatch.setattr(_http, "_post", lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.HTTPError("http://x", 400, "e", {}, io.BytesIO(body))))
    ep = Endpoint(base_url="http://x/v1", api_key="", model="m",
                  provider="openai_compatible", role="learner", extras={})
    with pytest.raises(urllib.error.HTTPError):
        _http._post_with_retry(ep, b"{}", 600, role="learner", source="test")
    assert slept and 7.0 <= max(slept) <= 7.3, slept
