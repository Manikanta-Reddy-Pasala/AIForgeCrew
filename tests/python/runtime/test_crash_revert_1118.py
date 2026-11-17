"""Giving up never hands back code whose tests crash when the start did not."""
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _best_state as bs


def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "calc.txt").write_text("ok\n")
    (r / "run.sh").write_text('grep -q crash calc.txt && { echo "Floating point exception"; exit 136; }\necho "[pass] all"\n')
    g("add", "."); g("commit", "-qm", "base")
    rev = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    return r, rev


def _st(r, rev):
    return SimpleNamespace(cwd=str(r), head0=rev, last_test_cmd=("run_command", {"cmd": f"cd {r} && bash run.sh 2>&1 | tail -5"}))


def test_a_crash_the_start_did_not_have_is_reverted(tmp_path):
    r, rev = _repo(tmp_path)
    (r / "calc.txt").write_text("crash\n")
    st = _st(r, rev)
    ev = list(bs.restore_if_worse(st))
    assert ev and "gave back the original code" in ev[0]["text"] and "git stash apply" in ev[0]["text"]
    assert (r / "calc.txt").read_text() == "ok\n"


def test_no_revert_when_it_does_not_crash_or_the_start_crashed_too(tmp_path):
    r, rev = _repo(tmp_path)
    (r / "calc.txt").write_text("changed but fine\n")
    assert list(bs.restore_if_worse(_st(r, rev))) == []
    assert (r / "calc.txt").read_text() == "changed but fine\n"
    # the start crashes too: keep the turn's code
    (r / "calc.txt").write_text("crash\n")
    subprocess.run(["git", "-C", str(r), "commit", "-qam", "crashing base"], check=True)
    rev2 = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    (r / "calc.txt").write_text("crash still\n")
    assert list(bs.restore_if_worse(_st(r, rev2))) == []
    assert (r / "calc.txt").read_text() == "crash still\n"
