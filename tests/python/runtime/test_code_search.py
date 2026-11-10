"""code_search ranks files by embed+keyword without a codegraph binary."""
from __future__ import annotations

from aiforge_core.runtime.tools import code_search as CS
from aiforge_core.runtime.chat_agent._registry import TOOLS
from aiforge_core.runtime.chat_agent._tools._schemas import CATALOG, _CORE_READ


def test_registered_and_readonly():
    assert "code_search" in TOOLS
    assert "code_search" in CATALOG
    assert "code_search" in _CORE_READ


def test_ranks_matching_file_above_noise(tmp_path):
    (tmp_path / "retry.py").write_text(
        "def exponential_backoff(attempt):\n"
        "    return 2 ** attempt\n"
        "def retry_with_jitter(fn):\n"
        "    pass\n", encoding="utf-8")
    (tmp_path / "ui.py").write_text(
        "def render_button(label):\n"
        "    return f'<button>{label}</button>'\n", encoding="utf-8")
    out = CS.search("exponential backoff retry", str(tmp_path), top_k=3)
    assert out["ok"] and out["hits"]
    assert out["hits"][0]["path"].endswith("retry.py")


def test_missing_query_refused(tmp_path):
    assert not CS.code_search({}, str(tmp_path))["ok"]
