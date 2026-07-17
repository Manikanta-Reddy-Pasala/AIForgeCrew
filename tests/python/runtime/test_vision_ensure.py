"""Bug4 (extended) — vision capability is proactively DETERMINED + PERSISTED for
every model: name-heuristic, explicit flag, or a live probe, at add/set/session-
start/first-use, and lazily when still unknown."""
from __future__ import annotations

import types

from aiforge_core.runtime import vision_detect as vd


def _patch_resolve(monkeypatch, model, base_url="http://x"):
    from aiforge_core.llm import router
    monkeypatch.setattr(router, "resolve",
                        lambda role: types.SimpleNamespace(model=model, base_url=base_url))


def test_ensure_explicit_flag_wins(monkeypatch):
    vd.reset_vision_cache()
    _patch_resolve(monkeypatch, "some-model")
    import aiforge_core.config.model_registry as mr
    monkeypatch.setattr(mr, "vision_for", lambda m, b="": "yes")
    assert vd.ensure_vision_known("chat") is True
    monkeypatch.setattr(mr, "vision_for", lambda m, b="": "no")
    assert vd.ensure_vision_known("chat") is False


def test_ensure_name_heuristic_persists(monkeypatch):
    vd.reset_vision_cache()
    _patch_resolve(monkeypatch, "qwen2.5-vl-7b")
    import aiforge_core.config.model_registry as mr
    saved = {}
    monkeypatch.setattr(mr, "vision_for", lambda m, b="": None)   # auto
    monkeypatch.setattr(mr, "detect_capability", lambda m, k: True)  # VLM name
    monkeypatch.setattr(mr, "set_vision_flag",
                        lambda m, b, f: saved.update(model=m, flag=f) or True)
    assert vd.ensure_vision_known("chat") is True
    assert saved["flag"] == "yes"                 # persisted durably


def test_ensure_unknown_probes_and_persists(monkeypatch):
    vd.reset_vision_cache()
    _patch_resolve(monkeypatch, "mystery-vlm")
    import aiforge_core.config.model_registry as mr
    monkeypatch.setattr(mr, "vision_for", lambda m, b="": None)
    monkeypatch.setattr(mr, "detect_capability", lambda m, k: False)  # name unknown
    saved = {}
    monkeypatch.setattr(mr, "set_vision_flag",
                        lambda m, b, f: saved.update(flag=f) or True)
    # probe succeeds (endpoint accepts the image) -> cached True
    monkeypatch.setattr(vd, "_probe_vision",
                        lambda model, role: vd._VISION_CACHE.__setitem__(model, True) or True)
    assert vd.ensure_vision_known("chat") is True
    assert saved["flag"] == "yes"                 # definite probe persisted


def test_ensure_inconclusive_probe_not_persisted(monkeypatch):
    vd.reset_vision_cache()
    _patch_resolve(monkeypatch, "busy-model")
    import aiforge_core.config.model_registry as mr
    monkeypatch.setattr(mr, "vision_for", lambda m, b="": None)
    monkeypatch.setattr(mr, "detect_capability", lambda m, k: False)
    hit = {"n": 0}
    monkeypatch.setattr(mr, "set_vision_flag",
                        lambda m, b, f: hit.__setitem__("n", hit["n"] + 1) or True)
    # probe inconclusive -> does NOT cache (mirrors _probe_vision transient path)
    monkeypatch.setattr(vd, "_probe_vision", lambda model, role: False)
    assert vd.ensure_vision_known("chat") is None
    assert hit["n"] == 0                           # nothing persisted on a guess


def test_set_vision_flag_persists_to_row(tmp_path, monkeypatch):
    import aiforge_core.config.model_registry as mr
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    row = mr.add_model(label="m", model="foo/bar", base_url="http://h", vision="auto")
    assert mr.vision_for("foo/bar", "http://h") is None      # auto
    assert mr.set_vision_flag("foo/bar", "http://h", "yes") is True
    assert mr.vision_for("foo/bar", "http://h") == "yes"     # durable
    assert mr.set_vision_flag("nope", "http://h", "yes") is False  # no row
