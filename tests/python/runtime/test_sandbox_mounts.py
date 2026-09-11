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


@pytest.mark.parametrize("bad", ["relative/dir", "/a:b", "/", ""])
def test_unsafe_paths_are_refused(bad):
    with pytest.raises(ValueError):
        sm.add(bad)


def test_the_chat_tool_records_and_says_to_restart(monkeypatch):
    from aiforge_core.runtime.chat_agent import TOOLS
    monkeypatch.setenv("AIFORGE_MOUNTS", "/home/me/.aiforge")
    out = TOOLS["mount_folder"]({"path": "/work/proj"}, "/")
    assert out["ok"] is True
    assert "./run.sh" in out["note"]
    assert "/work/proj" in sm.requested()


def test_mounting_a_folder_needs_approval():
    from aiforge_core.runtime.tools import tool_policy
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
    compose = (REPO / "docker-compose.yml").read_text()
    assert "- AIFORGE_MOUNTS" in compose
    r = subprocess.run(["bash", "-n", str(REPO / "run.sh")], capture_output=True)
    assert r.returncode == 0, r.stderr
