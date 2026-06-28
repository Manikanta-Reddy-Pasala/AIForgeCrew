"""N13 — the health LM probe must hit the same endpoint ingest uses."""
from __future__ import annotations

from unittest.mock import patch

from aiforge_memory.features.file import extract as file_extract
from aiforge_memory.ops import health
from aiforge_memory.query import translator


def _probed_url(monkeypatch):
    monkeypatch.delenv("AIFORGE_CODEMEM_LM_URL", raising=False)
    monkeypatch.delenv("AIFORGE_INTENT_LM_URL", raising=False)
    seen = {}

    def fake_get(url, timeout=None):
        seen["url"] = url
        raise RuntimeError("probe only")

    with patch.object(health.httpx, "get", side_effect=fake_get):
        health._check_lm()
    return seen["url"]


def test_health_probes_ingest_default_port(monkeypatch):
    url = _probed_url(monkeypatch)
    # Compare against the defaults the ingest modules were built with.
    assert url.startswith("http://127.0.0.1:1235/v1")
    assert translator.DEFAULT_LM_URL.startswith("http://127.0.0.1:1235")
    assert file_extract.DEFAULT_LM_URL.startswith("http://127.0.0.1:1235")


def test_health_honours_lm_env(monkeypatch):
    monkeypatch.setenv("AIFORGE_CODEMEM_LM_URL", "http://10.0.0.5:9999/v1")
    seen = {}

    def fake_get(url, timeout=None):
        seen["url"] = url
        raise RuntimeError("probe only")

    with patch.object(health.httpx, "get", side_effect=fake_get):
        health._check_lm()
    assert seen["url"] == "http://10.0.0.5:9999/v1/models"
