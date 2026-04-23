"""Unit tests for aiforge_core.planner — Phase 2 smolagents Planner.

All tests are fully offline: no LLM, no Postgres, no network.
"""
from __future__ import annotations

import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ─────────────────────────── shared fixtures ────────────────────────────

@dataclass
class _FakeTicket:
    id: int = 1
    identifier: str = "ONE-42"
    title: str = "Add validation to sync handler"
    body: str = "Improve error handling in the push sync path.\n"
    status: str = "in_progress"
    priority: str = "medium"
    assignee_role: str | None = "planner"
    parent_id: int | None = None
    branch: str | None = "aiforge/ONE-42"
    project: str | None = "PosServerBackend"
    labels: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


def _make_ctx(tmp_path: Path, ticket: _FakeTicket | None = None) -> dict:
    if ticket is None:
        ticket = _FakeTicket()
    import logging
    return {
        "ticket": ticket,
        "worktree_root": str(tmp_path),
        "store": None,
        "log": logging.getLogger("aiforge.test"),
    }


# ─────────────────────────── 1. TestTools: grep_repos ───────────────────

class TestGrepRepos:
    def test_returns_no_matches_string_on_empty(self, tmp_path: Path) -> None:
        """grep_repos returns '(no matches)' when rg exits 1 with no output."""
        from aiforge_core.planner.tools import make_grep_repos

        ctx = _make_ctx(tmp_path)
        grep = make_grep_repos(ctx)

        fake_proc = types.SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"",
        )
        with patch("aiforge_core.planner.tools.subprocess.run", return_value=fake_proc):
            result = grep(pattern="NonExistentClass", glob="*.java")

        assert result == "(no matches)"

    def test_respects_worktree_root(self, tmp_path: Path) -> None:
        """grep_repos passes WORKTREE_ROOT as the search root to rg."""
        from aiforge_core.planner.tools import make_grep_repos

        ctx = _make_ctx(tmp_path)
        grep = make_grep_repos(ctx)

        captured: list[list] = []

        def _fake_run(cmd, **kwargs):
            captured.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=b"file.java:1: match\n", stderr=b"")

        with patch("aiforge_core.planner.tools.subprocess.run", side_effect=_fake_run):
            grep(pattern="SomeClass", glob="*.java")

        assert len(captured) == 1
        # Last positional arg to rg should be the worktree_root.
        assert captured[0][-1] == str(tmp_path)

    def test_truncates_output_to_8000_chars(self, tmp_path: Path) -> None:
        """grep_repos truncates output at 8000 chars."""
        from aiforge_core.planner.tools import make_grep_repos

        ctx = _make_ctx(tmp_path)
        grep = make_grep_repos(ctx)

        big_output = b"x" * 10_000

        fake_proc = types.SimpleNamespace(returncode=0, stdout=big_output, stderr=b"")
        with patch("aiforge_core.planner.tools.subprocess.run", return_value=fake_proc):
            result = grep(pattern="x", glob="*.java")

        assert len(result) == 8000

    def test_multi_glob_builds_multiple_glob_flags(self, tmp_path: Path) -> None:
        """Each comma-separated glob pattern is passed as a separate --glob flag."""
        from aiforge_core.planner.tools import make_grep_repos

        ctx = _make_ctx(tmp_path)
        grep = make_grep_repos(ctx)

        captured: list[list] = []

        def _fake_run(cmd, **kwargs):
            captured.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with patch("aiforge_core.planner.tools.subprocess.run", side_effect=_fake_run):
            grep(pattern="foo", glob="*.java,*.py")

        cmd = captured[0]
        # Count --glob occurrences; should be 2.
        assert cmd.count("--glob") == 2
        assert "*.java" in cmd
        assert "*.py" in cmd


# ─────────────────────────── 2. TestTools: list_repos ───────────────────

class TestListRepos:
    def test_lists_directories(self, tmp_path: Path) -> None:
        """list_repos returns directory names under worktree_root."""
        from aiforge_core.planner.tools import make_list_repos

        # Create two sub-directories and one file.
        (tmp_path / "RepoA").mkdir()
        (tmp_path / "RepoB").mkdir()
        (tmp_path / "not-a-dir.txt").write_text("x")

        ctx = _make_ctx(tmp_path)
        list_repos = make_list_repos(ctx)
        result = list_repos()

        assert "RepoA" in result
        assert "RepoB" in result
        assert "not-a-dir.txt" not in result

    def test_returns_error_on_missing_root(self, tmp_path: Path) -> None:
        """list_repos returns ERROR string when root is missing."""
        from aiforge_core.planner.tools import make_list_repos

        ctx = _make_ctx(tmp_path)
        ctx["worktree_root"] = str(tmp_path / "does_not_exist")
        list_repos = make_list_repos(ctx)
        result = list_repos()

        assert "ERROR" in result


# ─────────────────────────── 3. TestWritePlanTool ───────────────────────

class TestWritePlanTool:
    def test_appends_sections_and_calls_update(self, tmp_path: Path) -> None:
        """write_plan appends ## Files / ## Plan / ## Cross-service to ticket body."""
        from aiforge_core.planner.tools import make_write_plan

        ticket = _FakeTicket(body="Original body.\n")
        ctx = _make_ctx(tmp_path, ticket=ticket)

        # MagicMock supports context-manager protocol automatically.
        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        write_plan = make_write_plan(ctx)

        with patch("aiforge_core.planner.tools.psycopg.connect", return_value=mock_conn):
            result = write_plan(
                files=["src/main/java/Foo.java", "src/main/java/Bar.java"],
                plan="1. Fix the null check.\n2. Add unit test.",
                cross_service="Notify PosClientBackend via NATS.",
            )

        assert "OK:" in result
        assert "## Files" in ticket.body
        assert "src/main/java/Foo.java" in ticket.body
        assert "## Plan" in ticket.body
        assert "Fix the null check" in ticket.body
        assert "## Cross-service" in ticket.body
        assert "NATS" in ticket.body

        # cursor.execute must have been called once with an UPDATE statement.
        mock_cur.execute.assert_called_once()
        call_sql = mock_cur.execute.call_args[0][0]
        call_params = mock_cur.execute.call_args[0][1]
        assert "UPDATE tickets" in call_sql
        assert ticket.id in call_params

    def test_no_cross_service_omits_section(self, tmp_path: Path) -> None:
        """write_plan omits ## Cross-service when cross_service is empty."""
        from aiforge_core.planner.tools import make_write_plan

        ticket = _FakeTicket(body="Body.\n")
        ctx = _make_ctx(tmp_path, ticket=ticket)

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        write_plan = make_write_plan(ctx)

        with patch("aiforge_core.planner.tools.psycopg.connect", return_value=mock_conn):
            write_plan(files=["src/Foo.java"], plan="1. Do it.", cross_service="")

        assert "## Cross-service" not in ticket.body

    def test_returns_error_string_on_db_failure(self, tmp_path: Path) -> None:
        """write_plan returns ERROR string (does not raise) when DB fails."""
        from aiforge_core.planner.tools import make_write_plan

        ctx = _make_ctx(tmp_path)
        write_plan = make_write_plan(ctx)

        with patch("aiforge_core.planner.tools.psycopg.connect",
                   side_effect=Exception("connection refused")):
            result = write_plan(files=["x.py"], plan="1. step")

        assert "ERROR" in result
        assert "connection refused" in result


# ─────────────────────────── 4. TestCreateChildTicketTool ───────────────

class TestCreateChildTicketTool:
    def test_returns_identifier_on_success(self, tmp_path: Path) -> None:
        """create_child_ticket returns the new ticket identifier string."""
        from aiforge_core.planner.tools import make_create_child_ticket

        ticket = _FakeTicket(id=1)
        ctx = _make_ctx(tmp_path, ticket=ticket)
        create_child = make_create_child_ticket(ctx)

        fake_child = _FakeTicket(id=2, identifier="ONE-43")

        with patch("aiforge_core.planner.tools.tickets") as mock_tickets:
            mock_tickets.create.return_value = fake_child
            result = create_child(
                title="Sub-task: update PosClientBackend",
                body="See parent ONE-42.",
                project="PosClientBackend",
                assignee_role="planner",
            )

        assert result == "ONE-43"
        mock_tickets.create.assert_called_once_with(
            title="Sub-task: update PosClientBackend",
            body="See parent ONE-42.",
            parent_id=ticket.id,
            project="PosClientBackend",
            assignee_role="planner",
        )

    def test_returns_error_string_on_failure(self, tmp_path: Path) -> None:
        """create_child_ticket returns ERROR string (does not raise) on exception."""
        from aiforge_core.planner.tools import make_create_child_ticket

        ctx = _make_ctx(tmp_path)
        create_child = make_create_child_ticket(ctx)

        with patch("aiforge_core.planner.tools.tickets") as mock_tickets:
            mock_tickets.create.side_effect = Exception("DB down")
            result = create_child(title="x", body="y", project="z")

        assert "ERROR" in result
        assert "DB down" in result


# ─────────────────────────── 5. TestReadFileTool ────────────────────────

class TestReadFileTool:
    def test_reads_slice(self, tmp_path: Path) -> None:
        """read_file returns only the requested line slice."""
        from aiforge_core.planner.tools import make_read_file

        src = tmp_path / "Test.java"
        src.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

        ctx = _make_ctx(tmp_path)
        read_file = make_read_file(ctx)

        result = read_file(path=str(src), start_line=2, end_line=3)
        assert "line2" in result
        assert "line3" in result
        assert "line1" not in result
        assert "line4" not in result

    def test_returns_error_on_missing_file(self, tmp_path: Path) -> None:
        """read_file returns ERROR string when file is not found."""
        from aiforge_core.planner.tools import make_read_file

        ctx = _make_ctx(tmp_path)
        read_file = make_read_file(ctx)

        result = read_file(path="/nonexistent/path/Foo.java")
        assert "ERROR" in result


# ─────────────────────────── 6. TestAgentBuilds ─────────────────────────

class TestAgentBuilds:
    def test_build_planner_agent_returns_code_agent_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default backend is CodeAgent (EVAL-1 winner)."""
        pytest.importorskip("smolagents")
        from smolagents import CodeAgent

        from aiforge_core.planner.agent import build_planner_agent

        ticket = _FakeTicket()

        class _StubModel:
            last_input_token_count = 0
            last_output_token_count = 0

            def __call__(self, *a, **kw):
                raise RuntimeError("stub should not be called")

        class _FakeLLMConfig:
            base_url = "http://localhost:1234/v1"
            model = "qwen3.6-35b-a3b"
            api_key = "test"

        monkeypatch.delenv("AIFORGE_PLANNER_BACKEND", raising=False)
        with patch("aiforge_core.planner.agent.LiteLLMModel", return_value=_StubModel()):
            agent, task_prompt = build_planner_agent(
                ticket, "context bundle", _FakeLLMConfig()
            )

        assert isinstance(agent, CodeAgent)
        assert "final_answer" in (
            set(agent.tools.keys())
            if isinstance(agent.tools, dict)
            else {t.name for t in agent.tools}
        )
        assert ticket.body in task_prompt

    def test_build_planner_agent_backend_flag_selects_toolcalling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AIFORGE_PLANNER_BACKEND=toolcalling falls back to ToolCallingAgent."""
        pytest.importorskip("smolagents")
        from smolagents import ToolCallingAgent

        from aiforge_core.planner.agent import build_planner_agent

        class _StubModel:
            def __call__(self, *a, **kw):
                raise RuntimeError("stub")

        class _FakeLLMConfig:
            base_url = "http://localhost:1234/v1"
            model = "qwen3.6-35b-a3b"
            api_key = "test"

        monkeypatch.setenv("AIFORGE_PLANNER_BACKEND", "toolcalling")
        with patch("aiforge_core.planner.agent.LiteLLMModel", return_value=_StubModel()):
            agent, _ = build_planner_agent(_FakeTicket(), "ctx", _FakeLLMConfig())
        assert isinstance(agent, ToolCallingAgent)

    def test_build_planner_agent_rejects_unknown_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiforge_core.planner.agent import build_planner_agent

        class _FakeLLMConfig:
            base_url = "http://localhost:1234/v1"
            model = "qwen3.6-35b-a3b"
            api_key = "test"

        monkeypatch.setenv("AIFORGE_PLANNER_BACKEND", "bogus")
        with patch("aiforge_core.planner.agent.LiteLLMModel"):
            with pytest.raises(ValueError, match="AIFORGE_PLANNER_BACKEND"):
                build_planner_agent(_FakeTicket(), "ctx", _FakeLLMConfig())

    def test_model_id_gets_openai_prefix(self, tmp_path: Path) -> None:
        """build_planner_agent prepends 'openai/' when model has no slash."""
        pytest.importorskip("smolagents")

        from aiforge_core.planner.agent import build_planner_agent

        ticket = _FakeTicket()

        class _FakeLLMConfig:
            base_url = "http://localhost:1234/v1"
            model = "gemma-4-26b"  # no slash
            api_key = "test"

        captured_kwargs: list[dict] = []

        class _CapturingModel:
            last_input_token_count = 0
            last_output_token_count = 0

            def __call__(self, *a, **kw):
                raise RuntimeError("stub")

        import logging

        def _fake_lm(**kwargs):
            captured_kwargs.append(kwargs)
            return _CapturingModel()

        with patch("aiforge_core.planner.agent.LiteLLMModel", side_effect=_fake_lm):
                build_planner_agent(ticket, "ctx", _FakeLLMConfig())

        assert len(captured_kwargs) == 1
        model_val = captured_kwargs[0].get("model_id") or captured_kwargs[0].get("model")
        assert model_val == "openai/gemma-4-26b"


# ─────────────────────────── 7. TestExtractSignatures ───────────────────

class TestExtractSignatures:
    _JAVA_SOURCE = """\
package com.example;

import java.util.List;

public class SyncController {

    private final SyncService syncService;

    public SyncController(SyncService syncService) {
        this.syncService = syncService;
    }

    public Mono<ResponseEntity<?>> queryAndProcess(@RequestBody MessageRequest<T> request) {
        return syncService.process(request);
    }

    protected Mono<Object> processMessageDirect(MessageRequest<?> request) {
        return syncService.processDirect(request);
    }

    private void helperMethod(String value) {
        // internal
    }
}
"""

    def test_java_method_signatures(self, tmp_path: Path) -> None:
        """extract_signatures returns line-prefixed public/protected/private sigs for Java."""
        from aiforge_core.planner.tools import make_extract_signatures

        src = tmp_path / "SyncController.java"
        src.write_text(self._JAVA_SOURCE, encoding="utf-8")

        ctx = _make_ctx(tmp_path)
        extract = make_extract_signatures(ctx)

        result = extract(path=str(src))

        # All three visibility levels should appear.
        assert "queryAndProcess" in result
        assert "processMessageDirect" in result
        assert "helperMethod" in result
        # Each result line must start with a line number.
        for line in result.splitlines():
            parts = line.split(":", 1)
            assert parts[0].strip().isdigit(), f"Expected line number prefix in: {line!r}"

    def test_respects_line_range(self, tmp_path: Path) -> None:
        """extract_signatures only scans lines within start_line..end_line."""
        from aiforge_core.planner.tools import make_extract_signatures

        src = tmp_path / "SyncController.java"
        src.write_text(self._JAVA_SOURCE, encoding="utf-8")

        ctx = _make_ctx(tmp_path)
        extract = make_extract_signatures(ctx)

        # The constructor is on line 9.  Restrict to lines 1-8 — should miss the constructor.
        result_narrow = extract(path=str(src), start_line=1, end_line=8)
        assert "SyncController" not in result_narrow or result_narrow == "(no signatures found in range)"

        # The first public method (queryAndProcess) is on line 13.  Lines 1-12 miss it.
        result_before = extract(path=str(src), start_line=1, end_line=12)
        assert "queryAndProcess" not in result_before

        # Lines 13-14 cover the queryAndProcess declaration.
        result_method = extract(path=str(src), start_line=13, end_line=14)
        assert "queryAndProcess" in result_method

    def test_returns_error_on_missing_file(self, tmp_path: Path) -> None:
        """extract_signatures returns ERROR string when file is not found."""
        from aiforge_core.planner.tools import make_extract_signatures

        ctx = _make_ctx(tmp_path)
        extract = make_extract_signatures(ctx)

        result = extract(path="/nonexistent/path/Missing.java")
        assert "ERROR" in result

    def test_no_signatures_returns_sentinel(self, tmp_path: Path) -> None:
        """extract_signatures returns the no-signatures sentinel for a trivial file."""
        from aiforge_core.planner.tools import make_extract_signatures

        src = tmp_path / "empty.java"
        src.write_text("// just a comment\n", encoding="utf-8")

        ctx = _make_ctx(tmp_path)
        extract = make_extract_signatures(ctx)

        result = extract(path=str(src))
        assert "no signatures found" in result


# ─────────────────────────── 8. TestWritePlanSignaturesAndPitfalls ──────

class TestWritePlanSignaturesAndPitfalls:
    def _make_mock_conn(self) -> tuple:
        """Return (mock_conn, mock_cur) wired for context-manager use."""
        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cur

    def test_appends_signatures_and_pitfalls(self, tmp_path: Path) -> None:
        """write_plan appends ## Signatures + ## Compile pitfalls when provided."""
        from aiforge_core.planner.tools import make_write_plan

        ticket = _FakeTicket(body="Ticket body.\n")
        ctx = _make_ctx(tmp_path, ticket=ticket)
        mock_conn, mock_cur = self._make_mock_conn()

        sigs = "src/main/java/Foo.java:42: public void oldMethod()"
        pits = "lambda ResponseEntity<?> cast required"
        write_plan = make_write_plan(ctx)

        with patch("aiforge_core.planner.tools.psycopg.connect", return_value=mock_conn):
            result = write_plan(
                files=["src/main/java/Foo.java"],
                plan="1. Rename the method.",
                signatures=sigs,
                pitfalls=pits,
            )

        assert "OK:" in result
        assert "## Signatures" in ticket.body
        assert "oldMethod" in ticket.body
        assert "## Compile pitfalls" in ticket.body
        assert "ResponseEntity<?>" in ticket.body

    def test_without_signatures_still_works(self, tmp_path: Path) -> None:
        """write_plan backward-compat: omits optional sections when absent."""
        from aiforge_core.planner.tools import make_write_plan

        ticket = _FakeTicket(body="Body.\n")
        ctx = _make_ctx(tmp_path, ticket=ticket)
        mock_conn, mock_cur = self._make_mock_conn()

        write_plan = make_write_plan(ctx)

        with patch("aiforge_core.planner.tools.psycopg.connect", return_value=mock_conn):
            result = write_plan(files=["src/Foo.java"], plan="1. Do it.")

        assert "OK:" in result
        assert "## Files" in ticket.body
        assert "## Plan" in ticket.body
        assert "## Signatures" not in ticket.body
        assert "## Compile pitfalls" not in ticket.body
        assert "## Implementation" not in ticket.body
