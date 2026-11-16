"""The JDK is picked from the build file before the first JVM build, and
learned from a background build's output."""
from aiforge_core.runtime.chat_agent import _shell
from aiforge_core.runtime.chat_agent._turn import _env_guard


def _setup(monkeypatch, tmp_path, pom_version, have):
    (tmp_path / "pom.xml").write_text(
        f"<project><properties><java.version>{pom_version}</java.version></properties></project>")
    monkeypatch.setattr(_shell, "_JDK_FOR", {})
    monkeypatch.setattr(_shell, "_JDK_CHECKED", set())
    monkeypatch.setattr(_shell, "_jdk_key", lambda cwd: str(tmp_path))
    monkeypatch.setattr(_shell, "_default_java_major", lambda: have)
    monkeypatch.setattr(_env_guard, "installed_jdks", lambda: [(25, "/j25"), (24, "/j24"), (21, "/j21")])
    monkeypatch.delenv("AIFORGE_CHAT_AUTO_JDK", raising=False)


def test_pom_needs_newer_java_picks_oldest_fitting(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, 24, 21)
    _shell._jdk_upfront(str(tmp_path))
    assert _shell._JDK_FOR[str(tmp_path)] == "/j24"


def test_java_on_path_is_enough(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, 17, 21)
    _shell._jdk_upfront(str(tmp_path))
    assert _shell._JDK_FOR == {}


def test_first_build_runs_with_the_jdk_even_in_background(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, 24, 21)
    seen = []
    monkeypatch.setattr(_shell, "_t_run_command_once",
                        lambda a, c: seen.append(a["cmd"]) or {"ok": True, "running": True, "id": "bg-1"})
    _shell._t_run_command_jdk({"cmd": f"cd {tmp_path} && mvn -o -q test"}, str(tmp_path))
    assert "JAVA_HOME=/j24" in seen[0] and seen[0].startswith(f"cd {tmp_path} && JAVA_HOME")


def test_learned_from_background_output(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, 8, 21)
    note = _shell.learn_jdk_from_output("[ERROR] class file has wrong version 68.0, should be 65.0",
                                        str(tmp_path))
    assert note and "JDK 24" in note and _shell._JDK_FOR[str(tmp_path)] == "/j24"
    assert _shell.learn_jdk_from_output("BUILD SUCCESS", str(tmp_path)) is None
