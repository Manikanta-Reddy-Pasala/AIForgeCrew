# Next-step Prediction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At the end of a simple-chat turn, work out the likely next action and either do it (safe + reversible + confident) or offer it as a chip — recording every outcome so accepted predictions sharpen the next one.

**Architecture:** A new `aiforge_core/runtime/next_step/` package with a narrow API, mirroring `rule_capture`: one capped LLM call on the `enhancer` role, a confidence floor, and fail-open on every path. The ACT/OFFER decision is a table over blast radius, owned by `_risk.py`, never by the model. Called from `_handle_final` after the answer is emitted, so it can never delay or replace what the user asked for. Storage passes through `memory.sync.redact` so credentials cannot reach it.

**Tech Stack:** Python 3.12, FastAPI (SSE), pytest, React 18 + TypeScript.

**Spec:** `docs/superpowers/specs/2026-08-26-next-step-prediction-design.md`

**Worktree:** `.worktrees/feat/next-step-prediction`, branch `feat/next-step-prediction`.

**Baseline for "tests pass":** `main` fails 7 LLM-dependent tests when run with a dead model endpoint. Compare against that, never against zero:

```bash
AIFORGE_LM_BASE_URL=http://127.0.0.1:9/v1 AIFORGE_INTENT_LM_URL=http://127.0.0.1:9/v1 \
  .venv/bin/python -m pytest tests/python -q --timeout=45 --timeout-method=signal -p no:randomly
```

(zsh does not word-split unquoted variables — use an array for node-id lists.)

---

## File Structure

**New:**

| Path | Responsibility |
|---|---|
| `aiforge_core/runtime/next_step/__init__.py` | The public API: `predict`, `outcome`, `history`, `Prediction` |
| `aiforge_core/runtime/next_step/_predict.py` | The prompt and the one capped call |
| `aiforge_core/runtime/next_step/_risk.py` | Blast radius, and the ACT/OFFER table |
| `aiforge_core/runtime/next_step/_store.py` | Predictions + outcomes, redacted and bounded |
| `tests/python/runtime/test_next_step_risk.py` | The ACT/OFFER table, both directions |
| `tests/python/runtime/test_next_step_predict.py` | Parsing, the confidence floor, fail-open |
| `tests/python/runtime/test_next_step_store.py` | The learning loop, bounds, no secrets |
| `tests/python/runtime/test_next_step_in_turn.py` | Event ordering and turn safety |
| `web/src/views/Chat.SuggestionChip.tsx` | The chip |

**Modified:**

| Path | Change |
|---|---|
| `aiforge_core/runtime/chat_agent/_loop.py` | `_handle_final` emits `suggestion` between `message` and `done` |
| `aiforge_core/api/routes/chat.py` | `POST /api/chat/suggestion/{id}` records an outcome and runs an accepted OFFER |
| `web/src/views/Chat.tsx` | Render the chip |
| `web/src/api/chat.ts` | Types + the outcome call |
| `.env.example` | The five settings |

---

## Task 1: The ACT/OFFER table

The decision that makes this feature safe. Written first, and tested hardest.

**Files:**
- Create: `aiforge_core/runtime/next_step/_risk.py`
- Test: `tests/python/runtime/test_next_step_risk.py`

- [ ] **Step 1: Write the failing tests**

```python
"""What a prediction is allowed to do on its own.

A table, not a threshold. A threshold is exactly what lets a confident model
do something expensive, and confidence is not evidence about blast radius.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.next_step import _risk


# ── tier 1: reversible and local — may act ───────────────────────────────

@pytest.mark.parametrize("tool,args", [
    ("read_file", {"path": "x/y.py"}),
    ("grep", {"pattern": "def run_once"}),
    ("list_dir", {"path": "."}),
    ("bash", {"cmd": "git status"}),
    ("bash", {"cmd": "ls -la"}),
])
def test_a_reversible_local_action_may_act(tool, args):
    assert _risk.verdict(tool, args, confidence=0.9, clean_tree=False) == _risk.ACT


def test_low_confidence_never_acts_even_when_safe():
    assert _risk.verdict("read_file", {"path": "x"}, confidence=0.4,
                         clean_tree=True) == _risk.OFFER


# ── tier 2: writes the workspace — needs a clean tree ────────────────────

@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "x/y.py"}),
    ("edit_file", {"path": "x/y.py"}),
    ("bash", {"cmd": "git commit -m wip"}),
])
def test_a_workspace_write_acts_only_on_a_clean_tree(tool, args):
    assert _risk.verdict(tool, args, confidence=0.95, clean_tree=True) == _risk.ACT
    assert _risk.verdict(tool, args, confidence=0.95, clean_tree=False) == _risk.OFFER


# ── tier 3: leaves the machine — never acts ──────────────────────────────

@pytest.mark.parametrize("tool,args", [
    ("bash", {"cmd": "git push origin main"}),
    ("bash", {"cmd": "rm -rf /etc/nginx"}),
    ("bash", {"cmd": "kubectl apply -f deploy.yaml"}),
    ("bash", {"cmd": "curl -X POST https://api.example.com/charge"}),
    ("web_fetch", {"url": "https://example.com"}),
    ("send_email", {"to": "someone@example.com"}),
])
def test_leaving_the_machine_never_acts_however_confident(tool, args):
    assert _risk.verdict(tool, args, confidence=1.0, clean_tree=True) == _risk.OFFER


def test_an_unknown_tool_is_treated_as_the_top_tier():
    """The unknown case has to be the careful one, or every new tool is a hole
    until somebody remembers to classify it."""
    assert _risk.verdict("some_new_tool", {}, confidence=1.0,
                         clean_tree=True) == _risk.OFFER


def test_a_dangerous_shell_command_never_acts():
    """command_risk already knows this; _risk must consult it rather than
    keep a second, drifting list."""
    assert _risk.verdict("bash", {"cmd": "sudo rm -rf /"}, confidence=1.0,
                         clean_tree=True) == _risk.OFFER


# ── the global off switch ────────────────────────────────────────────────

def test_predict_act_0_turns_every_act_into_an_offer(monkeypatch):
    monkeypatch.setenv("AIFORGE_PREDICT_ACT", "0")
    assert _risk.verdict("read_file", {"path": "x"}, confidence=1.0,
                         clean_tree=True) == _risk.OFFER


def test_a_plain_reply_with_no_tool_may_act():
    """A prediction that only says something has no blast radius at all."""
    assert _risk.verdict("", {}, confidence=0.9, clean_tree=False) == _risk.ACT
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/python/runtime/test_next_step_risk.py -q
```

Expected: `ModuleNotFoundError: aiforge_core.runtime.next_step`.

- [ ] **Step 3: Implement**

Create `aiforge_core/runtime/next_step/_risk.py`:

```python
"""What a prediction may do on its own: blast radius, not confidence.

The rule is a TABLE rather than a threshold, and that is the whole point. A
threshold says "act when sure", which is precisely what lets a confident model
push to production — confidence is evidence about whether the guess is right,
and says nothing at all about what it costs when it is wrong.

Three tiers. An unrecognised tool falls in the top one: the unknown case has to
be the careful one, or every tool added after this file is a hole until somebody
remembers to come back and classify it.
"""
from __future__ import annotations

import os

ACT = "ACT"
OFFER = "OFFER"

# Tier 1 — reversible, and nothing outside this machine sees it.
_READ_ONLY = frozenset({
    "read_file", "read", "grep", "search", "list_dir", "ls", "glob",
    "repo_map", "recall", "memory_search",
})

# Tier 2 — changes the workspace. Reversible only while git can undo it.
_WORKSPACE_WRITE = frozenset({"write_file", "edit_file", "apply_patch", "patch"})

# Shell verbs that leave the machine or cannot be undone, whatever
# ``command_risk`` makes of the rest of the line.
_LEAVES_THE_MACHINE = (
    "git push", "git tag", "docker push", "kubectl", "helm", "terraform",
    "aws ", "gcloud ", "az ", "ssh ", "scp ", "rsync ", "curl -x", "curl --request",
    "npm publish", "pip upload", "twine", "systemctl", "reboot", "shutdown",
)

_DEFAULT_MIN_CONFIDENCE = 0.75


def min_confidence() -> float:
    try:
        return float(os.environ.get("AIFORGE_PREDICT_MIN_CONFIDENCE") or
                     _DEFAULT_MIN_CONFIDENCE)
    except ValueError:
        return _DEFAULT_MIN_CONFIDENCE


def acting_enabled() -> bool:
    """``AIFORGE_PREDICT_ACT=0`` makes every prediction an offer.

    The "always ask me" setting. Kept separate from the kill switch: an operator
    may well want the suggestions and not want them acted on.
    """
    return (os.environ.get("AIFORGE_PREDICT_ACT") or "1").strip() != "0"


def _shell_leaves_the_machine(cmd: str) -> bool:
    from aiforge_core.runtime.tools import command_risk

    low = " ".join(str(cmd or "").lower().split())
    if any(v in low for v in _LEAVES_THE_MACHINE):
        return True
    # Consulted rather than duplicated: command_risk already owns "which shell
    # commands are dangerous", and a second list here would drift out of step
    # with it in whichever direction nobody was watching.
    return command_risk.assess(str(cmd or "")).get("level") != command_risk.SAFE


def tier(tool: str, args: dict) -> int:
    """1 = reversible and local, 2 = writes the workspace, 3 = neither."""
    name = str(tool or "").strip()
    if not name:
        return 1                    # a prediction that only says something
    if name in _READ_ONLY:
        return 1
    if name in _WORKSPACE_WRITE:
        return 2
    if name == "bash":
        cmd = str((args or {}).get("cmd") or (args or {}).get("command") or "")
        if _shell_leaves_the_machine(cmd):
            return 3
        return 2 if _writes_the_tree(cmd) else 1
    return 3


def _writes_the_tree(cmd: str) -> bool:
    low = " ".join(str(cmd or "").lower().split())
    return any(low.startswith(v) or f" {v}" in low
               for v in ("git commit", "git checkout", "git reset", "git merge",
                         "git rebase", "mv ", "cp ", "mkdir ", "touch "))


def verdict(tool: str, args: dict, *, confidence: float,
            clean_tree: bool) -> str:
    """``ACT`` or ``OFFER`` for one prediction. Never raises.

    Confidence is necessary and never sufficient: it gates tiers 1 and 2 and is
    ignored entirely at tier 3, where no degree of certainty makes an
    irreversible guess acceptable.
    """
    try:
        if not acting_enabled() or confidence < min_confidence():
            return OFFER
        level = tier(tool, args)
    except Exception:  # noqa: BLE001 — an unclassifiable action is not one we act on
        return OFFER
    if level == 1:
        return ACT
    if level == 2:
        # "Undo" has to mean something. On a dirty tree the user's own
        # uncommitted work is mixed in with ours, and `git checkout` stops being
        # a safe answer to "that was wrong".
        return ACT if clean_tree else OFFER
    return OFFER


__all__ = ["ACT", "OFFER", "tier", "verdict", "min_confidence", "acting_enabled"]
```

Create `aiforge_core/runtime/next_step/__init__.py` with the dataclass and
re-exports (filled in by Task 2; for now enough to import `_risk`):

```python
"""Predicting the next step, and deciding whether to take it."""
from __future__ import annotations

from aiforge_core.runtime.next_step import _risk

ACT, OFFER = _risk.ACT, _risk.OFFER

__all__ = ["ACT", "OFFER"]
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/python/runtime/test_next_step_risk.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/next_step tests/python/runtime/test_next_step_risk.py
git commit -m "feat(next-step): the ACT/OFFER table, decided by blast radius"
```

---

## Task 2: The prediction call

**Files:**
- Create: `aiforge_core/runtime/next_step/_predict.py`
- Modify: `aiforge_core/runtime/next_step/__init__.py`
- Test: `tests/python/runtime/test_next_step_predict.py`

- [ ] **Step 1: Write the failing tests**

```python
"""The one capped call, its parsing, and its floor.

Everything here FAILS OPEN. This runs on every turn, and a feature that
improves a good turn must never be able to break one.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import next_step
from aiforge_core.runtime.next_step import _predict


def _ctx(**kw):
    base = {"message": "connect to db.internal with those credentials",
            "did": "read `deploy/env.py`", "repo": "AIForgeCrew",
            "clean_tree": True}
    return {**base, **kw}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("AIFORGE_PREDICT_DISABLE", "AIFORGE_PREDICT_ACT",
              "AIFORGE_PREDICT_MIN_CONFIDENCE"):
        monkeypatch.delenv(k, raising=False)


def _reply(monkeypatch, raw):
    monkeypatch.setattr(_predict, "_llm", lambda *a, **k: raw)


def test_a_confident_safe_prediction_comes_back_as_act(monkeypatch):
    _reply(monkeypatch, '{"action":"check the connection","tool":"bash",'
                        '"args":{"cmd":"pg_isready -h db.internal"},'
                        '"confidence":0.9,"rationale":"credentials were given"}')
    p = next_step.predict(_ctx())
    assert p is not None
    assert p.verdict == next_step.ACT
    assert p.action == "check the connection"


def test_an_irreversible_prediction_comes_back_as_offer(monkeypatch):
    _reply(monkeypatch, '{"action":"push the fix","tool":"bash",'
                        '"args":{"cmd":"git push origin main"},'
                        '"confidence":0.99,"rationale":"the fix is committed"}')
    assert next_step.predict(_ctx()).verdict == next_step.OFFER


def test_below_the_floor_nothing_is_emitted_at_all(monkeypatch):
    """Not even an offer: a guess the model itself doubts is noise."""
    _reply(monkeypatch, '{"action":"maybe restart it","tool":"","args":{},'
                        '"confidence":0.3,"rationale":"unsure"}')
    assert next_step.predict(_ctx()) is None


@pytest.mark.parametrize("raw", ["", "not json at all", "{", '{"action":""}',
                                 '{"nope": 1}', None])
def test_a_useless_reply_fails_open(monkeypatch, raw):
    _reply(monkeypatch, raw)
    assert next_step.predict(_ctx()) is None


def test_an_llm_error_fails_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("model is down")

    monkeypatch.setattr(_predict, "_llm", _boom)
    assert next_step.predict(_ctx()) is None


def test_the_kill_switch_makes_no_call_at_all(monkeypatch):
    calls = []
    monkeypatch.setattr(_predict, "_llm", lambda *a, **k: calls.append(1) or "{}")
    monkeypatch.setenv("AIFORGE_PREDICT_DISABLE", "1")

    assert next_step.predict(_ctx()) is None
    assert calls == []


def test_every_prediction_has_a_stable_id(monkeypatch):
    _reply(monkeypatch, '{"action":"check it","tool":"","args":{},'
                        '"confidence":0.9,"rationale":"x"}')
    p = next_step.predict(_ctx())
    assert p.id and next_step.predict(_ctx()).id != p.id, "ids must be unique"


def test_accepted_history_is_offered_to_the_model(monkeypatch):
    """The learning loop, from the prompt side."""
    seen = {}
    monkeypatch.setattr(_predict, "_llm",
                        lambda role, msgs, **k: seen.update(prompt=msgs) or
                        '{"action":"x","tool":"","args":{},"confidence":0.9,'
                        '"rationale":"y"}')
    next_step.outcome_row({"id": "p-1", "trigger": "gave a host",
                           "action": "verify the connection", "tool": "bash",
                           "repo": "AIForgeCrew"}, accepted=True)

    next_step.predict(_ctx())

    assert "verify the connection" in str(seen["prompt"])
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/python/runtime/test_next_step_predict.py -q
```

Expected: `AttributeError: module ... has no attribute 'predict'`.

- [ ] **Step 3: Implement**

`_predict.py` mirrors `rule_capture._classify` — same `_extract_json` approach,
same fail-open discipline, same `_llm_complete` helper:

```python
"""One capped call: what does this user probably want next?

Modelled on ``rule_capture._classify`` deliberately — same capped single call on
a cheap role, same confidence floor, same fail-open on every path. That module
is the precedent for "an always-on LLM pass that must never break a turn", and
copying its shape is cheaper than rediscovering its lessons.

The prompt carries three things: what the user said, what the agent actually did
about it, and the last few predictions this user ACCEPTED in this repo. The
third is the entire learning mechanism — no training, no embeddings, just
showing the model what this person has said yes to before.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

log = logging.getLogger("aiforge.next_step")

_SYS = (
    "You predict the ONE next action a developer most likely wants, given what "
    "they just asked and what was just done. Reply with ONLY a JSON object: "
    '{"action":"<one imperative sentence>","tool":"<tool name or empty>",'
    '"args":{...},"confidence":0.0-1.0,"rationale":"<one clause>"}. '
    "Predict nothing speculative: if the next step is not strongly implied, "
    "return a confidence below 0.5. Never predict an action that deletes data, "
    "pushes code, deploys, or spends money."
)

_MAX_EXAMPLES = 5
_DEFAULT_TIMEOUT_S = 10


def _disabled() -> bool:
    return (os.environ.get("AIFORGE_PREDICT_DISABLE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _timeout() -> int:
    try:
        return int(os.environ.get("AIFORGE_PREDICT_TIMEOUT_S") or _DEFAULT_TIMEOUT_S)
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def _llm(role: str, messages: list, **kw) -> str:
    """Indirection so tests can replace the call without touching the network.

    Delegates to ``rule_capture``'s helper rather than a second client: that one
    already knows how this codebase reaches a model role.
    """
    from aiforge_core.runtime.rule_capture import _llm_complete

    return _llm_complete(role, messages, **kw)


def _examples(repo: str) -> str:
    from aiforge_core.runtime.next_step import _store

    rows = _store.accepted(repo, limit=_MAX_EXAMPLES)
    if not rows:
        return ""
    lines = "\n".join(f"- after {r.get('trigger')}: {r.get('action')}"
                      for r in rows)
    return f"\n\nThis user previously accepted:\n{lines}"


def _user_prompt(ctx: dict) -> str:
    return (f"They said: {str(ctx.get('message') or '')[:2000]}\n"
            f"What was just done: {str(ctx.get('did') or '')[:1000]}"
            f"{_examples(str(ctx.get('repo') or ''))}")


def raw_prediction(ctx: dict) -> dict | None:
    """The model's answer as a dict, or None. Never raises."""
    if _disabled():
        return None
    role = os.environ.get("AIFORGE_PREDICT_ROLE", "enhancer")
    try:
        raw = _llm(role,
                   [{"role": "system", "content": _SYS},
                    {"role": "user", "content": _user_prompt(ctx)}],
                   max_tokens=250, temperature=0.0, timeout_s=_timeout())
    except Exception as exc:  # noqa: BLE001 — a prediction never breaks a turn
        log.debug("next_step: prediction call failed (none): %s", exc)
        return None
    return _parse(raw or "")


def _parse(raw: str) -> dict | None:
    from aiforge_core.runtime.rule_capture import _classify

    obj = _classify._extract_json(raw)
    if not isinstance(obj, dict):
        return None
    action = str(obj.get("action") or "").strip().replace("\n", " ")
    if not action:
        return None
    try:
        confidence = float(obj.get("confidence"))
    except (TypeError, ValueError):
        return None
    args = obj.get("args")
    return {"action": action[:300],
            "tool": str(obj.get("tool") or "").strip(),
            "args": args if isinstance(args, dict) else {},
            "confidence": confidence,
            "rationale": str(obj.get("rationale") or "").strip()[:300],
            "id": f"p-{uuid.uuid4().hex[:8]}"}
```

`__init__.py` composes the two halves:

```python
"""Predicting the next step, and deciding whether to take it.

The public surface is three functions and a record. ``predict`` is the only one
the chat turn calls; the other two exist so the outcome can be recorded and
shown back to the operator.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from aiforge_core.runtime.next_step import _predict, _risk, _store

ACT, OFFER = _risk.ACT, _risk.OFFER


@dataclass(frozen=True)
class Prediction:
    id: str
    action: str
    tool: str
    args: dict
    confidence: float
    rationale: str
    verdict: str

    def as_event(self) -> dict:
        return {"type": "suggestion", **asdict(self)}


def predict(ctx: dict) -> Prediction | None:
    """The likely next action, or None. Never raises.

    ``ctx`` carries ``message`` (what the user said), ``did`` (what the agent
    did about it), ``repo`` and ``clean_tree``.
    """
    row = _predict.raw_prediction(ctx or {})
    if row is None:
        return None
    if row["confidence"] < _risk.min_confidence():
        # Below the floor nothing is emitted AT ALL, not even an offer: a guess
        # the model itself doubts is noise, and noise trains the user to ignore
        # the chip that matters.
        return None
    verdict = _risk.verdict(row["tool"], row["args"],
                            confidence=row["confidence"],
                            clean_tree=bool((ctx or {}).get("clean_tree")))
    p = Prediction(verdict=verdict, **{k: row[k] for k in
                                       ("id", "action", "tool", "args",
                                        "confidence", "rationale")})
    _store.remember(p, ctx or {})
    return p


def outcome(prediction_id: str, accepted: bool, *, edited: str = "") -> None:
    _store.record_outcome(prediction_id, accepted, edited=edited)


def outcome_row(row: dict, *, accepted: bool) -> None:
    """Record a complete row directly. For tests and for replay."""
    _store.append(row, accepted=accepted)


def history(limit: int = 20) -> list[dict]:
    return _store.history(limit)


__all__ = ["ACT", "OFFER", "Prediction", "predict", "outcome", "outcome_row",
           "history"]
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/python/runtime/test_next_step_predict.py -q
```

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/next_step tests/python/runtime/test_next_step_predict.py
git commit -m "feat(next-step): one capped prediction call, failing open on every path"
```

---

## Task 3: The store, and the learning loop

**Files:**
- Create: `aiforge_core/runtime/next_step/_store.py`
- Test: `tests/python/runtime/test_next_step_store.py`

- [ ] **Step 1: Write the failing tests**

```python
"""What is remembered, and what must never be.

The store is the learning loop's whole mechanism, and it is also the second
place a credential could come to live. It must not become that.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import next_step
from aiforge_core.runtime.next_step import _store


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))


def _p(pid="p-1", action="verify the connection", tool="bash"):
    return next_step.Prediction(id=pid, action=action, tool=tool,
                                args={"cmd": "pg_isready"}, confidence=0.9,
                                rationale="a host was given",
                                verdict=next_step.ACT)


def test_an_accepted_prediction_becomes_an_example():
    _store.remember(_p(), {"repo": "R", "message": "connect to the db"})
    _store.record_outcome("p-1", True)

    rows = _store.accepted("R", limit=5)
    assert [r["action"] for r in rows] == ["verify the connection"]


def test_a_dismissed_prediction_is_kept_but_never_used_as_an_example():
    """A feature that only learns from its wins drifts."""
    _store.remember(_p(), {"repo": "R", "message": "connect to the db"})
    _store.record_outcome("p-1", False)

    assert _store.accepted("R", limit=5) == []
    assert _store.history(10)[0]["accepted"] is False


def test_examples_do_not_cross_repos():
    _store.remember(_p("p-1"), {"repo": "A", "message": "x"})
    _store.record_outcome("p-1", True)
    assert _store.accepted("B", limit=5) == []


def test_a_prediction_carrying_a_credential_is_not_stored():
    """redact is the one place in the product that judges secrets. A row it
    refuses is DROPPED, not stored scrubbed."""
    _store.remember(_p(action="use AKIAIOSFODNN7EXAMPLE to connect"),
                    {"repo": "R", "message": "here is the key"})
    assert _store.history(10) == []


def test_argument_values_are_never_written():
    _store.remember(_p(), {"repo": "R", "message": "connect with hunter2ThatIsReal"})
    assert "hunter2ThatIsReal" not in str(_store.history(10))


def test_the_store_is_bounded():
    for i in range(_store.MAX_ROWS + 25):
        _store.remember(_p(pid=f"p-{i}"), {"repo": "R", "message": "x"})
    assert len(_store.history(10_000)) == _store.MAX_ROWS


def test_recording_an_outcome_for_an_unknown_id_is_not_an_error():
    _store.record_outcome("p-nope", True)          # must not raise


def test_an_unreadable_store_does_not_break_prediction(monkeypatch):
    monkeypatch.setattr(_store, "_path", lambda: (_ for _ in ()).throw(OSError("nope")))
    assert _store.accepted("R", limit=5) == []
    _store.remember(_p(), {"repo": "R", "message": "x"})     # must not raise
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/python/runtime/test_next_step_store.py -q
```

- [ ] **Step 3: Implement**

```python
"""Predictions and their outcomes, bounded and redacted.

Two rules, and the second is the important one:

* **Only ACCEPTED rows become examples.** Dismissals are kept, because a
  feature that learns only from its wins drifts, but they are counters rather
  than training data.
* **Nothing reaches this file without passing ``memory.sync.redact``.** A row
  the filter refuses is DROPPED, never stored scrubbed — the product has exactly
  one place that judges whether text carries a secret, and this must not become
  a second one with its own opinion.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from aiforge_core.config.paths import config_dir

log = logging.getLogger("aiforge.next_step")

_FILE = "next_step_history.json"

# Enough to see what a user habitually accepts, small enough to stay a glance.
MAX_ROWS = 200


def _path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _FILE


def _read() -> list[dict]:
    from aiforge_core.memory.sync import _io

    try:
        rows = _io.read_json(_path()).get("rows") or []
    except Exception:  # noqa: BLE001 — an unreadable store is an empty one
        return []
    return [r for r in rows if isinstance(r, dict)]


def _write(rows: list[dict]) -> None:
    from aiforge_core.memory.sync import _io

    try:
        _io.write_json(_path(), {"rows": rows[-MAX_ROWS:]})
    except Exception as exc:  # noqa: BLE001 — bookkeeping is not the payload
        log.debug("next_step: could not write the history: %s", exc)


def _safe(text: str) -> bool:
    """True when ``text`` may be written. Fails CLOSED."""
    from aiforge_core.memory.sync import redact

    try:
        return redact.review({"meta": {"title": ""}, "body": text}).rule \
            .startswith("noise.") or redact.review(
                {"meta": {"title": ""}, "body": text}).send
    except Exception:  # noqa: BLE001 — cannot judge it, do not store it
        return False


def remember(prediction, ctx: dict) -> None:
    """Record a prediction as pending. Never raises.

    ``trigger`` is a description of the situation, never the user's raw message:
    the message is what carries credentials, and this file must not.
    """
    trigger = str((ctx or {}).get("message") or "")[:300]
    blob = f"{trigger}\n{getattr(prediction, 'action', '')}"
    if not _safe(blob):
        log.debug("next_step: prediction not stored — the filter refused it")
        return
    rows = _read()
    rows.append({
        "id": prediction.id, "at": int(time.time()),
        "repo": str((ctx or {}).get("repo") or ""),
        "trigger": trigger, "action": prediction.action,
        "tool": prediction.tool,          # the NAME only; args are never stored
        "verdict": prediction.verdict,
        "confidence": prediction.confidence,
        "accepted": None,                 # pending until the user says
        "edited": "",
    })
    _write(rows)


def append(row: dict, *, accepted: bool) -> None:
    rows = _read()
    rows.append({**row, "at": int(time.time()), "accepted": bool(accepted)})
    _write(rows)


def record_outcome(prediction_id: str, accepted: bool, *, edited: str = "") -> None:
    """Mark a prediction accepted or dismissed. An unknown id is a no-op —
    a stale chip in an old browser tab must not be an error."""
    rows = _read()
    for r in rows:
        if r.get("id") == prediction_id:
            r["accepted"] = bool(accepted)
            r["edited"] = str(edited or "")[:300]
            break
    _write(rows)


def accepted(repo: str, limit: int = 5) -> list[dict]:
    """The most recent accepted predictions for ``repo``, newest last.

    Per repo on purpose: what a user accepts in one codebase says little about
    another, and mixing them makes every prediction blander.
    """
    rows = [r for r in _read()
            if r.get("accepted") is True and str(r.get("repo") or "") == str(repo)]
    return rows[-limit:]


def history(limit: int = 20) -> list[dict]:
    return list(reversed(_read()))[:limit]


__all__ = ["MAX_ROWS", "remember", "append", "record_outcome", "accepted",
           "history"]
```

Note on `_safe`: the double call above is deliberate-looking but wrong — replace
it with a single `review` call and keep the result:

```python
def _safe(text: str) -> bool:
    from aiforge_core.memory.sync import redact

    try:
        return redact.review({"meta": {"title": ""}, "body": text}).send
    except Exception:  # noqa: BLE001 — cannot judge it, do not store it
        return False
```

A prediction's `trigger` is short prose and will often trip `noise.thin`, which
is a filter tuned for *knowledge nodes*, not for this. Add the one exemption
explicitly rather than loosening the filter: accept when the only refusal is a
`noise.*` rule, refuse on `secrets.*` and `private.*`.

```python
def _safe(text: str) -> bool:
    """True when ``text`` may be written. Fails CLOSED.

    ``noise.*`` refusals are ignored on purpose: that stage judges whether a
    note is worth REPLICATING to a fleet, and a prediction trigger is neither a
    note nor replicated. ``secrets.*`` and ``private.*`` are what this call is
    for, and they are honoured.
    """
    from aiforge_core.memory.sync import redact

    try:
        v = redact.review({"meta": {"title": ""}, "body": text})
    except Exception:  # noqa: BLE001 — cannot judge it, do not store it
        return False
    return v.send or v.rule.startswith("noise.")
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/python/runtime/test_next_step_store.py -q
```

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/next_step/_store.py tests/python/runtime/test_next_step_store.py
git commit -m "feat(next-step): remember outcomes, and never the values"
```

---

## Task 4: Emit it from the turn

**Files:**
- Modify: `aiforge_core/runtime/chat_agent/_loop.py` (`_handle_final`)
- Test: `tests/python/runtime/test_next_step_in_turn.py`

- [ ] **Step 1: Write the failing test**

```python
"""The prediction's place in a turn: last, optional, and never in the way."""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent import _loop


def _events(monkeypatch, prediction):
    monkeypatch.setattr(_loop, "_predict_next_step", lambda *a, **k: prediction)
    return list(_loop._emit_suggestion("hello", "read a file", "/repo"))


def test_the_suggestion_arrives_after_the_answer(monkeypatch):
    """A prediction must never delay or replace what was actually asked for."""
    from aiforge_core.runtime import next_step

    p = next_step.Prediction(id="p-1", action="check it", tool="", args={},
                             confidence=0.9, rationale="x",
                             verdict=next_step.ACT)
    evs = _events(monkeypatch, p)
    assert [e["type"] for e in evs] == ["suggestion"]
    assert evs[0]["action"] == "check it"
    assert evs[0]["verdict"] == "ACT"


def test_no_prediction_emits_nothing(monkeypatch):
    assert _events(monkeypatch, None) == []


def test_a_raising_predictor_emits_nothing_and_does_not_propagate(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(_loop, "_predict_next_step", _boom)
    assert list(_loop._emit_suggestion("hello", "did a thing", "/repo")) == []
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/python/runtime/test_next_step_in_turn.py -q
```

- [ ] **Step 3: Implement**

Add to `_loop.py`, above `_handle_final`:

```python
def _predict_next_step(message: str, did: str, cwd):
    """The prediction, or None. Split out so a test can replace exactly this."""
    from aiforge_core.runtime import next_step

    return next_step.predict({"message": message, "did": did,
                              "repo": _repo_name(cwd),
                              "clean_tree": _is_clean_tree(cwd)})


def _emit_suggestion(message: str, did: str, cwd):
    """Yield at most one ``suggestion`` event. Never raises, never blocks.

    Placed AFTER the answer and before ``done`` — the same ordering trick
    ``plan_ready`` uses. The user reads their answer either way, so a slow or
    broken prediction costs them nothing.
    """
    try:
        p = _predict_next_step(message, did, cwd)
    except Exception as exc:  # noqa: BLE001 — a prediction never breaks a turn
        _log.debug("next_step: prediction skipped: %s", exc)
        return
    if p is not None:
        yield p.as_event()
```

`_is_clean_tree` and `_repo_name` are small helpers over the git status the
loop already computes for `_wt_fp0`; reuse that rather than shelling out again.

Then in `_handle_final`, between the answer and `done`:

```python
    _fire_stop("final", cwd)
    yield {"type": "message", "text": _strip_reasoning_prefix(step["text"])}
    yield from _emit_suggestion(st.user_message, st.did_summary, cwd)
    yield {"type": "done"}
    return "return"
```

`st.did_summary` is a one-line description of the tools this turn ran; the state
namespace already tracks the calls, so add the join there rather than
reconstructing it here.

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/python/runtime/test_next_step_in_turn.py tests/python/test_chat_agent_context.py -q
```

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/chat_agent/_loop.py tests/python/runtime/test_next_step_in_turn.py
git commit -m "feat(chat): emit the predicted next step after the answer"
```

---

## Task 5: Accept or dismiss

**Files:**
- Modify: `aiforge_core/api/routes/chat.py`
- Test: `tests/python/runtime/test_next_step_in_turn.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_accepting_records_the_outcome(chat_client):
    from aiforge_core.runtime import next_step

    p = next_step.Prediction(id="p-9", action="check it", tool="", args={},
                             confidence=0.9, rationale="x",
                             verdict=next_step.OFFER)
    next_step._store.remember(p, {"repo": "R", "message": "connect"})

    r = chat_client.post("/api/chat/suggestion/p-9", json={"accepted": True})
    assert r.status_code == 200
    assert next_step.history(5)[0]["accepted"] is True


def test_dismissing_records_it_too(chat_client):
    from aiforge_core.runtime import next_step

    p = next_step.Prediction(id="p-10", action="check it", tool="", args={},
                             confidence=0.9, rationale="x",
                             verdict=next_step.OFFER)
    next_step._store.remember(p, {"repo": "R", "message": "connect"})

    chat_client.post("/api/chat/suggestion/p-10", json={"accepted": False})
    assert next_step.history(5)[0]["accepted"] is False


def test_an_unknown_id_is_not_an_error(chat_client):
    """A stale chip in an old browser tab must not 500."""
    assert chat_client.post("/api/chat/suggestion/p-gone",
                            json={"accepted": True}).status_code == 200
```

- [ ] **Step 2: Run to verify it fails** — 404 on the route.

- [ ] **Step 3: Implement**

```python
@router.post("/api/chat/suggestion/{prediction_id}")
async def suggestion_outcome(prediction_id: str, request: Request) -> dict:
    """Record what the user did with a suggestion.

    Both answers are recorded. A feature that learns only from its successes
    drifts, and a dismissal is the clearer signal of the two.

    An unknown id is a no-op rather than a 404: a chip in a browser tab left
    open across a restart is not an error the user can do anything about.
    """
    from aiforge_core.runtime import next_step

    payload = await request.json()
    accepted = bool((payload or {}).get("accepted"))
    next_step.outcome(prediction_id, accepted,
                      edited=str((payload or {}).get("edited") or ""))
    return {"ok": True, "accepted": accepted}
```

Running an accepted OFFER is deliberately NOT done here: the chip's **Do it**
sends the action back as an ordinary chat message, so it goes through the same
approval gates, the same tool policy and the same transcript as anything else
the user asks for. A second execution path that bypasses those gates is exactly
the hole this feature must not open.

- [ ] **Step 4: Run to verify it passes**

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/api/routes/chat.py tests/python/runtime/test_next_step_in_turn.py
git commit -m "feat(api): record what the user did with a suggestion"
```

---

## Task 6: The chip

**Files:**
- Create: `web/src/views/Chat.SuggestionChip.tsx`
- Modify: `web/src/views/Chat.tsx`, `web/src/api/chat.ts`

- [ ] **Step 1: Write the component**

```tsx
/**
 * The predicted next step, under the reply.
 *
 * Two shapes, and both are shown. An ACT is reported in the past tense — it has
 * already happened — because a chip that appears only when there is a question
 * teaches the user that no chip means nothing was done, which is exactly the
 * wrong lesson.
 *
 * "Do it" sends the action back as an ordinary chat message rather than
 * executing anything here. That keeps every approval gate, tool policy and
 * transcript entry on one path.
 */
import { useState } from 'react';
import { api } from '../api/client';
import type { Suggestion } from '../api/chat';

export default function SuggestionChip(
  { s, onSend }: { s: Suggestion; onSend: (text: string) => void },
) {
  const [gone, setGone] = useState(false);
  if (gone) return null;

  const acted = s.verdict === 'ACT';

  async function answer(accepted: boolean) {
    setGone(true);
    try {
      await api.suggestionOutcome(s.id, accepted);
    } catch {
      /* recording an outcome must never break the chat */
    }
    if (accepted && !acted) onSend(s.action);
  }

  return (
    <div className="card" style={{ marginTop: 8, padding: '8px 12px' }}>
      <span className="muted">{acted ? 'Also did' : 'Next'}: </span>
      <span>{s.action}</span>
      {s.rationale && <span className="muted"> — {s.rationale}</span>}
      <span style={{ marginLeft: 12 }}>
        {!acted && (
          <button type="button" className="sm" onClick={() => answer(true)}>
            Do it
          </button>
        )}
        <button type="button" className="ghost sm" style={{ marginLeft: 6 }}
                onClick={() => answer(false)}>
          {acted ? 'Not useful' : 'Dismiss'}
        </button>
      </span>
    </div>
  );
}
```

- [ ] **Step 2: Wire it up**

In `web/src/api/chat.ts`:

```ts
export interface Suggestion {
  id: string;
  action: string;
  tool: string;
  confidence: number;
  rationale: string;
  verdict: 'ACT' | 'OFFER';
}
```

In `web/src/api/client.ts`:

```ts
  suggestionOutcome: (id: string, accepted: boolean) =>
    j<{ ok: boolean }>(`/chat/suggestion/${encodeURIComponent(id)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ accepted }),
    }),
```

In `Chat.tsx`, keep the last `suggestion` event in state alongside the
assistant message it followed, and render `<SuggestionChip>` under that bubble.
Clear it when the next user message is sent — a stale suggestion under a new
question is worse than none.

- [ ] **Step 3: Build**

```bash
cd web && ln -sfn ../../../web/node_modules node_modules \
  && ./node_modules/.bin/vite build && rm -f node_modules && cd ..
```

- [ ] **Step 4: Commit**

```bash
git add web/src
git commit -m "feat(web): show the predicted next step under the reply"
```

---

## Task 7: Settings, docs, and the gates

- [ ] **Step 1: Document the five settings in `.env.example`**, in the style of
the memory-sync block: what each does, and what the default costs.

- [ ] **Step 2: Surface the toggles in the Settings panel** beside memory sync —
both answer "what is this thing doing on its own".

- [ ] **Step 3: Live check.** Two real turns against a running server: one whose
next step is a read (expect ACT, reported past tense) and one whose next step is
a push (expect OFFER, never acted). Confirm `next_step_history.json` contains no
argument values and no credential from either turn.

- [ ] **Step 4: Full suite against the `main` baseline**

```bash
AIFORGE_LM_BASE_URL=http://127.0.0.1:9/v1 AIFORGE_INTENT_LM_URL=http://127.0.0.1:9/v1 \
  .venv/bin/python -m pytest tests/python tests/shell tests/tickets \
  -q --timeout=45 --timeout-method=signal -p no:randomly
```

Compare the FAILED set against `main`'s; only a difference is a regression.

- [ ] **Step 5: Ruff, cognitive complexity, then SonarQube on the NUC**

```bash
.venv/bin/python -m ruff check aiforge_core/runtime/next_step tests/python/runtime
.venv/bin/python /tmp/cc_scan.py aiforge_core/runtime/next_step
ssh ai@192.168.70.115 '…sonar-scanner-cli -Dsonar.projectKey=aiforgecrew-next-step …'
```

Fix everything this branch introduces; leave what is pre-existing on `main`.

- [ ] **Step 6: Merge to `main` and remove the worktree.**

---

## Self-review notes

**Spec coverage:** the package → Tasks 1-3; the ACT/OFFER table → 1; the call and
its fail-open → 2; the learning loop and secret handling → 3; emission and
ordering → 4; accept/dismiss → 5; the chip → 6; config, docs and gates → 7.

**Two decisions taken during planning, both stated rather than buried:**

- **"Do it" re-sends the action as a chat message** instead of executing it
  server-side. It costs one round trip and buys one execution path — every
  approval gate and tool policy already sits on it.
- **`noise.*` refusals from `redact` are ignored when storing a prediction**,
  while `secrets.*` and `private.*` are honoured. That stage judges whether a
  note is worth replicating to a fleet; a prediction trigger is neither a note
  nor replicated. The exemption is explicit so the filter itself is not loosened
  for everyone else.
