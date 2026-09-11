import json

import pytest

from aiforge_core.runtime import chat_agent as ca


def _scripted(outputs):
    """Return a complete_fn that yields the given outputs in order."""
    seq = list(outputs)

    def _fn(role, messages, **kw):
        return seq.pop(0)
    return _fn


def _collect(gen):
    return list(gen)


def test_no_markers_treated_as_final(tmp_path):
    fn = _scripted(["just a plain answer with no protocol markers"])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=fn))
    assert [e for e in evs if e["type"] == "message"][0]["text"].startswith("just a plain")


def test_parse_bare_args_json_null_does_not_surface():
    # A lone leaked marker is garbage, not an answer → nudge to continue.
    assert ca._parse("ARGS_JSON: null")["kind"] == "continue"


def test_parse_final_with_only_scaffolding_after_it_continues():
    # `FINAL:` present but only `ARGS_JSON: null` after it → don't answer garbage.
    assert ca._parse("FINAL:\nARGS_JSON: null")["kind"] == "continue"


def test_parse_strips_trailing_args_json_null_from_real_answer():
    p = ca._parse("Here is the answer.\nARGS_JSON: null")
    assert p["kind"] == "final"
    assert "ARGS_JSON" not in p["text"]
    assert p["text"] == "Here is the answer."


def test_parse_keeps_code_fences_and_real_action_values():
    # The stripper must NOT eat code fences or a keyword line with real content.
    txt = "Here:\n```python\naction = 1\n```"
    p = ca._parse("FINAL: " + txt)
    assert p["kind"] == "final"
    assert "```python" in p["text"]
    assert "action = 1" in p["text"]


def test_parse_inline_args_rescue():
    """dspy A/B finding: `ACTION: tool {"item": "x"}` + empty `ARGS_JSON: {}`
    — the empty marker slot must not shadow the good inline object."""
    step = ca._parse('ACTION: lookup_price {"item": "coffee"}\nARGS_JSON: {}')
    assert step["kind"] == "action"
    assert step["tool"] == "lookup_price"
    assert step["args"] == {"item": "coffee"}
    # normal shape unaffected
    step = ca._parse('ACTION: lookup_price\nARGS_JSON: {"item": "tea"}')
    assert step["args"] == {"item": "tea"}
    # genuinely empty args stay empty
    step = ca._parse("ACTION: list_services\nARGS_JSON: {}")
    assert step["args"] == {}


# ── prose is not a tool call ───────────────────────────────────────────────
# Native mode hands a plain-text reply to _parse; the case-insensitive,
# unanchored ACTION regex turned "Recommended action: the …" into a call to a
# tool named `the` ("unknown tool: the" in the chat).

@pytest.mark.parametrize("prose", [
    "The file lists the steps. Recommended action: the config must be validated first.",
    "Next action:\nthe schema needs a $id field.",
    "- Action: the team reviews it on Monday",
])
def test_prose_mentioning_action_is_not_dispatched(prose):
    step = ca._parse(prose)
    assert step["kind"] != "action", step


def test_protocol_action_line_still_dispatches():
    step = ca._parse('THOUGHT: read it\nACTION: file_read\nARGS_JSON: {"path": "a.py"}')
    assert step["kind"] == "action"
    assert step["tool"] == "file_read"
    assert step["args"] == {"path": "a.py"}


def test_inline_registered_tool_still_dispatches():
    step = ca._parse('I will read it. ACTION: file_read ARGS_JSON: {"path": "a.py"}')
    assert step["kind"] == "action"
    assert step["tool"] == "file_read"


def test_a_deliberate_call_to_a_missing_tool_still_dispatches():
    """So the model is told the tool does not exist (e.g. web_search)."""
    step = ca._parse('ACTION: web_search\nARGS_JSON: {"q": "x"}')
    assert step["kind"] == "action"
    assert step["tool"] == "web_search"


def test_tool_names_with_digits_are_not_cut():
    assert ca._ACTION_RE.search("ACTION: s3_get").group(1) == "s3_get"
