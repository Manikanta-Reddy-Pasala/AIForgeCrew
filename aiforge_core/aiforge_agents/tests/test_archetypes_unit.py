"""Unit tests for Doer, Validator, Tester, Architect, Learner."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import aiforge_core.aiforge_agents.archetypes  # noqa: F401
from aiforge_core.aiforge_agents import registry
from aiforge_core.aiforge_agents.orchestrator.run_ticket import (
    _filter_plan_targets,
)


# ─────────── Plan-target filter ────────────────────────────────────────

def test_filter_plan_targets_drops_invented_read() -> None:
    allowed = ["src/main/java/A.java", "src/main/java/B.java"]
    plan = {"steps": [
        {"id": 1, "action": "read", "target": "src/main/java/A.java"},
        {"id": 2, "action": "read", "target": "src/main/java/INVENTED.java"},
        {"id": 3, "action": "create", "target": "src/main/java/C.java"},
    ]}
    out, dropped = _filter_plan_targets(plan, allowed)
    targets = [s["target"] for s in out["steps"]]
    assert "src/main/java/A.java" in targets
    assert "src/main/java/INVENTED.java" not in targets
    assert "src/main/java/C.java" in targets   # create is exempt
    assert len(dropped) == 1
    assert dropped[0]["target"] == "src/main/java/INVENTED.java"


def test_filter_plan_targets_basename_rewrite() -> None:
    """Ends-with-basename match rewrites to canonical allowed path."""
    allowed = ["src/main/java/com/x/Y.java"]
    plan = {"steps": [
        {"id": 1, "action": "read", "target": "Y.java"},
    ]}
    out, dropped = _filter_plan_targets(plan, allowed)
    assert out["steps"][0]["target"] == "src/main/java/com/x/Y.java"
    assert dropped == []


def test_filter_plan_targets_empty_allowed_no_op() -> None:
    plan = {"steps": [{"id": 1, "action": "read", "target": "x.java"}]}
    out, dropped = _filter_plan_targets(plan, [])
    assert out == plan
    assert dropped == []


# ─────────── Doer apply path (P2) ──────────────────────────────────────

def test_doer_git_apply_dirty_worktree_refuses(tmp_path) -> None:
    from aiforge_core.aiforge_agents.archetypes.doer import _git_apply_diff
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=str(tmp_path))
    subprocess.run(["git", "config", "user.name", "a"], cwd=str(tmp_path))
    (tmp_path / "f.txt").write_text("hello\n")
    # Don't commit → dirty tree
    applied, branch, err = _git_apply_diff(
        repo_path=str(tmp_path), ticket_id="TKT", udiff="x",
    )
    assert applied is False
    assert err.startswith("dirty_worktree")


def test_doer_git_apply_creates_branch(tmp_path) -> None:
    from aiforge_core.aiforge_agents.archetypes.doer import _git_apply_diff
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=str(tmp_path))
    subprocess.run(["git", "config", "user.name", "a"], cwd=str(tmp_path))
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), check=True)

    udiff = (
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1 +1,2 @@\n"
        " hello\n"
        "+world\n"
    )
    applied, branch, err = _git_apply_diff(
        repo_path=str(tmp_path), ticket_id="TKT-9", udiff=udiff,
    )
    assert applied is True, f"err={err}"
    assert branch == "aiforge/TKT-9"
    head = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=str(tmp_path), capture_output=True, text=True,
    ).stdout
    assert "TKT-9" in head


# ─────────── Skills ──────────────────────────────────────────────────

def test_top_skills_for_dbless_returns_empty() -> None:
    from aiforge_core.aiforge_agents.learner import online
    # Force connection failure path — bogus DSN
    import os
    saved = os.environ.get("AIFORGE_DSN", "")
    os.environ["AIFORGE_DSN"] = "postgresql://nouser@localhost:1/none"
    online._DSN = os.environ["AIFORGE_DSN"]  # type: ignore[attr-defined]
    try:
        out = online.top_skills_for(repo="r", task_class="x", k=3)
        assert out == []
    finally:
        if saved:
            os.environ["AIFORGE_DSN"] = saved
            online._DSN = saved  # type: ignore[attr-defined]


def test_guess_task_class_keywords() -> None:
    from aiforge_core.aiforge_agents.orchestrator.run_ticket import (
        _guess_task_class,
    )
    assert _guess_task_class("Add README.md", "") == "readme"
    assert _guess_task_class("Add CRUD APIs for ledger", "") == "feature"
    assert _guess_task_class("Add JWT login flow", "") == "auth"
    assert _guess_task_class("Random title", "") == "unknown"


# ─────────── Prompt helpers (compaction + failure-recall) ─────────────

def test_compact_short_text_unchanged() -> None:
    from aiforge_core.aiforge_agents.runtime import prompt_helpers as ph
    assert ph.compact("hello world") == "hello world"


def test_compact_long_text_keeps_head_tail() -> None:
    from aiforge_core.aiforge_agents.runtime import prompt_helpers as ph
    text = "A" * 2000 + "B" * 5000 + "C" * 2000
    out = ph.compact(text, head=100, tail=100)
    assert out.startswith("A" * 100)
    assert out.endswith("C" * 100)
    assert "elided" in out


def test_render_failures_block_empty() -> None:
    from aiforge_core.aiforge_agents.runtime import prompt_helpers as ph
    assert ph.render_failures_block(None) == ""
    assert ph.render_failures_block([]) == ""


def test_render_failures_block_renders_lessons() -> None:
    from aiforge_core.aiforge_agents.runtime import prompt_helpers as ph
    out = ph.render_failures_block([
        {"mode": "F-001", "evidence": "com.bogus.X",
         "lesson": "use stdlib only", "seen_count": 3},
    ])
    assert "F-001" in out
    assert "×3" in out
    assert "com.bogus.X" in out
    assert "stdlib only" in out


def test_render_failures_block_custom_header() -> None:
    from aiforge_core.aiforge_agents.runtime import prompt_helpers as ph
    out = ph.render_failures_block(
        [{"mode": "x", "evidence": "y", "lesson": "z"}],
        header="# CUSTOM",
    )
    assert out.startswith("# CUSTOM")


# ─────────── web_fetch (URL auto-learn) ───────────────────────────────

def test_extract_urls_finds_http_https() -> None:
    from aiforge_core.aiforge_agents.runtime import web_fetch as wf
    urls = wf.extract_urls(
        "see https://example.com/docs and http://api.dev/v1, plus "
        "an irrelevant ftp://nope and a duplicate https://example.com/docs."
    )
    assert urls == ["https://example.com/docs", "http://api.dev/v1"]


def test_extract_urls_strips_trailing_punct() -> None:
    from aiforge_core.aiforge_agents.runtime import web_fetch as wf
    assert wf.extract_urls("see https://example.com/x.") == [
        "https://example.com/x",
    ]


def test_extract_urls_empty_input() -> None:
    from aiforge_core.aiforge_agents.runtime import web_fetch as wf
    assert wf.extract_urls("") == []
    assert wf.extract_urls("no urls here at all") == []


def test_fetch_and_summarise_no_urls_returns_empty() -> None:
    from aiforge_core.aiforge_agents.runtime import web_fetch as wf
    assert wf.fetch_and_summarise([]) == ""


# ─────────── Doer ─────────────────────────────────────────────────────

def test_doer_skips_when_no_write_step() -> None:
    d = registry.build("doer")
    out = d.run(ctx={"plan": {"steps": [{"action": "read"}]}})
    assert out["skipped"] is True
    assert out["reason"] == "no_write_step"


def test_doer_calls_llm_and_runs_detectors(tmp_path) -> None:
    # Fake target file
    (tmp_path / "src" / "X.java").parent.mkdir(parents=True)
    (tmp_path / "src" / "X.java").write_text("class X {}\n")

    fake_diff = (
        "```diff\n"
        "--- a/src/X.java\n"
        "+++ b/src/X.java\n"
        "@@ -1,1 +1,2 @@\n"
        " class X {}\n"
        "+// added\n"
        "```\n"
    )
    d = registry.build("doer")
    with patch(
        "aiforge_core.aiforge_agents.runtime.llm_client.call_text",
        return_value=fake_diff,
    ):
        out = d.run(ctx={
            "plan": {"steps": [{
                "id": 1, "action": "edit", "target": "src/X.java",
            }]},
            "repo_path": str(tmp_path),
            "ticket_id": "TKT-TEST",
            "repo": "x",
        })
    assert out["artifact_type"] == "doer_outcome"
    assert out["target"] == "src/X.java"
    assert "udiff" in out
    assert isinstance(out["problems"], list)


# ─────────── Validator ────────────────────────────────────────────────

def test_validator_approves_clean_diff() -> None:
    v = registry.build("validator")
    out = v.run(ctx={"doer_outcome": {
        "udiff": "diff content",
        "problems": [],
    }})
    assert out["decision"] == "approve"


def test_validator_blocks_on_hallucinated_import() -> None:
    v = registry.build("validator")
    out = v.run(ctx={"doer_outcome": {
        "udiff": "diff content",
        "problems": [{"mode": "F-001", "evidence": "com.bogus"}],
    }})
    assert out["decision"] == "block"
    assert "no_hallucinated_imports" in out["reason"]


def test_validator_skip_when_doer_skipped() -> None:
    v = registry.build("validator")
    out = v.run(ctx={"doer_outcome": {"skipped": True, "reason": "x"}})
    assert out["decision"] == "skip"


# ─────────── Tester ───────────────────────────────────────────────────

def test_tester_returns_test_specs() -> None:
    fake = {
        "tests": [
            {"name": "t1", "target_class": "X", "target_method": "foo",
             "scenario": "happy", "expected": "ok", "framework": "junit5"},
        ],
        "coverage_target": 0.9,
    }
    t = registry.build("tester")
    with patch(
        "aiforge_core.aiforge_agents.runtime.llm_client.call_json",
        return_value=fake,
    ):
        out = t.run(ctx={"understanding": {}, "plan": {}})
    assert len(out["tests"]) == 1
    assert out["coverage_target"] == 0.9


def test_tester_handles_invalid_json() -> None:
    t = registry.build("tester")
    with patch(
        "aiforge_core.aiforge_agents.runtime.llm_client.call_json",
        return_value=None,
    ):
        out = t.run(ctx={})
    assert out["tests"] == []
    assert out["error"] == "llm_invalid_json"


# ─────────── Architect ────────────────────────────────────────────────

def test_architect_request_changes_when_validation_blocked() -> None:
    a = registry.build("architect")
    out = a.run(ctx={
        "validation": {"decision": "block", "reason": "missing_imports"},
        "doer_outcome": {"udiff": "x"},
    })
    assert out["decision"] == "request_changes"
    assert "validation blocked" in out["comments"][0]


def test_architect_calls_llm_when_validated() -> None:
    fake = {
        "decision": "approve",
        "comments": ["lgtm"],
        "mr_title": "feat: pagination",
        "mr_body": "## Summary\nadd page+size",
    }
    a = registry.build("architect")
    with patch(
        "aiforge_core.aiforge_agents.runtime.llm_client.call_json",
        return_value=fake,
    ):
        out = a.run(ctx={
            "validation": {"decision": "approve"},
            "doer_outcome": {"udiff": "diff"},
            "understanding": {}, "plan": {},
        })
    assert out["decision"] == "approve"
    assert out["mr_title"] == "feat: pagination"


# ─────────── Learner ──────────────────────────────────────────────────

def test_learner_writes_episodic_and_procedural() -> None:
    L = registry.build("learner")
    with patch(
        "aiforge_core.aiforge_agents.learner.online.record_episodic",
    ) as rec_e, patch(
        "aiforge_core.aiforge_agents.learner.online.update_procedural",
    ) as upd_p:
        out = L.run(ctx={
            "ticket_id": "TKT-X",
            "repo": "r",
            "plan": {"steps": [{"action": "read"}, {"action": "edit"}]},
            "verifier_verdict": {"verdict": "pass"},
            "grounding": {"resolved": True, "unresolved_refs": []},
            "doer_outcome": {"target": "src/main/feature/X.java",
                             "problems": []},
            "validation": {"decision": "approve"},
            "review": {"decision": "approve"},
        })
    assert out["outcome"] == "success"
    assert out["task_class"] == "feature"
    assert out["tool_sequence"] == ["read", "edit"]
    rec_e.assert_called_once()
    upd_p.assert_called_once()


def test_learner_outcome_blocked_when_grounding_fails() -> None:
    L = registry.build("learner")
    with patch(
        "aiforge_core.aiforge_agents.learner.online.record_episodic",
    ), patch(
        "aiforge_core.aiforge_agents.learner.online.update_procedural",
    ):
        out = L.run(ctx={
            "ticket_id": "TKT-Y", "repo": "r",
            "plan": {"steps": [{"action": "edit"}]},
            "verifier_verdict": {"verdict": "pass"},
            "grounding": {"resolved": False,
                          "unresolved_refs": [{"target": "x"}]},
            "doer_outcome": {"target": "a/b/c.java", "problems": []},
            "validation": {"decision": "skip"},
        })
    assert out["outcome"] == "blocked"
