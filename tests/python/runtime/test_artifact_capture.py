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
