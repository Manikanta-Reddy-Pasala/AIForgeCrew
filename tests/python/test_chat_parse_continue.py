"""A THOUGHT with no ACTION must not end the run (truncated/forgot-protocol)."""
from aiforge_core.runtime.chat_agent import _parse


def test_thought_without_action_is_continue():
    out = ("THOUGHT: Now I need to create the job script. Let me first check "
           "the jira_integration skill, then create a comprehensive script that ")
    assert _parse(out)["kind"] == "continue"


def test_final_marker_still_final():
    assert _parse("FINAL: done")["kind"] == "final"


def test_action_still_action():
    out = 'THOUGHT: do it\nACTION: create_job_script\nARGS_JSON: {"name": "x"}'
    assert _parse(out)["kind"] == "action"


def test_plain_prose_without_thought_is_final():
    # No THOUGHT marker → a bare answer is still a final (backward-compatible).
    assert _parse("Here is the summary you asked for.")["kind"] == "final"
