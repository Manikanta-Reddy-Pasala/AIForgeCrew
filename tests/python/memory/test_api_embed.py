"""API embeddings backend — an OpenAI-compatible /v1/embeddings endpoint,
selected by AIFORGE_EMBED_BACKEND=api (no HF download, no torch)."""
from __future__ import annotations
import json
import pytest


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "api")
    monkeypatch.setenv("AIFORGE_EMBED_API_MODEL", "nomic-embed-text")
    monkeypatch.setenv("AIFORGE_EMBED_API_URL", "http://127.0.0.1:1234/v1")
    import aiforge_core.integrations.api_embed as ae
    ae.reset_for_tests()
    return ae


def _stub(monkeypatch, payload):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Resp(payload))


def test_local_embed_dispatches_to_api(api, monkeypatch):
    _stub(monkeypatch, {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]})
    from aiforge_core.memory import local_embed as le
    assert le.embed("hello") == [0.1, 0.2, 0.3, 0.4]
    assert le.embed_dim() == 4


def test_endpoint_appends_embeddings(api):
    assert api._endpoint() == "http://127.0.0.1:1234/v1/embeddings"


def test_missing_model_raises_loud(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "api")
    monkeypatch.delenv("AIFORGE_EMBED_API_MODEL", raising=False)
    import aiforge_core.integrations.api_embed as ae
    ae.reset_for_tests()
    with pytest.raises(RuntimeError, match="AIFORGE_EMBED_API_MODEL"):
        ae.embed("x")


def test_bad_shape_raises(api, monkeypatch):
    _stub(monkeypatch, {"unexpected": "shape"})
    with pytest.raises(RuntimeError, match="unexpected shape"):
        api.embed("x")


def test_api_backend_no_hf_import(api, monkeypatch):
    """Selecting the API backend must NOT touch sentence-transformers / HF."""
    _stub(monkeypatch, {"data": [{"embedding": [1.0, 2.0]}]})
    import sys
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # poison it
    from aiforge_core.memory import local_embed as le
    assert le.embed("x") == [1.0, 2.0]        # works without ST installed


# ── model2vec backend (static embeddings, no torch) ──────────────────────────
def test_model2vec_backend_dispatch(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "model2vec")
    import sys, types
    fake = types.ModuleType("model2vec")
    class _SM:
        @classmethod
        def from_pretrained(cls, src): return cls()
        def encode(self, xs): return [[0.5, 0.6, 0.7]] * len(xs)
    fake.StaticModel = _SM
    monkeypatch.setitem(sys.modules, "model2vec", fake)
    import aiforge_core.integrations.model2vec_embed as m2
    m2.reset_for_tests()
    from aiforge_core.memory import local_embed as le
    assert le.embed("hello") == [0.5, 0.6, 0.7]
    assert le.embed_dim() == 3
