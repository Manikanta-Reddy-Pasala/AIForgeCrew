"""Bug4 — vision auto-detect: tightened probe classifier, bundled test image,
add-time probe persists the flag."""
from __future__ import annotations

import os

from aiforge_core.runtime import vision_detect


def test_bundled_probe_image_exists_and_loads():
    assert os.path.isfile(vision_detect._PROBE_ASSET)
    b64 = vision_detect._probe_image_b64()
    # a real (non-1x1) PNG, so meaningfully larger than the inline fallback
    assert len(b64) > len(vision_detect._PROBE_PNG)


def test_classifier_only_definitive_modality_rejection_is_no():
    # a MODALITY-BOUND rejection phrase → definitive "no vision"
    assert vision_detect._classify_probe_error(
        RuntimeError("this model does not support image input")) is False
    assert vision_detect._classify_probe_error(
        RuntimeError("400: vision is not supported")) is False
    # PAYLOAD errors on a real VLM ("invalid image_url" / "cannot decode image")
    # must be INCONCLUSIVE (None), never a sticky no-vision — this was the
    # false-negative: a real VLM rejecting the probe image got marked non-vision.
    assert vision_detect._classify_probe_error(
        RuntimeError("400 invalid image content for this model")) is None
    assert vision_detect._classify_probe_error(
        RuntimeError("cannot decode image")) is None
    # ambiguous / transport errors → inconclusive (None)
    assert vision_detect._classify_probe_error(
        RuntimeError("HTTP 400 Bad Request")) is None
    assert vision_detect._classify_probe_error(
        ConnectionError("connection refused")) is None
    assert vision_detect._classify_probe_error(
        RuntimeError("invalid json in response")) is None


def test_classify_and_store_persists_probe_result(monkeypatch):
    calls = {}
    monkeypatch.setattr(vision_detect, "probe_vision_endpoint",
                        lambda *a, **k: True)

    class _Reg:
        @staticmethod
        def update_model(rid, **kw):
            calls["rid"], calls["vision"] = rid, kw.get("vision")

    import aiforge_core.config.model_registry as mr
    monkeypatch.setattr(mr, "update_model", _Reg.update_model)
    verdict = vision_detect.classify_and_store_vision("m1", "some-vlm", "http://x")
    assert verdict is True
    assert calls == {"rid": "m1", "vision": "yes"}


def test_classify_and_store_skips_on_inconclusive(monkeypatch):
    monkeypatch.setattr(vision_detect, "probe_vision_endpoint",
                        lambda *a, **k: None)
    hit = {"n": 0}
    import aiforge_core.config.model_registry as mr
    monkeypatch.setattr(mr, "update_model",
                        lambda *a, **k: hit.__setitem__("n", hit["n"] + 1))
    assert vision_detect.classify_and_store_vision("m1", "x", "http://x") is None
    assert hit["n"] == 0   # never persists a guess


def test_classifier_order_independent_rejections():
    from aiforge_core.runtime import vision_detect as vd
    # order-independent modality rejections → definitive no-vision
    for m in ("model is text only", "no image support", "multimodal not enabled",
              "vision not supported", "only supports text"):
        assert vd._classify_probe_error(RuntimeError(m)) is False, m
    # payload errors about the probe image → inconclusive (real VLM protected)
    for m in ("invalid image_url", "cannot decode image", "image format unsupported"):
        assert vd._classify_probe_error(RuntimeError(m)) is None, m


def test_classifier_reads_http_400_body_not_status_phrase():
    import io
    import urllib.error
    from aiforge_core.runtime import vision_detect as vd
    # the REAL LM-Studio path: HTTP 400 whose str() is only 'HTTP Error 400: Bad
    # Request' — the definitive signal is in the BODY. 'Bad Request' must NOT be
    # treated as a payload error (that regressed text-only detection to None).
    e = urllib.error.HTTPError("http://x", 400, "Bad Request", {},
                               io.BytesIO(b"this model does not support image inputs"))
    assert vd._classify_probe_error(e) is False
