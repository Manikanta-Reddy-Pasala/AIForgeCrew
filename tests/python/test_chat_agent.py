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


def test_final_immediately(tmp_path):
    fn = _scripted(["THOUGHT: easy\nFINAL: hello there"])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "hi"}], cwd=str(tmp_path), complete_fn=fn))
    assert evs[-1] == {"type": "done"}
    msg = [e for e in evs if e["type"] == "message"][0]
    assert msg["text"] == "hello there"


def test_file_write_then_final(tmp_path):
    fn = _scripted([
        'THOUGHT: write it\nACTION: file_write\nARGS_JSON: {"path": "a.txt", "content": "hi"}',
        "THOUGHT: done\nFINAL: wrote the file",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "make a.txt"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["name"] == "file_write"
    assert tool["result"]["ok"] is True
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert [e for e in evs if e["type"] == "message"][0]["text"] == "wrote the file"


def test_file_read_roundtrip(tmp_path):
    (tmp_path / "b.txt").write_text("payload-xyz")
    fn = _scripted([
        'ACTION: file_read\nARGS_JSON: {"path": "b.txt"}',
        "FINAL: read it",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "read b"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["content"] == "payload-xyz"


def test_run_command(tmp_path):
    fn = _scripted([
        'ACTION: run_command\nARGS_JSON: {"cmd": "echo hello-cmd"}',
        "FINAL: ran",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "echo"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["ok"] is True
    assert "hello-cmd" in tool["result"]["stdout"]


def test_unknown_tool_reports_error(tmp_path):
    fn = _scripted([
        "ACTION: teleport\nARGS_JSON: {}",
        "FINAL: gave up",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["ok"] is False
    assert "unknown tool" in tool["result"]["error"]


def test_loop_detection_same_action(tmp_path):
    # Always the SAME action — instead of circling, the agent ASKS the user
    # (message with awaiting_input) and stops.
    def _fn(role, messages, **kw):
        return 'ACTION: list_dir\nARGS_JSON: {"path": "."}'
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "loop"}], cwd=str(tmp_path),
        complete_fn=_fn))
    asks = [e for e in evs if e["type"] == "message" and e.get("awaiting_input")]
    assert asks and ("clarify" in asks[0]["text"] or "proceed" in asks[0]["text"])
    assert len([e for e in evs if e["type"] == "tool"]) < ca._LOOP_REPEAT
    assert evs[-1] == {"type": "done"}


def test_agent_can_ask_a_question(tmp_path):
    def _fn(role, messages, **kw):
        return "THOUGHT: need detail\nASK: Which port should I use?"
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "run it"}], cwd=str(tmp_path),
        complete_fn=_fn))
    msg = [e for e in evs if e["type"] == "message"]
    assert msg and msg[0].get("awaiting_input") is True
    assert "Which port" in msg[0]["text"]


def test_progressing_actions_not_killed(tmp_path):
    # Different actions each step → NOT a loop; runs until FINAL.
    seq = [f'ACTION: list_dir\nARGS_JSON: {{"path": "{i}"}}' for i in range(6)]
    seq.append("FINAL: done")
    fn = _scripted(seq)
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "work"}], cwd=str(tmp_path), complete_fn=fn))
    # no loop error; finished normally
    assert not [e for e in evs if e["type"] == "error"]
    assert [e for e in evs if e["type"] == "message"][0]["text"] == "done"


def test_llm_error_is_soft(tmp_path):
    def _fn(role, messages, **kw):
        raise RuntimeError("boom")
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=_fn))
    assert any(e["type"] == "error" for e in evs)
    assert evs[-1] == {"type": "done"}


def test_no_markers_treated_as_final(tmp_path):
    fn = _scripted(["just a plain answer with no protocol markers"])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=fn))
    assert [e for e in evs if e["type"] == "message"][0]["text"].startswith("just a plain")


def test_workspace_clamp(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", str(tmp_path))
    fn = _scripted([
        'ACTION: file_read\nARGS_JSON: {"path": "/etc/hosts"}',
        "FINAL: blocked",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "read outside"}], cwd=str(tmp_path),
        complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["ok"] is False
    assert "escapes" in tool["result"]["error"]


def test_memory_write_tool(tmp_path, monkeypatch):
    # embedded memory → writes land in sqlite_memory
    import importlib
    for k in ("AIFORGE_MEMORY_BACKEND", "NEO4J_URI", "AIFORGE_NEO4J_URI",
              "AIFORGE_PG_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    import aiforge_core.memory.backend_select as bs; importlib.reload(bs)
    import aiforge_core.memory.sqlite_memory as sm; importlib.reload(sm)
    fn = _scripted([
        'ACTION: memory_write\nARGS_JSON: {"text": "staging needs the VPN on first", "kind": "gotcha"}',
        "FINAL: saved",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "remember this"}], cwd=str(tmp_path / "myrepo"),
        complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["name"] == "memory_write"
    assert tool["result"]["ok"] is True
    assert sm.stats()["total"] == 1


def test_fenced_args_write_persists(tmp_path):
    # model wraps args in a ```json fence — must still extract + write
    fn = _scripted([
        'ACTION: file_write\nARGS_JSON:\n```json\n{"path": "out.txt", "content": "BANANA"}\n```',
        "FINAL: wrote it",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "write it"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["ok"] is True
    assert (tmp_path / "out.txt").read_text() == "BANANA"


def test_repo_map_in_system_prompt_each_turn(tmp_path):
    """The agent must see the dir structure every turn (no re-searching)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.java").write_text("class App {}")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x")
    from aiforge_core.runtime import chat_agent

    seen = {}

    def fake_complete(role, convo):
        seen["system"] = convo[0]["content"]
        return "FINAL: done"

    list(chat_agent.run_chat_agent([{"role": "user", "content": "hi"}],
                                   cwd=str(tmp_path), complete_fn=fake_complete))
    sysmsg = seen["system"]
    assert "REPO MAP" in sysmsg
    assert "App.java" in sysmsg          # structure present
    assert "node_modules" not in sysmsg  # junk skipped


def test_repo_context_starter_then_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import importlib

    from aiforge_core.memory import md_store
    importlib.reload(md_store)
    from aiforge_core.runtime import chat_agent
    (tmp_path / "pom.xml").write_text("x")
    (tmp_path / "README.md").write_text("# App\nOrders service.")
    # first time → auto starter from stack + README
    starter = chat_agent._repo_context(str(tmp_path))
    assert "PROJECT SUMMARY" in starter and "maven" in starter and "Orders service" in starter
    # after a session writes the per-repo summary → it's injected next time
    repo = chat_agent._repo_name(str(tmp_path))
    md_store.upsert_section(source=f"repo:{repo}", title=f"{repo} memory",
                            section_title="t", section_body="Added OrderController.")
    ctx = chat_agent._repo_context(str(tmp_path))
    assert "Added OrderController" in ctx


def test_rule_book_persists_and_injects(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import importlib

    from aiforge_core.memory import md_store
    importlib.reload(md_store)
    from aiforge_core.runtime import chat_agent
    d = str(tmp_path)
    assert chat_agent._t_remember_rule({"text": "always use yarn", "scope": "global"}, d)["ok"]
    assert chat_agent._t_remember_rule({"text": "controllers in src/api", "scope": "repo"}, d)["ok"]
    chat_agent._t_remember_rule({"text": "always use yarn", "scope": "global"}, d)  # dedup
    ctx = chat_agent._rules_context(d)
    assert "RULES" in ctx
    assert ctx.count("always use yarn") == 1          # deduped
    assert "controllers in src/api" in ctx            # repo rule present


def test_session_start_directive_in_prompt(tmp_path):
    """Fresh session must instruct the agent to recall + ask up-front."""
    from aiforge_core.runtime import chat_agent
    seen = {}

    def fake(role, convo):
        seen["sys"] = convo[0]["content"]
        return "FINAL: ok"

    list(chat_agent.run_chat_agent([{"role": "user", "content": "hi"}],
                                   cwd=str(tmp_path), complete_fn=fake))
    assert "SESSION START" in seen["sys"]
    assert "ASK" in seen["sys"]


def test_memory_recalled_at_session_start(tmp_path, monkeypatch):
    """On init (no assistant turn yet) the agent proactively recalls memory
    keyed to the opening request and injects it into the system prompt."""
    from aiforge_core.memory import unified_query as uq
    from aiforge_core.runtime import chat_agent

    calls = {"n": 0, "q": None}

    def fake_query(query, limit=6, **kw):
        calls["n"] += 1
        calls["q"] = query
        return {"hits": [{"text": "Orders service uses Flyway, not Hibernate ddl",
                          "source": "repo:myrepo"}]}

    monkeypatch.setattr(uq, "query", fake_query)
    seen = {}

    def fake(role, convo):
        seen["sys"] = convo[0]["content"]
        return "FINAL: ok"

    list(chat_agent.run_chat_agent(
        [{"role": "user", "content": "fix the orders migration"}],
        cwd=str(tmp_path), complete_fn=fake))
    assert calls["n"] == 1
    assert "orders migration" in (calls["q"] or "")
    assert "RELEVANT MEMORY" in seen["sys"]
    assert "Flyway" in seen["sys"]


def test_memory_recall_skipped_when_not_init(tmp_path, monkeypatch):
    """A follow-up turn (history already has an assistant reply) must NOT
    re-run the session-start recall."""
    from aiforge_core.memory import unified_query as uq
    from aiforge_core.runtime import chat_agent

    def boom(*a, **k):
        raise AssertionError("recall must not run on follow-up turns")

    monkeypatch.setattr(uq, "query", boom)

    def fake(role, convo):
        return "FINAL: ok"

    # prior assistant turn present → not an init turn
    list(chat_agent.run_chat_agent(
        [{"role": "user", "content": "do x"},
         {"role": "assistant", "content": "did x"},
         {"role": "user", "content": "now y"}],
        cwd=str(tmp_path), complete_fn=fake))


def test_rule_book_injected_into_system_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import importlib

    from aiforge_core.memory import md_store
    importlib.reload(md_store)
    from aiforge_core.runtime import chat_agent
    chat_agent._t_remember_rule({"text": "NEVER delete prod data", "scope": "global"}, str(tmp_path))
    seen = {}

    def fake(role, convo):
        seen["sys"] = convo[0]["content"]
        return "FINAL: ok"

    list(chat_agent.run_chat_agent([{"role": "user", "content": "hi"}],
                                   cwd=str(tmp_path), complete_fn=fake))
    assert "NEVER delete prod data" in seen["sys"]
    assert seen["sys"].index("RULES") < seen["sys"].index("You are AIForge")  # rules first


def test_diff_preview_is_markdown_not_json_string():
    from aiforge_core.runtime import chat_agent as ca
    # integration write → readable markdown (heading + fields), not a JSON blob
    p = ca._diff_preview("jira_create",
                         {"project": "ENG", "summary": "Fix", "description": "## D"},
                         "/tmp")
    assert p.startswith("### Create Jira issue")
    assert "**Project:**" in p and "## D" in p
    assert not p.lstrip().startswith("{")        # NOT a raw json dump
    # command / diff / unknown → fenced code so the renderer shows monospace
    assert "```bash" in ca._diff_preview("run_command", {"cmd": "ls"}, "/tmp")
    assert "```json" in ca._diff_preview("weird_tool", {"a": 1}, "/tmp")
    gl = ca._diff_preview("gitlab_comment",
                          {"project": "g/p", "iid": 5, "body": "looks good"}, "/tmp")
    assert gl.startswith("### Comment on GitLab") and "looks good" in gl


def test_xhtml_to_md_readable():
    from aiforge_core.runtime import chat_agent as ca
    out = ca._xhtml_to_md("<h2>Plan</h2><p>do <strong>x</strong> "
                          "<a href=\"http://x\">link</a></p><ul><li>a</li></ul>")
    assert "## Plan" in out and "**x**" in out
    assert "[link](http://x)" in out and "- a" in out
    assert "<" not in out          # no raw tags left


def test_confluence_create_preview_is_readable_not_xml_fence():
    from aiforge_core.runtime import chat_agent as ca
    p = ca._diff_preview("confluence_create",
                         {"space": "ENG", "title": "Doc", "body": "<h2>H</h2><p>t</p>"},
                         "/tmp")
    assert "## H" in p and "```xml" not in p      # rendered, not raw XHTML


def test_update_previews_show_a_diff(monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    import aiforge_core.runtime.tools.jira as jira
    monkeypatch.setattr(jira, "jira_read",
                        lambda a, c=None: {"ok": True, "summary": "old",
                                           "description": "old body"})
    p = ca._diff_preview("jira_update",
                         {"key": "ENG-1", "description": "new body"}, "/tmp")
    assert "```diff" in p and "-old body" in p and "+new body" in p


def test_compact_convo_condenses_long_history(monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "2000")
    convo = [{"role": "system", "content": "S" * 100}]
    for i in range(30):
        convo.append({"role": "assistant",
                      "content": "THOUGHT: t\nACTION: file_read\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 200})
    out = ca._compact_convo(convo, keep_recent=8)
    assert out[0]["role"] == "system"                      # system preserved
    # breadcrumb folded INTO the system message (no separate user turn → no
    # consecutive same-role messages); actions summarized.
    assert "auto-condensed" in out[0]["content"]
    assert "file_read" in out[0]["content"]
    assert len(out) == 1 + 8                               # system + recent tail
    # no two consecutive non-system same-role messages
    roles = [m["role"] for m in out]
    assert not any(roles[i] == roles[i+1] != "system" for i in range(len(roles)-1))
    assert roles[-1] == "user"                             # model continues
    assert sum(len(m["content"]) for m in out) < \
        sum(len(m["content"]) for m in convo)


def test_compact_convo_noop_under_budget(monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "48000")
    convo = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert ca._compact_convo(convo) is convo               # untouched
