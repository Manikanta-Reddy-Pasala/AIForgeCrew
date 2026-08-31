"""The chat agent's memory and search tools.

Everything here is about the same failure: a fact written under one key and
looked up under another is a fact nobody ever sees again. Both the write and
the recall path key on the GIT-TOPLEVEL basename, so a chat started in a
subdirectory still files into the repo the later recall queries.

The search tools are deliberately forgiving, because the model's first guess
at a path is often wrong: grep falls back to the whole project and SAYS it
did, rather than returning an empty result that reads like "not in this repo".
ripgrep is used when present and the pure-Python walk is the fallback, so the
answer is the same either way.

A rule is written to the same store the Library UI lists, and recorded in
memory separately — with repo=None for a global rule, because the ordinary
write path refuses a null repo and would silently drop exactly the rules that
are meant to apply everywhere.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent._tools import _memory as M


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_workspace_root", lambda: str(tmp_path))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return TOKEN\n")
    (tmp_path / "src" / "util.ts").write_text("const TOKEN = 1;\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("TOKEN\n")
    return tmp_path


# ─── recall ────────────────────────────────────────────────────────────


def test_recall_is_scoped_to_the_repo_the_write_path_uses(monkeypatch):
    """Otherwise chat's own facts are filtered out of chat's own recall."""
    from aiforge_core.memory import unified_query
    seen: dict = {}
    monkeypatch.setattr(M, "_chat_repo_key", lambda cwd: "AIForgeCrew")
    monkeypatch.setattr(unified_query, "query",
                        lambda q, limit=None, repo=None:
                        seen.update(q=q, limit=limit, repo=repo)
                        or {"hits": [{"text": "a fact", "source": "chat"}]})
    out = M._t_memory_lookup({"query": "deploy", "limit": 3}, "/repo/sub")
    assert out["hits"] == [{"text": "a fact", "source": "chat"}]
    assert seen == {"q": "deploy", "limit": 3, "repo": "AIForgeCrew"}


def test_a_long_hit_is_trimmed(monkeypatch):
    from aiforge_core.memory import unified_query
    monkeypatch.setattr(unified_query, "query",
                        lambda q, **kw: {"hits": [{"text": "x" * 900}]})
    assert len(M._t_memory_lookup({"query": "x"}, "/r")["hits"][0]["text"]) == 400


def test_a_recall_failure_is_soft(monkeypatch):
    from aiforge_core.memory import unified_query
    monkeypatch.setattr(unified_query, "query",
                        lambda q, **kw: (_ for _ in ()).throw(OSError("db")))
    assert M._t_memory_lookup({"query": "x"}, "/r") == {"ok": False,
                                                        "error": "db"}


def test_prior_conversations_are_searchable(monkeypatch):
    from aiforge_core.runtime import chat_store
    seen: dict = {}
    monkeypatch.setattr(chat_store, "search_messages",
                        lambda q, limit=None: seen.update(q=q, limit=limit)
                        or [{"session_id": 3, "text": "we chose postgres"}])
    out = M._t_search_chat_sessions({"q": "database", "limit": "4"}, "/r")
    assert out["hits"][0]["session_id"] == 3 and seen["limit"] == 4


def test_a_broken_session_search_is_soft(monkeypatch):
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "search_messages",
                        lambda q, **kw: (_ for _ in ()).throw(OSError("db")))
    assert M._t_search_chat_sessions({"query": "x"}, "/r")["ok"] is False


# ─── writing a fact ────────────────────────────────────────────────────


@pytest.fixture()
def writer(monkeypatch):
    from aiforge_core.runtime.tools import memory_write
    seen: dict = {}
    monkeypatch.setattr(memory_write, "memory_write",
                        lambda **kw: seen.update(kw) or {"ok": True, "id": 1})
    monkeypatch.setattr(M, "_chat_repo_key", lambda cwd: "AIForgeCrew")
    return seen


def test_a_fact_is_filed_under_the_repo_recall_will_query(writer):
    """A subdir write used to land under the subdir basename and was never
    recalled."""
    assert M._t_memory_write({"text": "the deploy needs sudo"},
                             "/repo/src/api")["ok"] is True
    assert writer["repo"] == "AIForgeCrew" and writer["source"] == "chat"
    assert "chat" in writer["tags"]


def test_an_explicit_repo_wins(writer):
    M._t_memory_write({"text": "x", "repo": "other"}, "/repo")
    assert writer["repo"] == "other"


def test_a_global_fact_carries_its_scope(writer):
    M._t_memory_write({"text": "x", "scope": "GLOBAL", "decision": True},
                      "/repo")
    assert writer["scope"] == "global" and writer["decision"] is True


def test_a_write_with_nothing_to_say_is_refused(writer):
    assert M._t_memory_write({}, "/repo") == {"ok": False,
                                              "error": "missing arg: text"}


def test_a_failing_store_is_reported(monkeypatch, writer):
    from aiforge_core.runtime.tools import memory_write
    monkeypatch.setattr(memory_write, "memory_write",
                        lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    assert M._t_memory_write({"text": "x"}, "/r") == {"ok": False,
                                                      "error": "disk"}


# ─── finding files ─────────────────────────────────────────────────────


def test_a_partial_name_locates_the_file(workspace):
    out = M._t_find({"name": "app"}, str(workspace))
    assert "src/app.py" in out["matches"] and out["base"] == str(workspace)


def test_directories_can_be_asked_for_on_their_own(workspace):
    matches = M._t_find({"name": "src", "kind": "dir"}, str(workspace))["matches"]
    assert matches == ["src/"]


def test_files_can_be_asked_for_on_their_own(workspace):
    matches = M._t_find({"query": "util", "kind": "file"},
                        str(workspace))["matches"]
    assert matches == ["src/util.ts"]


def test_vendor_directories_are_never_walked(workspace):
    matches = M._t_find({"name": "dep"}, str(workspace))["matches"]
    assert matches == []


def test_an_empty_name_lists_what_is_there(workspace):
    assert M._t_find({}, str(workspace))["matches"]


def test_the_result_is_bounded(workspace):
    for i in range(30):
        (workspace / f"f{i}.txt").write_text("")
    out = M._t_find({"name": "f", "limit": 5}, str(workspace))
    assert len(out["matches"]) == 5


# ─── grepping ──────────────────────────────────────────────────────────


@pytest.fixture()
def no_rg(monkeypatch):
    """Force the dependency-free path so the test is the same everywhere."""
    monkeypatch.setattr(M, "_ripgrep", lambda *a, **kw: None)


def test_a_pattern_is_found_with_its_file_and_line(workspace, no_rg):
    out = M._t_grep({"pattern": "TOKEN"}, str(workspace))
    assert out["ok"] is True
    assert any(m.startswith("src/app.py:2:") for m in out["matches"])


def test_the_search_is_case_insensitive(workspace, no_rg):
    assert M._t_grep({"pattern": "token"}, str(workspace))["matches"]


def test_a_glob_narrows_it_to_one_language(workspace, no_rg):
    out = M._t_grep({"pattern": "TOKEN", "glob": "*.ts"}, str(workspace))
    assert all(m.startswith("src/util.ts") for m in out["matches"])


def test_a_wrong_path_searches_the_project_and_says_so(workspace, no_rg):
    """An empty result would read as 'not in this repo'."""
    out = M._t_grep({"pattern": "TOKEN", "path": "srv"}, str(workspace))
    assert out["matches"] and "not found" in out["note"]


def test_a_real_subpath_narrows_the_search(workspace, no_rg):
    out = M._t_grep({"pattern": "TOKEN", "path": "src"}, str(workspace))
    assert out["note"] == "" and out["matches"]


def test_vendor_directories_are_skipped_here_too(workspace, no_rg):
    out = M._t_grep({"pattern": "TOKEN"}, str(workspace))
    assert not any("node_modules" in m for m in out["matches"])


def test_a_bad_regex_is_reported_not_raised(workspace, no_rg):
    out = M._t_grep({"pattern": "unbalanced("}, str(workspace))
    assert out["ok"] is False and "bad regex" in out["error"]


def test_a_grep_with_no_pattern_is_refused(workspace):
    assert M._t_grep({}, str(workspace))["error"] == "missing 'pattern'"


def test_the_matches_are_bounded(workspace, no_rg):
    (workspace / "big.txt").write_text("TOKEN\n" * 50)
    out = M._t_grep({"pattern": "TOKEN", "limit": 3}, str(workspace))
    assert len(out["matches"]) == 3 and out["truncated"] is True


def test_an_unreadable_file_does_not_stop_the_walk(workspace, no_rg,
                                                   monkeypatch):
    import builtins
    real = builtins.open

    def _open(path, *a, **kw):
        if str(path).endswith("app.py"):
            raise PermissionError("locked")
        return real(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", _open)
    out = M._t_grep({"pattern": "TOKEN"}, str(workspace))
    assert any("util.ts" in m for m in out["matches"])


def test_ripgrep_is_used_when_it_is_installed(workspace, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(M, "_ripgrep",
                        lambda pattern, target, glob, limit:
                        seen.update(pattern=pattern, glob=glob, limit=limit)
                        or ["src/app.py:2:TOKEN"])
    out = M._t_grep({"pattern": "TOKEN", "glob": "*.py"}, str(workspace))
    assert out["matches"] == ["src/app.py:2:TOKEN"]
    assert seen["glob"] == "*.py"


def test_the_ripgrep_command_excludes_the_vendor_dirs(workspace, monkeypatch):
    import shutil
    import types as pytypes
    seen: dict = {}
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/rg")
    monkeypatch.setattr(M.subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd)
                        or pytypes.SimpleNamespace(stdout="a.py:1:hit\n"))
    assert M._ripgrep("TOKEN", str(workspace), "*.py", 10) == ["a.py:1:hit"]
    assert "!node_modules" in seen["cmd"] and "-i" in seen["cmd"]


def test_without_ripgrep_the_caller_falls_back(workspace, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert M._ripgrep("x", str(workspace), None, 10) is None


def test_a_ripgrep_that_fails_falls_back_too(workspace, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/rg")
    monkeypatch.setattr(M.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(OSError("x")))
    assert M._ripgrep("x", str(workspace), None, 10) is None


# ─── remembering a rule ────────────────────────────────────────────────


@pytest.fixture()
def rules(monkeypatch):
    from aiforge_core.memory import backend_select, sqlite_memory
    from aiforge_core.runtime import repo_rules
    state: dict = {"written": None, "result": {"ok": True,
                                               "path": "/rules/r.md"},
                   "units": [], "embedded": True}
    monkeypatch.setattr(repo_rules, "write_rule",
                        lambda name, body, **kw: state.update(
                            written={"name": name, "body": body, **kw})
                        or state["result"])
    monkeypatch.setattr(M, "_elaborate_body",
                        lambda kind, text, name=None, description=None:
                        f"# {name}\n\n- {text}")
    monkeypatch.setattr(backend_select, "embedded", lambda: state["embedded"])
    monkeypatch.setattr(sqlite_memory, "write_unit",
                        lambda **kw: state["units"].append(kw))
    monkeypatch.setattr(M, "_chat_repo_key", lambda cwd: "AIForgeCrew")
    return state


def test_a_rule_lands_in_the_store_the_library_lists(rules):
    out = M._t_remember_rule({"text": "always run the tests"}, "/repo")
    assert out["ok"] is True and out["path"] == "/rules/r.md"
    assert rules["written"]["name"] == "always run the tests"
    assert rules["written"]["always"] is True
    assert out["remembered"].startswith("# always run the tests")


def test_the_rule_is_also_recalled_alongside_ordinary_facts(rules):
    M._t_remember_rule({"text": "always run the tests", "scope": "repo"},
                       "/repo")
    unit = rules["units"][0]
    assert unit["text"] == "RULE: always run the tests"
    assert unit["repo"] == "AIForgeCrew" and "rule" in unit["tags"]


def test_a_global_rule_is_recorded_without_a_repo(rules):
    """memory_write refuses a null repo, which would silently drop exactly the
    rules meant to apply everywhere."""
    M._t_remember_rule({"text": "never force-push"}, "/repo")
    assert rules["units"][0]["repo"] is None


def test_an_explicit_name_and_filters_are_forwarded(rules):
    M._t_remember_rule({"text": "x", "name": "python style",
                        "globs": "**/*.py, **/*.pyi",
                        "triggers": "lint, format"}, "/repo")
    assert rules["written"]["globs"] == ["**/*.py", "**/*.pyi"]
    assert rules["written"]["triggers"] == ["lint", "format"]


def test_an_already_parsed_list_is_accepted(rules):
    M._t_remember_rule({"text": "x", "globs": ["**/*.py"]}, "/repo")
    assert rules["written"]["globs"] == ["**/*.py"]


def test_the_name_falls_back_to_the_first_line(rules):
    M._t_remember_rule({"body": "# Ship on green\nand not before"}, "/repo")
    assert rules["written"]["name"] == "Ship on green"


def test_a_rule_with_no_text_is_refused(rules):
    assert M._t_remember_rule({}, "/repo")["error"] == "missing 'text'"
    assert rules["written"] is None


def test_a_rejected_rule_is_returned_as_is(rules):
    rules["result"] = {"ok": False, "error": "a rule by that name exists"}
    assert M._t_remember_rule({"text": "x"}, "/repo") == rules["result"]
    assert rules["units"] == [], "and nothing is recorded in memory"


def test_a_memory_write_failure_never_blocks_the_rule(rules, monkeypatch):
    from aiforge_core.memory import sqlite_memory
    monkeypatch.setattr(sqlite_memory, "write_unit",
                        lambda **kw: (_ for _ in ()).throw(OSError("db")))
    assert M._t_remember_rule({"text": "x"}, "/repo")["ok"] is True


def test_without_the_embedded_store_nothing_is_recorded(rules):
    rules["embedded"] = False
    M._t_remember_rule({"text": "x"}, "/repo")
    assert rules["units"] == []


def test_a_broken_rules_store_is_a_soft_error(rules, monkeypatch):
    from aiforge_core.runtime import repo_rules
    monkeypatch.setattr(repo_rules, "write_rule",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("ro")))
    assert M._t_remember_rule({"text": "x"}, "/repo")["ok"] is False


# ─── the bullets a rule file is made of ────────────────────────────────


def test_a_plain_bullet_is_always_on():
    assert M._parse_bullet("- run the tests") == ((), "run the tests")


def test_a_bullet_can_declare_when_it_applies():
    trig, text = M._parse_bullet("- [triggers: deploy, release] tag it first")
    assert trig == ("deploy", "release") and text == "tag it first"


def test_the_trigger_list_is_normalised():
    trig, _ = M._parse_bullet("- [triggers: Deploy , , RELEASE] x")
    assert trig == ("deploy", "release")


def test_a_line_without_the_bullet_marker_still_parses():
    assert M._parse_bullet("bare line") == ((), "bare line")
