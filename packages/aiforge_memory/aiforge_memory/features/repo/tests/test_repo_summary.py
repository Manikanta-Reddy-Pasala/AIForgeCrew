"""L1 — Stage 2: pack text → strict-JSON RepoSummary via LLM."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_memory.features.repo import extract as rs

FIX = Path(__file__).parent / "fixtures"


def _load_pack() -> str:
    return (FIX / "tiny_pack.md").read_text()


def _load_llm_ok() -> str:
    return (FIX / "llm_response_ok.json").read_text()


def test_summary_parses_clean_json() -> None:
    pack = _load_pack()
    with patch.object(rs, "_call_llm", return_value=_load_llm_ok()):
        summary = rs.summarize(pack, repo_name="tiny_repo")
    assert summary.lang_primary == "python"
    assert summary.build_cmd == "make build"
    assert summary.test_cmd == "make test"
    assert summary.run_cmd == "make run"
    assert summary.portforward_cmds == [
        "kubectl port-forward svc/tiny-repo 8080:8080 -n default"
    ]
    assert "Tiny Repo Runbook" in summary.runbook_md
    assert len(summary.runbook_md) >= 200  # tiny fixture; real repos hit 500+


def test_summary_strips_markdown_fences() -> None:
    pack = _load_pack()
    fenced = "```json\n" + _load_llm_ok() + "\n```"
    with patch.object(rs, "_call_llm", return_value=fenced):
        summary = rs.summarize(pack, repo_name="tiny_repo")
    assert summary.lang_primary == "python"


def test_summary_retries_on_invalid_json_then_succeeds() -> None:
    pack = _load_pack()
    bad = "not json at all"
    good = _load_llm_ok()
    with patch.object(rs, "_call_llm", side_effect=[bad, good]):
        summary = rs.summarize(pack, repo_name="tiny_repo")
    assert summary.build_cmd == "make build"


def test_summary_raises_after_two_invalid_responses() -> None:
    pack = _load_pack()
    with patch.object(rs, "_call_llm", side_effect=["bad1", "bad2"]):
        with pytest.raises(rs.RepoSummaryError):
            rs.summarize(pack, repo_name="tiny_repo")


def test_pack_truncated_at_max_input_chars() -> None:
    big = "x" * 1_000_000
    with patch.object(rs, "_call_llm") as mock_call:
        mock_call.return_value = _load_llm_ok()
        rs.summarize(big, repo_name="huge_repo", max_input_chars=200_000)
    sent_pack = mock_call.call_args.args[0]
    # Truncation marker present, total length capped
    assert len(sent_pack) <= 200_500
    assert "[TRUNCATED" in sent_pack
