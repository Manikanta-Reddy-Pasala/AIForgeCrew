"""Working out whether the current model can actually see an image.

There is no hardcoded allowlist: the endpoint is ASKED, by sending it a small
real PNG and reading what comes back. That makes the classification of a
failed probe the whole game, and it errs one way on purpose — only an explicit
modality rejection ("this model does not support image inputs", "text only")
is a definitive no. A 5xx, a 429, a bare 400 whose text is just "HTTP Error
400: Bad Request", a warm-up body, a payload complaint, or a rejection of the
FORM the image was sent in (base64 vs URL) all stay inconclusive, because
caching a false "no" permanently blinds a genuine VLM.

Reading the HTTP BODY is what makes that possible at all: urllib's HTTPError
str() drops it, and the body is where the real message lives.

Resolution order everywhere: the user's manual setting, the registry's
explicit flag, the name heuristic, a cached probe, then a live probe.
"""
from __future__ import annotations

import types as pytypes
import urllib.error

import pytest

from aiforge_core.runtime import vision_detect as V


@pytest.fixture(autouse=True)
def clean_cache():
    V.reset_vision_cache()
    yield
    V.reset_vision_cache()


def _http_error(code=400, body=b"", msg="Bad Request"):
    import io
    return urllib.error.HTTPError("http://x/v1", code, msg, {},
                                  io.BytesIO(body))


# ─── the probe image ───────────────────────────────────────────────────


def test_the_bundled_probe_image_is_used():
    """Some servers reject a degenerate 1x1, so a real small PNG ships with
    the package."""
    assert V._probe_image_b64() != V._PROBE_PNG


def test_a_missing_asset_falls_back_to_the_inline_pixel(monkeypatch):
    monkeypatch.setattr(V, "_PROBE_ASSET", "/nope/vision_probe.png")
    assert V._probe_image_b64() == V._PROBE_PNG


# ─── reading the error ─────────────────────────────────────────────────


def test_the_http_body_is_read_into_the_error_text():
    """str(HTTPError) is only 'HTTP Error 400: Bad Request' — the definitive
    signal lives in the body."""
    exc = _http_error(body=b"model does not support image inputs")
    assert "does not support image inputs" in V._error_text(exc)


def test_an_unreadable_body_still_yields_the_status_line():
    exc = _http_error()
    exc.read = lambda: (_ for _ in ()).throw(OSError("closed"))
    assert "400" in V._error_text(exc)


def test_a_plain_exception_is_just_its_text():
    assert V._error_text(RuntimeError("connection reset")) == "connection reset"


# ─── classifying a failed probe ────────────────────────────────────────


@pytest.mark.parametrize("body", [
    b"this model is text only",
    b"the model only supports text",
    b"model does not support image inputs",
])
def test_an_explicit_modality_rejection_is_a_definitive_no(body):
    assert V._classify_probe_error(_http_error(body=body)) is False


@pytest.mark.parametrize("code", [500, 502, 503, 429])
def test_a_server_error_or_rate_limit_is_never_a_verdict(code):
    """A mid-inference crash body could otherwise look like a rejection and
    poison the cache."""
    assert V._classify_probe_error(
        _http_error(code=code, body=b"model does not support images")) is None


def test_a_bare_400_says_nothing_either_way():
    assert V._classify_probe_error(_http_error()) is None


def test_a_model_still_loading_is_transient(monkeypatch):
    from aiforge_core.llm.client import _errors
    marker = next(iter(_errors._MODEL_DROP_MARKERS))
    exc = _http_error(body=(marker + " — multimodal input rejected").encode())
    assert V._classify_probe_error(exc) is None


@pytest.mark.parametrize("body", [
    b"base64 images are not supported, use image_url",
    b"data-uri not supported",
])
def test_a_complaint_about_how_the_image_was_sent_is_not_a_capability(body):
    """A real VLM that wants a URL emits exactly these."""
    assert V._classify_probe_error(_http_error(body=body)) is None


def test_a_transport_blip_is_inconclusive():
    assert V._classify_probe_error(OSError("connection reset")) is None


# ─── probing an endpoint ───────────────────────────────────────────────


@pytest.fixture
def endpoint(monkeypatch):
    from aiforge_core.llm.client import _http
    state: dict = {"raise": None, "seen": {}}

    def _post(ep, payload, timeout):
        state["seen"] = {"ep": ep, "payload": payload, "timeout": timeout}
        if state["raise"]:
            raise state["raise"]
        return {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(_http, "_post", _post)
    return state


def test_an_endpoint_that_accepts_the_image_can_see(endpoint):
    assert V.probe_vision_endpoint("qwen-vl", "http://lm:1234/v1") is True
    payload = endpoint["seen"]["payload"].decode()
    assert "image_url" in payload
    assert "data:image/png;base64," in payload
    assert '"max_tokens": 1' in payload, "the probe asks for nothing back"


def test_an_endpoint_that_rejects_the_modality_cannot(endpoint):
    endpoint["raise"] = _http_error(body=b"this model is text only")
    assert V.probe_vision_endpoint("gpt-text", "http://lm:1234/v1") is False


def test_an_unreachable_endpoint_is_inconclusive(endpoint):
    endpoint["raise"] = OSError("refused")
    assert V.probe_vision_endpoint("m", "http://lm:1234/v1") is None


def test_nothing_to_probe_is_inconclusive(endpoint):
    assert V.probe_vision_endpoint("", "http://x") is None
    assert V.probe_vision_endpoint("m", "") is None


def test_the_probe_timeout_is_tunable(endpoint, monkeypatch):
    monkeypatch.setenv("AIFORGE_VISION_PROBE_TIMEOUT_S", "3")
    V.probe_vision_endpoint("m", "http://x")
    assert endpoint["seen"]["timeout"] == 3


def test_a_junk_timeout_falls_back(endpoint, monkeypatch):
    monkeypatch.setenv("AIFORGE_VISION_PROBE_TIMEOUT_S", "soon")
    V.probe_vision_endpoint("m", "http://x")
    assert endpoint["seen"]["timeout"] == 8


# ─── caching a verdict ─────────────────────────────────────────────────


@pytest.fixture
def role_probe(monkeypatch):
    from aiforge_core.llm import router
    state: dict = {"verdict": True, "probes": 0,
                   "ep": pytypes.SimpleNamespace(model="qwen-vl",
                                                 base_url="http://lm/v1",
                                                 api_key="k")}
    monkeypatch.setattr(router, "resolve", lambda role: state["ep"])

    def _probe(model, base_url, api_key=None, **kw):
        state["probes"] += 1
        return state["verdict"]
    monkeypatch.setattr(V, "probe_vision_endpoint", _probe)
    return state


def test_a_definite_yes_is_probed_once_then_cached(role_probe):
    assert V._probe_vision("qwen-vl", "chat") is True
    assert V._probe_vision("qwen-vl", "chat") is True
    assert role_probe["probes"] == 1


def test_a_definite_no_is_cached_too(role_probe):
    role_probe["verdict"] = False
    assert V._probe_vision("m", "chat") is False
    assert V._probe_vision("m", "chat") is False
    assert role_probe["probes"] == 1


def test_an_inconclusive_probe_is_retried_next_time(role_probe):
    """Never permanently mark a genuine VLM as blind."""
    role_probe["verdict"] = None
    assert V._probe_vision("m", "chat") is False
    assert V._probe_vision("m", "chat") is False
    assert role_probe["probes"] == 2


def test_an_unresolvable_role_cannot_see(monkeypatch):
    from aiforge_core.llm import router
    monkeypatch.setattr(router, "resolve",
                        lambda role: (_ for _ in ()).throw(OSError("no cfg")))
    assert V._probe_vision("m", "chat") is False


# ─── the fast check ────────────────────────────────────────────────────


@pytest.fixture
def registry(monkeypatch):
    from aiforge_core.config import model_registry, runtime_settings
    state: dict = {"override": 0, "flag": None, "heuristic": False,
                   "set_flags": [], "updated": []}
    monkeypatch.setattr(runtime_settings, "get",
                        lambda key: state["override"])
    monkeypatch.setattr(model_registry, "vision_for",
                        lambda m, b: state["flag"])
    monkeypatch.setattr(model_registry, "detect_capability",
                        lambda m, kind: state["heuristic"])
    monkeypatch.setattr(model_registry, "set_vision_flag",
                        lambda m, b, v: state["set_flags"].append((m, v)))
    monkeypatch.setattr(model_registry, "update_model",
                        lambda rid, **kw: state["updated"].append((rid, kw)))
    return state


def test_the_users_own_setting_wins_over_everything(registry, role_probe):
    registry["override"] = 1
    registry["flag"] = "no"
    assert V.vision_enabled("chat") is True
    assert V.ensure_vision_known("chat") is True


def test_an_unreadable_setting_is_simply_not_an_override(monkeypatch):
    from aiforge_core.config import runtime_settings
    monkeypatch.setattr(runtime_settings, "get",
                        lambda k: (_ for _ in ()).throw(OSError("cfg")))
    assert V._settings_override() is False


def test_an_explicit_registry_flag_beats_a_probe(registry, role_probe):
    registry["flag"] = "yes"
    assert V.vision_enabled("chat") is True
    assert role_probe["probes"] == 0
    registry["flag"] = "no"
    assert V.vision_enabled("chat") is False
    assert role_probe["probes"] == 0


def test_a_recognised_vlm_family_needs_no_probe(registry, role_probe):
    """The same signal the Settings badge and the ADK path use, so all three
    detectors agree."""
    registry["heuristic"] = True
    assert V.vision_enabled("chat") is True
    assert role_probe["probes"] == 0


def test_an_unknown_model_kicks_a_background_probe(registry, role_probe,
                                                   monkeypatch):
    warmed: list = []
    monkeypatch.setattr(V, "warm_vision_async", lambda role: warmed.append(role))
    assert V.vision_enabled("chat") is False
    assert warmed == ["chat"], "so the next turn knows"


def test_the_upload_path_probes_right_there(registry, role_probe):
    assert V.vision_enabled("chat", probe=True) is True
    assert role_probe["probes"] == 1


def test_a_role_with_no_model_cannot_see(registry, role_probe):
    role_probe["ep"] = pytypes.SimpleNamespace(model="", base_url="")
    assert V.vision_enabled("chat") is False


def test_a_broken_router_cannot_see(registry, monkeypatch):
    from aiforge_core.llm import router
    monkeypatch.setattr(router, "resolve",
                        lambda role: (_ for _ in ()).throw(OSError("x")))
    assert V.vision_enabled("chat") is False
    assert V._resolve_vision_model("chat") == ("", "")


def test_a_broken_registry_does_not_stop_the_check(registry, role_probe,
                                                   monkeypatch):
    from aiforge_core.config import model_registry
    monkeypatch.setattr(model_registry, "vision_for",
                        lambda m, b: (_ for _ in ()).throw(OSError("cfg")))
    assert V.vision_enabled("chat", probe=True) is True


# ─── determining it up front ───────────────────────────────────────────


def test_a_name_heuristic_hit_is_persisted(registry, role_probe):
    """So it is durable rather than re-derived every boot."""
    registry["heuristic"] = True
    assert V.ensure_vision_known("chat") is True
    assert registry["set_flags"] == [("qwen-vl", "yes")]
    assert V._VISION_CACHE["qwen-vl"] is True


def test_a_definitive_probe_result_is_written_to_the_registry(registry,
                                                              role_probe):
    role_probe["verdict"] = False
    assert V.ensure_vision_known("chat") is False
    assert registry["set_flags"] == [("qwen-vl", "no")]


def test_an_inconclusive_probe_persists_nothing(registry, role_probe):
    role_probe["verdict"] = None
    assert V.ensure_vision_known("chat") is None
    assert registry["set_flags"] == []


def test_a_cached_verdict_short_circuits(registry, role_probe):
    V._VISION_CACHE["qwen-vl"] = True
    assert V.ensure_vision_known("chat") is True
    assert role_probe["probes"] == 0


def test_a_role_with_no_model_is_unresolvable(registry, role_probe):
    role_probe["ep"] = pytypes.SimpleNamespace(model="", base_url="")
    assert V.ensure_vision_known("chat") is None


def test_an_unwritable_registry_still_returns_the_verdict(registry, role_probe,
                                                          monkeypatch):
    from aiforge_core.config import model_registry
    monkeypatch.setattr(model_registry, "set_vision_flag",
                        lambda *a: (_ for _ in ()).throw(OSError("ro")))
    assert V.ensure_vision_known("chat") is True


# ─── warming in the background ─────────────────────────────────────────


def test_warming_never_blocks_the_request(monkeypatch):
    started: list = []
    import threading
    monkeypatch.setattr(threading, "Thread",
                        lambda target=None, name=None, daemon=None:
                        pytypes.SimpleNamespace(start=lambda: started.append(
                            (name, daemon, target))))
    V.warm_vision_async("chat")
    assert started[0][0] == "vision-warm-chat"
    assert started[0][1] is True


def test_warming_never_surfaces_an_error(monkeypatch):
    monkeypatch.setattr(V, "ensure_vision_known",
                        lambda role: (_ for _ in ()).throw(RuntimeError("x")))
    V._safe_ensure("chat")   # no raise


# ─── deciding at model-add time ────────────────────────────────────────


def test_a_probe_at_add_time_is_persisted_on_the_registry_row(registry,
                                                              endpoint):
    assert V.classify_and_store_vision("r1", "qwen-vl", "http://lm/v1") is True
    assert registry["updated"] == [("r1", {"vision": "yes"})]


def test_a_rejecting_model_is_stored_as_no(registry, endpoint):
    endpoint["raise"] = _http_error(body=b"this model is text only")
    assert V.classify_and_store_vision("r1", "m", "http://lm/v1") is False
    assert registry["updated"] == [("r1", {"vision": "no"})]


def test_an_unreachable_endpoint_falls_back_to_the_name(registry, endpoint):
    """A recognisable VLM is still marked yes even if it is not up yet."""
    endpoint["raise"] = OSError("refused")
    registry["heuristic"] = True
    assert V.classify_and_store_vision("r1", "llava-13b", "http://lm/v1") is True
    assert registry["updated"] == [("r1", {"vision": "yes"})]


def test_an_unknown_unreachable_model_stores_nothing(registry, endpoint):
    endpoint["raise"] = OSError("refused")
    assert V.classify_and_store_vision("r1", "mystery", "http://lm/v1") is None
    assert registry["updated"] == []


def test_only_this_models_cached_probe_is_evicted(registry, endpoint):
    """Clearing the whole cache would force a needless re-probe of every other
    model."""
    V._VISION_CACHE.update({"qwen-vl": True, "other": True})
    V.classify_and_store_vision("r1", "qwen-vl", "http://lm/v1")
    assert "qwen-vl" not in V._VISION_CACHE
    assert V._VISION_CACHE["other"] is True


def test_an_unwritable_registry_row_still_returns_the_verdict(registry,
                                                              endpoint,
                                                              monkeypatch):
    from aiforge_core.config import model_registry
    monkeypatch.setattr(model_registry, "update_model",
                        lambda rid, **kw: (_ for _ in ()).throw(OSError("ro")))
    assert V.classify_and_store_vision("r1", "m", "http://lm/v1") is True
