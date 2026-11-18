"""Giving up never hands back code that no longer builds when it built before."""
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _best_state as bs

RUN = ('grep -q broken Rule.kt && { echo "e: file:///r/Rule.kt:42:33 Unresolved reference \'KtX\'."; '
       'echo "> Task :rules:compileKotlin FAILED"; exit 1; }\n'
       'n=$(grep -c fail Rule.kt); echo "Tests run: 5, Failures: $n"; [ "$n" = 0 ]\n')


def _repo(tmp_path, body="ok\n"):
    r = tmp_path / "r"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "Rule.kt").write_text(body)
    (r / "run.sh").write_text(RUN)
    g("add", "."); g("commit", "-qm", "base")
    rev = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    return r, rev


def test_no_best_recorded_a_broken_build_goes_back_to_the_start(tmp_path):
    r, rev = _repo(tmp_path, "fail\n")
    (r / "Rule.kt").write_text("broken\n")
    st = SimpleNamespace(cwd=str(r), head0=rev,
                         last_test_cmd=("run_command", {"cmd": f"cd {r} && bash run.sh"}))
    ev = list(bs.restore_if_worse(st))
    assert ev and "does not build" in ev[0]["text"]
    assert (r / "Rule.kt").read_text() == "fail\n"


def test_best_state_is_restored_over_code_that_does_not_build(tmp_path):
    r, rev = _repo(tmp_path, "fail\nfail\n")
    cmd = f"cd {r} && bash run.sh"
    st = SimpleNamespace(cwd=str(r), head0=rev, edits_made=1)
    (r / "Rule.kt").write_text("fail\n")                 # better: 1 failing
    bs.note(st, cmd, 1, str(r))
    st.edits_made = 2
    (r / "Rule.kt").write_text("broken\n")               # then the build breaks
    st.best_last[bs.key_of(cmd)]["edits"] = 1
    ev = list(bs.restore_if_worse(st))
    assert any("restored the best tested state" in e["text"] and "does not build" in e["text"] for e in ev)
    assert (r / "Rule.kt").read_text() == "fail\n"


def test_a_failing_test_run_is_not_a_broken_build(tmp_path):
    r, rev = _repo(tmp_path, "ok\n")
    (r / "Rule.kt").write_text("fail\n")
    st = SimpleNamespace(cwd=str(r), head0=rev,
                         last_test_cmd=("run_command", {"cmd": f"cd {r} && bash run.sh"}))
    assert list(bs.restore_if_worse(st)) == []
    assert (r / "Rule.kt").read_text() == "fail\n"
