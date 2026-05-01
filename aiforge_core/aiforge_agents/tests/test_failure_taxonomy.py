"""Failure taxonomy — built-in + YAML extension."""
from __future__ import annotations

from pathlib import Path

from aiforge_core.aiforge_agents.runtime import failure_taxonomy as ft


def test_builtin_modes_loaded() -> None:
    modes = ft.load_all()
    ids = {m.id for m in modes}
    for i in range(1, 13):
        assert f"F-{i:03d}" in ids


def test_user_yaml_extension(tmp_path: Path, monkeypatch) -> None:
    yaml_path = tmp_path / "failure_taxonomy.yaml"
    yaml_path.write_text(
        "modes:\n"
        "  - id: F-013\n"
        "    name: SQL injection candidate\n"
        "    severity: halt\n"
        "    detector: regex\n"
        "    pattern: '(?i)select.*from.*where.*\\\\$\\\\{'\n"
        "    message: 'user input in SQL'\n"
    )
    monkeypatch.setenv("AIFORGE_FAILURE_TAXONOMY", str(yaml_path))
    modes = ft.load_all()
    f013 = [m for m in modes if m.id == "F-013"]
    assert len(f013) == 1
    assert f013[0].source == "user"
    assert f013[0].severity == "halt"


def test_regex_match_finds_user_mode(tmp_path: Path, monkeypatch) -> None:
    yaml_path = tmp_path / "ft.yaml"
    yaml_path.write_text(
        "modes:\n"
        "  - id: F-099\n"
        "    name: TODO_BOOM marker\n"
        "    detector: regex\n"
        "    pattern: TODO_BOOM\n"
    )
    monkeypatch.setenv("AIFORGE_FAILURE_TAXONOMY", str(yaml_path))
    hit = ft.match("here is a TODO_BOOM line")
    assert hit is not None
    assert hit.mode.id == "F-099"


def test_record_unknown_id_raises() -> None:
    import pytest
    with pytest.raises(KeyError):
        ft.record("F-999", "x")


def test_record_known_id_returns_match() -> None:
    h = ft.record("F-001", "import com.bogus.X;")
    assert h.mode.id == "F-001"
    assert "bogus" in h.evidence
