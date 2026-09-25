"""Repeated OKF pages, skills, workflows, and memory hits are not sent twice.

A repeat is skipped only when the same identity and the same body are already
in this turn. The first copy stays. A changed body, a different skill, and a
newer memory hit are sent whole. Ordinary source reads are never touched.

Compaction still fires when the turn reaches 80% of the window, and it keeps
the user's ask plus a tool result the model has not read yet.
"""
from __future__ import annotations

import json
import types

import pytest

from aiforge_core.runtime import context_seen as seen


_BODY = (
    "Follow these exact steps to deploy the hotfix, then run the smoke "
    "check before telling the user it is done."
)
_BODY_2 = (
    "A different playbook: cut the release branch, tag it, and open the "
    "merge request with the checklist filled in."
)
_MEM = (
    "Never run the full historical backfill when verifying a fix; "
    "prove it on about fifteen days of data."
)
_MEM_NEW = (
    "A newer decision: the trial balance is not complete just because "
    "debits equal credits; check the failure counts."
)


def _skill_block(name, body, desc="how"):
    return (f"APPLICABLE SKILLS — follow them.\n"
            f"### {name} — {desc}\n{body}")


def test_hash_tracks_the_body_not_the_surrounding_text():
    assert seen.content_hash(_BODY) == seen.content_hash("  " + _BODY + "\n")
    assert seen.content_hash(_BODY) == seen.content_hash(_BODY.replace("\n", "\r\n"))
    assert seen.content_hash(_BODY) != seen.content_hash(_BODY + " more")


def test_the_first_copy_of_a_skill_is_kept():
    block = _skill_block("deploy", _BODY)
    assert seen.shrink_block([], "skill", block) == block
    assert seen.shrink_block(
        [{"role": "user", "content": "deploy the service"}], "skill", block) == block


def test_a_repeated_skill_body_becomes_a_pointer_and_a_different_skill_stays():
    first = _skill_block("deploy", _BODY)
    again = _skill_block("deploy", _BODY) + "\n" + f"### release — ship\n{_BODY_2}"
    out = seen.shrink_block(
        [{"role": "system", "content": first}], "skill", again)
    assert _BODY not in out.split("### release", 1)[0]
    assert "already in context: skill deploy" in out
    assert seen.content_hash(_BODY) in out
    assert _BODY_2 in out


def test_a_changed_skill_body_is_sent_again():
    old = _skill_block("deploy", _BODY)
    changed = _BODY + " Step 4: roll back if the smoke check fails."
    out = seen.shrink_block(
        [{"role": "system", "content": old}], "skill",
        _skill_block("deploy", changed))
    assert changed in out
    assert "already in context" not in out


def test_a_shorter_page_is_not_treated_as_the_old_one():
    """Deleting a trailing line must still be sent. A prefix of the old body
    is not the same document."""
    old = _BODY + " Step 4: this line was removed."
    out = seen.shrink_block(
        [{"role": "system", "content": _skill_block("deploy", old)}],
        "skill", _skill_block("deploy", _BODY))
    assert _BODY in out
    assert "already in context" not in out


def test_a_different_skill_with_the_same_body_is_sent():
    out = seen.shrink_block(
        [{"role": "system", "content": _skill_block("deploy", _BODY)}],
        "skill", _skill_block("release", _BODY))
    assert _BODY in out
    assert "already in context" not in out


def test_an_okf_brief_already_in_the_seed_is_not_pasted_twice():
    knowledge = (
        "PROJECT facts that must survive compaction and still be here "
        "when the model plans the next edit.\n- keep the recent tail"
    )
    seed = ("--- MEMORY (prior facts / decisions / failures) ---\n" + knowledge)
    block = f"PROJECT MEMORY (aiforge):\n{knowledge}"
    out = seen.shrink_block([{"role": "user", "content": seed}], "okf", block)
    assert knowledge not in out
    assert "already in context: okf PROJECT MEMORY (aiforge)" in out
    assert seen.content_hash(knowledge) in out


def test_a_changed_okf_page_is_sent():
    old = "The page body that was injected at the start of the turn and is long enough."
    new = old + " Updated after the write."
    seed = "--- MEMORY (prior facts / decisions / failures) ---\n" + old
    out = seen.shrink_block(
        [{"role": "user", "content": seed}], "okf",
        f"PROJECT MEMORY (aiforge):\n{new}")
    assert new in out


def test_a_memory_hit_already_recalled_is_not_repeated_and_a_newer_one_is():
    recall = ("RELEVANT MEMORY:\n"
              f"- {_MEM}  (decision:15)\n")
    first = seen.dedupe_tool_result(
        [{"role": "system", "content": recall}], "memory_lookup", {},
        {"ok": True, "hits": [
            {"text": _MEM, "source": "decision:15"},
            {"text": _MEM_NEW, "source": "decision:16"},
        ]})
    assert first["hits"][0]["already_in_context"] is True
    assert _MEM not in first["hits"][0]["text"]
    assert seen.content_hash(_MEM) in first["hits"][0]["text"]
    assert first["hits"][1]["text"] == _MEM_NEW
    assert "already_in_context" not in first["hits"][1]


def test_a_short_memory_hit_is_never_dropped():
    short = "use yarn"
    out = seen.dedupe_tool_result(
        [{"role": "system", "content": f"- {short}  (pref)"}],
        "memory_lookup", {},
        {"ok": True, "hits": [{"text": short, "source": "pref"}]})
    assert out["hits"][0]["text"] == short


def test_a_second_lookup_of_the_same_hit_stays_a_pointer():
    first_obs = json.dumps({"ok": True, "hits": [
        {"text": _MEM, "source": "decision:15"}]})
    out = seen.dedupe_tool_result(
        [{"role": "user", "content": "OBSERVATION: " + first_obs}],
        "memory_lookup", {"query": "backfill"},
        {"ok": True, "hits": [{"text": _MEM, "source": "decision:15"}]})
    assert out["hits"][0]["already_in_context"] is True
    assert _MEM not in out["hits"][0]["text"]


def test_memory_write_does_not_echo_text_already_in_the_call():
    text = _MEM
    call = ('ACTION: memory_write\nARGS_JSON: '
            + json.dumps({"text": text}))
    out = seen.dedupe_tool_result(
        [{"role": "assistant", "content": call}], "memory_write", {"text": text},
        {"ok": True, "id": "m-1", "text": text, "label": "Observation_v2"})
    assert out["already_in_context"] is True
    assert text not in out["text"]
    assert out["id"] == "m-1"


def test_skill_search_sends_the_body_once_then_a_pointer(monkeypatch):
    skill = types.SimpleNamespace(name="deploy", body=_BODY, source="/skills/deploy/SKILL.md")
    monkeypatch.setattr("aiforge_core.runtime.skills.load", lambda cwd=None: [skill])
    first = seen.dedupe_tool_result(
        [], "skill_search", {"query": "deploy"},
        {"ok": True, "skills": [{"name": "deploy", "score": 3.0}]})
    assert first["skills"][0]["body"] == _BODY
    second = seen.dedupe_tool_result(
        [{"role": "system", "content": _skill_block("deploy", _BODY)}],
        "skill_search", {"query": "deploy"},
        {"ok": True, "skills": [{"name": "deploy", "score": 3.0}]})
    assert second["skills"][0]["already_in_context"] is True
    assert _BODY not in second["skills"][0]["body"]


def test_workflow_search_keeps_a_different_workflow(monkeypatch):
    wf = types.SimpleNamespace(name="release", body=_BODY_2, source="/wf/release/WORKFLOW.md")
    monkeypatch.setattr("aiforge_core.runtime.workflows.load", lambda cwd=None: [wf])
    out = seen.dedupe_tool_result(
        [{"role": "system", "content": _skill_block("deploy", _BODY)}],
        "workflow_search", {"query": "release"},
        {"ok": True, "workflows": [{"name": "release"}]})
    assert out["workflows"][0]["body"] == _BODY_2


def test_file_read_of_a_skill_already_in_the_prompt_is_a_pointer():
    fm = f"---\ntype: skill\nname: deploy\n---\n\n{_BODY}\n"
    out = seen.dedupe_tool_result(
        [{"role": "system", "content": _skill_block("deploy", _BODY)}],
        "file_read", {"path": "/repo/.aiforge/skills/deploy/SKILL.md"},
        {"ok": True, "content": fm})
    assert out["already_in_context"] is True
    assert _BODY not in out["content"]
    assert seen.content_hash(_BODY) in out["content"]


def test_file_read_of_a_changed_okf_page_is_sent_whole():
    old = "Original OKF page body that the model already has in this turn."
    new = old + " The page was edited."
    prior = ("ACTION: file_read\nARGS_JSON: "
             + json.dumps({"path": "/mem/okf/global/learnings/L-01.md"})
             + "\nOBSERVATION: " + json.dumps({"content": old}))
    page = f"---\nid: L-01\n---\n\n{new}\n"
    out = seen.dedupe_tool_result(
        [{"role": "user", "content": prior}], "file_read",
        {"path": "/mem/okf/global/learnings/L-01.md"},
        {"ok": True, "content": page})
    assert out["content"] == page


def test_file_read_of_the_same_okf_page_is_not_sent_twice():
    body = "Original OKF page body that the model already has in this turn."
    page = f"---\nid: L-01\n---\n\n{body}\n"
    prior = ("ACTION: file_write\nARGS_JSON: " + json.dumps({
        "path": "/mem/okf/global/learnings/L-01.md", "content": page}))
    out = seen.dedupe_tool_result(
        [{"role": "assistant", "content": prior}], "file_read",
        {"path": "/mem/okf/global/learnings/L-01.md"},
        {"ok": True, "content": page})
    assert out["already_in_context"] is True
    assert body not in out["content"]


def test_a_source_file_read_is_not_treated_as_a_playbook():
    src = "def deploy():\n    " + _BODY + "\n"
    result = {"ok": True, "content": src}
    out = seen.dedupe_tool_result(
        [{"role": "system", "content": _skill_block("deploy", _BODY)}],
        "file_read", {"path": "src/deploy.py"}, result)
    assert out is result


def test_read_files_drops_only_the_repeated_skill():
    skill = f"---\nname: deploy\n---\n\n{_BODY}\n"
    code = "x = 1\n"
    content = (f"=== .aiforge/skills/deploy/SKILL.md ===\n{skill}\n"
               f"=== src/app.py ===\n{code}")
    out = seen.dedupe_tool_result(
        [{"role": "system", "content": _skill_block("deploy", _BODY)}],
        "read_files", {"paths": ["SKILL.md", "src/app.py"]},
        {"ok": True, "content": content})
    assert _BODY not in out["content"].split("src/app.py", 1)[0]
    assert "already in context: skill deploy" in out["content"]
    assert "x = 1" in out["content"]


def test_the_ui_still_sees_the_raw_result_and_the_model_does_not(monkeypatch):
    from aiforge_core.runtime.chat_agent import _loop
    skill = types.SimpleNamespace(name="deploy", body=_BODY, source="SKILL.md")
    monkeypatch.setattr("aiforge_core.runtime.skills.load", lambda cwd=None: [skill])
    st = types.SimpleNamespace(
        convo=[{"role": "system", "content": _skill_block("deploy", _BODY)}],
        blocked_hits=0, reads_new=0, edits_made=0, builder_finalized=False)
    raw = {"ok": True, "skills": [{"name": "deploy", "score": 1}]}
    events = list(_loop._post_tool(
        st, "skill_search", {"query": "deploy"}, raw, "/repo", "sig", 1,
        None, types.SimpleNamespace(skills_md="")))
    assert events[0]["result"] == raw
    assert "body" not in events[0]["result"]["skills"][0]
    obs = st.convo[-1]["content"]
    assert "already in context: skill deploy" in obs
    assert _BODY not in obs


def test_compaction_fires_at_80_percent_and_keeps_the_ask_and_unread_tail(
        monkeypatch, tmp_path):
    """80% of the window, not the overflow edge. The user's ask stays in the
    breadcrumb and the unread tail stays verbatim."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.setenv("AIFORGE_LLM_MAX_TOKENS", "8192")
    from aiforge_core.runtime.chat_agent._context import _compaction as comp
    from aiforge_core.runtime.chat_agent._context import _window as w
    monkeypatch.setattr(w, "_window_tokens", lambda role=None: 100_000)
    sys = "S" * 2000
    budget = w._ctx_budget_chars("chat", sys_chars=len(sys))
    win_chars = 100_000 * 4
    # The trigger is 80% of the window. The reply's room is still inside the
    # window, so this is earlier than overflow.
    assert budget + len(sys) == int(win_chars * 0.80)
    assert budget + len(sys) < win_chars - 8192 * 4

    under = [{"role": "system", "content": sys},
             {"role": "user", "content": "u" * (budget // 2)},
             {"role": "assistant", "content": "ok"}]
    assert comp._compact_convo(under, role="chat") is under

    convo = [{"role": "system", "content": sys},
             {"role": "user", "content": "please add the export"}]
    # Pad past 80% with tool traffic, then an unread result the model has
    # not consumed yet.
    while sum(len(m["content"]) for m in convo[1:]) <= budget:
        convo.append({"role": "assistant", "content": "ACTION: file_read\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 4000})
    # The size-based tail keeps only the newest few turns. The unread result
    # sits just outside that tail, so it survives only because keep_min asks
    # for it. Without keep_min it is condensed away.
    unread = "OBSERVATION: unread tool result the model has not used"
    convo.append({"role": "user", "content": unread})
    for i in range(3):
        convo.append({"role": "assistant", "content": f"ACTION: grep\n{i}"})
        convo.append({"role": "user", "content": "OBSERVATION: later filler"})
    kept = comp._compact_convo(convo, role="chat", keep_recent=4, keep_min=8)
    assert "auto-condensed" in kept[0]["content"]
    assert "please add the export" in kept[0]["content"]
    assert any(m.get("content") == unread for m in kept)
    dropped = comp._compact_convo(convo, role="chat", keep_recent=4, keep_min=0)
    assert all(m.get("content") != unread for m in dropped)


def test_compaction_restores_a_pointer_whose_body_was_dropped(monkeypatch, tmp_path):
    """The system prompt may point at a brief that lives in the history.
    Once that history is condensed, the brief has to come back — a pointer
    with nothing above it is a lost page."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.setenv("AIFORGE_LLM_MAX_TOKENS", "8192")
    monkeypatch.setenv("AIFORGE_COMPACT_MODE", "heuristic")
    from aiforge_core.runtime.chat_agent._context import _compaction as comp
    from aiforge_core.runtime.chat_agent._context import _window as w
    monkeypatch.setattr(w, "_window_tokens", lambda role=None: 100_000)
    brief = (
        _BODY + "\n- keep the recent tail so compaction cannot drop this page"
    )
    sys = "CORE RULES\n" + seen.pointer("okf", "PROJECT MEMORY (repo)", brief)
    convo = [{"role": "system", "content": sys},
             {"role": "user", "content": "please add the export"},
             {"role": "assistant", "content": "--- MEMORY (prior facts / decisions / failures) ---\n" + brief}]
    budget = w._ctx_budget_chars("chat", sys_chars=len(sys))
    while sum(len(m["content"]) for m in convo[1:]) <= budget:
        convo.append({"role": "assistant", "content": "ACTION: file_read\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 4000})
    out = comp._compact_convo(convo, role="chat", keep_recent=4)
    assert brief in out[0]["content"]
    assert "already in context:" not in out[0]["content"]


def test_a_dangling_pointer_is_expanded_once_and_only_from_this_turn():
    """Two pointers to one dropped page become one body. A reset bag cannot
    revive another turn's text: the body has to be in the dropped middle."""
    body = _BODY
    p = seen.pointer("skill", "deploy", body)
    seen.reset_seen_bodies()
    kept = [
        {"role": "system", "content": "CORE\n" + p},
        {"role": "user", "content": "OBSERVATION: " + p},
    ]
    dropped = [{"role": "assistant", "content": _skill_block("deploy", body)}]
    out = seen.restore_dangling(kept, dropped)
    assert body in out[0]["content"]
    assert "already in context:" not in out[0]["content"]
    assert "already in context: skill deploy" in out[1]["content"]
    assert body not in out[1]["content"]
    seen.reset_seen_bodies()
    assert seen.restore_dangling(kept, []) is kept
