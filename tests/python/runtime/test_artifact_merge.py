"""The nightly library merge — duplicate rules / skills / workflows.

The library grows by accretion: every writer keys on a slug, so "run tests
first" and "always run the tests" are two files saying one thing, and nothing
in the system ever noticed. These pin the sweep that folds them, and — as much
as the merging itself — the four things that make it safe to run unattended:
it never touches a bundled or repo-local artifact, it archives before it
deletes, it refuses a merge that lost content, and it never asks the model
about the same cluster twice.

Hermetic: the config dir is a tmp_path and the model is a stub. The one test
that cares WHICH model path is used asserts the role, because being under the
operator's rate ceiling is the difference between a background sweep and a
background sweep that out-shouts interactive chat.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import artifact_merge as am


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for var in ("AIFORGE_RULES_DIR", "AIFORGE_SKILLS_DIR",
                "AIFORGE_WORKFLOWS_DIR", "AIFORGE_MERGE_SIMILARITY",
                "AIFORGE_MERGE_MAX_PER_RUN", "AIFORGE_MERGE_MIN_COVERAGE",
                "AIFORGE_ARTIFACT_MERGE", "AIFORGE_JOBS_DISABLE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _rule(name: str, body: str, **kw) -> dict:
    from aiforge_core.runtime import repo_rules
    return repo_rules.write_rule(name, body, **kw)


def _skill(name: str, desc: str, body: str) -> dict:
    from aiforge_core.runtime import skills
    return skills.write_skill(name, desc, body)


class _Merged:
    """What the model would have returned."""

    def __init__(self, name, body, description="", triggers=()):
        self.name = name
        self.body = body
        self.description = description
        self.triggers = list(triggers)


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace the structured call and RECORD it, so a test can assert both
    what came back and which role paid for it."""
    calls: list[dict] = []

    def _fake(role, messages, response_model, **kw):
        calls.append({"role": role, "messages": messages, "kw": kw})
        return _fake.reply

    _fake.reply = None
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete",
                        _fake)
    return calls, _fake


# ── clustering: deterministic, and it does not over-reach ───────────────────

_TESTS_A = ("Always run the unit tests before pushing a branch to origin. "
            "A red suite is never pushed, not even behind a flag.")
_TESTS_B = ("Run the unit tests before you push a branch. Never push a red "
            "suite to origin, flag or no flag.")
_UNRELATED = ("Database migrations are written forward-only. Never edit a "
              "migration that has already shipped to production.")


def test_two_spellings_of_one_rule_cluster():
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    clusters = am.find_clusters("rules")
    assert len(clusters) == 1
    assert {i.name for i in clusters[0]} == {"run tests before push",
                                            "always run the tests"}


def test_an_unrelated_rule_is_left_out():
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    _rule("forward only migrations", _UNRELATED)
    clusters = am.find_clusters("rules")
    assert len(clusters) == 1
    assert "forward only migrations" not in {i.name for i in clusters[0]}


def test_a_single_artifact_is_never_a_cluster():
    _rule("run tests before push", _TESTS_A)
    assert am.find_clusters("rules") == []


def test_similarity_is_symmetric_and_bounded():
    _rule("a", _TESTS_A)
    _rule("b", _TESTS_B)
    x, y = am.load("rules")
    assert 0.0 <= am.similarity(x, y) <= 1.0
    assert am.similarity(x, y) == am.similarity(y, x)


# ── what it refuses to touch ────────────────────────────────────────────────

def test_a_repo_local_skill_is_not_mergeable(tmp_path):
    """It belongs to that checkout. Folding it into a global artifact would
    move an instruction between scopes without anyone asking."""
    repo_skill = tmp_path / "repo" / ".aiforge" / "skills" / "x"
    repo_skill.mkdir(parents=True)
    (repo_skill / "SKILL.md").write_text("---\nname: x\n---\nbody\n")
    item = am._Item("skills", "x", "", (), "body",
                    str(repo_skill / "SKILL.md"))
    assert am.mergeable(item) is False


def test_a_bundled_default_is_not_mergeable():
    """It is re-seeded on upgrade, so a merge would be undone and then redone
    every release."""
    names = am._builtin_names("rules")
    if not names:
        pytest.skip("no bundled rules in this build")
    src = am._global_root("rules") / sorted(names)[0]
    item = am._Item("rules", "bundled", "", (), "body", str(src))
    assert am.mergeable(item) is False


def test_a_workflow_with_scripts_is_not_mergeable(monkeypatch):
    """The merged text may still call them and the writer cannot know which,
    so the sweep leaves the whole workflow alone rather than orphan a script."""
    monkeypatch.setattr("aiforge_core.runtime.workflows.scripts_for",
                        lambda _src: ["deploy.sh"])
    root = am._global_root("workflows")
    item = am._Item("workflows", "w", "", (), "body",
                    str(root / "w" / "WORKFLOW.md"))
    assert am.mergeable(item) is False


# ── the merge ───────────────────────────────────────────────────────────────

def test_a_merge_writes_one_artifact_and_archives_the_others(stub_llm):
    calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    fake.reply = _Merged("run tests before push", _TESTS_A + " " + _TESTS_B,
                         description="test before pushing")

    out = am.run(["rules"])

    assert out["merged"] == 1, out
    names = {i.name for i in am.load("rules")}
    assert names == {"run tests before push"}
    archived = list((am.archive_dir("rules")).glob("*.md"))
    assert len(archived) == 2, archived


def test_the_merged_body_records_where_it_came_from(stub_llm):
    _calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    fake.reply = _Merged("merged rule", _TESTS_A + " " + _TESTS_B)
    am.run(["rules"])
    body = next(i.body for i in am.load("rules"))
    assert "merged from rules:" in body
    assert "always run the tests" in body


def test_a_rule_keeps_its_scope_through_the_merge(stub_llm):
    """Two rules scoped to *.py must not come back applying to every turn —
    that is a bigger behaviour change than the duplication was."""
    _calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A, globs=["*.py"], always=False)
    _rule("always run the tests", _TESTS_B, globs=["*.py"], always=False)
    fake.reply = _Merged("merged rule", _TESTS_A + " " + _TESTS_B)
    am.run(["rules"])
    merged = next(iter(am.load("rules")))
    assert merged.extra
    extra = dict(merged.extra)
    assert extra["globs"] == ("*.py",)
    assert extra["always"] is False


def test_the_merge_runs_on_the_shared_client_as_learner(stub_llm):
    """Role is what puts this pass under the operator's rate ceiling and into
    the request meter — an uncapped background sweep is the bug that matters
    more than the duplicates it is cleaning up."""
    calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    fake.reply = _Merged("merged", _TESTS_A + " " + _TESTS_B)
    am.run(["rules"])
    assert len(calls) == 1
    assert calls[0]["role"] == "learner"
    assert calls[0]["kw"].get("temperature") == 0.0


# ── the guards around the merge ─────────────────────────────────────────────

def test_a_summary_instead_of_a_merge_is_refused(stub_llm):
    """The failure mode is not a wrong merge, it is a LOSSY one: five rules in,
    a tidy paragraph out, three instructions only in the archive."""
    _calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    fake.reply = _Merged("merged", "Run tests.")
    out = am.run(["rules"])
    assert out["merged"] == 0
    assert out["rows"][0]["action"] == "skipped"
    assert {i.name for i in am.load("rules")} == {"run tests before push",
                                                 "always run the tests"}


def test_an_empty_model_reply_is_refused():
    cluster = [am._Item("rules", "a", "", (), _TESTS_A, "s"),
               am._Item("rules", "b", "", (), _TESTS_B, "s")]
    assert am.validate_merge(_Merged("", "body"), cluster)
    assert am.validate_merge(_Merged("n", ""), cluster)


def test_a_model_outage_changes_nothing(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("502 from the gateway")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete",
                        _boom)
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    out = am.run(["rules"])
    assert out["rows"][0]["action"] == "error"
    assert len(am.load("rules")) == 2


# ── never re-decide, never overspend ────────────────────────────────────────

def test_a_skipped_cluster_is_not_re_asked_next_pass(stub_llm):
    """Otherwise every night buys another model call to reach the same verdict
    on artifacts nobody touched."""
    calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    fake.reply = _Merged("merged", "Run tests.")     # refused: too short
    am.run(["rules"])
    am.run(["rules"])
    assert len(calls) == 1


def test_editing_a_member_makes_the_cluster_pending_again(stub_llm):
    calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    fake.reply = _Merged("merged", "Run tests.")
    am.run(["rules"])
    _rule("always run the tests", _TESTS_B + " Also run the linter.")
    am.run(["rules"])
    assert len(calls) == 2


def test_the_pass_stops_at_its_budget(stub_llm):
    calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    _skill("deploy the service", "deploy", "Deploy by tagging a release and "
           "waiting for the rollout to report healthy.")
    _skill("deploying the service", "deploy", "Deploy by tagging a release, "
           "then wait until the rollout reports healthy.")
    fake.reply = _Merged("merged", _TESTS_A + " " + _TESTS_B)
    out = am.run(limit=1)
    assert len(calls) == 1
    assert len(out["rows"]) == 1


def test_a_dry_run_touches_nothing(stub_llm):
    calls, _fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    out = am.run(["rules"], dry_run=True)
    assert out["rows"][0]["action"] == "would_merge"
    assert calls == []
    assert len(am.load("rules")) == 2
    # and it must not have recorded a decision — a dry run that marked the
    # cluster seen would hide it from the pass that actually merges.
    assert am.load_state().get("decided") in (None, {})


def test_the_switch_turns_the_whole_sweep_off(monkeypatch, stub_llm):
    calls, _fake = stub_llm
    monkeypatch.setenv("AIFORGE_ARTIFACT_MERGE", "0")
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    out = am.run(["rules"])
    assert out["ok"] is False
    assert calls == []


def test_jobs_disable_also_stops_it(monkeypatch):
    monkeypatch.setenv("AIFORGE_JOBS_DISABLE", "1")
    assert am.enabled() is False


# ── the scheduler ───────────────────────────────────────────────────────────

def test_the_nightly_pass_is_registered(monkeypatch):
    """Pin the WIRING: a sweep nothing calls has folded nothing."""
    from aiforge_core.api import api as _api

    registered: list[dict] = []

    class _Pd:
        @staticmethod
        def register(name, fn, **kw):
            registered.append({"name": name, "fn": fn, "kw": kw})

    _api._register_artifact_merge(_Pd)
    assert [r["name"] for r in registered] == ["artifact-merge"]
    assert registered[0]["fn"] is am.scheduled_pass
    assert registered[0]["kw"]["at_hour"] == 4


def test_the_nightly_pass_is_not_registered_when_disabled(monkeypatch):
    from aiforge_core.api import api as _api
    monkeypatch.setenv("AIFORGE_ARTIFACT_MERGE", "0")
    registered: list = []

    class _Pd:
        @staticmethod
        def register(name, fn, **kw):
            registered.append(name)

    _api._register_artifact_merge(_Pd)
    assert registered == []


def test_the_scheduled_pass_never_raises(monkeypatch):
    """It runs inside the periodic loop; an exception out of here would take
    the loop's task with it."""
    monkeypatch.setattr(am, "run", lambda *_a, **_kw: (_ for _ in ()).throw(
        RuntimeError("boom")))
    out = am.scheduled_pass()
    assert out["ok"] is False and out["rows"] == []


def test_merging_an_always_on_rule_widens_rather_than_narrows(stub_llm):
    """One member applies everywhere, the other only to *.py. The merged rule
    must keep applying everywhere — a merge that narrows scope silently stops
    telling the agent something it used to be told."""
    _calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A, globs=["*.py"], always=False)
    _rule("always run the tests", _TESTS_B, always=True)
    fake.reply = _Merged("merged rule", _TESTS_A + " " + _TESTS_B)
    am.run(["rules"])
    extra = dict(next(iter(am.load("rules"))).extra)
    assert extra["always"] is True
    assert extra["globs"] == ("*.py",)


def test_the_sweep_counts_against_the_background_ceiling_not_chat():
    """role=learner is what keeps a nightly sweep at compaction_rpm (5/min by
    default) instead of competing with interactive chat at chat_rpm. Pinned
    because the role is a one-word change with no visible symptom."""
    from aiforge_core.llm import rate_limiter
    assert rate_limiter._category("learner") == "compaction"


# ── the API surface ─────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from aiforge_core.api.api import app
    return TestClient(app)


def test_the_routes_answer_and_merge_is_not_a_kind(client, stub_llm):
    """`/api/library/{kind}` sits at the same depth as `/api/library/merge`,
    so this also pins that the literal path wins over the kind parameter."""
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)

    preview = client.get("/api/library/merge/preview").json()
    assert [c["members"] for c in preview["clusters"]
            if c["kind"] == "rules"], preview

    dry = client.post("/api/library/merge/run", json={"dry_run": True}).json()
    assert dry["rows"] and dry["rows"][0]["action"] == "would_merge"

    # No body at all is a valid request — the nightly defaults.
    assert client.post("/api/library/merge/run").status_code == 200
    assert client.post("/api/library/merge/run",
                       json={"kinds": ["nope"]}).status_code == 400
    assert client.get("/api/library/merge/report").json()["enabled"] is True


# ── what a failed or oversized merge must not cost ──────────────────────────

def test_a_failed_write_restores_the_members(stub_llm, monkeypatch):
    """The sweep deletes BEFORE it writes — the merged artifact often reuses a
    member's name — so a write that fails is the one moment the library is
    missing them. Losing three rules to a full disk is worse than the
    duplication ever was."""
    _calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    fake.reply = _Merged("merged rule", _TESTS_A + " " + _TESTS_B)
    monkeypatch.setattr(am, "_WRITERS", dict(
        am._WRITERS, rules=lambda *_a, **_kw: {"ok": False, "error": "disk full"}))

    out = am.run(["rules"])

    assert out["rows"][0]["action"] == "error"
    assert {i.name for i in am.load("rules")} == {"run tests before push",
                                                 "always run the tests"}


def test_a_writer_that_raises_also_restores(stub_llm, monkeypatch):
    _calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    fake.reply = _Merged("merged rule", _TESTS_A + " " + _TESTS_B)

    def _boom(*_a, **_kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(am, "_WRITERS", dict(am._WRITERS, rules=_boom))
    am.run(["rules"])
    assert len(am.load("rules")) == 2


def test_an_oversized_cluster_is_skipped_not_truncated(stub_llm, monkeypatch):
    """Truncating the inputs is the worst option: the model merges what it saw,
    the coverage check fails against what it did not, and the cluster is
    refused forever."""
    calls, _fake = stub_llm
    monkeypatch.setenv("AIFORGE_MERGE_MAX_CHARS", "1000")
    _rule("run tests before push", _TESTS_A + " x" * 400)
    _rule("always run the tests", _TESTS_B + " x" * 400)
    out = am.run(["rules"])
    assert out["rows"][0]["action"] == "skipped"
    assert "char" in out["rows"][0]["reason"]
    assert calls == []


def test_a_dry_run_shows_every_cluster_not_just_the_budget(stub_llm):
    """The budget is a COST ceiling. A dry run spends nothing, so capping its
    report would hide work from the operator it exists to inform."""
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    _skill("deploy the service", "deploy", "Deploy by tagging a release and "
           "waiting for the rollout to report healthy.")
    _skill("deploying the service", "deploy", "Deploy by tagging a release, "
           "then wait until the rollout reports healthy.")
    out = am.run(dry_run=True, limit=1)
    assert len(out["rows"]) == 2, out


def test_force_re_considers_a_cluster_already_decided(stub_llm):
    calls, fake = stub_llm
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    fake.reply = _Merged("merged", "Run tests.")     # refused: lossy
    am.run(["rules"])
    am.run(["rules"])                                # not re-asked
    assert len(calls) == 1
    am.run(["rules"], force=True)                    # operator asks again
    assert len(calls) == 2


def test_two_passes_cannot_run_at_once(stub_llm, monkeypatch):
    """The nightly sweep and an operator hitting Run would otherwise
    archive-and-delete the same cluster twice."""
    _rule("run tests before push", _TESTS_A)
    _rule("always run the tests", _TESTS_B)
    assert am._LOCK.acquire(blocking=False)
    try:
        out = am.run(["rules"])
    finally:
        am._LOCK.release()
    assert out["ok"] is False and "already running" in out["error"]


def test_a_typod_hour_is_clamped(monkeypatch):
    """periodic reads at_hour as [0-23]; a 25 would give a task whose next run
    never arrives — a scheduler that silently never fires."""
    from aiforge_core.api import api as _api
    monkeypatch.setenv("AIFORGE_MERGE_HOUR", "25")
    seen = []

    class _Pd:
        @staticmethod
        def register(name, fn, **kw):
            seen.append(kw["at_hour"])

    _api._register_artifact_merge(_Pd)
    assert seen == [23]
