"""run_command must not BLOCK on a long-lived server-start command.

Regression for the chat "Agent error: network error" bug: the agent ran
``./run.sh`` (a foreground server that never returns) through ``run_command``.
run_command polls the process until it exits or the 600s timeout, so the turn
wedged for up to 10 minutes holding the request open — the ``serve`` tool (which
detaches and returns immediately) is what should start a server. These tests
pin the detector + the fast soft-redirect so a server-start can never wedge the
turn again.
"""
from __future__ import annotations

import sys
import time

from aiforge_core.runtime import chat_agent as ca


# ── detector: foreground, never-returning server launchers ──────────────────


def test_is_server_start_detects_explicit_server_commands():
    servers = [
        "npm run dev", "pnpm dev", "yarn start", "npm run serve",
        "bun run dev",
        "uvicorn app:app", "gunicorn wsgi:app", "flask run",
        "python -m http.server 8000", "python3 -m http.server",
        "python -m uvicorn app:app --reload",
        "vite", "next dev", "ng serve", "nodemon server.js",
        "php -S localhost:8000", "rails server", "python manage.py runserver",
        # chained one-shot THEN a server still blocks on the server
        "npm ci && npm run dev",
        # benign prefixes must not hide the server
        "FOO=bar npm run dev", "nohup uvicorn app:app", "sudo gunicorn wsgi:app",
    ]
    for c in servers:
        assert ca._is_server_start(c) is True, f"should detect server: {c!r}"


def test_is_server_start_allows_oneshot_and_backgrounded():
    allow = [
        # one-shot builds/tests/installs — they return
        "npm run build", "npm ci", "npm test", "yarn build", "pnpm install",
        "mvn package", "pytest -q", "git status", "ls -la", "python app.py",
        "python manage.py migrate", "cat run.sh",
        # already backgrounded by the caller → returns immediately
        "npm run dev &", "uvicorn app:app &",
        # server text inside a quoted string / echo is not a command
        'echo "npm run dev"', "echo ./run.sh",
    ]
    for c in allow:
        assert ca._is_server_start(c) is False, f"should allow: {c!r}"


def test_is_server_start_detects_launcher_script_by_content(tmp_path):
    """A launcher script is judged by what it DOES, not its name: a run.sh that
    starts a server is flagged; a run.sh that's a one-shot is left alone (so a
    legitimately-named run.sh still runs)."""
    (tmp_path / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -e\nexec uvicorn app:app --port 8000\n")
    (tmp_path / "oneshot.sh").write_text("#!/bin/sh\necho building\nmvn package\n")
    b = str(tmp_path)
    assert ca._is_server_start("./run.sh", b) is True
    assert ca._is_server_start("bash run.sh", b) is True
    assert ca._is_server_start("sh ./run.sh &", b) is False   # backgrounded
    assert ca._is_server_start("./oneshot.sh", b) is False    # one-shot content
    # No base / missing script → not flagged (preflight handles a missing one).
    assert ca._is_server_start("./run.sh") is False
    assert ca._is_server_start("./absent.sh", b) is False


# ── run_command redirects a server-start to serve, instantly (no wedge) ──────


def test_run_command_redirects_server_start_without_executing(tmp_path):
    # Explicit server command → redirected to `serve`, never executed.
    res = ca._t_run_command({"cmd": "npm run dev"}, str(tmp_path))
    assert res.get("ok") is False
    assert res.get("blocked") == "server_start"
    assert "serve" in (res.get("error") or "").lower()
    # A launcher script whose CONTENT starts a server is caught too.
    (tmp_path / "run.sh").write_text("#!/bin/sh\nexec uvicorn app:app\n")
    res2 = ca._t_run_command({"cmd": "./run.sh"}, str(tmp_path))
    assert res2.get("blocked") == "server_start"


def test_run_command_does_not_block_on_a_real_never_returning_server(
        tmp_path, monkeypatch):
    """The core of the bug: a real foreground server must NOT hold the turn.

    Uses a genuinely never-returning server (``http.server``) with a short
    timeout so that BEFORE the fix this blocks ~2s and times out; after the
    fix run_command short-circuits before spawning anything and returns at
    once. Asserting a tight wall-clock bound proves the wedge is gone.
    """
    monkeypatch.setenv("AIFORGE_CHAT_CMD_TIMEOUT_S", "2")
    cmd = f"{sys.executable} -m http.server 0"
    t0 = time.monotonic()
    res = ca._t_run_command({"cmd": cmd}, str(tmp_path))
    elapsed = time.monotonic() - t0
    assert res.get("blocked") == "server_start"
    assert elapsed < 1.0, f"server-start wedged the turn for {elapsed:.1f}s"
