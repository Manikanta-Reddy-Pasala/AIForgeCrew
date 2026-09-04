"""Cross-repo and intra-repo analysis fan-out.

Every explore agent here runs READ-ONLY (mode="analyze"), autonomously, in the
user's real checkout — so the hard read-only guard is the thing that keeps a
hallucinated write from auto-applying, and the worker binds the repo root on
its OWN thread because ThreadPoolExecutor does not copy contextvars and a
concurrent team run's process-global env would otherwise point codegraph at
the wrong repo.

The intra-repo plan exists for a different failure: a local model handed a
flat twelve-file sweep loses the worklist and stalls re-reading. Discovered
paths are validated against DISK first, so a mistyped path is dropped and the
agent never has to reproduce one from memory.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.runtime import analysis_pipeline as ap


def _git_repo(root, name):
    d = root / name
    (d / ".git").mkdir(parents=True)
    return d


# ─── naming a repo ─────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", ["analyse the aiforgecrew repo",
                                    "look at aiforgecrew"])
def test_a_specific_name_is_a_signal_on_its_own(prompt):
    assert ap._repo_named_in(prompt, "aiforgecrew") is True


@pytest.mark.parametrize("prompt,found", [
    ("check the `core` repo", True),
    ("the core repo needs work", True),
    ("repository core is stale", True),
    ("this is core to the design", False),
    ("the web page is slow", False),
])
def test_a_short_or_common_name_needs_a_repo_cue(prompt, found):
    """Matching "core"/"web"/"api" in prose pulls repos in spuriously."""
    assert ap._repo_named_in(prompt, "core") is (found if "core" in prompt
                                                 else False)
    if "web" in prompt:
        assert ap._repo_named_in(prompt, "web") is found


# ─── identifying the repos ─────────────────────────────────────────────


@pytest.fixture
def repos(monkeypatch, tmp_path):
    from aiforge_core.config import repo_map
    registry: dict = {}
    monkeypatch.setattr(repo_map, "list_all", lambda: {"paths": registry})
    monkeypatch.delenv("AIFORGE_ANALYSIS_MAX_REPOS", raising=False)
    return {"root": tmp_path, "registry": registry}


def test_a_registered_repo_named_in_the_prompt_is_used(repos):
    d = _git_repo(repos["root"], "aiforgecrew")
    repos["registry"]["aiforgecrew"] = str(d)
    out = ap.identify_repos("summarise aiforgecrew", str(repos["root"]))
    assert [r["name"] for r in out] == ["aiforgecrew"]


def test_a_path_in_the_prompt_is_used_only_when_it_is_a_repo(repos):
    d = _git_repo(repos["root"], "checkout")
    plain = repos["root"] / "notes"
    plain.mkdir()
    out = ap.identify_repos(f"compare {d} and {plain}", str(repos["root"]))
    assert [r["path"] for r in out] == [str(d)]


def test_child_repos_are_used_only_when_the_prompt_named_nothing(repos):
    """Otherwise "summarize repoA" in a parent of ten checkouts fans out over
    all ten."""
    a = _git_repo(repos["root"], "alpha")
    _git_repo(repos["root"], "beta")
    repos["registry"]["alpha"] = str(a)
    named = ap.identify_repos("summarise alpha", str(repos["root"]))
    assert [r["name"] for r in named] == ["alpha"]
    unnamed = ap.identify_repos("summarise everything", str(repos["root"]))
    assert {r["name"] for r in unnamed} == {"alpha", "beta"}


def test_a_pinned_folder_is_the_last_resort(repos, tmp_path):
    plain = tmp_path / "just-a-folder"
    plain.mkdir()
    out = ap.identify_repos("summarise this", str(plain))
    assert out == [{"name": "just-a-folder", "path": str(plain)}]


def test_the_repo_count_is_capped(repos, monkeypatch):
    monkeypatch.setenv("AIFORGE_ANALYSIS_MAX_REPOS", "2")
    for name in ("a1", "b2", "c3"):
        _git_repo(repos["root"], name)
    assert len(ap.identify_repos("summarise", str(repos["root"]))) == 2


@pytest.mark.parametrize("raw,expected", [("5", 5), ("1", 2), ("junk", 12)])
def test_the_repo_cap_is_floored(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFORGE_ANALYSIS_MAX_REPOS", raw)
    assert ap._repo_cap() == expected


def test_colliding_repo_names_are_disambiguated():
    """Two repos both named `api` collide as subtask slugs and the panel flips
    both rows together."""
    rows = [{"name": "api", "path": "/src/alpha/api"},
            {"name": "api", "path": "/src/beta/api"},
            {"name": "web", "path": "/src/web"}]
    ap._disambiguate_names(rows)
    assert [r["name"] for r in rows] == ["api (alpha)", "api (beta)", "web"]


def test_a_missing_registry_is_not_fatal(monkeypatch, tmp_path):
    from aiforge_core.config import repo_map
    monkeypatch.setattr(repo_map, "list_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("no file")))
    assert ap.identify_repos("summarise x", str(tmp_path))


def test_an_unreadable_parent_yields_nothing(tmp_path):
    added: list = []
    ap._child_repos(str(tmp_path / "gone"), lambda n, p: added.append(n))
    assert added == []


# ─── topics ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt,topics", [
    ("analyse auth, caching and logging", ["auth", "caching", "logging"]),
    ("explore: retries / timeouts", ["retries", "timeouts"]),
    ("focus on error handling", ["error handling"]),
])
def test_topics_are_extracted_after_a_cue(prompt, topics):
    assert ap.extract_topics(prompt) == topics


def test_paths_never_become_topics():
    """"analyze /home/ai/codeRepo/X" turned path segments into bogus topics."""
    assert ap.extract_topics("analyze ~/codeRepo/alpha and ~/codeRepo/beta") == []


def test_extraction_stops_at_the_deliverable_clause():
    out = ap.extract_topics("analyse auth and caching then write a report")
    assert out == ["auth", "caching"]


def test_filler_words_are_dropped():
    assert ap.extract_topics("analyse auth and them and all") == ["auth"]


def test_a_prompt_with_no_cue_has_no_topics():
    assert ap.extract_topics("what does this do?") == []
    assert ap.extract_topics("") == []


def test_the_topic_list_is_capped():
    prompt = "analyse " + ", ".join(f"topic{i}" for i in range(20))
    assert len(ap.extract_topics(prompt)) == 8


# ─── the fan-out decision ──────────────────────────────────────────────


def test_two_repos_fan_out(monkeypatch):
    monkeypatch.setattr(ap, "identify_repos",
                        lambda p, cwd: [{"name": "a"}, {"name": "b"}])
    monkeypatch.setattr(ap, "extract_topics", lambda p: ["auth"])
    fan, repos, topics = ap.should_fan_out("p", "/cwd")
    assert fan is True
    assert len(repos) == 2
    assert topics == ["auth"]


def test_one_repo_does_not_fan_out(monkeypatch):
    """Even with many topics there is no cross-repo parallelism to gain."""
    monkeypatch.setattr(ap, "identify_repos", lambda p, cwd: [{"name": "a"}])
    monkeypatch.setattr(ap, "extract_topics", lambda p: ["a", "b", "c"])
    assert ap.should_fan_out("p", "/cwd")[0] is False


@pytest.mark.parametrize("raw,expected", [("4", 4), ("0", 1), ("99", 8),
                                          ("junk", 1)])
def test_the_worker_count_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFORGE_ANALYSIS_MAX_WORKERS", raw)
    assert ap._max_workers() == expected


# ─── the explore brief ─────────────────────────────────────────────────


def test_the_brief_is_explicitly_read_only():
    out = ap._explore_prompt({"name": "alpha", "path": "/src/alpha"},
                             ["auth"], "compare the repos")
    assert "READ-ONLY" in out
    assert "Do NOT modify" in out
    assert "Focus topics: auth" in out
    assert "/src/alpha" in out


def test_a_topicless_brief_asks_for_an_overview():
    out = ap._explore_prompt({"name": "a", "path": "/p"}, [], "overall")
    assert "structured overview" in out


# ─── collecting findings ───────────────────────────────────────────────


def test_the_last_message_is_the_findings():
    findings, ok, err = ap._findings_from_events(
        [{"type": "thought", "text": "looking"},
         {"type": "message", "text": "# Findings\nfirst"},
         {"type": "message", "text": "# Findings\nfinal"}], {"name": "a"})
    assert ok is True
    assert err is None
    assert findings.endswith("final")


def test_an_error_event_ends_the_explore():
    _f, ok, err = ap._findings_from_events(
        [{"type": "error", "text": "model down"}], {"name": "a", "path": "/p"})
    assert ok is False
    assert err["error"] == "model down"


def test_a_stopped_run_produces_no_findings():
    findings, ok, _err = ap._findings_from_events(
        [{"type": "message", "text": "(stopped: user)"}], {"name": "a"})
    assert ok is False
    assert findings == ""


def test_a_clarification_message_is_not_findings():
    _f, ok, _err = ap._findings_from_events(
        [{"type": "message", "text": "which module?", "awaiting_input": True}],
        {"name": "a"})
    assert ok is False


# ─── one explore worker ────────────────────────────────────────────────


@pytest.fixture
def explorer(monkeypatch):
    import aiforge_core.runtime.chat_agent as ca
    from aiforge_core.runtime import request_context as rc
    state: dict = {"events": [{"type": "message", "text": "# Findings"}],
                   "kwargs": None, "bound": []}

    def _run(messages, **kw):
        state["kwargs"] = kw
        state["bound"].append(rc.get_repo_root())
        return iter(state["events"])
    monkeypatch.setattr(ca, "run_chat_agent", _run)
    return state


def test_an_explore_runs_read_only_and_unattended(explorer):
    """role= does NOT restrict tools, and session_id=None skips approvals — so
    without mode="analyze" a hallucinated write would auto-apply in the user's
    real repo."""
    out = ap._explore_one({"name": "alpha", "path": "/src/alpha"}, [], "overall")
    assert out["ok"] is True
    assert out["findings"] == "# Findings"
    kw = explorer["kwargs"]
    assert kw["mode"] == "analyze"
    assert kw["session_id"] is None
    assert kw["role"] == "researcher"
    assert kw["cwd"] == "/src/alpha"


def test_the_worker_binds_its_own_repo_root(explorer):
    """ThreadPoolExecutor does not copy contextvars, so a concurrent team run's
    process-global env would resolve codegraph to the wrong repo."""
    ap._explore_one({"name": "alpha", "path": "/src/alpha"}, [], "overall")
    assert explorer["bound"] == ["/src/alpha"]


def test_a_crashing_explore_is_reported_not_raised(explorer, monkeypatch):
    import aiforge_core.runtime.chat_agent as ca
    monkeypatch.setattr(ca, "run_chat_agent",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = ap._explore_one({"name": "a", "path": "/p"}, [], "o")
    assert out["ok"] is False
    assert out["error"] == "boom"


def test_a_file_group_explore_passes_exact_paths(explorer):
    """The agent never has to reproduce a path from memory."""
    out = ap._explore_files_group({"name": "files 1-2", "path": "/repo",
                                   "files": ["a.py", "b.py"]}, ["auth"], "o")
    assert out["ok"] is True
    assert explorer["kwargs"]["mode"] == "analyze"


def test_a_crashing_group_explore_is_reported(explorer, monkeypatch):
    import aiforge_core.runtime.chat_agent as ca
    monkeypatch.setattr(ca, "run_chat_agent",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no repo")))
    out = ap._explore_files_group({"name": "g", "path": "/p", "files": []}, [], "o")
    assert out["ok"] is False
    assert out["error"] == "no repo"


# ─── the intra-repo plan ───────────────────────────────────────────────


@pytest.fixture
def files_repo(tmp_path, monkeypatch):
    for i in range(8):
        (tmp_path / f"mod{i}.py").write_text("x = 1\n")
    monkeypatch.delenv("AIFORGE_ANALYSIS_MIN_FILES", raising=False)
    monkeypatch.delenv("AIFORGE_ANALYSIS_FILES_PER_GROUP", raising=False)
    return tmp_path


def test_only_paths_that_exist_are_planned(files_repo):
    """A mistyped path is dropped rather than fed to a stalling model."""
    prompt = "summarise mod0.py, mod1.py and typo_module.py"
    assert ap._discover_target_files(prompt, str(files_repo)) == ["mod0.py",
                                                                  "mod1.py"]


def test_a_repeated_path_is_listed_once(files_repo):
    assert ap._discover_target_files("mod0.py and mod0.py again",
                                     str(files_repo)) == ["mod0.py"]


def test_a_nested_relative_path_is_discovered(files_repo):
    (files_repo / "pkg").mkdir()
    (files_repo / "pkg" / "deep.py").write_text("x = 1\n")
    assert ap._discover_target_files("summarise pkg/deep.py",
                                     str(files_repo)) == ["pkg/deep.py"]


def test_an_absolute_path_is_not_discovered(files_repo):
    """Known limit: the path token must start with a word character, so an
    absolute path is matched from its first segment and resolves against the
    repo root instead of the filesystem — it is simply dropped."""
    assert ap._discover_target_files(f"look at {files_repo / 'mod0.py'}",
                                     str(files_repo)) == []


def test_many_named_files_are_split_into_bounded_groups(files_repo):
    prompt = "summarise " + ", ".join(f"mod{i}.py" for i in range(8))
    plan, groups, _topics = ap.plan_single_repo(prompt, str(files_repo))
    assert plan is True
    assert len(groups) == 2
    assert groups[0]["files"] == ["mod0.py", "mod1.py", "mod2.py", "mod3.py"]
    assert groups[0]["name"] == "files 1-4"
    assert groups[1]["name"] == "files 5-8"


def test_a_few_files_stay_on_the_plain_research_agent(files_repo):
    plan, groups, _t = ap.plan_single_repo("summarise mod0.py and mod1.py",
                                           str(files_repo))
    assert plan is False
    assert groups == []


@pytest.mark.parametrize("var,fn,raw,expected", [
    ("AIFORGE_ANALYSIS_MIN_FILES", "_min_files_to_plan", "1", 2),
    ("AIFORGE_ANALYSIS_MIN_FILES", "_min_files_to_plan", "junk", 6),
    ("AIFORGE_ANALYSIS_FILES_PER_GROUP", "_files_per_group", "0", 1),
    ("AIFORGE_ANALYSIS_FILES_PER_GROUP", "_files_per_group", "junk", 4),
])
def test_the_plan_thresholds_are_clamped(monkeypatch, var, fn, raw, expected):
    monkeypatch.setenv(var, raw)
    assert getattr(ap, fn)() == expected


# ─── synthesis ─────────────────────────────────────────────────────────


@pytest.fixture
def synth(monkeypatch):
    from aiforge_core.llm import client
    state: dict = {"reply": "# The draft", "prompt": None}

    def _complete(role, convo):
        state["prompt"] = convo[0]["content"]
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]
    monkeypatch.setattr(client, "complete", _complete)
    return state


def test_every_repos_findings_reach_the_synthesis(synth):
    out = ap._synthesize("compare them",
                         [{"name": "alpha", "path": "/a", "findings": "A findings"},
                          {"name": "beta", "path": "/b", "findings": "B findings"}],
                         ["auth"])
    assert out == "# The draft"
    assert "A findings" in synth["prompt"]
    assert "B findings" in synth["prompt"]
    assert "requested topics were: auth" in synth["prompt"]


def test_a_repo_that_produced_nothing_is_still_represented(synth):
    ap._synthesize("p", [{"name": "a", "path": "/a", "findings": ""}], [])
    assert "no findings — explore failed" in synth["prompt"]


def test_the_budget_is_per_repo_so_the_tail_is_not_dropped(synth):
    """A flat cut lands mid-stream and omits the later repos entirely."""
    results = [{"name": f"r{i}", "path": f"/r{i}", "findings": "x" * 20000}
               for i in range(6)]
    ap._synthesize("p", results, [])
    assert "## r5" in synth["prompt"]


def test_a_failed_synthesis_never_loses_the_raw_findings(synth):
    synth["reply"] = RuntimeError("model down")
    out = ap._synthesize("p", [{"name": "a", "path": "/a", "findings": "A"}], [])
    assert "synthesis failed" in out
    assert "A" in out


def test_an_empty_reply_falls_back_to_the_raw_findings(synth):
    synth["reply"] = "   "
    assert "A findings" in ap._synthesize(
        "p", [{"name": "a", "path": "/a", "findings": "A findings"}], [])


# ─── the fan-out skeleton ──────────────────────────────────────────────


@pytest.fixture
def fanout(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    state = {"cancelled": False, "results": {}}
    monkeypatch.setattr(chat_cancel, "is_cancelled",
                        lambda sid: state["cancelled"])
    monkeypatch.setattr(ap, "_synthesize",
                        lambda prompt, results, topics: f"# Draft ({len(results)})")
    monkeypatch.setenv("AIFORGE_ANALYSIS_MAX_WORKERS", "1")
    return state


def _explore_ok(unit, topics, overall):
    return {"name": unit["name"], "path": unit.get("path", ""), "ok": True,
            "findings": f"{unit['name']} findings"}


def test_the_fan_out_announces_tracks_and_synthesizes(fanout):
    units = [{"name": "alpha", "path": "/a"}, {"name": "beta", "path": "/b"}]
    events = list(ap._fan_out_and_synthesize("p", units, _explore_ok, ["auth"],
                                             None, "repository"))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "thought"
    assert kinds[1] == "subtasks"
    assert kinds[-2:] == ["message", "done"]
    assert events[1]["items"][0]["slug"] == "alpha"
    assert [e for e in events if e["type"] == "message"][0]["text"] == "# Draft (2)"


def test_a_failed_unit_is_marked_failed(fanout):
    def _explore_fail(unit, topics, overall):
        return {"name": unit["name"], "ok": False, "error": "model down",
                "findings": ""}
    events = list(ap._fan_out_and_synthesize("p", [{"name": "a", "path": "/a"}],
                                             _explore_fail, [], None, "repository"))
    update = next(e for e in events if e["type"] == "subtask_update")
    assert update["status"] == "failed"
    assert any("explore failed (model down)" in (e.get("text") or "")
               for e in events)


def test_a_worker_that_raises_becomes_a_failed_result(fanout):
    def _explode(unit, topics, overall):
        raise RuntimeError("thread died")
    events = list(ap._fan_out_and_synthesize("p", [{"name": "a", "path": "/a"}],
                                             _explode, [], None, "repository"))
    assert any("thread died" in (e.get("text") or "") for e in events)


def test_a_stop_before_any_result_says_so(fanout):
    fanout["cancelled"] = True
    events = list(ap._fan_out_and_synthesize("p", [{"name": "a", "path": "/a"}],
                                             _explore_ok, [], 7, "repository"))
    assert "Stopped before any repository" in events[-2]["text"]


def test_no_units_at_all(fanout):
    events = list(ap._fan_out_and_synthesize("p", [], _explore_ok, [], None,
                                             "repository"))
    assert "No repositorys could be analyzed" in events[-2]["text"]


def test_the_cross_repo_entry_point_resolves_its_own_repos(monkeypatch):
    monkeypatch.setattr(ap, "should_fan_out",
                        lambda p, cwd: (True, [{"name": "a", "path": "/a"}],
                                        ["auth"]))
    seen: dict = {}

    def _skeleton(prompt, units, fn, topics, sid, noun):
        seen.update(units=units, topics=topics, noun=noun, fn=fn)
        return iter(())
    monkeypatch.setattr(ap, "_fan_out_and_synthesize", _skeleton)
    list(ap.stream_analysis_team("p", "/cwd"))
    assert seen["noun"] == "repository"
    assert seen["fn"] is ap._explore_one
    assert seen["topics"] == ["auth"]


def test_the_planned_entry_point_resolves_its_own_groups(monkeypatch):
    monkeypatch.setattr(ap, "plan_single_repo",
                        lambda p, cwd: (True, [{"name": "files 1-4"}], []))
    seen: dict = {}
    monkeypatch.setattr(ap, "_fan_out_and_synthesize",
                        lambda prompt, units, fn, topics, sid, noun:
                        seen.update(noun=noun, fn=fn) or iter(()))
    list(ap.stream_analysis_planned("p", "/cwd"))
    assert seen["noun"] == "file group"
    assert seen["fn"] is ap._explore_files_group
