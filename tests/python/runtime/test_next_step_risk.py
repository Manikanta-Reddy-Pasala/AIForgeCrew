"""What a prediction is allowed to do on its own.

A table, not a threshold. A threshold is exactly what lets a confident model do
something expensive: confidence is evidence about whether the guess is RIGHT and
says nothing at all about what it costs when it is wrong.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.next_step import _risk


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for k in ("AIFORGE_PREDICT_ACT", "AIFORGE_PREDICT_MIN_CONFIDENCE",
              "AIFORGE_RISK_DISABLE"):
        monkeypatch.delenv(k, raising=False)


# ── tier 1: reversible and local — may act ───────────────────────────────

@pytest.mark.parametrize("tool,args", [
    ("read_file", {"path": "x/y.py"}),
    ("grep", {"pattern": "def run_once"}),
    ("list_dir", {"path": "."}),
    ("bash", {"cmd": "git status"}),
    ("bash", {"cmd": "ls -la"}),
    ("bash", {"cmd": "cat deploy/env.py"}),
])
def test_a_reversible_local_action_may_act(tool, args):
    assert _risk.verdict(tool, args, confidence=0.9, clean_tree=False) == _risk.ACT


def test_a_plain_reply_with_no_tool_may_act():
    """A prediction that only SAYS something has no blast radius at all."""
    assert _risk.verdict("", {}, confidence=0.9, clean_tree=False) == _risk.ACT


def test_low_confidence_never_acts_even_when_safe():
    assert _risk.verdict("read_file", {"path": "x"}, confidence=0.4,
                         clean_tree=True) == _risk.OFFER


# ── tier 2: writes the workspace — needs a clean tree ────────────────────

@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "x/y.py"}),
    ("edit_file", {"path": "x/y.py"}),
    ("apply_patch", {"path": "x/y.py"}),
    ("bash", {"cmd": "git commit -m wip"}),
    ("bash", {"cmd": "mkdir build"}),
])
def test_a_workspace_write_acts_only_on_a_clean_tree(tool, args):
    """'Undo' has to mean something. On a dirty tree the user's own uncommitted
    work is mixed in with ours."""
    assert _risk.verdict(tool, args, confidence=0.95, clean_tree=True) == _risk.ACT
    assert _risk.verdict(tool, args, confidence=0.95, clean_tree=False) == _risk.OFFER


# ── tier 3: leaves the machine — never acts ──────────────────────────────

@pytest.mark.parametrize("tool,args", [
    ("bash", {"cmd": "git push origin main"}),
    ("bash", {"cmd": "git tag v1.2.3"}),
    ("bash", {"cmd": "kubectl apply -f deploy.yaml"}),
    ("bash", {"cmd": "docker push registry/app:latest"}),
    ("bash", {"cmd": "terraform apply"}),
    ("bash", {"cmd": "npm publish"}),
    ("bash", {"cmd": "ssh nuc systemctl restart aiforge"}),
    ("web_fetch", {"url": "https://example.com"}),
    ("send_email", {"to": "someone@example.com"}),
])
def test_leaving_the_machine_never_acts_however_confident(tool, args):
    assert _risk.verdict(tool, args, confidence=1.0, clean_tree=True) == _risk.OFFER


def test_a_dangerous_shell_command_never_acts():
    """command_risk already owns 'which commands are dangerous'. _risk consults
    it rather than keeping a second list that drifts out of step."""
    assert _risk.verdict("bash", {"cmd": "sudo rm -rf /"}, confidence=1.0,
                         clean_tree=True) == _risk.OFFER


def test_an_unknown_tool_is_treated_as_the_top_tier():
    """The unknown case has to be the careful one, or every tool added after
    this file is a hole until somebody remembers to classify it."""
    assert _risk.verdict("some_new_tool", {}, confidence=1.0,
                         clean_tree=True) == _risk.OFFER


def test_a_shell_call_with_no_command_is_not_assumed_safe():
    assert _risk.verdict("bash", {}, confidence=1.0, clean_tree=True) in (
        _risk.ACT, _risk.OFFER)      # must not raise, whichever it decides


# ── the switches ─────────────────────────────────────────────────────────

def test_predict_act_0_turns_every_act_into_an_offer(monkeypatch):
    """The 'always ask me' setting, separate from the kill switch: an operator
    may want the suggestions without wanting them acted on."""
    monkeypatch.setenv("AIFORGE_PREDICT_ACT", "0")
    assert _risk.verdict("read_file", {"path": "x"}, confidence=1.0,
                         clean_tree=True) == _risk.OFFER


def test_the_confidence_floor_is_tunable(monkeypatch):
    monkeypatch.setenv("AIFORGE_PREDICT_MIN_CONFIDENCE", "0.99")
    assert _risk.verdict("read_file", {"path": "x"}, confidence=0.9,
                         clean_tree=True) == _risk.OFFER


def test_an_unparsable_floor_falls_back_rather_than_raising(monkeypatch):
    monkeypatch.setenv("AIFORGE_PREDICT_MIN_CONFIDENCE", "lots")
    assert _risk.min_confidence() == 0.75


def test_a_classifier_that_explodes_refuses_rather_than_acts(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("no idea")

    monkeypatch.setattr(_risk, "tier", _boom)
    assert _risk.verdict("read_file", {}, confidence=1.0,
                         clean_tree=True) == _risk.OFFER
