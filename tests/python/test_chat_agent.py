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


# ── commit hygiene — BASELINE-DIFF rewrite of a blanket git stage ─────────────

_ROOT = "/repo"


def test_rewrite_blanket_git_add_forms():
    f = lambda c, p: ca._rewrite_blanket_git(c, _ROOT, p)
    new, note = f("git add -A", ["a.py", "b.py"])
    assert new == "git -C /repo add -- a.py b.py" and note and "a.py" in note
    assert f("git add .", ["x.py"])[0] == "git -C /repo add -- x.py"
    assert f("git add --all", ["x.py"])[0] == "git -C /repo add -- x.py"
    assert f("git add -A .", ["x.py"])[0] == "git -C /repo add -- x.py"
    assert f("git add -- .", ["x.py"])[0] == "git -C /repo add -- x.py"


def test_rewrite_git_commit_a_forms():
    # A commit always gets a `git -C <root> reset -q` prepended (item 4c) so the
    # user's pre-STAGED index is cleared before the agent's targeted add.
    f = lambda c, p: ca._rewrite_blanket_git(c, _ROOT, p)[0]
    assert f('git commit -am "msg"', ["a.py"]) == \
        'git -C /repo reset -q && git -C /repo add -- a.py && git commit -m "msg"'
    assert f('git commit -a -m "msg"', ["a.py"]) == \
        'git -C /repo reset -q && git -C /repo add -- a.py && git commit -m "msg"'
    assert f('git commit --all -m "x"', ["a.py"]) == \
        'git -C /repo reset -q && git -C /repo add -- a.py && git commit -m "x"'
    assert f("git commit -a", ["a.py"]) == \
        "git -C /repo reset -q && git -C /repo add -- a.py && git commit"


def test_rewrite_drops_blanket_when_no_agent_changes():
    # Empty agent set → DROP the blanket add (→ `true`), NEVER fall back to
    # add -A (that's the sweep bug). With a commit present, a `reset -q` is
    # prepended so the commit can't pick up the user's pre-staged index.
    new, note = ca._rewrite_blanket_git("git add -A && git commit -m wip", _ROOT, [])
    assert new == "git -C /repo reset -q && true && git commit -m wip"
    assert note and "dropped" in note
    # commit -a with no agent changes → strip -a, stage nothing.
    new2, _ = ca._rewrite_blanket_git('git commit -am "x"', _ROOT, [])
    assert new2 == 'git -C /repo reset -q && true && git commit -m "x"'


def test_rewrite_leaves_targeted_git_alone():
    # A non-blanket git command must not be mangled.
    new, note = ca._rewrite_blanket_git(
        "git status && git add -- specific.py", _ROOT, ["a.py"])
    assert new == "git status && git add -- specific.py" and note is None
    # A commit message that merely mentions -a must not be rewritten.
    new2, note2 = ca._rewrite_blanket_git(
        'git commit -m "fixed flag"', _ROOT, ["a.py"])
    assert new2 == 'git commit -m "fixed flag"' and note2 is None


def test_detect_blanket_skips_heredoc_and_echo():
    # A blanket add inside a heredoc BODY is data, not a command — not detected.
    heredoc = "cat > script.sh <<'EOF'\ngit add -A\ngit commit -am all\nEOF"
    assert ca._detect_blanket_git(heredoc) is False
    # A blanket add inside an echo/quoted argument — not detected.
    assert ca._detect_blanket_git('echo "git add -A"') is False
    assert ca._detect_blanket_git("echo git add -A") is False
    # The real top-level forms ARE detected.
    assert ca._detect_blanket_git("git add -A && git commit -m x") is True
    assert ca._detect_blanket_git("cd sub && git add -A") is True
    # And a heredoc body left of a real command still detects the real one.
    assert ca._detect_blanket_git(heredoc + "\ngit add -A") is True


def test_detect_blanket_sees_through_prefixes_and_subshell():
    # item 4b — `sudo` / `env=val` / `git -C <dir>` / `(`-subshell wrappers
    # must NOT let a blanket add slip past undetected (which would SWEEP).
    assert ca._detect_blanket_git("sudo git add -A") is True
    assert ca._detect_blanket_git("FOO=bar git add -A") is True
    assert ca._detect_blanket_git("git -C foo add -A") is True
    assert ca._detect_blanket_git("(git add -A && git commit -am x)") is True
    assert ca._detect_blanket_git("{ git add . ; git commit -m y ; }") is True
    # a targeted add behind a prefix is still left alone
    assert ca._detect_blanket_git("sudo git add -- only.py") is False


def test_rewrite_neutralizes_prefixed_blanket_adds():
    # item 4b — with no agent changes the prefixed/wrapped blanket add becomes a
    # `true` no-op (never `add -A`), preserving the benign prefix so a wrapping
    # `(` still balances its `)`.
    f = lambda c: ca._rewrite_blanket_git(c, _ROOT, [])[0]
    assert f("sudo git add -A") == "sudo true"
    assert f("FOO=bar git add -A") == "FOO=bar true"
    assert f("git -C foo add -A") == "true"
    # subshell: blanket add → true, the single reset rides the first staging
    # segment, commit keeps its trailing `)` so the subshell stays balanced.
    out = f("(git add -A && git commit -am x)")
    assert out == "( git -C /repo reset -q && true && true && git commit -m x)"


def _git_init(repo):
    import subprocess
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, capture_output=True)


def test_run_chat_agent_commits_only_agent_changes(tmp_path):
    """Baseline-diff: pre-existing user dirt is NOT committed; the agent's
    file_write AND shell-written file ARE."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True)
    # An unrelated dirty file a blanket `git add -A` would have swept in.
    (tmp_path / "unrelated.txt").write_text("noise\n")

    fn = _scripted([
        'ACTION: file_write\nARGS_JSON: {"path": "mine.py", "content": "x = 1\\n"}',
        # Plain `>` output redirection is creation/overwrite, NOT a delete
        # (item 1) — the autonomous agent runs it without an approval gate.
        'ACTION: run_command\nARGS_JSON: {"cmd": "echo made > shellmade.txt"}',
        'ACTION: run_command\nARGS_JSON: {"cmd": "git add -A && git commit -m wip"}',
        "FINAL: done",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "go"}], cwd=repo, complete_fn=fn))
    show = subprocess.run(["git", "show", "--name-status", "--format=", "HEAD"],
                          cwd=repo, capture_output=True, text=True).stdout
    assert "mine.py" in show                 # agent's file_write committed
    assert "shellmade.txt" in show           # agent's shell-written file committed
    assert "unrelated.txt" not in show       # user's pre-existing dirt NOT swept
    # The unrelated user file is still dirty/untracked afterwards.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "unrelated.txt" in status
    cmd_tools = [e for e in evs if e["type"] == "tool" and e["name"] == "run_command"]
    note = next((t["result"].get("note") for t in cmd_tools
                 if t["result"].get("note")), "")
    assert "hygiene" in note


def test_run_command_blanket_stages_deletion(tmp_path):
    """A file the agent deleted AFTER the baseline snapshot is committed as a
    deletion by the baseline-diff blanket rewrite. (Tested through the
    run_command tool directly — an interactive `rm` is separately gated by the
    risk-approval policy, orthogonal to commit hygiene.)"""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "todelete.txt").write_text("bye\n")
    (tmp_path / "keep.txt").write_text("keep\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True)
    # Snapshot baseline on the CLEAN repo, then the agent deletes a file.
    ca._set_chat_baseline(repo)
    (tmp_path / "todelete.txt").unlink()
    res = ca._t_run_command(
        {"cmd": "git add -A && git commit -m wip"}, repo)
    assert res["ok"], res
    show = subprocess.run(["git", "show", "--name-status", "--format=", "HEAD"],
                          cwd=repo, capture_output=True, text=True).stdout
    assert "D\ttodelete.txt" in show         # deletion committed
    assert "keep.txt" not in show            # untouched file not re-committed


def test_run_chat_agent_cd_subdir_blanket_anchored(tmp_path):
    """`cd sub && git add -A` stages the right repo-relative path even though
    the add runs from a subdir — the rewrite anchors it at the repo root."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "sub").mkdir()
    fn = _scripted([
        'ACTION: file_write\nARGS_JSON: {"path": "sub/mine.py", "content": "x=1\\n"}',
        'ACTION: run_command\nARGS_JSON: {"cmd": "cd sub && git add -A && cd .. && git commit -m wip"}',
        "FINAL: done",
    ])
    _collect(ca.run_chat_agent(
        [{"role": "user", "content": "go"}], cwd=repo, complete_fn=fn))
    show = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                          cwd=repo, capture_output=True, text=True).stdout
    assert "sub/mine.py" in show


def test_run_chat_agent_empty_agent_change_no_sweep(tmp_path):
    """Agent changes nothing but issues a blanket add → the user's dirty file
    is NOT swept; the commit is a no-op."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "seed.txt").write_text("s\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True)
    (tmp_path / "userfile.txt").write_text("user edit\n")   # pre-existing dirt
    fn = _scripted([
        'ACTION: run_command\nARGS_JSON: {"cmd": "git add -A && git commit -m wip"}',
        "FINAL: done",
    ])
    _collect(ca.run_chat_agent(
        [{"role": "user", "content": "go"}], cwd=repo, complete_fn=fn))
    # HEAD is still the seed commit (no new commit landed with userfile).
    show = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                          cwd=repo, capture_output=True, text=True).stdout
    assert "userfile.txt" not in show
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "userfile.txt" in status         # still dirty, untouched


def test_run_chat_agent_concurrent_baseline_isolation(tmp_path):
    """Two concurrent run_chat_agent calls over two repos must not
    cross-contaminate their baselines (ContextVar isolation)."""
    import subprocess
    import threading

    def _mk_repo(name):
        repo = tmp_path / name
        repo.mkdir()
        _git_init(str(repo))
        # pre-existing user dirt unique to each repo
        (repo / f"user_{name}.txt").write_text("dirt\n")
        return str(repo)

    repo_a = _mk_repo("a")
    repo_b = _mk_repo("b")

    def _run(repo, mine):
        # If baselines leaked across threads, the other repo's dirt would
        # show up in this repo's commit.
        def fn(role, convo, **kw):
            return _run.scripts[repo].pop(0)
        _run.scripts.setdefault(repo, [
            f'ACTION: file_write\nARGS_JSON: {{"path": "{mine}", "content": "x=1\\n"}}',
            'ACTION: run_command\nARGS_JSON: {"cmd": "git add -A && git commit -m wip"}',
            "FINAL: done",
        ])
        list(ca.run_chat_agent([{"role": "user", "content": "go"}],
                               cwd=repo, complete_fn=fn))

    _run.scripts = {}
    ta = threading.Thread(target=_run, args=(repo_a, "mine_a.py"))
    tb = threading.Thread(target=_run, args=(repo_b, "mine_b.py"))
    ta.start(); tb.start(); ta.join(); tb.join()

    for repo, mine, other_dirt in ((repo_a, "mine_a.py", "user_a.txt"),
                                   (repo_b, "mine_b.py", "user_b.txt")):
        show = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                              cwd=repo, capture_output=True, text=True).stdout
        assert mine in show
        assert other_dirt not in show       # own user dirt not committed
        # nothing from the OTHER repo leaked in
        assert "mine_a.py" not in show or repo == repo_a
        assert "mine_b.py" not in show or repo == repo_b


# ── item 4: baseline-diff fail-safe — absent baseline NEVER sweeps ────────────

def test_absent_baseline_neutralizes_blanket_add(tmp_path):
    """item 4a — a direct run_command with NO run baseline (unknown, default
    ContextVar) must NEUTRALIZE the blanket add, never treat absent as clean
    and sweep the user's dirt."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "seed.txt").write_text("s\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True)
    (tmp_path / "userdirt.txt").write_text("user edit\n")   # pre-existing dirt
    # Reset the ContextVar to the unknown default (no _set_chat_baseline call).
    ca._CHAT_BASELINE.set((None, None))
    res = ca._t_run_command({"cmd": "git add -A && git commit -m wip"}, repo)
    # The commit is a no-op (nothing the agent staged); userdirt NOT swept.
    show = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                          cwd=repo, capture_output=True, text=True).stdout
    assert "userdirt.txt" not in show
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "userdirt.txt" in status            # still dirty, untouched
    assert "no run baseline" in (res.get("note") or "")


def test_prestaged_user_file_not_committed_by_agent(tmp_path):
    """item 4c — a blanket add+commit must reset the user's pre-STAGED index so
    only the agent's targeted file lands, not the user's staged file."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "seed.txt").write_text("s\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True)
    # User pre-stages their own file BEFORE the agent run.
    (tmp_path / "userstaged.txt").write_text("user\n")
    subprocess.run(["git", "add", "userstaged.txt"], cwd=repo, capture_output=True)
    # Baseline snapshot now sees userstaged.txt as pre-existing.
    ca._set_chat_baseline(repo)
    # Agent writes its own file then issues a blanket add+commit.
    (tmp_path / "agent.py").write_text("x=1\n")
    res = ca._t_run_command({"cmd": "git add -A && git commit -m wip"}, repo)
    assert res["ok"], res
    show = subprocess.run(["git", "show", "--name-status", "--format=", "HEAD"],
                          cwd=repo, capture_output=True, text=True).stdout
    assert "agent.py" in show                  # agent's file committed
    assert "userstaged.txt" not in show        # user's pre-staged file NOT swept


def test_prefixed_blanket_add_neutralized_no_baseline(tmp_path):
    """item 4b — sudo / env / git -C / subshell blanket adds are neutralized
    (not run unrewritten) when there's no trustworthy baseline."""
    import subprocess
    repo = str(tmp_path)
    _git_init(repo)
    (tmp_path / "seed.txt").write_text("s\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True)
    (tmp_path / "userdirt.txt").write_text("dirt\n")
    for cmd in ("FOO=bar git add -A && git commit -m wip",
                "git -C . add -A && git commit -m wip"):
        ca._CHAT_BASELINE.set((None, None))    # unknown baseline each time
        ca._t_run_command({"cmd": cmd}, repo)
        show = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                              cwd=repo, capture_output=True, text=True).stdout
        assert "userdirt.txt" not in show, f"swept by: {cmd}"
