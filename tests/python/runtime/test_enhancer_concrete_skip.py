"""Change 1 — skip the enhancer LLM call for concrete short imperatives.

A concrete imperative that already names a file + action ("fix the bug in
app.py") does NOT need the enhancer's "rewrite as a build spec" LLM call — it
just delays the ReAct loop on a serial local model. Gated behind
``AIFORGE_ENHANCER_SKIP_CONCRETE`` (default ENABLED; ``=0`` force-enhances).

Hermetic: the LLM client + memory backend are monkeypatched to RAISE if the
skip path fails to short-circuit, so a leaked call is a hard failure.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import parallel_subtasks as pp


def _no_llm(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("LLM/memory must NOT run for a concrete prompt")

    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _boom)
    monkeypatch.setattr("aiforge_core.llm.client.complete", _boom)


# ─── _is_concrete_prompt ──────────────────────────────────────────────


@pytest.mark.parametrize("p", [
    "fix the bug in app.py",
    "add a test to db.py",
    "update the parser in src/parse.ts",
    "refactor utils.go",
    "edit config.yaml",
    "delete old_module.py",
    "rename foo.js",
    "implement the handler in src/api/routes.py",
])
def test_concrete_true_for_action_plus_file(p):
    assert pp._is_concrete_prompt(p) is True, p


@pytest.mark.parametrize("p", [
    "we should improve how the app handles errors across the codebase",
    "add dark mode",                       # verb, no file
    "make the tests pass",                 # no listed verb, no file
    "add a test and fix the bug in app.py",  # multi-part conjunction
    "can you tell me about this repository?",
    "parse json",                          # 'json' w/o dot, no verb
    "fix the bug in app.py because the whole thing keeps crashing on startup "
    "and we should really rethink the error handling strategy end to end here",  # too long
    # Conceptual slash-phrases name NO file — must enhance (bug-hunt #1):
    "add TCP/IP support to the server",
    "fix the client/server handshake",
    "implement read/write locking",
    "update the CI/CD pipeline config",
    # Sequenced multi-part with 'then' (bug-hunt #2):
    "fix app.py, then also rewrite db.py",
])
def test_concrete_false_for_vague_or_multipart_or_long(p):
    assert pp._is_concrete_prompt(p) is False, p


def test_concrete_false_for_multiline():
    assert pp._is_concrete_prompt("fix app.py\nand add a test to db.py") is False


# ─── _enhance skip behaviour ──────────────────────────────────────────


def test_enhance_skips_concrete_no_llm(monkeypatch):
    _no_llm(monkeypatch)
    assert pp._enhance("fix the bug in app.py") == "fix the bug in app.py"
    assert pp._enhance("add a test to db.py") == "add a test to db.py"


def test_enhance_still_calls_llm_for_vague(monkeypatch):
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "enhanced spec")
    out = pp._enhance(
        "we should improve how the app handles errors across the codebase")
    assert out == "enhanced spec"


def test_enhance_skip_disabled_via_env(monkeypatch):
    """AIFORGE_ENHANCER_SKIP_CONCRETE=0 → even a concrete prompt is enhanced."""
    monkeypatch.setenv("AIFORGE_ENHANCER_SKIP_CONCRETE", "0")
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "enhanced spec")
    assert pp._enhance("fix the bug in app.py") == "enhanced spec"
