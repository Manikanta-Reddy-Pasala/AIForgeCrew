"""Per-tool memory learnings: a ``tool:<name>``-tagged learning resurfaces for
that tool on a same-type request, so the agent stops re-deriving (e.g. a
working JQL) every time. Covers the recall-side score boost + the chat
keyword→tag derivation.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture()
def cfg_dir(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    return None


def test_tool_tagged_learning_ranks_first_with_boost(cfg_dir):
    from aiforge_core.memory import sqlite_memory as m

    m.write_unit(kind="note", source="test", title="",
                 text="Search issues via the API endpoint returns results",
                 tags=[], repo="r")
    m.write_unit(kind="learning", source="test", title="",
                 text="Working Jira JQL for open bugs: project=ENG AND status=Open",
                 tags=["tool:jira"], repo="r")

    q = "how do I search jira issues"
    base = m.recall(q, repo="r")
    boosted = m.recall(q, repo="r", boost_tags=["tool:jira"])

    # Without boost the generic unit wins on semantics; with boost the
    # tool-scoped learning is pulled to the top.
    assert base and "API endpoint" in base[0]["text"]
    assert boosted and "Working Jira JQL" in boosted[0]["text"]


def test_tool_tags_derivation():
    from aiforge_core.runtime.chat_agent import _tool_tags

    assert _tool_tags("find my open jira issues with jql") == ["tool:jira"]
    assert _tool_tags("update the confluence space page") == ["tool:confluence"]
    assert "tool:git" in _tool_tags("rebase the branch and open a pull request")
    assert _tool_tags("what is 2 + 2") == []
