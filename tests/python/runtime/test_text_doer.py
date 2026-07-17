"""Tests for the TEXT-PROTOCOL Doer fallback (runtime.text_doer).

The native pipeline Doer is an ADK LlmAgent using NATIVE function-calling,
which the mlx_lm 0.31 "zero tool_use" bug breaks on local models. This
module reuses the chat agent's proven ACTION/ARGS_JSON/FINAL text protocol
as an alternate Doer. These tests exercise the testable core
(``run_text_doer``) with a SCRIPTED complete_fn, the local-detection flag
(``should_use_text_protocol``), and that ``build_pipeline`` still builds
with BOTH protocols.
"""
from __future__ import annotations

import types

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import text_doer as td


def _scripted(outputs, *, seen=None):
    """A complete_fn that returns the given model turns in order. When
    ``seen`` is a list, each call appends the flattened user-message text so
    a test can assert what seed reached the model."""
    seq = list(outputs)

    def _fn(role, messages, **kw):
        if seen is not None:
            user = next((m.get("content") for m in reversed(messages)
                         if m.get("role") == "user"), "")
            seen.append(user if isinstance(user, str) else str(user))
        return seq.pop(0)

    return _fn


# ─────────────────────────── run_text_doer core ──────────────────────────

def test_run_text_doer_captures_outcome_and_tests_ok(tmp_path, monkeypatch):
    # Stub run_tests so its result is deterministically green.
    monkeypatch.setitem(ca.TOOLS, "run_tests", lambda args, cwd: {"ok": True})
    fn = _scripted([
        'THOUGHT: write it\nACTION: file_write\n'
        'ARGS_JSON: {"path": "impl.py", "content": "x = 1\\n"}',
        'THOUGHT: test it\nACTION: run_tests\nARGS_JSON: {}',
        "THOUGHT: done\nFINAL: implemented impl.py, tests green",
    ])
    out = td.run_text_doer({"plan_md": "write impl.py"}, str(tmp_path),
                           complete_fn=fn)
    assert "implemented impl.py" in out["doer_outcome"]
    assert out["tests_ok"] is True
    assert out["typecheck_ok"] is None
    assert out["lint_ok"] is None
    # the file edit actually landed in the tmp cwd
    assert (tmp_path / "impl.py").read_text() == "x = 1\n"


def test_run_text_doer_tests_fail_sets_false(tmp_path, monkeypatch):
    monkeypatch.setitem(ca.TOOLS, "run_tests", lambda args, cwd: {"ok": False})
    fn = _scripted([
        'THOUGHT: test\nACTION: run_tests\nARGS_JSON: {}',
        "THOUGHT: red\nFINAL: tests failed",
    ])
    out = td.run_text_doer({"plan_md": "p"}, str(tmp_path), complete_fn=fn)
    assert out["tests_ok"] is False


def test_run_text_doer_no_test_tool_leaves_signal_none(tmp_path):
    fn = _scripted([
        'ACTION: file_write\nARGS_JSON: {"path": "a.txt", "content": "hi"}',
        "FINAL: wrote a.txt",
    ])
    out = td.run_text_doer({"plan_md": "p"}, str(tmp_path), complete_fn=fn)
    assert out["tests_ok"] is None
    assert out["typecheck_ok"] is None
    assert out["lint_ok"] is None
    assert (tmp_path / "a.txt").read_text() == "hi"


def test_run_text_doer_typecheck_and_lint_signals(tmp_path, monkeypatch):
    monkeypatch.setitem(ca.TOOLS, "typecheck", lambda args, cwd: {"ok": True})
    monkeypatch.setitem(ca.TOOLS, "format", lambda args, cwd: {"ok": False})
    fn = _scripted([
        'ACTION: typecheck\nARGS_JSON: {}',
        'ACTION: format\nARGS_JSON: {"path": "."}',
        "FINAL: done",
    ])
    out = td.run_text_doer({"plan_md": "p"}, str(tmp_path), complete_fn=fn)
    assert out["typecheck_ok"] is True
    assert out["lint_ok"] is False       # format -> lint_ok
    assert out["tests_ok"] is None


def test_run_text_doer_folds_present_vars_skips_empty(tmp_path):
    seen: list = []
    fn = _scripted(["FINAL: ok"], seen=seen)
    td.run_text_doer(
        {"plan_md": "PLAN-BODY-XYZ", "context_brief_md": "",
         "rules_md": "RULE-BODY-ABC", "verifier_verdict": None},
        str(tmp_path), complete_fn=fn)
    seed = seen[0]
    assert "PLAN-BODY-XYZ" in seed          # present var folded in
    assert "RULE-BODY-ABC" in seed          # present var folded in
    assert "context_brief_md" not in seed   # empty var omitted (no label)


def test_run_text_doer_soft_fails_on_complete_error(tmp_path, monkeypatch):
    # No LLM retries → no escalating backoff sleeps, so the failure path is fast
    # and deterministic.
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "0")

    def _boom(role, messages, **kw):
        raise RuntimeError("model exploded")
    # Must NOT raise; returns a user-facing failure outcome with None signals.
    # chat_agent deliberately converts a completion failure into a plain,
    # actionable message (no raw stack), so the outcome reads as a warning that
    # nothing was changed — not the raw exception text.
    out = td.run_text_doer({"plan_md": "p"}, str(tmp_path), complete_fn=_boom)
    assert out["doer_outcome"]              # non-empty failure text
    _o = out["doer_outcome"].lower()
    assert ("didn't respond" in _o or "nothing was changed" in _o
            or "error" in _o or "explod" in _o)
    assert out["tests_ok"] is None
    assert out["typecheck_ok"] is None
    assert out["lint_ok"] is None


def test_run_text_doer_never_raises_on_internal_error(tmp_path, monkeypatch):
    # Force run_chat_agent itself to blow up at call time.
    def _explode(*a, **k):
        raise ValueError("boom")
    monkeypatch.setattr(ca, "run_chat_agent", _explode)
    out = td.run_text_doer({"plan_md": "p"}, str(tmp_path), complete_fn=lambda *a, **k: "x")
    assert out["doer_outcome"].startswith("text-doer error:")
    assert out["tests_ok"] is None


# ─────────────────────────── should_use_text_protocol ─────────────────────

def _ep(base_url):
    return types.SimpleNamespace(base_url=base_url, provider="openai_compatible")


def test_should_use_text_explicit_text(monkeypatch):
    monkeypatch.setenv("AIFORGE_DOER_PROTOCOL", "text")
    assert td.should_use_text_protocol() is True


def test_should_use_text_explicit_native(monkeypatch):
    monkeypatch.setenv("AIFORGE_DOER_PROTOCOL", "native")
    assert td.should_use_text_protocol() is False


def test_should_use_text_auto_local_now_native(monkeypatch):
    # Native is the default EVERYWHERE now — a local endpoint no longer forces
    # the text protocol (LM Studio etc. do native FC fine; the same reason
    # OpenWebUI works). Only AIFORGE_DOER_PROTOCOL=text opts out (for mlx-lm).
    monkeypatch.setenv("AIFORGE_DOER_PROTOCOL", "auto")
    assert td.should_use_text_protocol() is False


def test_should_use_text_default_unset_is_native(monkeypatch):
    monkeypatch.delenv("AIFORGE_DOER_PROTOCOL", raising=False)  # unset => native
    assert td.should_use_text_protocol() is False


def test_should_use_text_auto_cloud(monkeypatch):
    monkeypatch.setenv("AIFORGE_DOER_PROTOCOL", "auto")
    assert td.should_use_text_protocol() is False


def test_should_use_text_resolve_error_defaults_native(monkeypatch):
    monkeypatch.setenv("AIFORGE_DOER_PROTOCOL", "auto")
    from aiforge_core.llm import router

    def _boom(role):
        raise RuntimeError("router down")
    monkeypatch.setattr(router, "resolve", _boom)
    assert td.should_use_text_protocol() is False


# ─────────────────────────── pipeline still builds ────────────────────────

@pytest.fixture
def _stub_models(monkeypatch):
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as gt

    class _Stub(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            yield LlmResponse(content=gt.Content(
                role="model", parts=[gt.Part(text="stub")]))

    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_OBSERVABILITY_DISABLE", "1")
    import aiforge_core.runtime.pipeline as pl
    monkeypatch.setattr(pl, "build_litellm_model", lambda role: _Stub(model="stub"))
    return pl


def test_build_pipeline_with_native_doer(_stub_models, monkeypatch):
    pl = _stub_models
    monkeypatch.setattr(td, "should_use_text_protocol", lambda role="doer": False)
    wf = pl.build_pipeline(skip_researcher=True)
    assert wf is not None


def test_build_pipeline_with_text_doer(_stub_models, monkeypatch):
    pl = _stub_models
    monkeypatch.setattr(td, "should_use_text_protocol", lambda role="doer": True)
    wf = pl.build_pipeline(skip_researcher=True)
    assert wf is not None


def test_text_doer_node_runs_in_real_graph(_stub_models, monkeypatch):
    """Drive the REAL Workflow graph with a text-doer node and stubbed
    run_chat_agent — proves the FunctionNode executes inside ADK and writes
    doer_outcome + quality signals into session state."""
    import asyncio

    pl = _stub_models
    monkeypatch.setattr(td, "should_use_text_protocol", lambda role="doer": True)

    def _fake_run_chat_agent(messages, **kw):
        yield {"type": "tool", "name": "run_tests", "args": {},
               "result": {"ok": True}}
        yield {"type": "message", "text": "TEXT-DOER-RAN: implemented it"}
        yield {"type": "done"}
    monkeypatch.setattr(ca, "run_chat_agent", _fake_run_chat_agent)

    wf = pl.build_pipeline(skip_researcher=True)

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as gt

    async def _go():
        svc = InMemorySessionService()
        runner = Runner(agent=wf, app_name="t", session_service=svc,
                        auto_create_session=True)
        session = await svc.create_session(app_name="t", user_id="u")
        msg = gt.Content(role="user", parts=[gt.Part(text="do it")])
        async for _ in runner.run_async(user_id="u", session_id=session.id,
                                        new_message=msg):
            pass
        s = await svc.get_session(app_name="t", user_id="u",
                                  session_id=session.id)
        return dict(s.state or {})

    state = asyncio.run(asyncio.wait_for(_go(), timeout=120))
    assert "TEXT-DOER-RAN" in (state.get("doer_outcome") or "")
    assert state.get("tests_ok") is True


def test_feedback_verdict_is_seeded():
    # A loop re-run must show the text Doer what feedback rejected, else the
    # 2nd pass repeats the mistake blind.
    assert "feedback_verdict" in td._SEED_KEYS


def test_node_pins_workspace_jail_during_run_and_restores(monkeypatch):
    # The text-doer node must set AIFORGE_WORKSPACE_DIR = cwd while running
    # (restores the worktree jail the native scope_guard provided) and put it
    # back afterward.
    import asyncio
    import os

    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", "/prev/ws")
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/the/worktree")
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)  # start unset-ish
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/the/worktree")

    seen = {}

    def _fake_run(snapshot, cwd, **kw):
        seen["ws_during"] = os.environ.get("AIFORGE_WORKSPACE_DIR")
        seen["cwd"] = cwd
        return {"doer_outcome": "ok", "tests_ok": None,
                "typecheck_ok": None, "lint_ok": None}

    monkeypatch.setattr(td, "run_text_doer", _fake_run)

    class _Ctx:
        state = {}
    asyncio.run(td._text_doer_node(_Ctx()))
    assert seen["ws_during"] == seen["cwd"] == "/the/worktree"
    # restored (was unset going in)
    assert os.environ.get("AIFORGE_WORKSPACE_DIR") is None


def test_no_edit_guard_retries_then_flags_incomplete(monkeypatch):
    """A Doer that finishes with ZERO edits (hallucinated 'already done') gets
    one corrective retry and, still empty, is flagged incomplete."""
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime import text_doer as td
    monkeypatch.setenv("AIFORGE_DOER_MIN_EDIT_RETRIES", "1")
    passes = []

    def fake_no_edit(msgs, **kw):
        passes.append(msgs[0]["content"])
        yield {"type": "message", "text": "FINAL: already implemented, compiles"}
        yield {"type": "done"}
    monkeypatch.setattr(ca, "run_chat_agent", fake_no_edit)
    r = td.run_text_doer({"plan_md": "add priority"}, "/tmp",
                         complete_fn=lambda *a, **k: "")
    assert len(passes) == 2                      # original + 1 corrective retry
    assert "CORRECTION" in passes[1]             # retry seed carries the nudge
    assert r["edit_count"] == 0 and r["incomplete"] is True
    assert "INCOMPLETE" in r["doer_outcome"]


def test_no_edit_guard_noop_when_edit_made(monkeypatch):
    """A Doer that makes a real edit is NOT retried and NOT flagged."""
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime import text_doer as td
    passes = []

    def fake_edit(msgs, **kw):
        passes.append(1)
        yield {"type": "tool", "name": "file_patch", "result": {"ok": True}}
        yield {"type": "message", "text": "FINAL: patched"}
        yield {"type": "done"}
    monkeypatch.setattr(ca, "run_chat_agent", fake_edit)
    r = td.run_text_doer({"plan_md": "x"}, "/tmp", complete_fn=lambda *a, **k: "")
    assert len(passes) == 1 and r["edit_count"] == 1 and r["incomplete"] is False


def test_seed_prepends_codegraph_mandate_when_available(monkeypatch):
    """When a CodeGraph index exists, the Doer seed leads with the mandatory
    codegraph-first rule; without an index the seed is unchanged."""
    from aiforge_core.runtime import text_doer as td
    from aiforge_core.runtime.tools import codegraph as cg
    # gated on the SINGLE shared gate (binary + index + not disabled + not
    # opted out), not just the binary — so it never bans grep on an un-indexed repo.
    monkeypatch.setattr(cg, "enabled_for_run", lambda cwd=None: True)
    s = td._build_seed({"plan_md": "edit clean_amount"})
    assert s.startswith("MANDATORY — CodeGraph")
    assert "codegraph_callers" in s and "grep is NOT allowed" in s
    monkeypatch.setattr(cg, "enabled_for_run", lambda cwd=None: False)
    assert not td._build_seed({"plan_md": "x"}).startswith("MANDATORY")
