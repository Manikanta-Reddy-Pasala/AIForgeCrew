"""The workspace jail reads shell commands too.

The agent's way around a refused file_write was run_command with a redirect or
cp/mv into the same folder. shell_write_targets lists what a command writes.
"""
import os

import pytest

from aiforge_core.runtime import scope_guard
from aiforge_core.runtime.shell_writes import shell_write_targets


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return str(d)


def _r(p):
    return os.path.realpath(p)


@pytest.mark.parametrize("cmd,expected", [
    ("cat > /srv/other/x.py <<'EOF'\nprint(1)\nEOF", ["/srv/other/x.py"]),
    ("echo hi >> /srv/other/log.txt", ["/srv/other/log.txt"]),
    ("echo hi 2>/srv/other/err", ["/srv/other/err"]),
    ("some | tee -a /srv/a /srv/b", ["/srv/a", "/srv/b"]),
    ("cp -r src /srv/dest", ["/srv/dest"]),
    ("mv a b /srv/dir/", ["/srv/dir"]),
    ("cp -t /srv/dir a b", ["/srv/dir"]),
    ("rsync -a ./ /srv/mirror/", ["/srv/mirror"]),
    ("touch /srv/t1 /srv/t2", ["/srv/t1", "/srv/t2"]),
    ("mkdir -p /srv/newdir", ["/srv/newdir"]),
    ("rm -rf /srv/old", ["/srv/old"]),
    ("chmod 644 /srv/f", ["/srv/f"]),
    ("sed -i 's/a/b/' /srv/conf", ["/srv/conf"]),
    ("sed -i -e 's/a/b/' /srv/conf", ["/srv/conf"]),
    ("dd if=/dev/zero of=/srv/img bs=1M count=1", ["/srv/img"]),
    ("git -C /srv/repo commit -m x", ["/srv/repo"]),
    ("sudo tee /srv/root-owned", ["/srv/root-owned"]),
])
def test_writes_are_found(cmd, expected, ws):
    assert shell_write_targets(cmd, ws) == [_r(p) for p in expected]


@pytest.mark.parametrize("cmd", [
    "ls -la /srv", "cat /srv/x", "grep -r foo /srv", "git -C /srv/repo status",
    "echo x > /dev/null", "echo x > /tmp/scratch", "cmd 2>&1 | head",
    "echo $HOME > $OUT",                       # an expansion it cannot see through
    "cp onlyone",
])
def test_reads_temp_and_unknowable_are_not_writes(cmd, ws):
    assert shell_write_targets(cmd, ws) == []


def test_cd_is_followed(ws):
    assert shell_write_targets("cd /srv/other && echo x > notes.md", ws) \
        == [_r("/srv/other/notes.md")]


def test_relative_writes_resolve_inside_the_workspace(ws, monkeypatch):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "_temp_roots", lambda: ())   # ws is under /tmp
    assert shell_write_targets("echo x > out.txt", ws) == [_r(os.path.join(ws, "out.txt"))]


def test_the_jail_reads_shell_only_when_asked(monkeypatch, ws):
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    args = {"cmd": "echo x > /srv/elsewhere/a.py"}
    assert scope_guard.outside_workspace("run_command", args, ws) == []
    assert scope_guard.outside_workspace("run_command", args, ws,
                                         include_shell=True) == [_r("/srv/elsewhere/a.py")]
    inside = {"cmd": "echo x > a.py && mkdir -p sub"}
    assert scope_guard.outside_workspace("run_command", inside, ws,
                                         include_shell=True) == []
