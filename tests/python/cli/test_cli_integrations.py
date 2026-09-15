"""Jira / Confluence settings — and never a secret on screen."""

from __future__ import annotations

import pytest
from aiforge_cli import integrations as integ


def test_a_token_is_reported_as_a_state_not_a_value():
    rows = dict(integ.summary("jira", {"base_url": "https://jira.internal",
                                       "user": "m", "has_token": True,
                                       "token": "s3cret"}))
    assert rows["has_token"] == "configured"
    assert "s3cret" not in "".join(rows.values())
    assert "token" not in rows                      # never echoed, even if returned


def test_an_unset_token_says_so():
    assert dict(integ.summary("jira", {"has_token": False}))["has_token"] == "not set"


def test_assignments_are_typed():
    patch = integ.parse_assignments(["base_url=https://x", "insecure_tls=true", "port=8080"])
    assert patch == {"base_url": "https://x", "insecure_tls": True, "port": 8080}


def test_an_empty_secret_is_refused_rather_than_sent():
    # The API reads an empty token as "keep the current one", so accepting this
    # would report a wipe that never happened.
    with pytest.raises(ValueError) as exc:
        integ.parse_assignments(["token="])
    assert "keep the stored one" in str(exc.value)


def test_a_bare_word_is_not_an_assignment():
    with pytest.raises(ValueError):
        integ.parse_assignments(["jira"])


def test_a_hyperlink_is_only_emitted_when_asked():
    assert integ.link("ONE-320", "https://j/ONE-320", enabled=False) == "ONE-320"
    assert "\033]8;;https://j/ONE-320" in integ.link("ONE-320", "https://j/ONE-320")
