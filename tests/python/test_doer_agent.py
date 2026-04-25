"""Unit tests for aiforge_core.doer — Phase 1 smolagents Doer.

Tests are fully offline: no LLM, no Postgres, no filesystem writes
beyond a tmp directory.
"""
from __future__ import annotations

import textwrap
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_core.doer.scope_guard import ScopeGuard, ScopeViolation, parse_allowed_files
from aiforge_core.doer.tools import make_edit_block, make_read_file


# ─────────────────────────── helpers ────────────────────────────────────

def _make_scope_guard(paths: list[str]) -> ScopeGuard:
    return ScopeGuard(set(paths))


# ─────────────────────────── 1. scope_guard_blocks_outside_path ─────────

class TestScopeGuard:
    def test_blocks_outside_path(self, tmp_path: Path) -> None:
        """AC: ScopeGuard raises ScopeViolation for unlisted paths."""
        allowed_file = str(tmp_path / "src" / "Foo.java")
        guard = _make_scope_guard([allowed_file])
        outside = str(tmp_path / "src" / "Bar.java")
        with pytest.raises(ScopeViolation) as exc_info:
            guard.check(outside)
        assert "Bar.java" in str(exc_info.value)

    def test_allows_matching_basename(self, tmp_path: Path) -> None:
        guard = _make_scope_guard(["src/main/java/Foo.java"])
        guard.check(str(tmp_path / "repo" / "src" / "main" / "java" / "Foo.java"))

    def test_allows_when_no_constraint(self, tmp_path: Path) -> None:
        guard = _make_scope_guard([])  # empty = no constraint
        guard.check(str(tmp_path / "anything" / "Goes.java"))  # must not raise

    def test_parse_allowed_files_files_header(self) -> None:
        body = textwrap.dedent("""
            ## Scope
            Fix the bug.

            ## Files
            - src/main/java/com/example/Foo.java:45
            - src/test/java/FooTest.java

            ## Acceptance
            Tests pass.
        """)
        result = parse_allowed_files(body)
        assert "src/main/java/com/example/Foo.java" in result
        assert "src/test/java/FooTest.java" in result

    def test_parse_allowed_files_allowed_header(self) -> None:
        body = textwrap.dedent("""
            ## Allowed files
            - src/main/java/Bar.java
        """)
        result = parse_allowed_files(body)
        assert "src/main/java/Bar.java" in result

    def test_parse_returns_empty_when_no_header(self) -> None:
        body = "No files section here.\n"
        result = parse_allowed_files(body)
        assert result == set()


# ─────────────────────────── 2. edit_block_happy_path ───────────────────

class TestEditBlock:
    def test_happy_path(self, tmp_path: Path) -> None:
        """AC: edit_block replaces exactly one occurrence and writes the file."""
        target = tmp_path / "Foo.java"
        target.write_text("public class Foo {\n    int x = 1;\n}\n", encoding="utf-8")

        guard = _make_scope_guard([str(target)])
        edit_block_tool = make_edit_block(str(tmp_path), guard)

        result = edit_block_tool(
            path="Foo.java",
            find="    int x = 1;\n",
            replace="    int x = 42;\n",
        )

        assert "OK:" in result
        assert target.read_text(encoding="utf-8") == "public class Foo {\n    int x = 42;\n}\n"

    def test_find_not_found(self, tmp_path: Path) -> None:
        target = tmp_path / "Foo.java"
        target.write_text("public class Foo {}\n", encoding="utf-8")
        guard = _make_scope_guard([str(target)])
        edit_block_tool = make_edit_block(str(tmp_path), guard)

        result = edit_block_tool(path="Foo.java", find="DOES_NOT_EXIST", replace="x")
        assert "ERROR" in result
        assert "not found" in result

    def test_ambiguous_match(self, tmp_path: Path) -> None:
        target = tmp_path / "Foo.java"
        target.write_text("x\nx\n", encoding="utf-8")
        guard = _make_scope_guard([str(target)])
        edit_block_tool = make_edit_block(str(tmp_path), guard)

        result = edit_block_tool(path="Foo.java", find="x\n", replace="y\n")
        assert "ERROR" in result
        assert "2" in result  # "matches 2 times"

    def test_scope_violation(self, tmp_path: Path) -> None:
        allowed = tmp_path / "Allowed.java"
        allowed.write_text("// allowed\n")
        outside = tmp_path / "Outside.java"
        outside.write_text("// not allowed\n")

        guard = _make_scope_guard([str(allowed)])
        edit_block_tool = make_edit_block(str(tmp_path), guard)

        result = edit_block_tool(path="Outside.java", find="// not allowed", replace="x")
        assert "SCOPE_VIOLATION" in result


# ─────────────────────────── 3. final_answer_stops_loop ─────────────────

class TestFinalAnswerStopsLoop:
    """Verify final_answer tool + ToolCallingAgent construction."""

    def test_final_answer_tool_returns_summary(self) -> None:
        """AC: final_answer tool returns its argument unchanged."""
        from aiforge_core.doer.tools import final_answer

        result = final_answer(summary="all done")
        assert result == "all done"

    def test_tool_calling_agent_constructs_with_our_tools(self, tmp_path: Path) -> None:
        """AC: ToolCallingAgent accepts our tool list without error."""
        pytest.importorskip("smolagents")
        from smolagents import ToolCallingAgent

        from aiforge_core.doer.scope_guard import ScopeGuard
        from aiforge_core.doer.tools import make_tools

        guard = ScopeGuard(set())
        tools = make_tools(str(tmp_path), guard)

        # Use a stub model — we only test construction, not invocation.
        class _StubModel:
            last_input_token_count = 0
            last_output_token_count = 0

            def __call__(self, *a, **kw):
                raise RuntimeError("stub model should not be called in this test")

        # Construction must not raise.
        agent = ToolCallingAgent(
            tools=tools,
            model=_StubModel(),
            max_steps=1,
        )
        # ToolCallingAgent must have been created — spot-check it has tools.
        agent_tools = getattr(agent, "tools", None) or {}
        tool_names = (
            set(agent_tools.keys())
            if isinstance(agent_tools, dict)
            else {t.name for t in agent_tools}
        )
        # final_answer is always present (added automatically by ToolCallingAgent).
        assert "final_answer" in tool_names


# ─────────────────────────── 4. compile_fail_propagates ─────────────────

class TestCompileFailPropagates:
    def test_non_zero_exit_captured(self, tmp_path: Path) -> None:
        """AC: run_compile returns EXIT=1 and error text on compile failure."""
        from aiforge_core.doer.tools import make_run_compile

        run_compile = make_run_compile(str(tmp_path))

        fake_proc = types.SimpleNamespace(
            returncode=1,
            stdout=b"[ERROR] Compilation failure\n",
            stderr=b"BUILD FAILURE\n",
        )
        with patch("aiforge_core.doer.tools.subprocess.run", return_value=fake_proc):
            output = run_compile()

        assert "EXIT=1" in output
        assert "Compilation failure" in output or "BUILD FAILURE" in output

    def test_zero_exit_on_success(self, tmp_path: Path) -> None:
        from aiforge_core.doer.tools import make_run_compile

        run_compile = make_run_compile(str(tmp_path))

        fake_proc = types.SimpleNamespace(
            returncode=0,
            stdout=b"BUILD SUCCESS\n",
            stderr=b"",
        )
        with patch("aiforge_core.doer.tools.subprocess.run", return_value=fake_proc):
            output = run_compile()

        assert "EXIT=0" in output


# ─────────────────────────── 5. result_shrink_helpers ──────────────────

class TestResultShrinkHelpers:
    """GA-style tool-result compaction (auto-shrink + line cap)."""

    def test_cap_lines_under_threshold_unchanged(self) -> None:
        from aiforge_core.doer.tools import _cap_lines

        text = "a\nb\nc"
        assert _cap_lines(text, max_lines=10) == text

    def test_cap_lines_over_threshold_truncates(self) -> None:
        from aiforge_core.doer.tools import _cap_lines

        text = "\n".join(str(i) for i in range(20))
        out = _cap_lines(text, max_lines=5, hint="use offset")
        assert out.startswith("0\n1\n2\n3\n4")
        assert "15 more lines" in out
        assert "use offset" in out

    def test_shrink_fenced_blocks_short_unchanged(self) -> None:
        from aiforge_core.doer.tools import _shrink_fenced_blocks

        text = "before\n```py\nx = 1\ny = 2\n```\nafter"
        assert _shrink_fenced_blocks(text) == text

    def test_shrink_fenced_blocks_long_truncates(self) -> None:
        from aiforge_core.doer.tools import _shrink_fenced_blocks

        body_lines = [f"line{i}" for i in range(20)]
        text = "head\n```java\n" + "\n".join(body_lines) + "\n```\ntail"
        out = _shrink_fenced_blocks(text)
        assert "line0" in out
        assert "line4" in out  # 5-line preview keeps 0..4
        assert "line19" not in out
        assert "(20 lines)" in out
        assert "```java" in out  # language tag preserved

    def test_read_file_caps_at_800_lines(self, tmp_path: Path) -> None:
        from aiforge_core.doer.tools import make_read_file

        big = tmp_path / "Huge.java"
        big.write_text("\n".join(f"// line {i}" for i in range(2000)))
        read_file = make_read_file(str(tmp_path))
        out = read_file(path="Huge.java")
        assert "// line 0" in out
        assert "// line 799" in out
        assert "// line 1999" not in out
        assert "more lines" in out
        assert "offset/limit" in out

    def test_grep_caps_at_120_lines(self, tmp_path: Path) -> None:
        from aiforge_core.doer.tools import make_grep

        # ripgrep must be installed for this test (it is in dev env).
        for i in range(300):
            (tmp_path / f"f{i}.txt").write_text("MARKER\n")
        grep = make_grep(str(tmp_path))
        out = grep(pattern="MARKER", path=".")
        if "ERROR: rg" in out:
            pytest.skip("ripgrep not installed")
        line_count = len(out.splitlines())
        # Capped at 120 + 1 truncation marker line.
        assert line_count <= 122, out
