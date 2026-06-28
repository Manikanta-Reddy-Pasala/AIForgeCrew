"""L2 — service_extract: parse, override, hallucination drop."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_memory.features.service import extract as se

FIX = Path(__file__).parent / "fixtures"
MULTI_REPO = FIX / "multi_repo"


def _llm_ok() -> str:
    return (FIX / "llm_services_ok.json").read_text()


def test_parse_returns_two_services(tmp_path) -> None:
    # copy multi_repo to tmp so .aiforge/ writes don't pollute fixture
    repo = tmp_path / "repo"
    shutil.copytree(MULTI_REPO, repo)
    with patch.object(se, "_call_llm", return_value=_llm_ok()):
        services = se.extract_services(
            "fake pack", repo_path=repo, repo_name="multi",
        )
    names = {s.name for s in services}
    assert names == {"api", "worker"}
    api = next(s for s in services if s.name == "api")
    assert api.role == "api"
    assert api.port == 8080
    assert "api/main.py" in api.files
    assert api.source == "llm"


def test_hallucinated_files_dropped(tmp_path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(MULTI_REPO, repo)
    bad = json.dumps({
        "services": [{
            "name": "ghost",
            "description": "x",
            "role": "api",
            "tech_stack": [],
            "port": None,
            "files": ["does/not/exist.py", "api/main.py"],
        }]
    })
    with patch.object(se, "_call_llm", return_value=bad):
        services = se.extract_services(
            "fake pack", repo_path=repo, repo_name="multi",
        )
    assert services[0].files == ["api/main.py"]


def test_override_replaces_named_service(tmp_path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(MULTI_REPO, repo)
    (repo / ".aiforge").mkdir()
    shutil.copy(FIX / "services_override.yaml", repo / ".aiforge" / "services.yaml")

    with patch.object(se, "_call_llm", return_value=_llm_ok()):
        services = se.extract_services(
            "fake pack", repo_path=repo, repo_name="multi",
        )
    by_name = {s.name: s for s in services}
    assert by_name["api"].source == "manual"
    assert "Operator-edited" in by_name["api"].description
    # worker untouched
    assert by_name["worker"].source == "llm"


def test_invalid_json_twice_raises(tmp_path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(MULTI_REPO, repo)
    with patch.object(se, "_call_llm", side_effect=["bad1", "bad2"]):
        with pytest.raises(se.ServiceExtractError):
            se.extract_services("pack", repo_path=repo, repo_name="m")


def test_one_retry_then_succeed(tmp_path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(MULTI_REPO, repo)
    with patch.object(se, "_call_llm", side_effect=["bad", _llm_ok()]):
        services = se.extract_services("pack", repo_path=repo, repo_name="m")
    assert len(services) == 2


def test_override_file_glob_expands(tmp_path) -> None:
    """`file_glob` in services.yaml expands to actual files at merge time."""
    repo = tmp_path / "repo"
    shutil.copytree(MULTI_REPO, repo)
    (repo / ".aiforge").mkdir()
    (repo / ".aiforge" / "services.yaml").write_text(
        "services:\n"
        "  - name: api\n"
        "    description: glob-based\n"
        "    role: api\n"
        "    file_glob: api/**/*.py\n"
    )
    with patch.object(se, "_call_llm", return_value=_llm_ok()):
        services = se.extract_services("pack", repo_path=repo, repo_name="m")
    api = next(s for s in services if s.name == "api")
    assert api.source == "manual"
    # All 3 .py files under api/ should be picked up
    assert any("main.py" in f for f in api.files)
    assert any("routes.py" in f for f in api.files)
    assert any("__init__.py" in f for f in api.files)
