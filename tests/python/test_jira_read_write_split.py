"""Jira READ/WRITE tool disambiguation.

Two reported failures came from the same cause — the model could not tell a
reader from a writer:

* "get my tickets" reached for ``jira_create`` instead of ``jira_search``;
* "show the comments on X" reached for ``jira_comment`` (the POSTER), because
  no comment READER existed at all.
"""
from __future__ import annotations

from aiforge_core.runtime.chat_agent import _registry
from aiforge_core.runtime.chat_agent._tools import _schemas
from aiforge_core.runtime.tools import tool_policy


def test_comment_reader_exists_and_is_registered():
    from aiforge_core.runtime.tools import jira
    assert callable(jira.jira_comments)
    assert "jira_comments" in _registry.TOOLS
    assert "jira_comments" in _schemas.CATALOG


def test_reader_never_prompts_writer_always_does():
    assert tool_policy.decide("jira_comments")["policy"] == tool_policy.ALLOW
    assert tool_policy.decide("jira_comment")["policy"] == tool_policy.ASK


def test_descriptions_state_read_or_write_up_front():
    # A small local model picks by description prefix; ambiguity there is what
    # routed "get tickets" into the issue creator.
    for name in ("jira_search", "jira_read", "jira_comments"):
        assert _schemas.CATALOG[name][0].startswith("READ"), name
    for name in ("jira_create", "jira_update", "jira_comment"):
        assert _schemas.CATALOG[name][0].startswith("WRITE"), name


def test_comment_writer_points_at_the_reader():
    assert "jira_comments" in _schemas.CATALOG["jira_comment"][0]


def test_every_integration_reader_and_writer_is_labelled():
    # The same ambiguity exists across Confluence and GitLab; a model that has
    # to guess read-vs-write from prose picks wrong under load. Keep the whole
    # integration surface labelled, not just the Jira tools that broke first.
    readers = ("confluence_search", "confluence_read", "confluence_comments",
               "gitlab_search", "gitlab_read")
    writers = ("confluence_create", "confluence_update", "confluence_comment",
               "gitlab_create", "gitlab_update", "gitlab_comment",
               "gitlab_mr_create", "gitlab_mr_comment")
    for name in readers:
        assert _schemas.CATALOG[name][0].startswith("READ"), name
    for name in writers:
        assert _schemas.CATALOG[name][0].startswith("WRITE"), name


def test_every_comment_writer_points_at_its_reader():
    for writer, reader in (("jira_comment", "jira_comments"),
                           ("confluence_comment", "confluence_comments")):
        assert reader in _schemas.CATALOG[writer][0]


def test_comments_requires_a_key():
    from aiforge_core.runtime.tools import jira
    r = jira.jira_comments({})
    assert r["ok"] is False
    assert "key" in r["error"]
