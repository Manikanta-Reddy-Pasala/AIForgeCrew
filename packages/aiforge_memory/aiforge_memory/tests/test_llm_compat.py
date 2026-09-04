"""Unit tests for llm_compat.response_format().

Verifies the env-driven backend-compat shim returns the right
``response_format`` value for each mode + that all 4 LLM call sites
in the codebase use it (no stray ``json_object`` literals)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from aiforge_memory.llm_compat import response_format


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIFORGE_CODEMEM_RESPONSE_FORMAT", raising=False)


def test_default_returns_json_schema() -> None:
    rf = response_format()
    assert rf is not None
    assert rf["type"] == "json_schema"
    assert "json_schema" in rf
    assert rf["json_schema"]["schema"]["type"] == "object"
    assert rf["json_schema"]["strict"] is False


def test_json_object_legacy_mode(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CODEMEM_RESPONSE_FORMAT", "json_object")
    assert response_format() == {"type": "json_object"}


def test_text_mode(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CODEMEM_RESPONSE_FORMAT", "text")
    assert response_format() == {"type": "text"}


def test_none_mode_drops_field(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CODEMEM_RESPONSE_FORMAT", "none")
    assert response_format() is None


def test_unknown_mode_falls_through_to_default(monkeypatch) -> None:
    """An unrecognised value silently falls through to the safe
    default. Beats a hard-fail on operator typos."""
    monkeypatch.setenv("AIFORGE_CODEMEM_RESPONSE_FORMAT", "bogus")
    rf = response_format()
    assert rf is not None
    assert rf["type"] == "json_schema"


def test_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CODEMEM_RESPONSE_FORMAT", "JSON_OBJECT")
    assert response_format() == {"type": "json_object"}


# ─── Regression: no call site uses bare json_object literal ────────────


_PKG_ROOT = Path(__file__).parent.parent


def _call_sites() -> list[str]:
    """Every module that posts a chat completion, discovered.

    This used to be a hand-written tuple, and it listed
    ``query/translator.py`` — deleted with the Neo4j layer — so both guards
    below have failed on a missing file ever since. Worse in the other
    direction: a NEW call site added tomorrow would not have been guarded at
    all. Finding them is the same work the guard is for.
    """
    found = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        if "tests" in path.parts or path.name == "llm_compat.py":
            continue
        if "chat.completions.create" in path.read_text(encoding="utf-8"):
            found.append(str(path.relative_to(_PKG_ROOT)))
    return found


def test_the_call_site_list_is_not_empty():
    """A discovery bug that finds nothing would make both guards vacuous."""
    assert _call_sites()


@pytest.mark.parametrize("rel_path", _call_sites())
def test_no_bare_json_object_literal_in_call_sites(rel_path: str) -> None:
    """Regression guard: every LLM call site MUST go through
    llm_compat.response_format(), not a bare {"type": "json_object"}
    literal. Catches drift if someone reverts the patch."""
    text = (_PKG_ROOT / rel_path).read_text(encoding="utf-8")
    # The file may mention json_object in comments/docstrings; the bare
    # literal of the form `response_format={"type": "json_object"}` is
    # what we're guarding against.
    pattern = r'response_format\s*=\s*\{\s*"type"\s*:\s*"json_object"'
    assert re.search(pattern, text) is None, (
        f"{rel_path} still has a bare json_object literal — "
        "must use llm_compat.response_format()"
    )


@pytest.mark.parametrize("rel_path", _call_sites())
def test_call_sites_import_response_format(rel_path: str) -> None:
    """Each call site must import and call llm_compat.response_format."""
    text = (_PKG_ROOT / rel_path).read_text(encoding="utf-8")
    assert "llm_compat" in text, (
        f"{rel_path} must import from llm_compat"
    )
    assert "response_format" in text, (
        f"{rel_path} must import response_format from llm_compat"
    )
