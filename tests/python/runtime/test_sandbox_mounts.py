"""Host folders the docker sandbox sees: Settings lists them, the chat and
Settings can add one, and the host's ./run.sh mounts the list at start."""
import pathlib
import subprocess

import pytest

from aiforge_core.runtime import sandbox_mounts as sm

REPO = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    monkeypatch.setenv("AIFORGE_SANDBOX", "1")


def test_added_folders_wait_for_a_restart(monkeypatch):
    monkeypatch.setenv("AIFORGE_MOUNTS", "/home/me/.aiforge:/srv/projects")
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/srv/projects")
    sm.add("/data/shared")
    st = sm.state()
    rows = {r["path"]: (r["kind"], r["status"]) for r in st["folders"]}
    assert rows["/home/me/.aiforge"] == ("config", "mounted")
    assert rows["/srv/projects"] == ("projects", "mounted")
    assert rows["/data/shared"][1].startswith("waiting")
    assert st["restart_needed"] is True


def test_a_removed_folder_stays_mounted_until_restart(monkeypatch):
    monkeypatch.setenv("AIFORGE_MOUNTS", "/home/me/.aiforge:/data/shared")
    sm.add("/data/shared")
    assert sm.state()["restart_needed"] is False
    sm.remove("/data/shared")
    row = [r for r in sm.state()["folders"] if r["path"] == "/data/shared"][0]
    assert row["status"].startswith("removed")


@pytest.mark.parametrize("bad", ["relative/dir", "/a:b", "/", "", "/data/a#b",
                                 '/x"y', "/x$HOME", "/home/me", "/home", "/home/me/"])
def test_unsafe_paths_are_refused(bad, monkeypatch):
    """`#` would be cut as a comment and mount a DIFFERENT folder; quotes and
    `$` break or get interpolated in the compose file; the home folder or above
    it defeats the sandbox."""
    monkeypatch.setenv("HOME", "/home/me")
    with pytest.raises(ValueError):
        sm.add(bad)


def test_the_chat_tool_records_and_says_to_restart(monkeypatch):
    from aiforge_core.runtime.chat_agent import TOOLS
    monkeypatch.setenv("AIFORGE_MOUNTS", "/home/me/.aiforge")
    out = TOOLS["mount_folder"]({"path": "/work/proj"}, "/")
    assert out["ok"] is True
    assert "./run.sh" in out["note"]
    assert "/work/proj" in sm.requested()


def test_the_mount_tool_hands_over_the_exact_host_command(monkeypatch):
    """"Run ./run.sh" leaves the user guessing; --mount approves in one step."""
    from aiforge_core.runtime.chat_agent import TOOLS
    monkeypatch.setenv("AIFORGE_MOUNTS", "/home/me/.aiforge")
    out = TOOLS["mount_folder"]({"path": "/work/proj"}, "/")
    assert out["next_step"] == "./run.sh --mount /work/proj"
    assert "--mount /work/proj" in out["note"]


def test_the_chat_tool_can_unmount_too(monkeypatch):
    """Settings has Remove; chat had no counterpart at all."""
    from aiforge_core.runtime.chat_agent import TOOLS
    monkeypatch.setenv("AIFORGE_MOUNTS", "/home/me/.aiforge:/work/proj")
    sm.add("/work/proj")
    out = TOOLS["unmount_folder"]({"path": "/work/proj"}, "/")
    assert out["ok"] is True
    assert "/work/proj" not in sm.requested()
    # it is still mounted in THIS box until the host restarts it — say so
    assert "restart" in out["note"]
    assert out["status"].startswith("removed")


def test_unmounting_a_folder_that_was_never_listed_changes_nothing(monkeypatch):
    from aiforge_core.runtime.chat_agent import TOOLS
    out = TOOLS["unmount_folder"]({"path": "/not/listed"}, "/")
    assert out["ok"] is True
    assert out["status"] == "not listed"


def test_unmounting_needs_no_approval(monkeypatch):
    """Mounting WIDENS what the box can see and is gated; unmounting narrows it,
    so gating it would only teach the user to click through prompts."""
    from aiforge_core.runtime.tools import tool_policy
    monkeypatch.delenv("AIFORGE_SANDBOX", raising=False)
    assert tool_policy.decide("unmount_folder",
                              {"path": "/x"})["policy"] == tool_policy.ALLOW


def test_mounting_a_folder_needs_approval_outside_the_box(monkeypatch):
    """Natively it asks; in the sandbox the request is free because the HOST
    still has to approve it before anything is mounted."""
    from aiforge_core.runtime.tools import tool_policy
    monkeypatch.delenv("AIFORGE_SANDBOX", raising=False)
    assert tool_policy.decide("mount_folder", {"path": "/x"})["policy"] == tool_policy.ASK


def test_save_secret_keeps_the_value_out_of_memory_and_the_step(monkeypatch, tmp_path):
    from aiforge_core.runtime.chat_agent import TOOLS
    from aiforge_core.runtime.chat_agent._tools import _memory
    notes = []
    monkeypatch.setattr(_memory, "_t_memory_write",
                        lambda args, cwd: notes.append(args["text"]) or {"ok": True})
    args = {"name": "jira_token", "value": "s3cr3t-VALUE", "purpose": "Jira API"}
    out = TOOLS["save_secret"](args, "/")
    assert out["ok"] is True
    path = pathlib.Path(out["path"])
    assert path.read_text() == "s3cr3t-VALUE"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.parent.parent.name == "security"
    assert "s3cr3t" not in " ".join(notes)            # memory holds the location only
    assert str(path) in notes[0]
    assert "s3cr3t" not in str(out)
    assert "s3cr3t" not in str(args)                  # the UI step shows no value


def test_run_sh_mounts_the_list_at_the_same_path(tmp_path):
    """The host side: mounts.list → a compose override mounting each folder at
    its own path, a missing folder skipped loudly, and AIFORGE_MOUNTS set."""
    src = (REPO / "run.sh").read_text()
    assert '--mount) _MOUNT_ARGS+=("${2:-}"); shift ;;' in src
    assert '"$AIFORGE_CONFIG_DIR/mounts.list"' in src
    assert "printf '      - \"%s:%s\"\\n' \"$_m\" \"$_m\"" in src
    assert "is not a folder on this machine" in src
    # The list is writable from inside the box, so it is a REQUEST: only a
    # host-side approval (--mount, or the prompt) grants a mount.
    assert 'approved-mounts' in src
    assert "mount waiting for approval" in src
    assert "is your home folder (or above it)" in src
    compose = (REPO / "docker-compose.yml").read_text()
    assert "- AIFORGE_MOUNTS" in compose
    r = subprocess.run(["bash", "-n", str(REPO / "run.sh")], capture_output=True)
    assert r.returncode == 0, r.stderr


def test_a_secret_is_masked_before_the_tool_even_runs(monkeypatch, tmp_path):
    """tool_start leaves before the tool runs; it must already be masked."""
    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setitem(_loop.TOOLS, "save_secret", lambda a, c: {"ok": True})
    g = _loop._dispatch_tool("save_secret", {"name": "t", "value": "s3cr3t"},
                             str(tmp_path), 1, None)
    first = next(g)
    assert first["type"] == "tool_start"
    assert "s3cr3t" not in str(first)
