"""Known fixes: what the fix changed is kept, and a failure sharing any of
its separate errors with a recorded one finds it."""
import subprocess

import pytest

from aiforge_core.runtime import ops_memory as om


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AIFORGE_OPS_MEMORY", raising=False)


def test_partial_signature_finds_the_fix_same_repo_first():
    om.record_fix("build:A.java: cannot find symbol|build:A.java: package x does not exist",
                  "cannot find symbol", repo="shop", files=["pom.xml"], functions=[])
    om.record_fix("build:A.java: cannot find symbol", "cannot find symbol",
                  repo="other", files=["B.java"], functions=[])
    hit = om.known_fix("build:A.java: package x does not exist|build:C.java: boom", "shop")
    assert hit and hit["files"] == ["pom.xml"]
    assert om.known_fix("build:Z.java: unrelated", "shop") is None
    exact = om.known_fix("build:A.java: cannot find symbol", "shop")
    assert exact and exact["files"] == ["B.java"]          # the exact signature wins


def test_change_summary_and_note(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "pom.xml").write_text("<deps>\n</deps>\n")
    g("add", "."); g("commit", "-qm", "base")
    (r / "pom.xml").write_text("<deps>\n  <dep>junit-jupiter</dep>\n</deps>\n")
    ch = om._change_summary(str(r), ["pom.xml"])
    assert "# pom.xml" in ch and "+  <dep>junit-jupiter</dep>" in ch
    om.record_fix("build:T.java: package org.junit does not exist", "junit missing",
                  repo="shop", files=["pom.xml"], functions=[], change=ch)
    note = om.fix_note(om.known_fix("build:T.java: package org.junit does not exist", "shop"), "shop")
    assert "What changed then" in note and "junit-jupiter" in note


def _repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "Calc.java").write_text("int total() { return 1; }\n")
    g("add", "."); g("commit", "-qm", "base")
    return r


def test_a_fix_in_a_later_turn_is_recorded(tmp_path):
    from types import SimpleNamespace
    r = _repo(tmp_path)
    cmd = {"cmd": f"cd {r} && mvn -o -q test -Dtest=CalcTest"}
    red = {"ok": False, "code": 1, "stdout": "[ERROR] Failures:\n[ERROR]   CalcTest.total:5 expected: <2> but was: <1>\n"
           "[ERROR] Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\n"}
    green = {"ok": True, "code": 0, "stdout": "[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n"}
    turn1 = SimpleNamespace(file_hashes={})
    om.fix_hook(turn1, "run_command", cmd, red, str(r), "proj")        # turn 1 ends red
    assert om.load()["pending"]
    (r / "Calc.java").write_text("int total() { return 2; }\n")          # fixed in turn 2
    turn2 = SimpleNamespace(file_hashes={})
    om.fix_hook(turn2, "run_command", cmd, green, str(r), "proj")
    fx = om.fixes_for("proj")
    assert fx and fx[0]["files"] == ["Calc.java"] and "return 2" in fx[0]["change"]
    assert not om.load()["pending"]
    assert om.forget_fix(fx[0]["id"]) and om.fixes_for("proj") == []
