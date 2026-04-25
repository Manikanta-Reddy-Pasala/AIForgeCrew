from unittest.mock import patch, MagicMock
from aiforge_core.legacy import embed as embed_mod


def _fake_urlopen(response_json):
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.read.return_value = __import__("json").dumps(response_json).encode()
    return mock


def test_embed_single():
    with patch.object(embed_mod.urllib.request, "urlopen",
                      return_value=_fake_urlopen({"embedding": [0.1] * 1024})):
        v = embed_mod.embed("hello")
    assert len(v) == 1024


def test_embed_batch():
    with patch.object(embed_mod.urllib.request, "urlopen",
                      return_value=_fake_urlopen({"embeddings": [[0.1] * 1024, [0.2] * 1024]})):
        vs = embed_mod.embed_batch(["a", "b"])
    assert len(vs) == 2
    assert len(vs[0]) == 1024


def test_embed_url_env_override(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_URL", "http://custom:9999")
    import importlib
    importlib.reload(embed_mod)
    assert embed_mod.SIDECAR_URL == "http://custom:9999"
