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


def test_tool_start_precedes_tool_with_matching_call_id(tmp_path):
    """A slow tool used to show NOTHING until it finished — `tool_start`
    fires first (same name/args) so the UI can show 'running…' immediately,
    and its call_id matches the completed `tool` event so the UI can flip
    the same row in place instead of showing two rows."""
    fn = _scripted([
        'ACTION: file_write\nARGS_JSON: {"path": "a.txt", "content": "hi"}',
        "FINAL: wrote the file",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "make a.txt"}], cwd=str(tmp_path), complete_fn=fn))
    start = [e for e in evs if e["type"] == "tool_start"][0]
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert start["name"] == tool["name"] == "file_write"
    assert start["args"] == tool["args"]
    assert start["call_id"] == tool["call_id"]
    assert evs.index(start) < evs.index(tool)


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


def test_llm_error_is_soft(tmp_path, monkeypatch):
    # Keep the retry backoff from actually sleeping (default is now 3 retries
    # with escalating 3s/6s/9s waits) so the test stays fast.
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "1")
    monkeypatch.setattr(ca.time, "sleep", lambda *_a, **_k: None)

    def _fn(role, messages, **kw):
        raise RuntimeError("boom")
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=_fn))
    # A transient LLM failure is handled SOFTLY: a plain ⚠️ message (never a raw
    # "error" / llm.exhausted stack), then a clean done — nothing was changed.
    assert any(e["type"] == "message" and "didn't respond" in e.get("text", "")
               for e in evs)
    assert not any(e["type"] == "error" for e in evs)
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
    # L-3: keep_recent is scaled DOWN to the (tiny 2000-char) budget — 8 turns
    # of 200+ chars each wouldn't fit — so the tail is the adaptive 4, not 8.
    assert len(out) == 1 + 4                               # system + adaptive tail
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


def test_compact_convo_sentinel_strip_is_exact(monkeypatch):
    # A condense block is stripped by unique sentinel, so a legit look-alike
    # phrase elsewhere in the system message is never eaten, and the block
    # can't accumulate across repeated condenses.
    from aiforge_core.runtime import chat_agent as ca
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "800")
    sysc = ("KEEP_ME. mentions '[earlier conversation auto-condensed ... "
            "this point.]' literally. KEEP_END.")
    convo = [{"role": "system", "content": sysc}]
    for i in range(20):
        convo.append({"role": "assistant", "content": "ACTION: grep\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBS " + "z" * 100})
    out = ca._compact_convo(convo)
    out = ca._compact_convo(out)          # re-condense
    out = ca._compact_convo(out)
    s = out[0]["content"]
    assert "KEEP_ME" in s and "KEEP_END" in s          # legit text preserved
    assert s.count(ca._CONDENSE_OPEN) == 1             # exactly one block


# ── commit hygiene — REFUSE blanket git stages (no rewrite/baseline) ──────────


def test_is_blanket_git_refuses_blanket_forms():
    refuse = [
        "git add -A", "git add .", "git add --all", "git add -A .",
        "git add -- .", 'git commit -am "x"', "git commit -a",
        'git commit --all -m "x"', "git commit -a -m msg",
        "(git add -A)", "(git add -A && git commit)",
        "(git add -A && git commit -am x)",
        "{ git add . ; git commit -m y ; }",
        "sudo git add -A", "FOO=bar git add -A", "git -C foo add -A",
        "git add -A && git commit && git push", "cd sub && git add -A",
    ]
    for c in refuse:
        assert ca._is_blanket_git(c) is True, f"should refuse: {c!r}"


def test_is_blanket_git_allows_targeted_and_quoted():
    allow = [
        "git add foo.py bar.py", "git commit -m x",
        "git add foo.py && git commit -m x",
        "git status && git add -- specific.py", "sudo git add -- only.py",
        'git commit -m "fixed -a flag"',            # -a only inside the message
        'echo "git add -A"',                        # quoted text, not a command
        "echo git add -A",                          # echo arg, not a git command
    ]
    for c in allow:
        assert ca._is_blanket_git(c) is False, f"should allow: {c!r}"


def test_is_blanket_git_skips_heredoc_body():
    # A blanket add inside a heredoc BODY is data, not a command.
    heredoc = "cat > script.sh <<'EOF'\ngit add -A\ngit commit -am all\nEOF"
    assert ca._is_blanket_git(heredoc) is False
    # …but a real blanket add to the right of the heredoc IS still caught.
    assert ca._is_blanket_git(heredoc + "\ngit add -A") is True


def _git_init(repo):
    import subprocess
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, capture_output=True)


def test_run_command_refuses_blanket_add_without_executing(tmp_path):
    """A blanket `git add -A && git commit` is NOT executed: the user's dirty
    file is never swept, no commit lands, and a soft block dict is returned."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "seed.txt").write_text("s\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True)
    (tmp_path / "userdirt.txt").write_text("user edit\n")   # pre-existing dirt

    res = ca._t_run_command({"cmd": "git add -A && git commit -m wip"}, repo)
    assert res["ok"] is False
    assert res["blocked"] == "blanket_git"
    assert "git add" in res["error"]
    # No new commit landed; the user's dirt is untouched and still untracked.
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert log.count("\n") == 1                # only the seed commit
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "userdirt.txt" in status


def test_run_command_allows_targeted_add(tmp_path):
    """A targeted `git add <path>` is executed normally (not refused)."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "mine.py").write_text("x = 1\n")
    res = ca._t_run_command({"cmd": "git add mine.py"}, repo)
    assert res["ok"] is True, res
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "mine.py" in staged


def test_run_command_blanket_subshell_no_separator_refused(tmp_path):
    """The no-separator `(git add -A)` form is caught and refused — it does
    NOT slip past as one unrecognized token."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "userdirt.txt").write_text("dirt\n")
    for cmd in ("(git add -A)", "(git add -A && git commit -m wip)",
                "sudo git add -A", "git add -A && git commit && git push"):
        res = ca._t_run_command({"cmd": cmd}, repo)
        assert res.get("blocked") == "blanket_git", f"not refused: {cmd}"
        # nothing was staged
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                cwd=repo, capture_output=True, text=True).stdout
        assert staged.strip() == "", f"swept by: {cmd}"


def test_run_chat_agent_blanket_becomes_observation(tmp_path):
    """End-to-end: a blanket add issued by the model surfaces as a tool result
    with blocked=blanket_git (an OBSERVATION) and never commits."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "userdirt.txt").write_text("noise\n")
    fn = _scripted([
        'ACTION: run_command\nARGS_JSON: {"cmd": "git add -A && git commit -m wip"}',
        "FINAL: done",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "go"}], cwd=repo, complete_fn=fn))
    cmd_tools = [e for e in evs if e["type"] == "tool" and e["name"] == "run_command"]
    assert cmd_tools and cmd_tools[0]["result"].get("blocked") == "blanket_git"
    # No commit created in the fresh (commit-less) repo.
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True)
    assert log.returncode != 0 or log.stdout.strip() == ""


# ── Backlog additions: usage event, cancellable LLM, condense summary ─────────

def test_usage_event_emitted(tmp_path):
    fn = _scripted(["THOUGHT: x\nFINAL: done"])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "hi"}], cwd=str(tmp_path), complete_fn=fn))
    usage = [e for e in evs if e["type"] == "usage"]
    assert usage and 0 <= usage[0]["pct"] <= 100
    assert usage[0]["budget_chars"] > 0


def test_cancellable_complete_returns_sentinel_when_cancelled():
    """H1: a cancel set while the LLM call runs makes the wrapper return the
    _CANCELLED sentinel promptly (the call is abandoned, not awaited)."""
    import threading
    import time as _t
    from aiforge_core.runtime import chat_cancel
    sid = 77123
    chat_cancel.start(sid)

    def slow(role, messages, **kw):
        _t.sleep(5)          # simulate a slow generation
        return "FINAL: too late"

    box = {}
    def run():
        box["out"] = ca._complete_cancellable(slow, "doer", [], sid)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    _t.sleep(0.3)
    chat_cancel.cancel(sid)  # Stop pressed mid-generation
    th.join(timeout=2)
    assert not th.is_alive(), "wrapper did not return promptly on cancel"
    assert box["out"] is ca._CANCELLED
    chat_cancel.finish(sid)


def test_cancellable_complete_passes_through_empty():
    """A legitimately-empty completion is returned as-is (not the cancel
    sentinel) when no cancel is set."""
    from aiforge_core.runtime import chat_cancel
    sid = 77124
    chat_cancel.start(sid)
    try:
        assert ca._complete_cancellable(lambda r, m, **k: "", "doer", [], sid) == ""
    finally:
        chat_cancel.finish(sid)


def test_condense_summary_includes_earlier_asks(monkeypatch):
    """A condensed middle carries earlier asks/outcomes, not just tool counts."""
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "200")
    convo = [{"role": "system", "content": "SYS"}]
    convo.append({"role": "user", "content": "build the invoice exporter please"})
    convo.append({"role": "assistant", "content": "ACTION: file_write\nfoo"})
    for i in range(30):
        convo.append({"role": "user", "content": f"OBSERVATION: {'x' * 50}"})
        convo.append({"role": "assistant", "content": f"ACTION: grep\nq{i}"})
    out = ca._compact_convo(convo, keep_recent=4)
    sys_text = out[0]["content"]
    assert "auto-condensed" in sys_text
    assert "Earlier asks:" in sys_text and "invoice exporter" in sys_text


def test_cave_mode_skips_optional_blocks_and_shrinks_budget(tmp_path, monkeypatch):
    """Cave mode drops skills/workflows/mentions blocks + condenses sooner."""
    from aiforge_core.runtime import chat_agent as ca
    seen = {"skills": 0, "workflows": 0, "mentions": 0}
    import aiforge_core.runtime.skills as sk
    import aiforge_core.runtime.workflows as wf
    import aiforge_core.runtime.mentions as mn
    monkeypatch.setattr(sk, "auto_context", lambda *a, **k: (seen.__setitem__("skills", seen["skills"] + 1), "SKILLS")[1])
    monkeypatch.setattr(wf, "auto_context", lambda *a, **k: (seen.__setitem__("workflows", seen["workflows"] + 1), "WF")[1])
    monkeypatch.setattr(mn, "expand", lambda *a, **k: (seen.__setitem__("mentions", seen["mentions"] + 1), ("M", 0))[1])

    # Budget shrinks in cave mode.
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "0")
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    normal = ca._ctx_budget_chars()
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "1")
    assert ca._ctx_budget_chars() < normal

    fn = _scripted(["FINAL: done"])
    list(ca.run_chat_agent([{"role": "user", "content": "hi"}],
                           cwd=str(tmp_path), complete_fn=fn))
    # In cave mode the optional blocks were never assembled.
    assert seen == {"skills": 0, "workflows": 0, "mentions": 0}
