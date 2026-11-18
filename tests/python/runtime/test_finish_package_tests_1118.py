"""The finish re-run widens a single-class JVM test filter to its package."""
from aiforge_core.runtime.chat_agent._turn._outcomes import _widen_to_package as w


def test_gradle_and_maven_single_class_filters_widen(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_FINISH_PACKAGE_TESTS", raising=False)
    assert w('./gradlew :core:test --tests "com.acme.format.MultilineTest"') == \
        './gradlew :core:test --tests "com.acme.format.*"'
    assert w("./gradlew test --tests com.acme.format.FooTest.bar") == \
        "./gradlew test --tests com.acme.format.*"
    assert w("mvn -o -q test -Dtest=com.acme.sync.OrderServiceTest") == \
        "mvn -o -q test -Dtest=com.acme.sync.*"
    assert w("mvn -o -q test -Dtest=com.acme.sync.OrderServiceTest#totals") == \
        "mvn -o -q test -Dtest=com.acme.sync.*"


def test_left_alone(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_FINISH_PACKAGE_TESTS", raising=False)
    for c in ("mvn -o test -Dtest=OrderServiceTest",            # no package: unknown scope
              "./gradlew test",                                  # already whole
              "cargo test --test test_version_req",
              "./gradlew test --tests com.a.XTest --tests com.b.YTest"):   # several filters
        assert w(c) == c
    monkeypatch.setenv("AIFORGE_CHAT_FINISH_PACKAGE_TESTS", "0")
    assert w("./gradlew test --tests com.acme.FooTest") == "./gradlew test --tests com.acme.FooTest"


def test_give_up_reverts_new_failures_in_the_package(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from aiforge_core.runtime.chat_agent._turn import _best_state as bs, _base_reds as br
    monkeypatch.delenv("AIFORGE_CHAT_FINISH_PACKAGE_TESTS", raising=False)
    br._CACHE.clear()
    r = tmp_path / "r"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "code.txt").write_text("good\n")
    # a fake gradle: the package run reports neighbour tests failing when code is bad
    (r / "gradlew").write_text('#!/bin/bash\nif grep -q bad code.txt; then echo "FooTest > a() FAILED"; '
                               'echo "FooTest > b() FAILED"; echo "2 tests completed, 2 failed"; exit 1; fi\n'
                               'echo "BUILD SUCCESSFUL"\n')
    (r / "gradlew").chmod(0o755)
    g("add", "."); g("commit", "-qm", "base")
    rev = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (r / "code.txt").write_text("bad\n")
    st = SimpleNamespace(cwd=str(r), head0=rev, best_state={"x": {}}, best_last={},
                         last_test_cmd=("run_command", {"cmd": f"cd {r} && ./gradlew test --tests com.acme.fmt.NewTest"}))
    ev = list(bs.restore_if_worse(st))
    assert any("newly failing" in e["text"] for e in ev)
    assert (r / "code.txt").read_text() == "good\n"
