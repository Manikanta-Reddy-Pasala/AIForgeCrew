"""Doer tool layer — JSON-only file_write / file_patch / bulk_edit (ONE-89).

These tests pin the architectural fix that drops GA's two-channel
``<file_content>`` text-block design. After ONE-89, every tool's full
input must fit in the OpenAI ``function.arguments`` JSON object:

* ``file_write({path, content})`` — content is a string arg.
* ``file_patch({path, old_string, new_string})``.
* ``bulk_edit({edits: [{path, old_content, new_content}, ...]})``.

The deprecated ``<file_content>...</file_content>`` text-block path
is retained as a one-time-WARN fallback for the legacy mlx-lm 0.31
Java tickets that still emit text-format. Once they migrate to native
tool_calls, the fallback can be deleted.

The ``list index out of range`` regression on ONE-89 turn 4 (no-tool-call
exit path interacting with handler counters) is also pinned here.

GA isn't installable as a wheel and the AIForge test suite must be
runnable on a developer Mac without the NUC-deployed GA tree. We
inject a stub GA module into ``sys.modules`` so ``import_ga()`` returns
a synthetic ``StepOutcome`` + ``GenericAgentHandler`` pair sufficient
to instantiate ``AiForgeDoerHandler``. The test exercises the override
methods directly.
"""
from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ─── Stub GA module factory ────────────────────────────────────────────


@dataclass
class _StubStepOutcome:
    data: Any
    next_prompt: Any = None
    should_exit: bool = False


class _StubGenericAgentHandler:
    """Minimal GA base — enough for AiForgeDoerHandler.__init__ to chain
    super().__init__ and for our tool overrides to call ``_get_abs_path``,
    ``_get_anchor_prompt``, ``_run_event_hooks``."""

    def __init__(self, parent, last_history=None, cwd: str = "./temp") -> None:
        self.parent = parent
        self.cwd = cwd
        self.last_history = last_history
        self._done_hooks: list = []
        self.current_turn = 0
        self.max_turns = 0
        # GA's real base seeds these; mirror so attribute access works.
        self._violation = None

    def _get_abs_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(self.cwd, path))

    def _get_anchor_prompt(self, skip: bool = False) -> str:
        return "[anchor] continue."


def _install_stub_ga(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Install a stub GA into sys.modules + patch import_ga so
    ``_make_handler_class()`` works without the NUC-deployed GA tree.

    Returns the stub dict (same shape as ``ga_compat.import_ga()``).
    """
    stub = {
        "agent_runner_loop": lambda *a, **k: iter([]),
        "exhaust": lambda g: None,
        "StepOutcome": _StubStepOutcome,
        "LLMSession": object,
        "ToolClient": object,
        "GenericAgentHandler": _StubGenericAgentHandler,
    }
    from aiforge_core.doer import ga_runner
    monkeypatch.setattr(ga_runner, "import_ga", lambda: stub)
    return stub


# ─── Helpers ──────────────────────────────────────────────────────────


def _make_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                  allowed: set | None = None):
    """Build an AiForgeDoerHandler with stub GA + everything-allowed
    ScopeGuard. Returns ``(handler, StepOutcome)``."""
    _install_stub_ga(monkeypatch)
    from aiforge_core.doer import ga_runner
    from aiforge_core.doer.scope_guard import ScopeGuard

    HandlerCls, StepOutcome = ga_runner._make_handler_class()
    parent = types.SimpleNamespace(
        task_dir=str(tmp_path),
        verbose=False,
        _turn_end_hooks={},
        _aiforge_ticket=types.SimpleNamespace(identifier="TEST-89"),
    )
    guard = ScopeGuard(allowed or set())  # empty set = no constraint
    handler = HandlerCls(parent, scope_guard=guard, counters={},
                         last_history=None, cwd=str(tmp_path))
    return handler, StepOutcome


def _drive(gen) -> Any:
    """Exhaust a do_* generator, return the StepOutcome from .return.

    do_file_write / do_file_patch yield log strings then ``return
    StepOutcome(...)``. We need to walk to StopIteration to grab
    e.value (matches GA's ``exhaust`` helper)."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


# ─── do_file_write ─────────────────────────────────────────────────────


class TestDoFileWriteJsonOnly:
    """JSON-only path: args carry path + content, no text-block needed."""

    def test_writes_file_with_json_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = _make_handler(tmp_path, monkeypatch)
        target = tmp_path / "hello.py"
        outcome = _drive(handler.do_file_write(
            {"path": str(target), "content": "print('hi')\n"},
            response="",
        ))
        assert outcome.data["status"] == "success"
        assert outcome.data["path"] == str(target)
        assert outcome.data["bytes_written"] == len("print('hi')\n")
        assert target.read_text() == "print('hi')\n"
        # Counter increments — gates downstream require ≥1 edit_block_ok.
        assert handler._counters["edit_block_ok"] == 1

    def test_auto_creates_parent_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ONE-89 turn 3 repro: model wrote ``src/shapes/__init__.py``
        but ``src/shapes/`` didn't exist — tool errored. Fix: tool
        auto-creates parent dirs."""
        handler, _ = _make_handler(tmp_path, monkeypatch)
        nested = tmp_path / "src" / "shapes" / "__init__.py"
        assert not nested.parent.exists()
        outcome = _drive(handler.do_file_write(
            {"path": str(nested), "content": "# pkg\n"},
            response="",
        ))
        assert outcome.data["status"] == "success"
        assert nested.exists()
        assert nested.read_text() == "# pkg\n"

    def test_missing_content_arg_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No content arg AND no <file_content> block → JSON-validation
        style error message that points the model at the right fix."""
        handler, _ = _make_handler(tmp_path, monkeypatch)
        target = tmp_path / "x.py"
        outcome = _drive(handler.do_file_write(
            {"path": str(target)},  # no content
            response="",
        ))
        assert outcome.data["status"] == "error"
        assert "'content' arg missing" in outcome.data["msg"]
        # File NOT created (preserves no-side-effects on input error).
        assert not target.exists()

    def test_missing_path_arg_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = _make_handler(tmp_path, monkeypatch)
        outcome = _drive(handler.do_file_write(
            {"content": "x"},  # no path
            response="",
        ))
        assert outcome.data["status"] == "error"
        assert "'path' arg" in outcome.data["msg"]


class TestDoFileWriteDeprecatedFallback:
    """Backwards-compat: legacy <file_content>...</file_content> in
    response text fills in for missing JSON content arg. This protects
    mlx-lm 0.31 Java tickets that still emit the old format."""

    def test_text_block_fallback_writes_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = _make_handler(tmp_path, monkeypatch)
        target = tmp_path / "legacy.py"
        # response carries the inline <file_content> block; tool_call
        # args lack ``content`` (legacy mlx-lm shape).
        response = (
            "Some narration.\n"
            "<file_content>print('legacy')\n</file_content>\n"
            "Trailing text."
        )
        outcome = _drive(handler.do_file_write(
            {"path": str(target)},
            response=response,
        ))
        assert outcome.data["status"] == "success"
        assert target.read_text() == "print('legacy')\n"


# ─── do_file_patch ─────────────────────────────────────────────────────


class TestDoFilePatchJsonOnly:
    """JSON-only path: args carry path + old_string + new_string."""

    def test_patches_file_with_old_new_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = _make_handler(tmp_path, monkeypatch)
        target = tmp_path / "calc.py"
        target.write_text("def add(a, b):\n    return a + b\n")
        outcome = _drive(handler.do_file_patch(
            {"path": str(target),
             "old_string": "return a + b",
             "new_string": "return a + b + 0  # noqa"},
            response="",
        ))
        assert outcome.data["status"] == "success"
        assert "noqa" in target.read_text()
        assert handler._counters["edit_block_ok"] == 1

    def test_accepts_old_content_new_content_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bulk_edit forwards in {old_content, new_content} shape;
        do_file_patch should accept either alias."""
        handler, _ = _make_handler(tmp_path, monkeypatch)
        target = tmp_path / "x.py"
        target.write_text("hello\n")
        outcome = _drive(handler.do_file_patch(
            {"path": str(target),
             "old_content": "hello",
             "new_content": "world"},
            response="",
        ))
        assert outcome.data["status"] == "success"
        assert target.read_text() == "world\n"

    def test_old_string_not_found_errors_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = _make_handler(tmp_path, monkeypatch)
        target = tmp_path / "x.py"
        target.write_text("foo\n")
        outcome = _drive(handler.do_file_patch(
            {"path": str(target),
             "old_string": "BANG",
             "new_string": "BAZ"},
            response="",
        ))
        assert outcome.data["status"] == "error"
        assert "not found" in outcome.data["msg"]
        # Counter NOT bumped on failure.
        assert handler._counters.get("edit_block_ok", 0) == 0

    def test_old_string_ambiguous_errors_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When old_string matches multiple times, refuse rather than
        guessing — model needs to add unique context."""
        handler, _ = _make_handler(tmp_path, monkeypatch)
        target = tmp_path / "x.py"
        target.write_text("a\na\na\n")
        outcome = _drive(handler.do_file_patch(
            {"path": str(target),
             "old_string": "a",
             "new_string": "b"},
            response="",
        ))
        assert outcome.data["status"] == "error"
        assert "ambiguous" in outcome.data["msg"]
        # File untouched.
        assert target.read_text() == "a\na\na\n"

    def test_missing_args_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = _make_handler(tmp_path, monkeypatch)
        outcome = _drive(handler.do_file_patch(
            {"path": str(tmp_path / "x.py")},  # no old/new string
            response="",
        ))
        assert outcome.data["status"] == "error"


# ─── do_bulk_edit ──────────────────────────────────────────────────────


class TestDoBulkEditJsonOnly:
    """bulk_edit forwards each item to do_file_patch — pure JSON args."""

    def test_applies_multiple_edits_atomically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = _make_handler(tmp_path, monkeypatch)
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("ALPHA\n")
        b.write_text("BETA\n")
        outcome = _drive(handler.do_bulk_edit(
            {"edits": [
                {"path": str(a), "old_content": "ALPHA",
                 "new_content": "alpha"},
                {"path": str(b), "old_content": "BETA",
                 "new_content": "beta"},
            ]},
            response="",
        ))
        # Both files patched.
        assert a.read_text() == "alpha\n"
        assert b.read_text() == "beta\n"
        # Both edits counted.
        assert handler._counters["edit_block_ok"] == 2


# ─── No-tool-call exit path / list index out of range regression ───────


class TestNoToolCallExitGuard:
    """ONE-89 turn 4: model emitted only narrative text (zero tool_calls).
    The handler's ``_done_hooks`` attribute must always exist so the
    GA agent_loop's exit logic can read it without IndexError /
    AttributeError. This pins the defensive seeding in __init__."""

    def test_handler_seeds_done_hooks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler, _ = _make_handler(tmp_path, monkeypatch)
        # Attribute exists, is a list, is empty (no-op pop is safe via
        # the len()==0 guard in agent_loop).
        assert hasattr(handler, "_done_hooks")
        assert isinstance(handler._done_hooks, list)

    def test_pop_from_empty_done_hooks_does_not_index_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate what agent_loop.py:99-101 does on a NO_TOOL_CALL
        exit: check len, pop only if non-zero. Empty list = no pop,
        no IndexError. This pins the contract used by GA's loop."""
        handler, _ = _make_handler(tmp_path, monkeypatch)
        # Empty by design.
        assert len(handler._done_hooks) == 0
        # The GA loop's guard pattern.
        if len(handler._done_hooks) == 0:
            popped = None
        else:  # would only execute if non-empty
            popped = handler._done_hooks.pop(0)
        assert popped is None  # path taken: empty-list-skip


# ─── Schema introspection ──────────────────────────────────────────────


class TestNativeSchemas:
    """The OpenAI tools array advertises file_write/file_patch with
    proper JSON params (no <file_content> mention)."""

    def test_file_write_schema_requires_content_string(self) -> None:
        from aiforge_core.doer import ga_runner
        sch = ga_runner._FILE_WRITE_SCHEMA["function"]
        assert sch["name"] == "file_write"
        params = sch["parameters"]
        assert params["properties"]["content"]["type"] == "string"
        assert params["properties"]["path"]["type"] == "string"
        assert set(params["required"]) == {"path", "content"}
        # Description must explicitly tell the model NOT to use the old
        # <file_content> text-block channel — anti-instruction is fine,
        # promoting it as a positive option is not.
        desc = sch["description"]
        assert "do NOT" in desc or "NOT use" in desc

    def test_file_patch_schema_requires_old_new_string(self) -> None:
        from aiforge_core.doer import ga_runner
        sch = ga_runner._FILE_PATCH_SCHEMA["function"]
        assert sch["name"] == "file_patch"
        params = sch["parameters"]
        assert params["properties"]["old_string"]["type"] == "string"
        assert params["properties"]["new_string"]["type"] == "string"
        assert set(params["required"]) == {"path", "old_string", "new_string"}
        desc = sch["description"]
        assert "do NOT" in desc or "NOT use" in desc


class TestPreambleNoFileContent:
    """``_DOER_GA_PREAMBLE_TMPL`` (and the rendered preamble) must not
    advertise the deprecated <file_content> text-block channel."""

    def test_template_does_not_advertise_text_block(self) -> None:
        """Anti-instructions ('do NOT use') are fine — they steer the
        model away from the legacy channel. What MUST NOT appear is
        the marker presented as an INPUT path (e.g. 'put content in
        <file_content> tags'). Verify the only mentions are negated."""
        from aiforge_core.doer import ga_runner
        tmpl = ga_runner._DOER_GA_PREAMBLE_TMPL
        # Forbidden: any positive instruction that names <file_content>
        # as a place to put content.
        forbidden = (
            "put content in <file_content>",
            "put content inside <file_content>",
            "use <file_content>",
            "wrap content in <file_content>",
        )
        for needle in forbidden:
            assert needle.lower() not in tmpl.lower(), (
                f"preamble still promotes <file_content> via: {needle}"
            )

    def test_template_documents_json_args(self) -> None:
        """Positive guidance: tells the model where to put content."""
        from aiforge_core.doer import ga_runner
        tmpl = ga_runner._DOER_GA_PREAMBLE_TMPL
        # New copy advertises the JSON-only contract explicitly.
        assert "JSON" in tmpl or "json" in tmpl.lower()
        # file_write / file_patch lines mention their concrete args.
        assert "content" in tmpl.lower()
        assert "old_string" in tmpl
