"""Skills and workflows now capture themselves from chat, like rules do.

Before this, ``learn_skill`` / ``learn_workflow`` fired only when the AGENT
remembered to call them, so the library grew for corrections and never for
procedures. The capture is deliberately stingy — it declines an ordinary turn
before spending a model call, and it refuses to write something the library
already has.
"""
from __future__ import annotations

import types

import pytest

from aiforge_core.runtime import artifact_capture as ac


def _steps(n: int) -> list[dict]:
    return [{"type": "tool", "name": "run_command", "text": f"step {i}"}
            for i in range(n)]


def _proposal(kind="skill", name="deploy the sidecar", body="1. do x\n2. do y",
              description="how to deploy it", triggers=("deploy",)):
    return types.SimpleNamespace(kind=kind, name=name, description=description,
                                 triggers=list(triggers), body=body)


# ── the gate, before any model call ──────────────────────────────────────

def test_an_ordinary_question_is_not_a_procedure():
    assert ac.worth_capturing("what does this function do?", _steps(1)) is False


def test_a_turn_that_did_real_work_qualifies():
    assert ac.worth_capturing("fix the failing build", _steps(5)) is True


@pytest.mark.parametrize("ask", [
    "save this as a skill",
    "remember this workflow for next time",
    "turn that into a runbook",
])
def test_an_explicit_ask_always_qualifies(ask):
    """However small the turn — the user asked."""
    assert ac.worth_capturing(ask, []) is True


def test_it_declines_without_calling_the_model(monkeypatch):
    monkeypatch.setattr(ac, "_propose",
                        lambda *a, **k: pytest.fail("must not call the model"))
    out = ac.capture_from_chat(prompt="what is this?", final_text="it is x",
                               steps=_steps(1), cwd="/repo")
    assert out["captured"] is False
    assert out["skipped"] == "not a procedure"


# ── what it writes, and what it refuses to write ─────────────────────────

def test_a_reusable_procedure_is_saved(monkeypatch):
    written: dict = {}
    monkeypatch.setattr(ac, "_propose", lambda *a, **k: _proposal())
    monkeypatch.setattr(ac, "existing_match", lambda *a, **k: "")
    monkeypatch.setattr(ac, "_write",
                        lambda kind, prop, cwd: written.update(
                            kind=kind, name=prop.name) or {"ok": True, "path": "/p"})
    out = ac.capture_from_chat(prompt="deploy it", final_text="done",
                               steps=_steps(4), cwd="/repo")
    assert out["captured"] is True
    assert written == {"kind": "skill", "name": "deploy the sidecar"}


def test_nothing_reusable_writes_nothing(monkeypatch):
    monkeypatch.setattr(ac, "_propose", lambda *a, **k: _proposal(kind="none"))
    monkeypatch.setattr(ac, "_write",
                        lambda *a, **k: pytest.fail("must not write"))
    out = ac.capture_from_chat(prompt="deploy it", final_text="done",
                               steps=_steps(4), cwd="/repo")
    assert out["captured"] is False
    assert out["skipped"] == "nothing reusable"


def test_a_duplicate_is_not_written_at_all(monkeypatch):
    """Dedupe at ADMISSION: cheaper than letting the nightly sweep merge it
    back out, and it uses the same similarity the sweep uses."""
    monkeypatch.setattr(ac, "_propose", lambda *a, **k: _proposal())
    monkeypatch.setattr(ac, "existing_match", lambda *a, **k: "deploying sidecars")
    monkeypatch.setattr(ac, "_write",
                        lambda *a, **k: pytest.fail("must not write a duplicate"))
    out = ac.capture_from_chat(prompt="deploy it", final_text="done",
                               steps=_steps(4), cwd="/repo")
    assert out["captured"] is False
    assert out["duplicate_of"] == "deploying sidecars"


def test_a_model_outage_is_not_a_failed_turn(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("model down")
    monkeypatch.setattr(ac, "_propose", _boom)
    out = ac.capture_from_chat(prompt="deploy it", final_text="done",
                               steps=_steps(4), cwd="/repo")
    assert out["captured"] is False
    assert "model down" in out["error"]


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_ARTIFACT_CAPTURE", "0")
    monkeypatch.setattr(ac, "_propose",
                        lambda *a, **k: pytest.fail("must not call the model"))
    out = ac.capture_from_chat(prompt="save this as a skill", final_text="x",
                               steps=_steps(9), cwd="/repo")
    assert out["skipped"] == "disabled"


# ── the dedupe check itself ──────────────────────────────────────────────

def test_existing_match_finds_the_near_duplicate(monkeypatch):
    from aiforge_core.runtime import artifact_merge as am
    body = ("Run the full test suite before pushing; fix failures first and "
            "never push a red suite to main.")
    have = am.item_from("skills", "testing before a push", "run tests", [], body)
    monkeypatch.setattr(am, "load", lambda kind: [have] if kind == "skills" else [])
    assert ac.existing_match("skill", "test before push", "run the tests", [],
                             body) == "testing before a push"


def test_existing_match_is_silent_on_something_new(monkeypatch):
    from aiforge_core.runtime import artifact_merge as am
    have = am.item_from("skills", "import ordering", "sort imports", [],
                        "Group imports stdlib, third-party, local.")
    monkeypatch.setattr(am, "load", lambda kind: [have] if kind == "skills" else [])
    assert ac.existing_match("skill", "deploy the sidecar", "deploy it", [],
                             "1. build the image\n2. push it\n3. roll out") == ""


# ── the parts the mocked tests above never execute ───────────────────────

def test_a_malformed_switch_falls_back_instead_of_raising(monkeypatch):
    """These read operator-set env vars. A typo in a unit file must not take
    the post-turn thread down — it should behave as if unset."""
    monkeypatch.setenv("AIFORGE_ARTIFACT_CAPTURE_MIN_STEPS", "three")
    monkeypatch.setenv("AIFORGE_ARTIFACT_CAPTURE_DEDUPE", "high")
    assert ac._env_int("AIFORGE_ARTIFACT_CAPTURE_MIN_STEPS", 3) == 3
    assert ac._env_float("AIFORGE_ARTIFACT_CAPTURE_DEDUPE", 0.72) == 0.72
    # and a negative count cannot turn the gate off entirely
    monkeypatch.setenv("AIFORGE_ARTIFACT_CAPTURE_MIN_STEPS", "-5")
    assert ac._env_int("AIFORGE_ARTIFACT_CAPTURE_MIN_STEPS", 3) == 0


def test_the_proposal_schema_declines_by_default():
    """Every field defaults, so a model that answers with `{}` yields
    kind='none' — a refusal, not a half-built artifact."""
    proposal = ac._proposal_model()
    empty = proposal()
    assert empty.kind == "none"
    assert empty.name == ""
    assert empty.body == ""
    assert empty.triggers == []
    filled = proposal(kind="workflow", name="cut a release", body="1. tag")
    assert (filled.kind, filled.name) == ("workflow", "cut a release")


def test_the_digest_carries_the_ask_the_actions_and_the_answer():
    """What the model is shown decides what it can propose: drop the tool
    steps and it can only summarise the reply."""
    digest = ac._turn_digest("how do we deploy?", "Deployed.",
                             [{"type": "tool", "name": "run_command",
                               "text": "kubectl rollout status"},
                              {"type": "thought", "text": "ignored"},
                              "a malformed step"])   # survives junk in the list
    assert "USER ASKED" in digest
    assert "how do we deploy?" in digest
    assert "run_command" in digest
    assert "kubectl rollout status" in digest
    assert "FINAL ANSWER" in digest
    assert "Deployed." in digest
    assert "ignored" not in digest          # only tool steps are actions


def test_a_turn_with_no_tool_steps_still_produces_a_digest():
    digest = ac._turn_digest("explain X", "X is Y", [])
    assert "(no tool steps)" in digest


def test_a_skill_and_a_workflow_go_to_different_writers(monkeypatch):
    """The kind decides the library it lands in; routing it wrong files a
    procedure where nothing looks for it."""
    from aiforge_core.runtime import skills, workflows
    seen: dict[str, dict] = {}
    monkeypatch.setattr(skills, "write_skill",
                        lambda **kw: seen.setdefault("skill", kw) or {"ok": True})
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda **kw: seen.setdefault("workflow", kw) or {"ok": True})

    ac._write("skill", _proposal(kind="skill"), cwd="/repo")
    assert "skill" in seen
    assert "workflow" not in seen
    assert seen["skill"]["name"] == "deploy the sidecar"
    assert seen["skill"]["scope"] == "global"

    ac._write("workflow", _proposal(kind="workflow", name="cut a release"),
              cwd="/repo")
    assert seen["workflow"]["name"] == "cut a release"


def test_a_saved_capture_reports_where_it_landed(monkeypatch):
    """End-to-end with only the model and the filesystem stubbed, so the real
    _write and the success path both run."""
    from aiforge_core.runtime import skills
    monkeypatch.delenv("AIFORGE_ARTIFACT_CAPTURE", raising=False)
    monkeypatch.setattr(ac, "_propose", lambda *a, **k: _proposal())
    monkeypatch.setattr(ac, "existing_match", lambda *a, **k: "")
    monkeypatch.setattr(skills, "write_skill",
                        lambda **kw: {"ok": True, "name": kw["name"],
                                      "path": "/home/u/.aiforge/skills/x/SKILL.md"})
    out = ac.capture_from_chat(prompt="do the thing", final_text="done",
                               steps=_steps(4), cwd="/repo", session_id=7)
    assert out == {"ok": True, "captured": True, "kind": "skill",
                   "name": "deploy the sidecar",
                   "path": "/home/u/.aiforge/skills/x/SKILL.md"}


def test_the_proposal_is_asked_on_the_learner_role(monkeypatch):
    """``learner`` is load-bearing, not decoration: it is the unattended
    maintenance role, so this call sits under the operator's BACKGROUND rate
    ceiling and appears in the request meter like every other background
    sender. Asking as ``chat`` would quietly spend the interactive budget of
    whoever happened to be typing."""
    from aiforge_core.llm import structured as st
    seen: dict = {}

    def fake(role, messages, model, **kw):
        seen.update(role=role, messages=messages, model=model, kw=kw)
        return _proposal()

    monkeypatch.setattr(st, "structured_complete", fake)
    out = ac._propose("how do we deploy?", "Deployed.", _steps(2))

    assert seen["role"] == "learner"
    assert seen["messages"][0]["role"] == "system"
    assert "REUSABLE procedure" in seen["messages"][0]["content"]
    assert "how do we deploy?" in seen["messages"][1]["content"]
    assert seen["kw"]["temperature"] == 0.0     # a procedure, not a riff
    assert out.kind == "skill"


def test_a_failed_write_is_reported_not_swallowed(monkeypatch):
    """A disk error must surface as captured=False with the reason, or the
    library silently stops growing and nobody knows."""
    from aiforge_core.runtime import skills
    monkeypatch.delenv("AIFORGE_ARTIFACT_CAPTURE", raising=False)
    monkeypatch.setattr(ac, "_propose", lambda *a, **k: _proposal())
    monkeypatch.setattr(ac, "existing_match", lambda *a, **k: "")
    monkeypatch.setattr(skills, "write_skill",
                        lambda **kw: {"ok": False, "error": "disk full"})
    out = ac.capture_from_chat(prompt="do the thing", final_text="done",
                               steps=_steps(4), cwd="/repo")
    assert out["ok"] is False
    assert out["captured"] is False
    assert out["error"] == "disk full"
