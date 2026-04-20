from pathlib import Path
import os
import pytest
from aiforge_core.config import PaperclipConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_mode_is_cloud(monkeypatch):
    monkeypatch.delenv("AIFORGE_ARCHITECT_MODE", raising=False)
    cfg = PaperclipConfig.load(REPO_ROOT)
    assert cfg.architect_mode.mode == "cloud"
    assert cfg.architect_mode.model == "claude-code-external"
    assert cfg.architect_mode.prompt_file == "system-prompt.md"


def test_local_30b_mode_via_env(monkeypatch):
    monkeypatch.setenv("AIFORGE_ARCHITECT_MODE", "local_30b")
    cfg = PaperclipConfig.load(REPO_ROOT)
    assert cfg.architect_mode.mode == "local_30b"
    assert cfg.architect_mode.model == "gemma-4-31b-it"
    assert cfg.architect_mode.prompt_file == "system-prompt.local-30b.md"


def test_invalid_mode_falls_back_to_cloud(monkeypatch):
    monkeypatch.setenv("AIFORGE_ARCHITECT_MODE", "nonsense")
    cfg = PaperclipConfig.load(REPO_ROOT)
    assert cfg.architect_mode.mode == "cloud"


def test_local_30b_prompt_file_exists():
    p = REPO_ROOT / "agents" / "architect" / "system-prompt.local-30b.md"
    assert p.exists()
