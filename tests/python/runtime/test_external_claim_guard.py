"""'I created the Confluence page' must be backed by a successful tool call."""
import pytest

from aiforge_core.runtime.chat_agent._turn import _external_claim as X


@pytest.mark.parametrize("text,done,missing", [
    ("I created the Confluence page for the design.", set(), ["confluence"]),
    ("The page has been published to the wiki.", set(), ["confluence"]),
    ("I've filed the Jira ticket ONE-12 for this.", set(), ["jira"]),
    ("I created the Confluence page.", {"confluence"}, []),
    ("I created the Confluence page and raised a Jira issue.", {"confluence"}, ["jira"]),
    ("I could not create the Confluence page.", set(), []),
    ("I have not created the page yet.", set(), []),
    ("Here is the draft you can paste into Confluence.", set(), []),
    ("I wrote notes.md with the plan.", set(), []),
])
def test_unbacked_claims(text, done, missing):
    assert X.unbacked_claims(text, done) == missing


def test_only_a_successful_write_tool_counts():
    class St:
        pass
    st = St()
    X.note_external(st, "confluence_create", {"ok": False, "error": "x"})
    X.note_external(st, "confluence_read", {"ok": True})
    assert not getattr(st, "external_ok", None)
    X.note_external(st, "confluence_create", {"ok": True, "id": "9"})
    assert st.external_ok == {"confluence"}


def test_a_final_that_claims_a_page_without_a_call_is_sent_back_then_disclaimed(tmp_path):
    from aiforge_core.runtime import chat_agent as ca
    claim = "FINAL: I created the Confluence page for the design in ENG."
    evs = list(ca.run_chat_agent([{"role": "user", "content": "create the page"}],
                                 cwd=str(tmp_path), complete_fn=lambda r, c: claim))
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and "Nothing was actually created" in msgs[-1]


def test_a_final_after_a_failed_create_is_not_trusted(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.chat_agent import _registry
    monkeypatch.setitem(_registry.TOOLS, "confluence_create",
                        lambda args, cwd: {"ok": False, "error": "HTTP 403"})
    state = {"n": 0}

    def fn(role, convo):
        state["n"] += 1
        if state["n"] == 1:
            return ('ACTION: confluence_create\nARGS_JSON: {"title": "T", "space": '
                    '"ENG", "body": "<p>x</p>"}')
        return "FINAL: I created the Confluence page T."

    evs = list(ca.run_chat_agent([{"role": "user", "content": "create a confluence page T in ENG"}],
                                 cwd=str(tmp_path), complete_fn=fn))
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and "Nothing was actually created" in msgs[-1]


def test_a_final_after_a_successful_create_is_left_alone(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.chat_agent import _registry
    monkeypatch.setitem(_registry.TOOLS, "confluence_create",
                        lambda args, cwd: {"ok": True, "id": "99", "verified": True,
                                           "status": "draft", "url": "https://c/x"})
    state = {"n": 0}

    def fn(role, convo):
        state["n"] += 1
        if state["n"] == 1:
            return ('ACTION: confluence_create\nARGS_JSON: {"title": "T", "space": '
                    '"ENG", "body": "<p>x</p>"}')
        return "FINAL: I created the Confluence page T as a draft: https://c/x"

    evs = list(ca.run_chat_agent([{"role": "user", "content": "create a confluence page T in ENG"}],
                                 cwd=str(tmp_path), complete_fn=fn))
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and "Nothing was actually created" not in msgs[-1] and "draft" in msgs[-1]
