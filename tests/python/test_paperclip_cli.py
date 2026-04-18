from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from paperclip import cli

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path: Path, monkeypatch):
    """
    Run CLI against a copy-lite of the repo: same config files but isolated
    DB. We do this by pointing PAPERCLIP_REPO at a freshly-made dir that
    symlinks the config files, then overrides the db location via CWD.
    """
    work = tmp_path / "repo"
    work.mkdir()
    # Symlink in the exact config files the CLI reads.
    for name in ("paperclip.config.yml",):
        (work / name).symlink_to(REPO_ROOT / name)
    (work / "agents").symlink_to(REPO_ROOT / "agents", target_is_directory=True)
    (work / "security").symlink_to(REPO_ROOT / "security", target_is_directory=True)
    monkeypatch.setenv("PAPERCLIP_REPO", str(work))
    yield


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def test_create_and_list() -> None:
    rc, out = _run(["ticket", "create", "--title", "login fails", "--body", "repro"])
    assert rc == 0
    tid = out.strip().split()[0]
    assert tid.startswith("TICKET-")

    rc, out = _run(["ticket", "list"])
    assert rc == 0
    assert tid in out


def test_show_and_comment_and_audit() -> None:
    rc, out = _run(["ticket", "create", "--title", "auth bug"])
    tid = out.strip().split()[0]

    rc, out = _run(["ticket", "comment", tid, "--author", "em", "--body", "plan: 3 subtasks"])
    assert rc == 0

    rc, out = _run(["ticket", "show", tid])
    assert rc == 0
    assert "plan: 3 subtasks" in out
    assert "Allowed next states: planning" in out

    rc, out = _run(["audit", tid])
    assert rc == 0
    assert "create" in out and "comment" in out


def test_permission_denied_blocks_sr_developer_assign() -> None:
    # Sr Developer cannot ticket_assign per DESIGN §5.2.
    rc, out = _run(["ticket", "create", "--title", "x"])
    tid = out.strip().split()[0]
    rc, _ = _run(["ticket", "assign", tid, "--to", "sr_architect", "--actor", "sr-developer"])
    assert rc == 4  # PermissionError exit code


def test_advance_invalid_transition_errors() -> None:
    rc, out = _run(["ticket", "create", "--title", "x"])
    tid = out.strip().split()[0]
    rc, _ = _run(["ticket", "advance", tid, "--to", "merged", "--actor", "em"])
    assert rc == 1


def test_doctor_runs() -> None:
    rc, out = _run(["doctor"])
    assert rc == 0
    assert "db-write: OK" in out
