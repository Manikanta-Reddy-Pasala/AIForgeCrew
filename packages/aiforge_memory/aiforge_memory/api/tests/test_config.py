"""L0 — RepoConfig load + apply_to_env."""
from __future__ import annotations

import os
from pathlib import Path

from aiforge_memory.config import RepoConfig


def test_load_with_no_yaml_uses_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AIFORGE_CODEMEM_LM_URL", raising=False)
    monkeypatch.delenv("AIFORGE_NEO4J_URI", raising=False)
    cfg = RepoConfig.load(tmp_path)
    assert cfg.name == tmp_path.name
    assert cfg.path == str(tmp_path)
    assert cfg.skip_services is False
    assert cfg.embed_url.startswith("http://")
    assert cfg.neo4j_uri.startswith("bolt://")


def test_load_with_yaml_overrides(tmp_path: Path) -> None:
    (tmp_path / ".aiforge").mkdir()
    (tmp_path / ".aiforge" / "codemem.yaml").write_text(
        "repo:\n"
        "  name: my-app\n"
        "knowledge:\n"
        "  readmes: [README.md, docs/ARCH.md]\n"
        "  conventions: [.aiforge/CONV.md]\n"
        "  exclude: ['target/**']\n"
        "ingest:\n"
        "  skip_summaries: true\n"
        "  file_summary_max_bytes: 16384\n"
        "llm:\n"
        "  url: http://example:1234/v1\n"
        "  model: my-coder\n"
        "  repo_summary_max_tokens: 12000\n"
        "embed:\n"
        "  url: http://example:8764\n"
        "neo4j:\n"
        "  uri: bolt://example:7687\n"
        "  user: ne\n"
        "  password: pw\n"
    )
    cfg = RepoConfig.load(tmp_path)
    assert cfg.name == "my-app"
    assert cfg.readmes == ["README.md", "docs/ARCH.md"]
    assert cfg.conventions == [".aiforge/CONV.md"]
    assert cfg.exclude == ["target/**"]
    assert cfg.skip_summaries is True
    assert cfg.file_summary_max_bytes == 16384
    assert cfg.llm_url == "http://example:1234/v1"
    assert cfg.llm_model == "my-coder"
    assert cfg.repo_summary_max_tokens == 12000
    assert cfg.embed_url == "http://example:8764"
    assert cfg.neo4j_uri == "bolt://example:7687"
    assert cfg.neo4j_user == "ne"
    assert cfg.neo4j_password == "pw"


def test_explicit_name_overrides_yaml(tmp_path: Path) -> None:
    (tmp_path / ".aiforge").mkdir()
    (tmp_path / ".aiforge" / "codemem.yaml").write_text(
        "repo:\n  name: from-yaml\n"
    )
    cfg = RepoConfig.load(tmp_path, name="explicit")
    assert cfg.name == "explicit"


def test_apply_to_env(tmp_path: Path) -> None:
    # Snapshot AIFORGE_* env keys, then restore in a finally so this
    # test's apply_to_env() side-effects never leak into the rest of
    # the pytest session (notably the live Neo4j tests).
    aiforge_keys = [k for k in os.environ if k.startswith("AIFORGE_")]
    snapshot = {k: os.environ[k] for k in aiforge_keys}
    for k in aiforge_keys:
        del os.environ[k]
    try:
        cfg = RepoConfig(
            name="x", path=str(tmp_path),
            llm_url="http://test:1234/v1",
            embed_url="http://test:8764",
            neo4j_uri="bolt://test:7687",
        )
        cfg.apply_to_env()
        assert os.environ["AIFORGE_CODEMEM_LM_URL"] == "http://test:1234/v1"
        assert os.environ["AIFORGE_EMBED_URL"] == "http://test:8764"
        assert os.environ["AIFORGE_NEO4J_URI"] == "bolt://test:7687"
    finally:
        # Strip everything cfg.apply_to_env() set, then restore snapshot.
        for k in [k for k in os.environ if k.startswith("AIFORGE_")]:
            del os.environ[k]
        os.environ.update(snapshot)


def test_env_var_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_NEO4J_URI", "bolt://envhost:7687")
    monkeypatch.setenv("AIFORGE_CODEMEM_LM_URL", "http://envllm:9999/v1")
    cfg = RepoConfig.load(tmp_path)
    assert cfg.neo4j_uri == "bolt://envhost:7687"
    assert cfg.llm_url == "http://envllm:9999/v1"


def test_malformed_yaml_falls_back(tmp_path: Path) -> None:
    (tmp_path / ".aiforge").mkdir()
    (tmp_path / ".aiforge" / "codemem.yaml").write_text("not: valid: yaml: [")
    cfg = RepoConfig.load(tmp_path)
    assert cfg.name == tmp_path.name   # falls back to dir name
